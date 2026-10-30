# SPDX-License-Identifier: Apache-2.0
"""The two routes ``opseval run --server`` calls (ADR-0058 E2), in accounts mode only.

- ``GET /catalogue/export`` (``runs:read``): the files under a label, exactly as stored, for
  the CLI to write with ``catalogue pull``'s own rules.
- ``PUT /runs/{run_id}/upload`` (``runs:start``, "report a CI run" in ADR-0050): a finished
  run folder, written through the run store into the served project and task, exactly as
  ``opseval runs import`` writes one.

Authorization is the app's one ``authorize`` dependency (``app.ROUTE_NEEDS``). A request that
names a project or task other than the one served is 404, the same answer a key scoped to
another task gets (ADR-0050): neither learns what exists.

The upload is checked in this order, and nothing is written until every check passed: the
size (413), the layout (409 ``UNKNOWN_LAYOUT`` for one newer than this server's, before the
rest is parsed by models that may not know its fields), the models (``extra="forbid"``, 422),
each audio file's sha256 (422 ``CHECKSUM_MISMATCH``), then the store: a run id another project
holds or a different run under this id is 409; the same run again is 200 ``unchanged``.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import FastAPI, Query, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from opseval.errors import ConfigError
from opseval.models.profile import SutProfile
from opseval.models.progress import RunProgress
from opseval.models.record import RunRecord
from opseval.models.results import SuiteResult
from opseval.models.scenario import Scenario
from opseval.models.upload import (
    CatalogueExport,
    ExportedFile,
    RunUpload,
    UploadOut,
    same_result,
)
from opseval.server.actions import ApiError, ServerContext
from opseval.server.lookup import safe_id
from opseval.storage import (
    KNOWN_LAYOUTS,
    AudioPathRefused,
    PrunedMarker,
    UnknownLayout,
    check_audio_path,
    parse_pruned_marker,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from opseval.accounts.roles import Principal
    from opseval.accounts.service import Accounts
    from opseval.db.run_store import DbRunStore
    from opseval.server.access import AccessConfig

UPLOAD_AUDIT = "run.upload"
CHUNK_LIMIT_NOTE = "the run stays in the CI job's folder; `opseval runs import` can send it"


class _Audio:
    """An uploaded run's audio, checked, in memory: what an import reads from a folder."""

    def __init__(self, files: dict[str, bytes], marker: str | None) -> None:
        self.files = files
        self.marker = marker

    def size(self, relative: str) -> int | None:
        data = self.files.get(relative)
        return None if data is None else len(data)

    def read(self, relative: str) -> bytes:
        try:
            return self.files[relative]
        except KeyError:
            raise FileNotFoundError(relative) from None

    def pruned(self) -> PrunedMarker | None:
        return parse_pruned_marker(self.marker) if self.marker is not None else None


class UploadedRun:
    """A run a CI job uploaded, as :func:`opseval.db.run_import.import_from` reads a run:
    the same reads a run folder answers, from the checked upload."""

    def __init__(self, upload: RunUpload, audio: dict[str, bytes]) -> None:
        self.upload = upload
        self._audio = _Audio(audio, upload.audio_marker)

    def exists(self, run_id: str) -> bool:
        return run_id == self.upload.run_id

    def check_layout(self, run_id: str) -> None:
        if self.upload.layout not in KNOWN_LAYOUTS:
            raise UnknownLayout(f"run {run_id!r} is in layout {self.upload.layout!r}")

    def has_result(self, run_id: str) -> bool:
        return self.upload.result is not None

    def has_error(self, run_id: str) -> bool:
        return self.upload.error is not None

    def load_result(self, run_id: str) -> SuiteResult:
        if self.upload.result is None:  # pragma: no cover - has_result is asked first
            raise ConfigError("the upload has no result")
        return self.upload.result

    def load_error(self, run_id: str) -> dict[str, Any] | None:
        return self.upload.error

    def load_records(self, run_id: str) -> list[RunRecord]:
        return sorted(self.upload.records, key=lambda r: (r.test_id, r.attempt_index))

    def load_snapshot(self, run_id: str) -> tuple[SutProfile, dict[str, Scenario]] | None:
        if self.upload.profile is None:
            return None
        return self.upload.profile, {s.id: s for s in self.upload.scenarios}

    def load_manifest(self, run_id: str) -> dict[str, Any]:
        return dict(self.upload.manifest)

    def load_progress(self, run_id: str) -> RunProgress | None:
        return self.upload.progress

    def cancel_requested(self, run_id: str) -> bool:
        return self.upload.cancel_requested

    def audio(self, run_id: str) -> _Audio:
        return self._audio

    def audio_marker_text(self, run_id: str) -> str | None:
        return self.upload.audio_marker

    def rescores(self, run_id: str) -> list[str]:
        return []

    def load_rescore(self, run_id: str, stamp: str) -> SuiteResult:  # pragma: no cover
        raise ConfigError("an upload carries no re-scores")

    def rescore_files(self, run_id: str, stamp: str) -> tuple[str | None, str | None]:
        return None, None  # pragma: no cover - rescores() is empty

    def load_report(self, run_id: str) -> str | None:
        return self.upload.report

    def load_junit(self, run_id: str) -> str | None:
        return self.upload.junit


def _first_problem(exc: ValidationError) -> str:
    """Where the first problem is and what it is; never the value sent."""
    errors = exc.errors()
    if not errors:
        return "invalid"
    first = errors[0]
    where = ".".join(str(p) for p in first.get("loc", ()))
    return f"{where}: {first.get('msg', 'invalid')}".lstrip(": ")


def decode_audio(upload: RunUpload) -> dict[str, bytes]:
    """Each audio file's bytes, with its path and sha256 checked; 422 otherwise."""
    import hashlib

    out: dict[str, bytes] = {}
    for item in upload.audio:
        try:
            check_audio_path(item.path)
        except AudioPathRefused as exc:
            raise ApiError(422, "BAD_AUDIO_PATH", str(exc)) from None
        if item.path in out:
            raise ApiError(422, "BAD_AUDIO_PATH", f"{item.path} is sent twice")
        try:
            data = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(422, "BAD_AUDIO", f"{item.path}: not base64") from None
        if hashlib.sha256(data).hexdigest() != item.sha256:
            raise ApiError(
                422,
                "CHECKSUM_MISMATCH",
                f"{item.path}: its sha256 is not the one sent; nothing was stored",
            )
        out[item.path] = data
    return out


def parse_upload(body: bytes, run_id: str) -> RunUpload:
    """The upload, checked: the layout first, then the models."""
    try:
        data = json.loads(body)
    except ValueError:
        raise ApiError(422, "INVALID_REQUEST", "the body is not JSON") from None
    if not isinstance(data, dict):
        raise ApiError(422, "INVALID_REQUEST", "the body is not a run upload")
    layout = data.get("layout")
    if isinstance(layout, str) and layout not in KNOWN_LAYOUTS:
        raise ApiError(
            409,
            "UNKNOWN_LAYOUT",
            f"the run is in layout {layout[:64]!r}, which this server cannot read (it reads "
            f"{', '.join(sorted(KNOWN_LAYOUTS))}); upgrade the server, or the run stays in the "
            "CI job's folder",
        )
    try:
        upload = RunUpload.model_validate(data)
    except ValidationError as exc:
        raise ApiError(422, "INVALID_UPLOAD", _first_problem(exc)) from None
    if upload.run_id != run_id:
        raise ApiError(422, "RUN_ID_MISMATCH", "the body is another run's")
    return upload


def store_upload(store: DbRunStore, upload: RunUpload, audio: dict[str, bytes]) -> UploadOut:
    """Write the run through the store (as ``runs import`` does) and its CI origin. Raises
    :class:`ApiError` for a refusal; a storage outage stays a ``StorageError`` (503)."""
    from opseval.db.run_import import import_from

    run_id = upload.run_id
    if store.foreign(run_id):
        raise ApiError(409, "RUN_ID_TAKEN", f"run id {run_id!r} is taken; not changed")
    if store.exists(run_id) and not store.importing(run_id):
        held_result = store.load_result(run_id) if store.has_result(run_id) else None
        if upload.result is not None and held_result is not None:
            same = same_result(held_result, upload.result)
        else:
            same = upload.result is None and store.load_error(run_id) == upload.error
        if not same:
            raise ApiError(
                409,
                "RUN_EXISTS",
                f"the server holds a different run under {run_id!r}; not changed",
            )
        outcome = UploadOut(run_id=run_id, outcome="unchanged", detail="already stored")
    else:
        try:
            done = import_from(UploadedRun(upload, audio), run_id, store)
        except UnknownLayout as exc:  # pragma: no cover - parse_upload refused it first
            raise ApiError(409, "UNKNOWN_LAYOUT", str(exc)) from None
        except (ConfigError, ValidationError, ValueError) as exc:
            detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            raise ApiError(422, "UPLOAD_REFUSED", detail) from None
        if done.outcome == "refused":
            raise ApiError(422, "UPLOAD_REFUSED", done.detail)
        outcome = UploadOut(run_id=run_id, outcome=done.outcome, detail=done.detail)
    if upload.ci is not None:
        body: dict[str, Any] = upload.ci.model_dump(mode="json", exclude_none=True)
        if upload.catalogue is not None:
            body["catalogue"] = upload.catalogue.model_dump(mode="json")
        store.set_origin(run_id, "ci", body)
    return outcome


def register(app: FastAPI, ctx: ServerContext, access: AccessConfig | None, api: str) -> None:
    accounts = access.accounts if access is not None else None
    settings = ctx.settings

    def need() -> Accounts:
        if accounts is None:
            raise ApiError(
                409,
                "ACCOUNTS_MODE_ONLY",
                "reporting a CI run needs accounts mode: serve with a database",
            )
        return accounts

    def served(acc: Accounts, project: str, task: str) -> None:
        """404 unless ``project``/``task`` are the ones served (ADR-0050: never say more)."""
        from opseval.db.catalogue_store import parse_project

        try:
            asked = parse_project(project)
        except ConfigError:
            raise ApiError(404, "NOT_FOUND", "not found") from None
        here = acc.served
        if asked != parse_project(here.project_ref) or task != here.task_slug:
            raise ApiError(404, "NOT_FOUND", "not found")

    def db_store() -> DbRunStore:
        from opseval.db.run_store import DbRunStore

        if not isinstance(ctx.store, DbRunStore):  # pragma: no cover - accounts mode has one
            raise ApiError(409, "SERVER_MODE_ONLY", "this server keeps no runs in a database")
        return ctx.store

    @app.get(api + "/catalogue/export")
    def export_catalogue(
        project: Annotated[str, Query(min_length=1, max_length=200)],
        task: Annotated[str, Query(min_length=1, max_length=64)],
        label: Annotated[str, Query(min_length=1, max_length=64)] = "production",
    ) -> CatalogueExport:
        from opseval.db.catalogue_store import resolve
        from opseval.db.schema import Task

        acc = need()
        served(acc, project, task)
        here = acc.served

        async def go(s: AsyncSession) -> list[ExportedFile]:
            found = await s.get(Task, here.task_id)
            if found is None:  # pragma: no cover - resolved at start
                raise ApiError(404, "NOT_FOUND", "not found")
            chosen = await resolve(s, found, label)
            return [
                ExportedFile(
                    kind=cast(Any, item.kind),
                    name=item.name,
                    version=version.version,
                    path=version.path,
                    source=version.source,
                )
                for item, version in chosen
            ]

        try:
            files = acc.db.run_in_session(go)
        except ConfigError as exc:
            raise ApiError(422, "BAD_LABEL", str(exc)) from None
        if not files:
            raise ApiError(
                404, "NO_SUCH_LABEL", f"nothing in this task carries the label {label!r}"
            )
        return CatalogueExport(
            project=here.project_ref, task=here.task_slug, label=label, files=files
        )

    @app.put(api + "/runs/{run_id}/upload", response_model=UploadOut)
    async def upload_run(
        run_id: str,
        request: Request,
        project: Annotated[str, Query(min_length=1, max_length=200)],
        task: Annotated[str, Query(min_length=1, max_length=64)],
    ) -> Response:
        acc = need()
        served(acc, project, task)
        if not safe_id(run_id):
            raise ApiError(404, "NOT_FOUND", "not found")
        limit = settings.upload_max_mb * 1024 * 1024
        declared = request.headers.get("content-length", "")
        too_big = ApiError(
            413,
            "UPLOAD_TOO_LARGE",
            f"the upload is larger than OPSEVAL_UPLOAD_MAX_MB ({settings.upload_max_mb} MB); "
            + CHUNK_LIMIT_NOTE,
        )
        if declared.isdigit() and int(declared) > limit:
            raise too_big
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise too_big
        upload = parse_upload(bytes(body), run_id)
        audio = decode_audio(upload)
        store = db_store()
        out = await run_in_threadpool(store_upload, store, upload, audio)
        principal = cast("Principal | None", getattr(request.state, "principal", None))
        await run_in_threadpool(
            acc.audit,
            principal,
            UPLOAD_AUDIT,
            target=f"/runs/{run_id}",
            address=str(getattr(request.state, "address", "")),
            detail={
                "run_id": run_id,
                "outcome": out.outcome,
                "bytes": len(body),
                "ci": upload.ci.provider if upload.ci is not None else None,
            },
        )
        return Response(
            content=out.model_dump_json(),
            status_code=201 if out.outcome == "imported" else 200,
            media_type="application/json",
        )


__all__ = ["UPLOAD_AUDIT", "UploadedRun", "decode_audio", "parse_upload", "register"]
