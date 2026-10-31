# SPDX-License-Identifier: Apache-2.0
"""``opseval runs import``: run folders into the database (ADR-0051).

Each folder is read through :class:`~opseval.storage.FileStore`, so it is validated as every
reader validates it, and written through :class:`~opseval.db.run_store.DbRunStore`. Import is
idempotent on the run id: a run already imported unchanged is skipped, and a different run
under an id the database already holds is refused. A folder that cannot be read whole (an
unknown layout, a broken result or record, audio it would have to store unencrypted) is
reported and skipped, never half-imported: a failure part-way removes what was written.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from opseval.db.run_store import DbRunStore
from opseval.errors import ConfigError, StorageError
from opseval.models.profile import SutProfile
from opseval.models.progress import RunProgress
from opseval.models.record import RunRecord
from opseval.models.results import SuiteResult
from opseval.models.scenario import Scenario
from opseval.models.upload import same_result
from opseval.storage import PrunedMarker, UnknownLayout


class SourceAudio(Protocol):
    def size(self, relative: str) -> int | None: ...
    def read(self, relative: str) -> bytes: ...
    def pruned(self) -> PrunedMarker | None: ...


class RunSource(Protocol):
    """What an import reads from a run: a run folder (:class:`~opseval.storage.FileStore`), or
    a run a CI job uploaded (``server.ci_api``)."""

    def exists(self, run_id: str) -> bool: ...
    def check_layout(self, run_id: str) -> None: ...
    def has_result(self, run_id: str) -> bool: ...
    def has_error(self, run_id: str) -> bool: ...
    def load_result(self, run_id: str) -> SuiteResult: ...
    def load_error(self, run_id: str) -> dict[str, Any] | None: ...
    def load_records(self, run_id: str) -> list[RunRecord]: ...
    def load_snapshot(self, run_id: str) -> tuple[SutProfile, dict[str, Scenario]] | None: ...
    def load_manifest(self, run_id: str) -> dict[str, Any]: ...
    def load_progress(self, run_id: str) -> RunProgress | None: ...
    def cancel_requested(self, run_id: str) -> bool: ...
    def audio(self, run_id: str) -> SourceAudio: ...
    def audio_marker_text(self, run_id: str) -> str | None: ...
    def rescores(self, run_id: str) -> list[str]: ...
    def load_rescore(self, run_id: str, stamp: str) -> SuiteResult: ...
    def rescore_files(self, run_id: str, stamp: str) -> tuple[str | None, str | None]: ...
    def load_report(self, run_id: str) -> str | None: ...
    def load_junit(self, run_id: str) -> str | None: ...


Outcome = Literal["imported", "unchanged", "refused"]


class Refusal(StrEnum):
    """Why an import was refused, typed, so a caller maps each to its answer in one place
    (``server.ci_api.REFUSALS`` for an upload)."""

    RUN_ID_TAKEN = "run_id_taken"
    """The id is another project's run: never compared with, replaced or removed."""
    RUN_EXISTS = "run_exists"
    """The database holds a different run under this id."""
    IMPORTING = "importing"
    """Another import of this run is in flight (it holds the run's import lock)."""
    NOT_A_RUN = "not_a_run"
    RUNNING = "running"
    """No result and no error yet: the run has not ended."""
    UNKNOWN_LAYOUT = "unknown_layout"
    INVALID = "invalid"
    """The run cannot be read or stored as it is (plain audio, a broken record, ...)."""


@dataclass(frozen=True)
class Imported:
    run_id: str
    outcome: Outcome
    detail: str
    reason: Refusal | None = None
    """Set exactly when ``outcome`` is ``refused``."""


def _sealed_audio(src: RunSource, run_id: str) -> list[tuple[str, bytes]]:
    """Every audio file the run's records name, read and checked sealed before anything is
    written. A pruned run carries its marker instead."""
    from opseval.voice.crypto import MAGIC

    audio = src.audio(run_id)
    out: list[tuple[str, bytes]] = []
    for record in src.load_records(run_id):
        for frame in record.frames:
            ref = frame.audio
            if ref is None or ref.path is None or ref.pruned:
                continue
            if audio.size(ref.path) is None:
                continue  # pruned, or missing: the record says so, the reader reports it
            data = audio.read(ref.path)
            if not data.startswith(MAGIC):
                raise ConfigError(
                    f"{ref.path} is plain audio (the demo's synthetic voices); the database "
                    "keeps audio only encrypted (ADR-0053)"
                )
            out.append((ref.path, data))
    return out


def import_run(src: RunSource, run_id: str, dst: DbRunStore) -> Imported:
    """One run folder into the database; never raises for a run it cannot take."""
    try:
        return import_from(src, run_id, dst)
    except (ConfigError, ValidationError, ValueError, OSError, StorageError) as exc:
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        notes = getattr(exc, "__notes__", ())
        return Imported(run_id, "refused", "; ".join([detail, *notes]), Refusal.INVALID)


def _refused(run_id: str, reason: Refusal, detail: str) -> Imported:
    return Imported(run_id, "refused", detail, reason)


def import_from(src: RunSource, run_id: str, dst: DbRunStore) -> Imported:
    """One run into the database, raising what :func:`import_run` reports: a
    :class:`StorageError` stays one (the server answers 503 and the uploader tries again).

    Serialised per run id by a Postgres advisory lock held for the whole import
    (``DbRunStore.import_lock``): a second import of the same id while one is in flight is
    refused (:attr:`Refusal.IMPORTING`), never allowed to remove the first's rows. The lock is
    a session's, so it goes with a process that dies: a run left ``importing`` while nobody
    holds its lock was cut short, and only then is it removed and imported again."""
    if dst.foreign(run_id):
        # Checked before anything is read or written: another project's run is never
        # compared with, replaced or removed (review of #93).
        return _refused(run_id, Refusal.RUN_ID_TAKEN, "run id exists in another project")
    if not src.exists(run_id):
        return _refused(run_id, Refusal.NOT_A_RUN, "not a run folder (no manifest.json)")
    try:
        src.check_layout(run_id)
    except UnknownLayout as exc:
        return _refused(run_id, Refusal.UNKNOWN_LAYOUT, str(exc).splitlines()[0])
    if not src.has_result(run_id) and not src.has_error(run_id):
        return _refused(
            run_id, Refusal.RUNNING, "still running (no result): import it once it ends"
        )
    with dst.import_lock(run_id) as held:
        if not held:
            return _refused(run_id, Refusal.IMPORTING, "another import of this run is in progress")
        return _import_locked(src, run_id, dst)


def _import_locked(src: RunSource, run_id: str, dst: DbRunStore) -> Imported:
    result = src.load_result(run_id) if src.has_result(run_id) else None
    error = src.load_error(run_id) if result is None else None
    replaced = ""
    if dst.importing(run_id):
        # An earlier import of this run was cut short (a crash, a lost connection): nobody
        # holds its lock (we do), so what it wrote is removed, then the run imported whole.
        dst.delete_run(run_id)
        replaced = "; replaced an import that had been cut short"
    elif dst.exists(run_id):
        same = (
            same_result(dst.load_result(run_id), result)
            if result is not None and dst.has_result(run_id)
            else result is None and dst.load_error(run_id) == error
        )
        if same:
            return Imported(run_id, "unchanged", "already imported")
        return _refused(
            run_id, Refusal.RUN_EXISTS, "the database holds a different run under this id"
        )
    records = src.load_records(run_id)
    snapshot = src.load_snapshot(run_id)
    audio = _sealed_audio(src, run_id)
    marker = src.audio(run_id).pruned()
    created = False
    try:
        dst.import_manifest(run_id, src.load_manifest(run_id))
        created = True
        progress = src.load_progress(run_id)
        if progress is not None:
            dst.save_progress(run_id, progress)
        if src.cancel_requested(run_id):
            dst.request_cancel(run_id, by="import")
        if snapshot is not None:
            dst.save_profile(run_id, snapshot[0])
            for scenario in snapshot[1].values():
                dst.save_scenario(run_id, scenario)
        target = dst.audio(run_id)
        for path, data in audio:
            target.write(path, data)
        for record in records:
            dst.save_record(record)
        if marker is not None:
            dst.import_pruned_marker(run_id, src.audio_marker_text(run_id))
        if result is None:
            dst.save_error(run_id, error or {"code": "HARNESS_ERROR", "message": ""})
            return Imported(run_id, "imported", f"could not be carried out (error.json){replaced}")
        for stamp in src.rescores(run_id):
            html, xml = src.rescore_files(run_id, stamp)
            dst.save_rescore(run_id, stamp, src.load_rescore(run_id, stamp), html or "", xml or "")
        report, junit = src.load_report(run_id), src.load_junit(run_id)
        if report is not None:
            dst.save_report(run_id, report)
        if junit is not None:
            dst.save_junit(run_id, junit)
        dst.save_result(result)
    except BaseException as exc:
        if created:
            _roll_back(dst, run_id, exc)
        raise
    return Imported(
        run_id, "imported", f"{len(records)} record(s), {len(audio)} audio file(s){replaced}"
    )


def _roll_back(dst: DbRunStore, run_id: str, exc: BaseException) -> None:
    """Remove what a failed import wrote. If that fails too, the original error is still the
    one raised, with the clean-up's failure noted on it: the run stays marked ``importing``,
    so the next import replaces it."""
    try:
        dst.delete_run(run_id)  # never half-imported
    except Exception as cleanup:  # noqa: BLE001 - noted on the original, never raised instead
        exc.add_note(
            f"what was written for run {run_id} could not be removed "
            f"({type(cleanup).__name__}: {cleanup}); it stays marked as importing, and the "
            "next import replaces it"
        )


__all__ = ["Imported", "Refusal", "RunSource", "import_from", "import_run"]
