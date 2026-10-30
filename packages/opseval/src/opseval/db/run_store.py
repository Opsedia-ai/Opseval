# SPDX-License-Identifier: Apache-2.0
"""Runs in the database (ADR-0051): :class:`DbRunStore` implements ``storage.RunStore`` on the
``runs`` tables and a :class:`~opseval.blobs.BlobStore`.

What a reader sees is what the run wrote: ``load_result`` and ``load_record`` read the stored
JSON from the blob store, never rebuild it from rows. The rows (attempts, scores, verdicts,
metrics, usage) exist for queries.

**A blob is written behind a pending row.** Its ``blobs`` row is committed ``pending`` first,
then the object is put, then the row that refers to it is written in the same transaction
that marks it ``committed``. A crash in between leaves a pending row, which retention finds
(:meth:`DbRunStore.orphans`), never a row pointing at a missing object.

The run store's contract is synchronous (the runner and the console call it from plain
code), and the database driver is asyncio. The store runs its own event loop on a thread and
waits for each call, so any thread may use it, and the connections stay on the loop that made
them.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import shlex
import threading
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import defer

from opseval.blobs import BlobStore
from opseval.config import Settings
from opseval.db import migrate
from opseval.db.catalogue_store import get_project, get_task
from opseval.db.engine import make_engine, sessions
from opseval.db.schema import (
    Attempt,
    AttemptMetric,
    Blob,
    Run,
    RunScenario,
    ScoreRow,
    UsageEventRow,
    VerdictRow,
)
from opseval.errors import BlobStoreError, ConfigError, StorageError
from opseval.models.profile import SutProfile
from opseval.models.progress import RunProgress
from opseval.models.record import RunRecord
from opseval.models.results import SuiteResult, VerdictStatus
from opseval.models.scenario import Scenario
from opseval.models.usage import UsageEvent
from opseval.storage import (
    AUDIO_MARKER,
    KNOWN_LAYOUTS,
    LAYOUT,
    LEGACY_LAYOUT,
    RESCORE_PREFIX,
    SAFE_ID,
    FinishedRuns,
    PrunedMarker,
    RunCounts,
    StoredRun,
    UnknownLayout,
    UnreadableRun,
    check_audio_path,
    marker_text,
    parse_pruned_marker,
    parse_snapshot,
    process_gone,
)
from opseval.verdicts import gate_state

T = TypeVar("T")
JSON = "application/json"
MARKER = f"audio/{AUDIO_MARKER}"


class _Loop:
    """An event loop on its own thread: the store's synchronous calls run their coroutines
    here and wait for them, each for at most ``timeout`` seconds (a hung database is a
    :class:`StorageError`, never a hung run or console). After :meth:`close`, a call fails at
    once instead of waiting on a loop that is gone."""

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._lock = threading.Lock()
        self._closed = False
        self.thread.start()

    @property
    def closed(self) -> bool:
        return self._closed

    def run(self, coro: Coroutine[Any, Any, T], *, timeout: float | None = None) -> T:
        if threading.current_thread() is self.thread:  # pragma: no cover - a programming error
            coro.close()
            raise RuntimeError("the run store was called from its own loop")
        with self._lock:
            if self._closed:
                coro.close()
                raise StorageError("the run store is closed")
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        limit = self.timeout if timeout is None else timeout
        done, _ = concurrent.futures.wait([future], timeout=limit)
        if not done:
            future.cancel()
            raise StorageError(
                f"the database did not answer within {limit:g}s (OPSEVAL_STORAGE_TIMEOUT_SECONDS)"
            )
        return future.result()

    def close(self) -> None:
        """Stop the loop and close it. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        if not self.thread.is_alive() and not self.loop.is_closed():
            self.loop.close()


_DB_ERRORS: tuple[type[BaseException], ...] = (
    SQLAlchemyError,
    OSError,
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
)
"""What the database driver raises when the database fails, wrapped into
:class:`StorageError` at the store boundary."""


def _when(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _json(value: Any) -> Any:
    """A value as JSONB holds it (datetimes as text, as ``FileStore`` writes them)."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _record_name(test_id: str, attempt: int) -> str:
    return f"records/{SAFE_ID.sub('_', test_id)}__attempt{attempt}.json"


def _audio_prefix(test_id: str, attempt: int) -> str:
    # The recorder's own folder for an attempt (voice.recording.AudioRecorder._relative).
    test = SAFE_ID.sub("_", test_id).strip(".") or "scenario"
    return f"audio/{test}/attempt{attempt}/"


def _usage_row(e: UsageEvent) -> dict[str, Any]:
    return {
        "at": e.at,
        "role": e.role,
        "provider": e.provider,
        "model": e.model,
        "answered": e.answered,
        "input_tokens": e.input_tokens,
        "output_tokens": e.output_tokens,
        "reasoning_tokens": e.reasoning_tokens,
        "cached_input_tokens": e.cached_input_tokens,
        "audio_in_seconds": e.audio_in_seconds,
        "audio_out_seconds": e.audio_out_seconds,
        "characters": e.characters,
        "latency_ms": e.latency_ms,
        "rate_limited": e.rate_limited,
        "retries": e.retries,
        "cost_usd": e.cost_usd,
        "cost_source": e.cost_source,
        "price_from": e.price_from,
    }


@dataclass(frozen=True)
class Orphan:
    """A pending blob retention may delete: its upload never got the row that refers to it."""

    id: uuid.UUID
    run_id: str
    name: str
    key: str
    created_at: datetime


@dataclass(frozen=True)
class OrphanOutcome:
    deleted: list[str]
    failed: list[tuple[str, str]]


class DbRunAudio:
    """A run's audio in the blob store, one blob per file, named as in a run folder. Always
    sealed: audio the recorder did not encrypt is refused, never stored (ADR-0053)."""

    sealed_only = True

    def __init__(self, store: DbRunStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id

    def write(self, relative: str, data: bytes) -> None:
        from opseval.voice.crypto import MAGIC, file_key_id

        check_audio_path(relative)
        if not data.startswith(MAGIC):
            raise BlobStoreError(
                "audio is only ever stored encrypted in server mode (ADR-0053): set "
                "OPSEVAL_AUDIO_ENCRYPTION_KEY, or OPSEVAL_RECORD_AUDIO=hashes"
            )
        self.store._put_pending(
            self.run_id,
            relative,
            "audio",
            data,
            "application/octet-stream",
            encryption="aes-256-gcm",
            key_id=file_key_id(data),
        )

    def size(self, relative: str) -> int | None:
        check_audio_path(relative)
        row = self.store._blob(self.run_id, relative)
        return None if row is None else row.bytes

    def read(self, relative: str) -> bytes:
        check_audio_path(relative)
        row = self.store._blob(self.run_id, relative)
        if row is None:
            raise FileNotFoundError(relative)
        return self.store._get(row)

    def pruned(self) -> PrunedMarker | None:
        row = self.store._blob(self.run_id, MARKER)
        if row is None:
            return None
        try:
            return parse_pruned_marker(self.store._get(row).decode("utf-8"))
        except (OSError, UnicodeDecodeError, StorageError):
            return PrunedMarker(None, readable=False)

    def _files(self) -> list[Blob]:
        rows = self.store._blobs(self.run_id, kind="audio")
        return [r for r in rows if r.name != MARKER]

    def count(self) -> int | None:
        return len(self._files()) or None

    def prune(self, *, at: datetime, older_than_days: int) -> int:
        """The marker first, then each object and its row (object first): a prune stopped
        part-way still reads as pruned, and is finished by the next one."""
        files = self._files()
        if not files:
            return 0
        previous = self.store._blob(self.run_id, MARKER)
        before = None
        if previous is not None:
            try:
                before = self.store._get(previous).decode("utf-8")
            except (OSError, UnicodeDecodeError, StorageError):
                before = None
        body = marker_text(before, at=at, older_than_days=older_than_days, files=len(files))
        self.store._put_committed(self.run_id, MARKER, "other", body.encode("utf-8"), JSON)
        for row in files:
            self.store._delete_blob(row)
        return len(files)

    def location(self) -> str:
        return f"run {self.run_id} (database)"

    def export_args(self) -> str:
        """What ``opseval audio export`` takes to find this run: its id and project."""
        project = self.store.project_ref or "<org/project>"
        return f"{shlex.quote(self.run_id)} --project {shlex.quote(project)}"


class DbRunStore:
    """``storage.RunStore`` on Postgres and a blob store (ADR-0051). Scoped to a project, and
    to a task when it writes: reads see the project's runs (or every run, unscoped), and every
    write names the project in its ``WHERE``, so a store never changes another project's run.

    Every failure of the database or the blob store leaves it as a :class:`StorageError` (a
    harness error, exit 3): never a missing run, never the agent's fault."""

    def __init__(
        self,
        engine: AsyncEngine,
        blobs: BlobStore,
        *,
        project_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        label: str = "database",
        grace_hours: float = 24.0,
        loop: _Loop | None = None,
        timeout: float = 60.0,
        project_ref: str | None = None,
    ) -> None:
        self.project_ref = project_ref
        """The project as named (``org/project``), for the commands a report prints."""
        self.engine = engine
        self.blobs = blobs
        self.project_id = project_id
        self.task_id = task_id
        self.label = label
        self.grace = timedelta(hours=grace_hours)
        self._loop = loop or _Loop(timeout)
        self._sessions: async_sessionmaker[AsyncSession] = sessions(engine)
        self._owners: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
        """Each run's (project, task), looked up once: they never change."""
        self._results: dict[str, tuple[datetime, SuiteResult]] = {}
        """Finished results read, by run, valid while the run's ``updated_at`` is unchanged."""

    # --- opening ---------------------------------------------------------------------------

    @classmethod
    def connect(
        cls,
        settings: Settings,
        blobs: BlobStore,
        *,
        project: str | None = None,
        task: str | None = None,
    ) -> DbRunStore:
        """A store on ``OPSEVAL_DATABASE_URL``, its schema checked current, scoped to
        ``project`` (``org/project``) and its ``task`` (a slug; the project's ``default``
        task when only the project is named and the store writes)."""
        loop = _Loop(settings.storage_timeout_seconds)
        try:
            engine = loop.run(_make(settings))
            try:
                found = loop.run(migrate.current(engine))
            except _DB_ERRORS as exc:
                raise StorageError(
                    f"the database could not be read ({type(exc).__name__})"
                ) from exc
            if found != migrate.head():
                raise ConfigError(
                    f"schema at {found or 'nothing'}, needs {migrate.head()}: run "
                    "`opseval db upgrade`"
                )
            project_id = task_id = None
            if project is not None:
                project_id, task_id = loop.run(_resolve(engine, project, task))
        except BaseException:
            loop.close()
            raise
        return cls(
            engine,
            blobs,
            project_id=project_id,
            task_id=task_id,
            label=f"database ({project or 'every project'})",
            grace_hours=settings.blob_pending_grace_hours,
            loop=loop,
            project_ref=project,
        )

    def close(self) -> None:
        """Release the connections and stop the store's loop. Idempotent; a call after it
        fails at once (:class:`StorageError`) rather than hang."""
        if self._loop.closed:
            return
        with contextlib.suppress(StorageError, *_DB_ERRORS):
            self._loop.run(self.engine.dispose(), timeout=10)
        self._loop.close()

    # --- plumbing --------------------------------------------------------------------------

    def _run(self, fn: Callable[[AsyncSession], Awaitable[T]], *, write: bool = False) -> T:
        async def go() -> T:
            async with self._sessions() as session:
                if write:
                    async with session.begin():
                        return await fn(session)
                return await fn(session)

        try:
            return self._loop.run(go())
        except (ConfigError, StorageError):
            raise
        except _DB_ERRORS as exc:
            raise StorageError(
                f"the database failed ({type(exc).__name__}); nothing was lost that was "
                "already committed"
            ) from exc

    def _scope(self, query: Any, table: Any = Run) -> Any:
        if self.project_id is not None:
            query = query.where(table.project_id == self.project_id)
        return query

    def _live(self, query: Any) -> Any:
        """Runs that are not being deleted (a delete part-way is finished by the next prune)."""
        return self._scope(query).where(Run.status != "deleting")

    def _row(self, run_id: str) -> Run | None:
        async def go(s: AsyncSession) -> Run | None:
            row: Run | None = await s.scalar(self._live(select(Run).where(Run.id == run_id)))
            return row

        return self._run(go)

    def _owner(self, run_id: str) -> tuple[uuid.UUID, uuid.UUID]:
        """The run's (project, task). A run of another project is refused by name, never
        written to (review of #93)."""
        hit = self._owners.get(run_id)
        if hit is not None:
            return hit

        async def go(s: AsyncSession) -> tuple[uuid.UUID, uuid.UUID] | None:
            found = (
                await s.execute(select(Run.project_id, Run.task_id).where(Run.id == run_id))
            ).one_or_none()
            return None if found is None else (found[0], found[1])

        found = self._run(go)
        if found is None:
            raise ConfigError(f"no run {run_id!r} in {self.label}")
        if self.project_id is not None and found[0] != self.project_id:
            raise ConfigError(f"run {run_id!r} belongs to another project: not written")
        self._owners[run_id] = found
        return found

    def foreign(self, run_id: str) -> bool:
        """The id is taken by a run of another project (``runs import`` refuses it first)."""
        if self.project_id is None:
            return False

        async def go(s: AsyncSession) -> uuid.UUID | None:
            found: uuid.UUID | None = await s.scalar(select(Run.project_id).where(Run.id == run_id))
            return found

        owner = self._run(go)
        return owner is not None and owner != self.project_id

    def _blob(self, run_id: str, name: str) -> Blob | None:
        """The newest blob of a run by name, pending or committed."""

        async def go(s: AsyncSession) -> Blob | None:
            q = (
                select(Blob)
                .where(Blob.run_id == run_id, Blob.name == name)
                .order_by(Blob.created_at.desc(), Blob.id.desc())
                .limit(1)
            )
            row: Blob | None = await s.scalar(self._scope(q, Blob))
            return row

        return self._run(go)

    def _blobs(self, run_id: str, *, kind: str) -> list[Blob]:
        async def go(s: AsyncSession) -> list[Blob]:
            q = select(Blob).where(Blob.run_id == run_id, Blob.kind == kind)
            return list(await s.scalars(self._scope(q, Blob)))

        return self._run(go)

    def _get(self, row: Blob) -> bytes:
        """A blob's bytes. Missing from a store other than the one that wrote it is named as
        that mismatch (``BLOB_STORE_MISMATCH``), never as a run whose files are gone."""
        try:
            return self.blobs.get(row.key)
        except FileNotFoundError:
            if row.store != self.blobs.name or row.location != self.blobs.location:
                raise StorageError(
                    f"BLOB_STORE_MISMATCH: {row.name} of run {row.run_id} was written to the "
                    f"{row.store} store at {row.location}, and this process reads the "
                    f"{self.blobs.name} store at {self.blobs.location} (OPSEVAL_BLOB_STORE, "
                    "OPSEVAL_BLOB_DIR, OPSEVAL_S3_*)"
                ) from None
            raise
        except StorageError:
            raise
        except OSError as exc:
            raise BlobStoreError(f"cannot read blob {row.key}: {type(exc).__name__}") from exc

    def _put_pending(
        self,
        run_id: str,
        name: str,
        kind: str,
        data: bytes,
        media_type: str,
        *,
        encryption: str = "none",
        key_id: str | None = None,
        committed: bool = False,
    ) -> uuid.UUID:
        """The pending-row protocol's first two steps: the row (committed), then the object.
        With ``committed``, the row is marked committed once the object is there."""
        project_id, task_id = self._owner(run_id)
        blob_id = uuid.uuid4()
        key = f"runs/{SAFE_ID.sub('_', run_id)}/{blob_id.hex}"
        expected = getattr(self.blobs, "at_rest", "none") if encryption == "none" else encryption

        async def row(s: AsyncSession) -> None:
            s.add(
                Blob(
                    id=blob_id,
                    project_id=project_id,
                    task_id=task_id,
                    run_id=run_id,
                    name=name,
                    kind=kind,
                    media_type=media_type,
                    bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    store=self.blobs.name,
                    location=self.blobs.location[:500],
                    key=key,
                    region=self.blobs.region,
                    encryption=expected,
                    key_id=key_id,
                    state="pending",
                    legal_hold=False,
                )
            )

        self._run(row, write=True)
        at_rest = self.blobs.put(key, data, media_type=media_type)
        values: dict[str, Any] = {}
        if encryption == "none" and at_rest != expected:
            values["encryption"] = at_rest
        if committed:
            values["state"] = "committed"
        if values:

            async def finish(s: AsyncSession) -> None:
                await s.execute(
                    update(Blob)
                    .where(Blob.id == blob_id, Blob.project_id == project_id)
                    .values(**values)
                )

            self._run(finish, write=True)
        return blob_id

    def _put_committed(
        self, run_id: str, name: str, kind: str, data: bytes, media_type: str
    ) -> uuid.UUID:
        return self._put_pending(run_id, name, kind, data, media_type, committed=True)

    def _delete_blob(self, row: Blob) -> None:
        """The object first, then its row: a crash in between leaves a row retention retries."""
        self.blobs.delete(row.key)

        async def go(s: AsyncSession) -> None:
            await s.execute(
                delete(Blob).where(Blob.id == row.id, Blob.project_id == row.project_id)
            )

        self._run(go, write=True)

    def _read_blob(self, blob_id: uuid.UUID | None) -> bytes | None:
        if blob_id is None:
            return None

        async def go(s: AsyncSession) -> Blob | None:
            found: Blob | None = await s.scalar(
                self._scope(select(Blob).where(Blob.id == blob_id), Blob)
            )
            return found

        row = self._run(go)
        return None if row is None else self._get(row)

    # --- the RunStore contract -------------------------------------------------------------

    def where(self) -> str:
        return self.label

    def run_ids(self) -> list[str]:
        async def go(s: AsyncSession) -> list[str]:
            return list(await s.scalars(self._live(select(Run.id)).order_by(Run.id)))

        return self._run(go)

    def exists(self, run_id: str) -> bool:
        return self._row(run_id) is not None

    def has_result(self, run_id: str) -> bool:
        row = self._row(run_id)
        return row is not None and row.result_blob_id is not None

    def has_error(self, run_id: str) -> bool:
        row = self._row(run_id)
        return row is not None and row.error is not None

    def deletable(self, run_id: str) -> bool:
        return self.exists(run_id)

    def delete_run(self, run_id: str) -> bool:
        """Delete a run, crash-safe (review of #93). One transaction first marks the run
        ``deleting`` (no longer listed), removes its attempts, verdicts, scores, metrics and
        usage, and sends every blob back to ``pending``; then each object is deleted and then
        its row, as orphans are; the run row goes once no blob row is left. A failure part-way
        leaves pending rows of a ``deleting`` run, which :meth:`orphans` collects at once:
        never a row that points at a missing object, never an object nothing refers to.

        A run with a blob under legal hold is refused whole, before anything changes."""
        if not self.exists(run_id):
            return False
        project_id, _ = self._owner(run_id)

        async def detach(s: AsyncSession) -> list[Blob]:
            run = await s.scalar(
                select(Run).where(Run.id == run_id, Run.project_id == project_id).with_for_update()
            )
            if run is None:
                return []
            blobs = list(
                await s.scalars(
                    select(Blob).where(Blob.run_id == run_id, Blob.project_id == project_id)
                )
            )
            if any(b.legal_hold for b in blobs):
                raise BlobStoreError(f"run {run_id} has a blob under legal hold: not deleted")
            await s.execute(
                update(Run)
                .where(Run.id == run_id, Run.project_id == project_id)
                .values(
                    status="deleting",
                    result_blob_id=None,
                    report_blob_id=None,
                    junit_blob_id=None,
                    updated_at=func.now(),
                )
            )
            await s.execute(
                delete(Attempt).where(Attempt.run_id == run_id, Attempt.project_id == project_id)
            )
            await s.execute(
                delete(VerdictRow).where(
                    VerdictRow.run_id == run_id, VerdictRow.project_id == project_id
                )
            )
            await s.execute(
                delete(UsageEventRow).where(
                    UsageEventRow.run_id == run_id, UsageEventRow.project_id == project_id
                )
            )
            await s.execute(delete(RunScenario).where(RunScenario.run_id == run_id))
            await s.execute(
                update(Blob)
                .where(Blob.run_id == run_id, Blob.project_id == project_id)
                .values(state="pending")
            )
            return blobs

        blobs = self._run(detach, write=True)
        self._results.pop(run_id, None)
        failed = self._drop_blobs(blobs)
        if failed:
            raise BlobStoreError(
                f"{len(failed)} object(s) of run {run_id} could not be deleted "
                f"({failed[0]}); the run is no longer listed, and the next prune deletes "
                "what is left"
            )
        self._drop_deleted([run_id])
        return True

    def _drop_blobs(self, rows: list[Blob]) -> list[str]:
        failed: list[str] = []
        for row in rows:
            try:
                self._delete_blob(row)
            except StorageError as exc:
                failed.append(f"{row.name}: {exc}")
        return failed

    def _drop_deleted(self, run_ids: list[str] | None = None) -> list[str]:
        """Remove ``deleting`` runs (of this scope) that have no blob row left."""

        async def go(s: AsyncSession) -> list[str]:
            left = select(Blob.id).where(Blob.run_id == Run.id).exists()
            q = self._scope(select(Run.id)).where(Run.status == "deleting", ~left)
            if run_ids is not None:
                q = q.where(Run.id.in_(run_ids))
            gone = list(await s.scalars(q))
            if gone:
                await s.execute(self._scope(delete(Run)).where(Run.id.in_(gone)))
            return gone

        gone = self._run(go, write=True)
        for run_id in gone:
            self._owners.pop(run_id, None)
        return gone

    def _all_blobs(self, run_id: str) -> list[Blob]:
        async def go(s: AsyncSession) -> list[Blob]:
            return list(
                await s.scalars(self._scope(select(Blob).where(Blob.run_id == run_id), Blob))
            )

        return self._run(go)

    def layout(self, run_id: str) -> str:
        row = self._row(run_id)
        return LEGACY_LAYOUT if row is None else row.layout

    def layout_known(self, run_id: str) -> bool:
        return self.layout(run_id) in KNOWN_LAYOUTS

    def check_layout(self, run_id: str) -> None:
        _check_layout(run_id, self.layout(run_id))

    def _task(self) -> tuple[uuid.UUID, uuid.UUID]:
        if self.project_id is None or self.task_id is None:
            raise ConfigError("writing a run to the database needs a project and a task")
        return self.project_id, self.task_id

    def write_manifest(self, run_id: str, manifest: dict[str, Any]) -> None:
        self._write_manifest(run_id, {"layout": LAYOUT, **manifest})

    def import_manifest(self, run_id: str, manifest: dict[str, Any]) -> None:
        """A manifest as another store wrote it, its layout kept (``runs import``): a run
        written before per-call usage must still read as one. The run is marked
        ``importing`` until its result (or error) is written: an import cut short is found,
        and replaced, by the next one."""
        self._write_manifest(run_id, {"layout": LEGACY_LAYOUT, **manifest}, importing=True)

    def importing(self, run_id: str) -> bool:
        """The run was being imported and that import never finished."""
        row = self._row(run_id)
        return row is not None and row.importing

    def _write_manifest(self, run_id: str, body: dict[str, Any], **extra: Any) -> None:
        project_id, task_id = self._task()
        values = {
            "manifest": _json(body),
            "layout": str(body.get("layout", LEGACY_LAYOUT)),
            "suite": str(body.get("suite", ""))[:200],
            "started_at": _when(body.get("started_at")),
            **extra,
        }

        async def go(s: AsyncSession) -> str | None:
            stmt = insert(Run).values(
                id=run_id, project_id=project_id, task_id=task_id, status="running", **values
            )
            written: str | None = await s.scalar(
                stmt.on_conflict_do_update(
                    index_elements=[Run.id],
                    set_={**values, "updated_at": func.now()},
                    where=(Run.project_id == project_id)
                    & (Run.task_id == task_id)
                    & (Run.status != "deleting"),
                ).returning(Run.id)
            )
            return written

        if self._run(go, write=True) is None:
            # The id is taken by a run this store may not touch: said, never written over.
            raise ConfigError(
                f"run id {run_id!r} exists in another project or task (or is being deleted): "
                "not written"
            )
        self._owners[run_id] = (project_id, task_id)

    def load_manifest(self, run_id: str) -> dict[str, Any]:
        row = self._row(run_id)
        return dict(row.manifest) if row is not None else {}

    def save_profile(self, run_id: str, profile: SutProfile) -> None:
        self._set(run_id, profile_snapshot=profile.model_dump_json(indent=2))

    def save_scenario(self, run_id: str, scenario: Scenario) -> None:
        self._owner(run_id)  # the run is this project's: run_scenarios hang off it alone
        body = scenario.model_dump_json(indent=2, by_alias=True)

        async def go(s: AsyncSession) -> None:
            stmt = insert(RunScenario).values(run_id=run_id, scenario_id=scenario.id, body=body)
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[RunScenario.run_id, RunScenario.scenario_id],
                    set_={"body": body},
                )
            )

        self._run(go, write=True)

    def load_snapshot(self, run_id: str) -> tuple[SutProfile, dict[str, Scenario]] | None:
        self.check_layout(run_id)
        row = self._row(run_id)
        if row is None or row.profile_snapshot is None:
            return None

        async def go(s: AsyncSession) -> list[RunScenario]:
            q = select(RunScenario).where(RunScenario.run_id == run_id)
            return list(await s.scalars(q))

        scenarios = sorted(
            (f"{SAFE_ID.sub('_', r.scenario_id)}.json", r.body) for r in self._run(go)
        )
        return parse_snapshot(run_id, [("profile.json", row.profile_snapshot), *scenarios])

    def save_record(self, record: RunRecord) -> str:
        name = _record_name(record.test_id, record.attempt_index)
        data = record.model_dump_json(indent=2).encode("utf-8")
        blob_id = self._put_pending(record.run_id, name, "record", data, JSON)
        project_id, task_id = self._owner(record.run_id)
        prefix = _audio_prefix(record.test_id, record.attempt_index)
        values = {
            "terminal_state": record.terminal_state.value,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "trace_coverage": record.trace_coverage,
            "redaction_applied": record.redaction.applied,
            "record_blob_id": blob_id,
        }
        superseded: list[Blob] = []

        async def go(s: AsyncSession) -> None:
            before = await s.scalar(
                select(Attempt).where(
                    Attempt.run_id == record.run_id,
                    Attempt.project_id == project_id,
                    Attempt.test_id == record.test_id,
                    Attempt.attempt_index == record.attempt_index,
                )
            )
            if before is not None:
                old = await s.get(Blob, before.record_blob_id)
                if old is not None and old.id != blob_id:
                    superseded.append(old)
            stmt = insert(Attempt).values(
                id=uuid.uuid4(),
                run_id=record.run_id,
                project_id=project_id,
                task_id=task_id,
                test_id=record.test_id,
                attempt_index=record.attempt_index,
                **values,
            )
            attempt_id = await s.scalar(
                stmt.on_conflict_do_update(
                    index_elements=[Attempt.run_id, Attempt.test_id, Attempt.attempt_index],
                    set_=values,
                    where=Attempt.project_id == project_id,
                ).returning(Attempt.id)
            )
            if attempt_id is None:
                raise ConfigError(f"run {record.run_id!r} belongs to another project: not written")
            mine = Blob.project_id == project_id
            await s.execute(update(Blob).where(Blob.id == blob_id, mine).values(state="committed"))
            for old in superseded:
                # No longer referred to: back to pending, an orphan retention collects.
                await s.execute(update(Blob).where(Blob.id == old.id, mine).values(state="pending"))
            await s.execute(
                update(Blob)
                .where(
                    mine,
                    Blob.run_id == record.run_id,
                    Blob.kind == "audio",
                    Blob.name.startswith(prefix, autoescape=True),
                )
                .values(state="committed")
            )
            await s.execute(delete(AttemptMetric).where(AttemptMetric.attempt_id == attempt_id))
            metrics = record.voice.metrics if record.voice is not None else None
            for m in metrics or ():
                s.add(
                    AttemptMetric(
                        attempt_id=attempt_id,
                        project_id=project_id,
                        turn=m.turn,
                        name=m.name,
                        value=m.value,
                        unit=m.unit,
                        degraded=m.degraded,
                        reason=m.reason,
                    )
                )
            await s.execute(delete(UsageEventRow).where(UsageEventRow.attempt_id == attempt_id))
            for e in record.usage_events or ():
                s.add(
                    UsageEventRow(
                        run_id=record.run_id,
                        project_id=project_id,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        **_usage_row(e),
                    )
                )

        self._run(go, write=True)
        for old in superseded:
            # Left pending if this fails: retention deletes it once the run has ended.
            with contextlib.suppress(StorageError):
                self._delete_blob(old)
        return f"db:{record.run_id}/{name}"

    def load_record(self, run_id: str, test_id: str, attempt: int) -> RunRecord | None:
        self.check_layout(run_id)

        async def go(s: AsyncSession) -> tuple[Attempt, Blob | None] | None:
            q = (
                select(Attempt, Blob)
                .outerjoin(Blob, Blob.id == Attempt.record_blob_id)
                .where(
                    Attempt.run_id == run_id,
                    Attempt.test_id == test_id,
                    Attempt.attempt_index == attempt,
                )
            )
            found = (await s.execute(self._scope(q, Attempt))).one_or_none()
            return None if found is None else (found[0], found[1])

        found = self._run(go)
        if found is None:
            return None
        if found[1] is None:
            raise FileNotFoundError(f"the record of {test_id} attempt {attempt} is gone")
        return RunRecord.model_validate_json(self._get(found[1]))

    def load_records(self, run_id: str) -> list[RunRecord]:
        self.check_layout(run_id)

        async def go(s: AsyncSession) -> list[tuple[Attempt, Blob | None]]:
            q = (
                select(Attempt, Blob)
                .outerjoin(Blob, Blob.id == Attempt.record_blob_id)
                .where(Attempt.run_id == run_id)
            )
            return [(a, b) for a, b in (await s.execute(self._scope(q, Attempt))).all()]

        out = []
        for row, blob in self._run(go):
            if blob is None:
                raise FileNotFoundError(f"the record of {row.test_id} is gone")
            out.append(RunRecord.model_validate_json(self._get(blob)))
        return sorted(out, key=lambda r: (r.test_id, r.attempt_index))

    def _set(self, run_id: str, **values: Any) -> None:
        """Update the run's row; exactly one row, of this project, or a :class:`ConfigError`."""
        project_id, _ = self._owner(run_id)

        async def go(s: AsyncSession) -> int:
            done = await s.execute(
                update(Run)
                .where(Run.id == run_id, Run.project_id == project_id)
                .values(**values, updated_at=func.now())
            )
            return int(getattr(done, "rowcount", 0) or 0)

        if self._run(go, write=True) != 1:
            raise ConfigError(f"run {run_id!r} belongs to another project, or is gone: not written")

    def save_progress(self, run_id: str, progress: RunProgress) -> None:
        self._set(run_id, progress=progress.model_dump(mode="json"))

    def load_progress(self, run_id: str) -> RunProgress | None:
        row = self._row(run_id)
        return None if row is None else _progress(row.progress)

    def request_cancel(self, run_id: str, *, by: str) -> bool:
        if not self.exists(run_id):
            return False
        project_id, _ = self._owner(run_id)
        body = {"requested_at": datetime.now(UTC).isoformat(), "by": by}

        async def go(s: AsyncSession) -> int:
            done = await s.execute(
                update(Run)
                .where(Run.id == run_id, Run.project_id == project_id, Run.cancel.is_(None))
                .values(cancel=body)
            )
            return int(getattr(done, "rowcount", 0) or 0)

        return self._run(go, write=True) == 1

    def cancel_requested(self, run_id: str) -> bool:
        async def go(s: AsyncSession) -> bool:
            q = self._live(select(Run.cancel.is_not(None)).where(Run.id == run_id))
            found: bool | None = await s.scalar(q)
            return bool(found)

        return self._run(go)

    def save_error(self, run_id: str, error: dict[str, Any]) -> None:
        """A run that could not be carried out, even one that failed before its manifest."""
        body = _json(error)
        if self._row(run_id) is None:
            self._write_manifest(run_id, {"layout": LAYOUT, "run_id": run_id})
        self._set(
            run_id,
            error=body,
            status="harness_error",
            ended_at=datetime.now(UTC),
            importing=False,
        )

    def load_error(self, run_id: str) -> dict[str, Any] | None:
        row = self._row(run_id)
        return dict(row.error) if row is not None and isinstance(row.error, dict) else None

    def save_result(self, result: SuiteResult) -> None:
        """The run, its verdict and score rows, and its result, report and JUnit blobs, in one
        transaction: until it commits, the run is still running."""
        run_id = result.run_id
        project_id, task_id = self._owner(run_id)
        blob_id = self._put_pending(
            run_id, "result.json", "result", result.model_dump_json(indent=2).encode(), JSON
        )
        report, junit = self._blob(run_id, "report.html"), self._blob(run_id, "junit.xml")
        passed = sum(t.status is VerdictStatus.PASSED for t in result.tests)
        failed = sum(t.status is VerdictStatus.FAILED for t in result.tests)

        async def go(s: AsyncSession) -> int:
            attempts = {
                (a.test_id, a.attempt_index): a.id
                for a in await s.scalars(
                    select(Attempt).where(
                        Attempt.run_id == run_id, Attempt.project_id == project_id
                    )
                )
            }
            await s.execute(
                delete(VerdictRow).where(
                    VerdictRow.run_id == run_id, VerdictRow.project_id == project_id
                )
            )
            if attempts:
                await s.execute(
                    delete(ScoreRow).where(ScoreRow.attempt_id.in_(list(attempts.values())))
                )
            for t in result.tests:
                s.add(
                    VerdictRow(
                        run_id=run_id,
                        project_id=project_id,
                        task_id=task_id,
                        test_id=t.test_id,
                        status=t.status.value,
                        skip_kind=t.skip_kind,
                        flaky=t.flaky,
                        cut_short_of=t.cut_short_of,
                        criticality=t.criticality.value,
                        pass_rate=t.pass_rate,
                    )
                )
                for a in t.attempts:
                    attempt_id = attempts.get((t.test_id, a.attempt_index))
                    if attempt_id is None:
                        continue  # no record was written for it: nothing to hang scores on
                    await s.execute(
                        update(Attempt)
                        .where(Attempt.id == attempt_id)
                        .values(passed=a.passed, invalid_reason=a.invalid_reason)
                    )
                    for x in a.scores:
                        s.add(
                            ScoreRow(
                                attempt_id=attempt_id,
                                project_id=project_id,
                                scorer_id=x.scorer_id,
                                scorer_version=x.scorer_version,
                                axis=x.axis.value,
                                status=x.status.value,
                                score=x.score,
                                threshold=x.threshold,
                                weight=x.weight,
                                degraded=x.degraded,
                                vacuous=x.details.get("vacuous") is True,
                                reason_code=_code(x.reason),
                            )
                        )
            await s.execute(
                delete(UsageEventRow).where(
                    UsageEventRow.run_id == run_id,
                    UsageEventRow.project_id == project_id,
                    UsageEventRow.attempt_id.is_(None),
                )
            )
            for e in result.usage_events:
                s.add(
                    UsageEventRow(
                        run_id=run_id, project_id=project_id, task_id=task_id, **_usage_row(e)
                    )
                )
            committed = [blob_id] + [b.id for b in (report, junit) if b is not None]
            await s.execute(
                update(Blob)
                .where(Blob.id.in_(committed), Blob.project_id == project_id)
                .values(state="committed")
            )
            done = await s.execute(
                update(Run)
                .where(Run.id == run_id, Run.project_id == project_id)
                .values(
                    status="finished",
                    importing=False,
                    suite=result.suite[:200],
                    exit_code=result.exit_code,
                    gate_state=gate_state(result),
                    passed=passed,
                    failed=failed,
                    unjudged=len(result.tests) - passed - failed,
                    flaky=sum(t.flaky for t in result.tests),
                    started_at=result.started_at,
                    ended_at=result.ended_at,
                    result_blob_id=blob_id,
                    report_blob_id=report.id if report is not None else None,
                    junit_blob_id=junit.id if junit is not None else None,
                    updated_at=func.now(),
                )
            )
            return int(getattr(done, "rowcount", 0) or 0)

        if self._run(go, write=True) != 1:
            raise ConfigError(f"run {run_id!r} belongs to another project, or is gone: not written")

    def load_result(self, run_id: str) -> SuiteResult:
        self.check_layout(run_id)
        row = self._row(run_id)
        data = self._read_blob(row.result_blob_id if row is not None else None)
        if data is None:
            raise FileNotFoundError(f"run {run_id} has no result")
        return SuiteResult.model_validate_json(data)

    def read_finished(self) -> FinishedRuns:
        """Every finished run, newest first, in one query; each result downloaded once and
        reused while its run is unchanged. A run whose result cannot be read is named in
        ``unreadable`` (``RUN_UNREADABLE``), never dropped."""

        async def go(s: AsyncSession) -> list[tuple[str, str, datetime, Blob | None]]:
            q = (
                select(Run.id, Run.layout, Run.updated_at, Blob)
                .outerjoin(Blob, Blob.id == Run.result_blob_id)
                .where(Run.result_blob_id.is_not(None))
            )
            return [(r, lay, at, b) for r, lay, at, b in (await s.execute(self._live(q))).all()]

        out: list[SuiteResult] = []
        unreadable: list[UnreadableRun] = []
        for run_id, layout, changed, blob in self._run(go):
            if layout not in KNOWN_LAYOUTS:
                continue  # listed by the console as an error row: shown, never misread
            hit = self._results.get(run_id)
            if hit is not None and hit[0] == changed:
                out.append(hit[1])
                continue
            try:
                if blob is None:
                    raise FileNotFoundError(f"run {run_id} has no result")
                result = SuiteResult.model_validate_json(self._get(blob))
            except (ValueError, OSError, StorageError) as exc:
                unreadable.append(UnreadableRun(run_id, _why(exc)))
                continue
            self._results[run_id] = (changed, result)
            out.append(result)
        return FinishedRuns(
            sorted(out, key=lambda r: r.started_at, reverse=True),
            sorted(unreadable, key=lambda u: u.run_id),
        )

    def listing(self) -> list[StoredRun]:
        """Every run as a listing needs it, from the ``runs`` rows alone: one query, no
        result downloaded (review of #93)."""

        async def go(s: AsyncSession) -> list[Run]:
            q = self._live(select(Run).options(defer(Run.profile_snapshot)))
            return list(await s.scalars(q))

        out: list[StoredRun] = []
        for row in self._run(go):
            finished = row.result_blob_id is not None
            counts = None
            if finished and row.started_at is not None and row.gate_state is not None:
                counts = RunCounts(
                    suite=row.suite,
                    started_at=row.started_at,
                    gate=row.gate_state,
                    passed=row.passed or 0,
                    failed=row.failed or 0,
                    unjudged=row.unjudged or 0,
                    flaky=row.flaky or 0,
                )
            out.append(
                StoredRun(
                    run_id=row.id,
                    layout=row.layout,
                    manifest=dict(row.manifest),
                    finished=finished,
                    error=dict(row.error) if isinstance(row.error, dict) else None,
                    progress=None if finished else _progress(row.progress),
                    cancel_requested=row.cancel is not None,
                    changed_at=row.updated_at.timestamp() if finished else None,
                    counts=counts,
                )
            )
        return out

    def changed_at(self, run_id: str, rescore: str | None = None) -> float | None:
        if rescore is not None:
            found = self._blob(run_id, f"{RESCORE_PREFIX}{rescore}/result.json")
            return None if found is None else found.created_at.timestamp()
        row = self._row(run_id)
        if row is None or row.result_blob_id is None:
            return None
        return row.updated_at.timestamp()

    def save_report(self, run_id: str, html: str) -> None:
        self._put_pending(run_id, "report.html", "report", html.encode("utf-8"), "text/html")

    def load_report(self, run_id: str) -> str | None:
        row = self._row(run_id)
        data = self._read_blob(row.report_blob_id if row is not None else None)
        return None if data is None else data.decode("utf-8")

    def save_junit(self, run_id: str, xml: str) -> None:
        self._put_pending(run_id, "junit.xml", "junit", xml.encode("utf-8"), "application/xml")

    def load_junit(self, run_id: str) -> str | None:
        row = self._row(run_id)
        data = self._read_blob(row.junit_blob_id if row is not None else None)
        return None if data is None else data.decode("utf-8")

    def save_rescore(
        self, run_id: str, stamp: str, result: SuiteResult, html: str, xml: str
    ) -> str:
        base = f"{RESCORE_PREFIX}{stamp}/"
        if self._blob(run_id, base + "result.json") is not None:
            raise FileExistsError(base)
        project_id, _ = self._owner(run_id)
        ids = [
            self._put_pending(run_id, base + "report.html", "report", html.encode(), "text/html"),
            self._put_pending(run_id, base + "junit.xml", "junit", xml.encode(), "application/xml"),
            self._put_pending(
                run_id,
                base + "result.json",
                "result",
                result.model_dump_json(indent=2).encode(),
                JSON,
            ),
        ]

        async def go(s: AsyncSession) -> None:
            await s.execute(
                update(Blob)
                .where(Blob.id.in_(ids), Blob.project_id == project_id)
                .values(state="committed")
            )

        self._run(go, write=True)
        return stamp

    def rescores(self, run_id: str) -> list[str]:
        async def go(s: AsyncSession) -> list[str]:
            q = select(Blob.name).where(
                Blob.run_id == run_id,
                Blob.state == "committed",
                Blob.kind == "result",
                Blob.name.startswith(RESCORE_PREFIX, autoescape=True),
            )
            return list(await s.scalars(self._scope(q, Blob)))

        names = self._run(go)
        return sorted(n[len(RESCORE_PREFIX) : -len("/result.json")] for n in names)

    def load_rescore(self, run_id: str, stamp: str) -> SuiteResult:
        found = self._blob(run_id, f"{RESCORE_PREFIX}{stamp}/result.json")
        if found is None or found.state != "committed":
            raise FileNotFoundError(stamp)
        return SuiteResult.model_validate_json(self._get(found))

    def audio(self, run_id: str) -> DbRunAudio:
        return DbRunAudio(self, run_id)

    def import_pruned_marker(self, run_id: str, text: str | None) -> None:
        """A run folder's audio prune marker, kept as written (unreadable stays unreadable)."""
        body = (text if text is not None else "").encode("utf-8")
        self._put_committed(run_id, MARKER, "other", body, JSON)

    # --- retention of orphans --------------------------------------------------------------

    def orphans(self, now: datetime | None = None) -> list[Orphan]:
        """Pending blobs older than ``OPSEVAL_BLOB_PENDING_GRACE_HOURS`` whose run is no longer
        running (it finished, failed, or its process on this host is gone), and every blob of
        a run whose delete was cut short (``deleting``: no grace, nothing refers to them).
        Never a listing of the store: an object without a row is not Opseval's to delete."""
        cutoff = (now or datetime.now(UTC)) - self.grace

        async def go(s: AsyncSession) -> list[tuple[Blob, Run]]:
            q = (
                select(Blob, Run)
                .join(Run, Run.id == Blob.run_id)
                .where(
                    Blob.state == "pending",
                    ~Blob.legal_hold,
                    or_(Blob.created_at < cutoff, Run.status == "deleting"),
                )
            )
            return [(b, r) for b, r in (await s.execute(self._scope(q, Blob))).all()]

        out = []
        for blob, run in self._run(go):
            ended = run.status != "running" or run.error is not None or process_gone(run.manifest)
            if ended:
                out.append(Orphan(blob.id, blob.run_id, blob.name, blob.key, blob.created_at))
        return sorted(out, key=lambda o: (o.created_at, str(o.id)))

    def delete_orphans(self, items: list[Orphan]) -> OrphanOutcome:
        """Each orphan's object, then its row; re-checked as pending first. A run whose delete
        was cut short goes once its last blob has."""
        deleted: list[str] = []
        failed: list[tuple[str, str]] = []
        for item in items:

            async def still(s: AsyncSession, item: Orphan = item) -> Blob | None:
                row: Blob | None = await s.scalar(
                    self._scope(select(Blob).where(Blob.id == item.id), Blob)
                )
                return row if row is not None and row.state == "pending" else None

            try:
                row = self._run(still)
                if row is None:
                    continue
                self._delete_blob(row)
            except StorageError as exc:
                failed.append((f"{item.run_id}/{item.name}", f"{type(exc).__name__}: {exc}"))
                continue
            deleted.append(f"{item.run_id}/{item.name}")
        self._drop_deleted()
        return OrphanOutcome(deleted, failed)


def _check_layout(run_id: str, found: str) -> None:
    if found not in KNOWN_LAYOUTS:
        raise UnknownLayout(
            f"run {run_id!r} was written in layout {found!r}, which this version of "
            f"Opseval cannot read (it reads {', '.join(sorted(KNOWN_LAYOUTS))}); "
            "upgrade Opseval to read it"
        )


def _progress(raw: Any) -> RunProgress | None:
    if raw is None:
        return None
    try:
        return RunProgress.model_validate(raw)
    except ValueError:
        return None


def _why(exc: BaseException) -> str:
    text = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _code(reason: str | None) -> str | None:
    """A reason's ``CODE:`` prefix (``TRACE_UNAVAILABLE: …``), or ``None``."""
    if not reason:
        return None
    head = reason.split(":", 1)[0].strip()
    return head[:100] if head and head.replace("_", "").isalnum() and head.isupper() else None


async def _make(settings: Settings) -> AsyncEngine:
    return make_engine(settings)


async def _resolve(
    engine: AsyncEngine, project: str, task: str | None
) -> tuple[uuid.UUID, uuid.UUID | None]:
    async with sessions(engine)() as s:
        found = await get_project(s, project)
        if task is None:
            return found.id, None
        return found.id, (await get_task(s, project, task)).id


__all__ = ["DbRunAudio", "DbRunStore", "Orphan", "OrphanOutcome"]
