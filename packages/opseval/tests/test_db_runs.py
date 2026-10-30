# SPDX-License-Identifier: Apache-2.0
"""Runs and blobs in the database (ADR-0051): DbRunStore against FileStore, the constraints
that keep the one rule, the pending-blob protocol, and import."""

from __future__ import annotations

import asyncio
import base64
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from opseval.blobs import FileBlobStore
from opseval.config import Settings
from opseval.db import migrate
from opseval.db.catalogue_store import ensure_project, ensure_task
from opseval.db.engine import make_engine, sessions
from opseval.db.run_import import import_run
from opseval.db.run_store import DbRunStore
from opseval.demo import catalogue_dir
from opseval.demo.refund_agent import run_agent
from opseval.errors import BlobStoreError, ConfigError, StorageError
from opseval.models.profile import FrameKind
from opseval.models.record import FrameRecord, RunRecord, TerminalState, VoiceTimeline
from opseval.models.results import SuiteResult
from opseval.rescore import rescore_stored
from opseval.runner import RunOptions, launch
from opseval.server import views
from opseval.storage import FileStore, RunStore
from opseval.voice import AudioRecorder
from opseval.voice.crypto import AudioKey
from opseval.voice.playback import turns, whole_call

PROJECT = "acme/bot"
KEY = base64.b64encode(bytes(range(32))).decode()


def settings_for(url: str, tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        allowed_hosts="127.0.0.1",
        database_url=url,
        reports_dir=str(tmp_path / "reports"),
    )


async def prepare(url: str, tmp_path: Path) -> None:
    engine = make_engine(settings_for(url, tmp_path))
    try:
        await migrate.upgrade(engine)
        async with sessions(engine)() as s, s.begin():
            await ensure_project(s, PROJECT)
    finally:
        await engine.dispose()


@pytest.fixture
async def db(pg_url: str, tmp_path: Path) -> AsyncIterator[DbRunStore]:
    await prepare(pg_url, tmp_path)
    store = DbRunStore.connect(
        settings_for(pg_url, tmp_path),
        FileBlobStore(tmp_path / "blobs"),
        project=PROJECT,
        task="default",
    )
    try:
        yield store
    finally:
        store.close()


async def demo(store: RunStore, tmp_path: Path, bug: str | None = None) -> SuiteResult:
    cat = catalogue_dir()
    settings = Settings(
        _env_file=None, allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "reports")
    )
    async with run_agent(bug=bug) as url:  # type: ignore[arg-type]
        options = RunOptions(target=url, profile=cat / "profile.yaml", store=store)
        return await launch(cat / "suites" / "smoke.yaml", options, settings)


def sql(store: DbRunStore, query: str, **params: Any) -> list[Any]:
    async def go() -> list[Any]:
        async with store.engine.connect() as conn:
            return list((await conn.execute(text(query), params)).all())

    return store._loop.run(go())


def execute(store: DbRunStore, query: str, **params: Any) -> None:
    async def go() -> None:
        async with store.engine.begin() as conn:
            await conn.execute(text(query), params)

    store._loop.run(go())


# --- the same answers from both stores --------------------------------------------------------


def answers(store: RunStore, run_id: str) -> dict[str, Any]:
    """Every read the console, rescore and analytics make, as plain data."""
    result = store.load_result(run_id)
    records = store.load_records(run_id)
    snapshot = store.load_snapshot(run_id)
    manifest = dict(store.load_manifest(run_id))
    detail = views.run_detail(store, run_id)
    return {
        "layout": store.layout(run_id),
        "has_result": store.has_result(run_id),
        "has_error": store.has_error(run_id),
        "manifest": manifest,
        "result": result.model_dump(mode="json"),
        "records": [r.model_dump(mode="json") for r in records],
        "one": [store.load_record(run_id, r.test_id, r.attempt_index) == r for r in records],
        "snapshot": None
        if snapshot is None
        else (snapshot[0].model_dump(), {k: v.model_dump() for k, v in snapshot[1].items()}),
        "detail": detail.model_dump(mode="json") if detail else None,
        "attempts": [
            views.attempt_record(store, run_id, r.test_id, r.attempt_index).model_dump(mode="json")  # type: ignore[union-attr]
            for r in records
        ],
        "list": [r.model_dump(mode="json") for r in views.list_runs(store)],
        "usage": [u.model_dump(mode="json") for u in views.usage(store)],
        "trend": [t.model_dump(mode="json") for t in views.trend(store)],
        "rescores": store.rescores(run_id),
        "report": store.load_report(run_id),
        "junit": store.load_junit(run_id),
        "progress": store.load_progress(run_id),
        "cancel": store.cancel_requested(run_id),
    }


class TestTheSameAnswers:
    async def test_a_demo_run_imported_reads_exactly_as_its_folder(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        files = FileStore(tmp_path / "reports")
        result = await demo(files, tmp_path, bug="skip-approval")
        assert result.exit_code == 1
        outcome = import_run(files, result.run_id, db)
        assert outcome.outcome == "imported", outcome.detail
        assert answers(db, result.run_id) == answers(files, result.run_id)

    async def test_a_run_written_straight_to_the_database(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        result = await demo(db, tmp_path)
        assert result.exit_code == 0
        assert db.load_result(result.run_id) == result
        assert not (tmp_path / "reports").exists(), "nothing was written to a folder"
        rows = sql(db, "SELECT status, exit_code, gate_state, passed, failed FROM runs")
        assert rows == [("finished", 0, "safe", len(result.tests), 0)]
        scores = sql(db, "SELECT status, score FROM scores")
        assert scores and all((s == "infra_error") == (v is None) for s, v in scores)
        attempts = sum(len(t.attempts) for t in result.tests)
        assert sql(db, "SELECT count(*) FROM attempts") == [(attempts,)]
        assert sql(db, "SELECT count(*) FROM verdicts") == [(len(result.tests),)]
        # Every blob the run refers to is committed; none is left pending.
        assert sql(db, "SELECT DISTINCT state FROM blobs") == [("committed",)]
        detail = views.run_detail(db, result.run_id)
        assert detail is not None and detail.summary.gate == "safe"

    async def test_rescore_in_the_database(self, db: DbRunStore, tmp_path: Path) -> None:
        result = await demo(db, tmp_path, bug="forget-method")
        settings = Settings(_env_file=None, allowed_hosts="127.0.0.1")
        outcome = await rescore_stored(db, result.run_id, settings)
        assert db.rescores(result.run_id) == [outcome.stamp]
        assert db.load_rescore(result.run_id, outcome.stamp) == outcome.result
        assert db.changed_at(result.run_id, outcome.stamp) is not None
        assert db.load_result(result.run_id) == result, "the original is never rewritten"
        with pytest.raises(FileExistsError):
            db.save_rescore(result.run_id, outcome.stamp, outcome.result, "", "")


class TestLayouts:
    async def test_an_unknown_layout_is_refused_by_every_reader(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        result = await demo(db, tmp_path)
        execute(db, "UPDATE runs SET layout = 'opseval-runs/99'")
        assert not db.layout_known(result.run_id)
        from opseval.storage import UnknownLayout

        with pytest.raises(UnknownLayout):
            db.load_result(result.run_id)
        with pytest.raises(UnknownLayout):
            db.load_records(result.run_id)
        assert db.read_finished().results == []
        (row,) = [r for r in views.list_runs(db) if r.id == result.run_id]
        assert row.error is not None and "newer Opseval" in row.error

    async def test_an_old_layout_keeps_its_layout_when_imported(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        files = FileStore(tmp_path / "reports")
        result = await demo(files, tmp_path)
        manifest = files.load_manifest(result.run_id)
        manifest["layout"] = "opseval-runs/5"  # before per-call usage
        files.write_manifest(result.run_id, manifest)  # write_manifest puts its own first
        path = tmp_path / "reports" / result.run_id / "manifest.json"
        path.write_text(path.read_text().replace('"opseval-runs/8"', '"opseval-runs/1"', 1))
        assert import_run(files, result.run_id, db).outcome == "imported"
        assert db.layout(result.run_id) == files.layout(result.run_id)
        usage = views.usage(db)
        assert [u.per_call for u in usage] == [u.per_call for u in views.usage(files)]


# --- what the schema refuses -----------------------------------------------------------------


class TestConstraints:
    async def _attempt(self, db: DbRunStore, tmp_path: Path) -> tuple[str, str, str]:
        result = await demo(db, tmp_path)
        ((attempt, project),) = sql(db, "SELECT id, project_id FROM attempts LIMIT 1")
        return result.run_id, str(attempt), str(project)

    @pytest.mark.parametrize(
        ("status", "score"), [("infra_error", 0.0), ("failed", None), ("passed", None)]
    )
    async def test_a_harness_error_is_never_a_score_and_a_score_never_missing(
        self, db: DbRunStore, tmp_path: Path, status: str, score: float | None
    ) -> None:
        _, attempt, project = await self._attempt(db, tmp_path)
        with pytest.raises(IntegrityError, match="ck_scores_infra_error_score"):
            execute(
                db,
                "INSERT INTO scores (id, attempt_id, project_id, scorer_id, axis, status, score, "
                "threshold, weight, degraded, vacuous) VALUES (:id, :a, :p, 'x', 'output', :s, "
                ":v, 1, 1, false, false)",
                id=uuid.uuid4(),
                a=attempt,
                p=project,
                s=status,
                v=score,
            )

    async def test_an_unmeasured_metric_says_why(self, db: DbRunStore, tmp_path: Path) -> None:
        _, attempt, project = await self._attempt(db, tmp_path)
        insert = (
            "INSERT INTO attempt_metrics (id, attempt_id, project_id, turn, name, value, unit, "
            "degraded, reason) VALUES (:id, :a, :p, NULL, 'voice.dead_air_ms', :v, 'ms', false, "
            ":r)"
        )
        with pytest.raises(IntegrityError, match="ck_attempt_metrics_unmeasured_reason"):
            execute(db, insert, id=uuid.uuid4(), a=attempt, p=project, v=None, r=None)
        execute(db, insert, id=uuid.uuid4(), a=attempt, p=project, v=None, r="no spans")
        execute(db, insert, id=uuid.uuid4(), a=attempt, p=project, v=0.0, r=None)

    async def test_a_cost_is_unknown_exactly_when_it_is_missing(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        run_id, _, project = await self._attempt(db, tmp_path)
        ((task,),) = sql(db, "SELECT task_id FROM runs")
        with pytest.raises(IntegrityError, match="ck_usage_events_cost_source"):
            execute(
                db,
                "INSERT INTO usage_events (id, run_id, project_id, task_id, at, role, model, "
                "answered, rate_limited, retries, cost_usd, cost_source) VALUES (:id, :r, :p, :t, "
                "now(), 'judge', 'm', true, 0, 0, 0, 'unknown')",
                id=uuid.uuid4(),
                r=run_id,
                p=project,
                t=task,
            )

    async def test_a_run_is_in_a_task_of_its_own_project(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        async def other_project() -> tuple[uuid.UUID, uuid.UUID]:
            async with sessions(db.engine)() as s, s.begin():
                project = await ensure_project(s, "acme/other")
                task, _ = await ensure_task(s, "acme/other", "voice", kind="voice", modality="text")
                return project.id, task.id

        other, other_task = db._loop.run(other_project())
        assert db.project_id is not None and db.task_id is not None
        with pytest.raises(IntegrityError, match="fk_runs_task_id_tasks"):
            execute(
                db,
                "INSERT INTO runs (id, project_id, task_id, suite, layout, status, manifest) "
                "VALUES ('r-cross', :p, :t, '', 'opseval-runs/8', 'running', '{}')",
                p=db.project_id,
                t=other_task,
            )
        assert other is not None

    async def test_a_blob_state_and_kind_are_checked(self, db: DbRunStore, tmp_path: Path) -> None:
        result = await demo(db, tmp_path)
        with pytest.raises(IntegrityError, match="ck_blobs_state"):
            execute(db, "UPDATE blobs SET state = 'lost' WHERE run_id = :r", r=result.run_id)


# --- the pending-blob protocol and retention -------------------------------------------------


class TestPendingBlobs:
    def _run(self, db: DbRunStore, run_id: str = "r-pending") -> str:
        db.write_manifest(run_id, {"run_id": run_id, "started_at": datetime.now(UTC).isoformat()})
        return run_id

    async def test_a_crash_before_the_referring_row_leaves_an_orphan(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        run_id = self._run(db)
        db.save_report(run_id, "<html>never finished</html>")  # pending until save_result
        (row,) = db._all_blobs(run_id)
        assert row.state == "pending" and db.blobs.exists(row.key)
        later = datetime.now(UTC) + timedelta(hours=25)
        assert db.orphans(later) == [], "the run may still be going: never its blobs"
        db.save_error(run_id, {"code": "HARNESS_ERROR", "message": "crashed"})
        assert db.orphans(datetime.now(UTC)) == [], "inside the grace period"
        (orphan,) = db.orphans(later)
        outcome = db.delete_orphans([orphan])
        assert outcome.deleted == [f"{run_id}/report.html"] and outcome.failed == []
        assert not db.blobs.exists(row.key) and db._all_blobs(run_id) == []

    async def test_a_row_whose_object_never_arrived_is_cleaned_too(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        run_id = self._run(db)

        class Down(FileBlobStore):
            def put(self, key: str, data: bytes, *, media_type: str) -> str:
                raise BlobStoreError("the store is down")

        real, db.blobs = db.blobs, Down(tmp_path / "blobs")
        try:
            with pytest.raises(BlobStoreError):
                db.save_report(run_id, "x")
        finally:
            db.blobs = real
        db.save_error(run_id, {"code": "HARNESS_ERROR", "message": "blob store down"})
        (orphan,) = db.orphans(datetime.now(UTC) + timedelta(hours=25))
        assert db.delete_orphans([orphan]).deleted == [f"{run_id}/report.html"]

    async def test_an_object_that_cannot_be_deleted_keeps_its_row(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        run_id = self._run(db)
        db.save_report(run_id, "x")
        db.save_error(run_id, {"code": "X", "message": ""})
        orphans = db.orphans(datetime.now(UTC) + timedelta(hours=25))

        class Stuck(FileBlobStore):
            def delete(self, key: str) -> None:
                raise BlobStoreError("denied")

        db.blobs = Stuck(tmp_path / "blobs")
        outcome = db.delete_orphans(orphans)
        assert outcome.deleted == [] and "denied" in outcome.failed[0][1]
        assert len(db._all_blobs(run_id)) == 1, "the row stays, so the next prune retries"

    async def test_a_record_saved_again_leaves_no_stale_blob(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        result = await demo(db, tmp_path)
        records = sql(db, "SELECT count(*) FROM blobs WHERE kind = 'record'")
        attempts = sql(db, "SELECT count(*) FROM attempts")
        assert records == attempts, "each attempt's first save was replaced and deleted"
        assert db.orphans(datetime.now(UTC) + timedelta(days=2)) == []
        assert result.exit_code == 0

    async def test_a_blob_under_legal_hold_is_never_deleted(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        result = await demo(db, tmp_path)
        execute(db, "UPDATE blobs SET legal_hold = true WHERE name = 'result.json'")
        with pytest.raises(BlobStoreError, match="legal hold"):
            db.delete_run(result.run_id)
        assert db.load_result(result.run_id) == result

    async def test_retention_deletes_a_whole_run(self, db: DbRunStore, tmp_path: Path) -> None:
        from opseval.retention import apply, plan

        result = await demo(db, tmp_path)
        keys = [b.key for b in db._all_blobs(result.run_id)]
        later = datetime.now(UTC) + timedelta(days=100)
        items = plan(db, older_than_days=90, now=later)
        assert [(i.run_id, i.action) for i in items] == [(result.run_id, "delete")]
        assert apply(db, items).deleted == [result.run_id]
        assert db.run_ids() == [] and not any(db.blobs.exists(k) for k in keys)
        assert sql(db, "SELECT count(*) FROM scores") == [(0,)]


# --- writers at once ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_two_runs_write_at_once(self, db: DbRunStore, tmp_path: Path) -> None:
        first, second = await asyncio.gather(
            demo(db, tmp_path / "a"), demo(db, tmp_path / "b", bug="skip-approval")
        )
        assert {first.exit_code, second.exit_code} == {0, 1}
        assert sorted(db.run_ids()) == sorted([first.run_id, second.run_id])
        assert db.load_result(first.run_id) == first and db.load_result(second.run_id) == second

    async def test_many_threads_share_one_store(self, db: DbRunStore, tmp_path: Path) -> None:
        errors: list[BaseException] = []

        def write(n: int) -> None:
            try:
                run_id = f"r-thread-{n}"
                db.write_manifest(run_id, {"run_id": run_id})
                for i in range(5):
                    db.save_report(run_id, f"<p>{n}/{i}</p>")
                assert db.request_cancel(run_id, by="t") and not db.request_cancel(run_id, by="t")
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(db.run_ids()) == 8


# --- progress, cancel, errors ------------------------------------------------------------------


class TestRunState:
    async def test_an_error_before_the_manifest_is_a_harness_error(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        db.save_error("r-early", {"code": "CONFIG", "message": "bad profile"})
        assert db.has_error("r-early") and not db.has_result("r-early")
        detail = views.run_detail(db, "r-early")
        assert detail is not None and detail.exit_code == 3
        assert detail.summary.passed is None, "never drawn as zeros"

    async def test_a_running_run_shows_its_progress_and_takes_a_cancel(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        from opseval.progress import ProgressLog

        db.write_manifest("r-live", {"run_id": "r-live", "suite": "smoke"})
        log = ProgressLog(db, "r-live", ["a", "b"])
        log.started("a", 2)
        with pytest.raises(views.RunInProgress) as found:
            views.run_detail(db, "r-live")
        assert found.value.progress is not None and found.value.progress.scenarios_planned == 2
        assert db.request_cancel("r-live", by="console") and db.cancel_requested("r-live")
        assert not db.request_cancel("r-live", by="console")

    async def test_a_store_without_a_task_cannot_write(
        self, db: DbRunStore, pg_url: str, tmp_path: Path
    ) -> None:
        reader = DbRunStore.connect(
            settings_for(pg_url, tmp_path), FileBlobStore(tmp_path / "blobs"), project=PROJECT
        )
        try:
            with pytest.raises(ConfigError, match="needs a project and a task"):
                reader.write_manifest("r-x", {})
        finally:
            reader.close()

    async def test_an_old_schema_is_named(self, pg_url: str, tmp_path: Path) -> None:
        engine = make_engine(settings_for(pg_url, tmp_path))
        await migrate.upgrade(engine, "0004")
        await engine.dispose()
        with pytest.raises(ConfigError, match="opseval db upgrade"):
            DbRunStore.connect(settings_for(pg_url, tmp_path), FileBlobStore(tmp_path / "b"))


# --- voice: audio in the blob store ------------------------------------------------------------


def voice_run(store: RunStore, run_id: str, key: str | None = KEY) -> RunRecord:
    started = datetime.now(UTC)
    store.write_manifest(run_id, {"run_id": run_id, "started_at": started.isoformat()})
    recorder = AudioRecorder(
        store.audio(run_id),
        "v-001",
        0,
        "files",
        key=AudioKey.from_text(key) if key else None,
        run_id=run_id,
    )
    frames = []
    for seq, (speaker, value) in enumerate([("caller", 1000), ("agent", -2000)]):
        pcm = bytes(array_of(value, 8000 * 300 // 1000))
        ref = recorder.keep(
            turn=seq + 1,
            speaker=speaker,
            pcm16=pcm,
            rate=8000,
            start_ms=seq * 400,  # type: ignore[arg-type]
        )
        frames.append(
            FrameRecord(
                seq=seq,
                at=started,
                direction="sent" if speaker == "caller" else "received",
                kind=FrameKind.MESSAGE,
                payload={},
                text=f"turn {seq}",
                audio=ref,
            )
        )
    record = RunRecord(
        run_id=run_id,
        test_id="v-001",
        attempt_index=0,
        seed=1,
        target="ws://127.0.0.1:1",
        started_at=started,
        ended_at=started,
        terminal_state=TerminalState.GOAL_REACHED,
        frames=tuple(frames),
        voice=VoiceTimeline(codec="pcm16", rate=8000),
    )
    store.save_record(record)
    return record


def array_of(value: int, n: int) -> bytes:
    from array import array

    return array("h", [value] * n).tobytes()


class TestVoice:
    async def test_audio_is_stored_sealed_and_plays_back(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        files = FileStore(tmp_path / "reports")
        record = voice_run(db, "r-voice")
        twin = voice_run(files, "r-voice")
        assert [b.encryption for b in db._blobs("r-voice", kind="audio")] == ["aes-256-gcm"] * 2
        assert {b.state for b in db._blobs("r-voice", kind="audio")} == {"committed"}
        key = AudioKey.from_text(KEY)
        assert [t.state for t in turns(db.audio("r-voice"), record)] == ["available"] * 2
        db_call = whole_call(db.audio("r-voice"), record, key)
        file_call = whole_call(files.audio("r-voice"), twin, key)
        assert db_call.wav == file_call.wav

    async def test_plain_audio_is_refused(self, db: DbRunStore, tmp_path: Path) -> None:
        with pytest.raises(BlobStoreError, match="only ever stored encrypted"):
            voice_run(db, "r-plain", key=None)
        assert db._blobs("r-plain", kind="audio") == []

    async def test_a_path_out_of_audio_is_refused(self, db: DbRunStore) -> None:
        from opseval.storage import AudioPathRefused

        db.write_manifest("r-a", {"run_id": "r-a"})
        for bad in ("../x", "audio/../result.json", "records/x.json", "/audio/x"):
            with pytest.raises(AudioPathRefused):
                db.audio("r-a").size(bad)

    async def test_audio_retention_leaves_a_marker(self, db: DbRunStore, tmp_path: Path) -> None:
        record = voice_run(db, "r-old")
        db.save_error("r-old", {"code": "X", "message": "ended"})
        assert db.audio("r-old").count() == 2
        db.audio("r-old").prune(at=datetime(2026, 10, 1, tzinfo=UTC), older_than_days=30)
        assert db.audio("r-old").count() is None
        assert {(t.state, t.note) for t in turns(db.audio("r-old"), record)} == {
            ("pruned", "audio pruned on 2026-10-01")
        }

    async def test_a_voice_folder_imports_with_its_audio(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        files = FileStore(tmp_path / "reports")
        record = voice_run(files, "r-imp")
        (tmp_path / "reports" / "r-imp" / "error.json").write_text('{"code": "X"}')
        assert import_run(files, "r-imp", db).outcome == "imported"
        key = AudioKey.from_text(KEY)
        assert whole_call(db.audio("r-imp"), record, key).wav == (
            whole_call(files.audio("r-imp"), record, key).wav
        )


# --- import --------------------------------------------------------------------------------------


class TestImport:
    async def test_import_is_idempotent_and_refuses_a_changed_run(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        files = FileStore(tmp_path / "reports")
        result = await demo(files, tmp_path)
        assert import_run(files, result.run_id, db).outcome == "imported"
        again = import_run(files, result.run_id, db)
        assert (again.outcome, again.detail) == ("unchanged", "already imported")
        changed = result.model_copy(update={"exit_code": 1})
        files.save_result(changed)
        refused = import_run(files, result.run_id, db)
        assert refused.outcome == "refused" and "different run" in refused.detail
        assert db.load_result(result.run_id) == result

    async def test_unreadable_or_running_folders_are_skipped_whole(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        files = FileStore(tmp_path / "reports")
        files.write_manifest("r-running", {"run_id": "r-running"})
        assert import_run(files, "r-running", db).outcome == "refused"
        result = await demo(files, tmp_path)
        broken = next((tmp_path / "reports" / result.run_id / "records").glob("*.json"))
        broken.write_text("{not json")
        out = import_run(files, result.run_id, db)
        assert out.outcome == "refused"
        assert db.run_ids() == [], "never half-imported"

    async def test_a_plain_voice_folder_is_refused_before_anything_is_written(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        files = FileStore(tmp_path / "reports")
        voice_run(files, "r-demo-voice", key=None)
        (tmp_path / "reports" / "r-demo-voice" / "error.json").write_text("{}")
        out = import_run(files, "r-demo-voice", db)
        assert out.outcome == "refused" and "plain audio" in out.detail
        assert db.run_ids() == []


# --- serve and the command line in server mode -------------------------------------------------

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class TestServeFromTheDatabase:
    async def test_start_a_run_read_it_back_and_play_audio(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        import httpx

        from opseval.server import create_app

        cat = catalogue_dir()
        settings = Settings(
            _env_file=None,
            allowed_hosts="127.0.0.1",
            reports_dir=str(tmp_path / "reports"),
            audio_encryption_key=KEY,
        )
        app = create_app(
            settings=settings,
            catalogue=cat,
            profile=cat / "profile.yaml",
            token=TOKEN,
            store=db,
        )
        transport = httpx.ASGITransport(app=app)
        async with (
            run_agent(bug=None) as url,
            httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000", headers=AUTH
            ) as http,
        ):
            started = await http.post(
                "/api/v1/runs", json={"suite": "smoke", "repeats": 1, "target": url}
            )
            assert started.status_code == 202
            run_id = started.json()["runId"]
            for _ in range(400):
                runs = {r["id"]: r for r in (await http.get("/api/v1/runs")).json()}
                if runs.get(run_id, {}).get("state") == "finished":
                    break
                await asyncio.sleep(0.05)
            assert runs[run_id]["state"] == "finished" and runs[run_id]["failed"] == 0
            assert db.has_result(run_id) and not (tmp_path / "reports").exists()
            detail = (await http.get(f"/api/v1/runs/{run_id}")).json()
            test = detail["tests"][0]
            attempt = await http.get(f"/api/v1/runs/{run_id}/attempts/{test['id']}/0")
            assert attempt.status_code == 200 and attempt.json()["entries"]
            health = (await http.get("/api/v1/health")).json()
            assert health["reportsDir"].startswith("database")
            cost = await http.get("/api/v1/analytics/cost")
            assert cost.status_code == 200

            record = voice_run(db, "r-heard")
            db.save_error("r-heard", {"code": "X", "message": "a voice run"})
            base = "/api/v1/runs/r-heard/attempts/v-001/0/audio"
            listed = (await http.get(base)).json()
            assert [t["state"] for t in listed["turns"]] == ["available", "available"]
            call = await http.get(base + "/call")
            assert call.status_code == 200
            assert (
                call.content == whole_call(db.audio("r-heard"), record, AudioKey.from_text(KEY)).wav
            )

    async def test_run_and_import_from_the_command_line(
        self, db: DbRunStore, pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from opseval.cli import app

        monkeypatch.setenv("OPSEVAL_DATABASE_URL", pg_url)
        monkeypatch.setenv("OPSEVAL_ALLOWED_HOSTS", "127.0.0.1")
        monkeypatch.setenv("OPSEVAL_BLOB_DIR", str(tmp_path / "blobs"))
        monkeypatch.setenv("OPSEVAL_REPORTS_DIR", str(tmp_path / "reports"))
        monkeypatch.setenv("OPSEVAL_DOTENV", "false")
        cat = catalogue_dir()
        runner = CliRunner()
        async with run_agent(bug="skip-approval") as url:
            args = [
                "run",
                str(cat / "suites" / "smoke.yaml"),
                "--profile",
                str(cat / "profile.yaml"),
                "--target",
                url,
                "--project",
                PROJECT,
                "--junit",
                str(tmp_path / "junit.xml"),
            ]
            done = await asyncio.to_thread(runner.invoke, app, args)
        assert done.exit_code == 1, done.output
        assert "Stored: run" in done.output and (tmp_path / "junit.xml").is_file()
        assert not (tmp_path / "reports").exists()
        (stored,) = db.read_finished().results
        assert stored.exit_code == 1

        files = FileStore(tmp_path / "folders")
        local = await demo(files, tmp_path)
        folder = tmp_path / "folders" / local.run_id
        imported = await asyncio.to_thread(
            runner.invoke, app, ["runs", "import", str(folder), "--project", PROJECT]
        )
        assert imported.exit_code == 0 and "imported" in imported.output
        again = await asyncio.to_thread(
            runner.invoke, app, ["runs", "import", str(folder), "--project", PROJECT]
        )
        assert again.exit_code == 0 and "unchanged" in again.output
        bad = await asyncio.to_thread(
            runner.invoke,
            app,
            ["runs", "import", str(tmp_path / "nowhere"), "--project", PROJECT],
        )
        assert bad.exit_code == 3 and "refused" in bad.output

        pruned = await asyncio.to_thread(
            runner.invoke, app, ["prune", "--project", PROJECT, "--older-than", "1"]
        )
        assert pruned.exit_code == 0, pruned.output
        assert "newer than 1 days" in pruned.output

    def test_database_url_without_a_project_is_refused(self) -> None:
        from typer.testing import CliRunner

        from opseval.cli import app

        out = CliRunner().invoke(
            app,
            [
                "run",
                "x",
                "--profile",
                "p",
                "--target",
                "ws://127.0.0.1:1",
                "--database-url",
                "postgresql://u:secret@127.0.0.1/db",
            ],
        )
        assert out.exit_code == 3 and "needs --project" in out.output
        assert "secret" not in out.output


class TestOnS3:
    async def test_a_run_through_the_s3_store(self, pg_url: str, tmp_path: Path) -> None:
        from tests.test_blobs import FakeS3, s3

        await prepare(pg_url, tmp_path)
        fake = FakeS3()
        store = DbRunStore.connect(
            settings_for(pg_url, tmp_path), s3(fake), project=PROJECT, task="default"
        )
        try:
            result = await demo(store, tmp_path)
            assert store.load_result(result.run_id) == result
            assert len(store.load_records(result.run_id)) == sum(
                len(t.attempts) for t in result.tests
            )
            rows = sql(store, "SELECT DISTINCT store, encryption FROM blobs")
            assert rows == [("s3", "sse:AES256")]
            assert fake.objects and all(k.startswith("/runs/runs/") for k in fake.objects)
        finally:
            store.close()


# --- review of #93 ---------------------------------------------------------------------------

OTHER = "acme/other"


async def other_store(pg_url: str, tmp_path: Path) -> DbRunStore:
    engine = make_engine(settings_for(pg_url, tmp_path))
    try:
        async with sessions(engine)() as s, s.begin():
            await ensure_project(s, OTHER)
    finally:
        await engine.dispose()
    return DbRunStore.connect(
        settings_for(pg_url, tmp_path),
        FileBlobStore(tmp_path / "blobs"),
        project=OTHER,
        task="default",
    )


def footprint(store: DbRunStore, run_id: str) -> dict[str, Any]:
    """Every row and object of a run, to prove nothing changed."""
    blobs = sql(
        store, "SELECT id, state, key, sha256 FROM blobs WHERE run_id = :r ORDER BY id", r=run_id
    )
    return {
        "run": sql(
            store,
            "SELECT project_id, task_id, status, updated_at, manifest::text, result_blob_id, "
            "progress::text, cancel::text FROM runs WHERE id = :r",
            r=run_id,
        ),
        "attempts": sql(
            store, "SELECT id, record_blob_id FROM attempts WHERE run_id = :r ORDER BY id", r=run_id
        ),
        "verdicts": sql(store, "SELECT count(*) FROM verdicts WHERE run_id = :r", r=run_id),
        "blobs": blobs,
        "objects": [store.blobs.get(b.key) for b in blobs],
    }


class TestTenancy:
    async def test_import_never_touches_another_projects_run(
        self, db: DbRunStore, pg_url: str, tmp_path: Path
    ) -> None:
        other = await other_store(pg_url, tmp_path)
        try:
            files = FileStore(tmp_path / "reports")
            result = await demo(files, tmp_path)
            run_id = result.run_id
            assert import_run(files, run_id, other).outcome == "imported"
            before = footprint(other, run_id)
            out = import_run(files, run_id, db)
            assert out.outcome == "refused" and "another project" in out.detail
            assert footprint(other, run_id) == before, "B untouched: rows and objects"
            assert db.run_ids() == [] and not db.exists(run_id)
        finally:
            other.close()

    async def test_every_write_is_scoped_to_the_project(
        self, db: DbRunStore, pg_url: str, tmp_path: Path
    ) -> None:
        from opseval.models.progress import RunProgress

        other = await other_store(pg_url, tmp_path)
        try:
            result = await demo(other, tmp_path)
            run_id = result.run_id
            record = other.load_records(run_id)[0]
            before = footprint(other, run_id)
            progress = RunProgress.model_validate({"stage": "running", "scenarios": []})
            writes = [
                lambda: db.save_progress(run_id, progress),
                lambda: db.save_error(run_id, {"code": "X"}),
                lambda: db.write_manifest(run_id, {"run_id": run_id}),
                lambda: db.save_record(record),
                lambda: db.save_result(result),
                lambda: db.save_report(run_id, "<p>x</p>"),
                lambda: db.save_scenario(
                    run_id, next(iter(other.load_snapshot(run_id)[1].values()))
                ),  # type: ignore[index]
            ]
            for write in writes:
                with pytest.raises(ConfigError, match="another project"):
                    write()
            assert db.request_cancel(run_id, by="x") is False
            assert db.delete_run(run_id) is False
            assert footprint(other, run_id) == before
        finally:
            other.close()


class TestCrashSafeRetention:
    async def test_a_held_run_and_a_503_run_never_keep_the_others(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        from opseval.retention import apply, plan

        held = (await demo(db, tmp_path / "1")).run_id
        broken = (await demo(db, tmp_path / "2")).run_id
        normal = (await demo(db, tmp_path / "3")).run_id
        execute(
            db,
            "UPDATE blobs SET legal_hold = true WHERE run_id = :r AND name = 'result.json'",
            r=held,
        )
        broken_keys = {b.key for b in db._all_blobs(broken)}
        normal_keys = {b.key for b in db._all_blobs(normal)}

        class Outage(FileBlobStore):
            def delete(self, key: str) -> None:
                if key in broken_keys:
                    raise BlobStoreError("the blob store at s3.example.test answered HTTP 503")
                super().delete(key)

        real, db.blobs = db.blobs, Outage(tmp_path / "blobs")
        later = datetime.now(UTC) + timedelta(days=3)
        outcome = apply(db, plan(db, older_than_days=1, now=later))
        assert outcome.deleted == [normal]
        assert {f[0] for f in outcome.failed} == {held, broken}
        assert "legal hold" in dict(outcome.failed)[held] and "503" in dict(outcome.failed)[broken]
        assert not any(real.exists(k) for k in normal_keys)
        assert db.load_result(held).run_id == held, "held: whole and readable"
        # The broken run is no longer listed, and no row points at a missing object.
        assert sorted(db.run_ids()) == [held]
        assert sql(db, "SELECT count(*) FROM attempts WHERE run_id = :r", r=broken) == [(0,)]
        rows = db._all_blobs(broken)
        assert rows and {r.state for r in rows} == {"pending"}
        assert all(real.exists(r.key) for r in rows)
        # The store answers again: the next sweep finishes the delete.
        db.blobs = real
        swept = db.delete_orphans(db.orphans())
        assert swept.failed == [] and len(swept.deleted) == len(broken_keys)
        assert sql(db, "SELECT count(*) FROM runs WHERE id = :r", r=broken) == [(0,)]
        assert not any(real.exists(k) for k in broken_keys)

    async def test_prune_lists_failures_and_still_sweeps_orphans(
        self, db: DbRunStore, pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.cli import _prune

        run_id = (await demo(db, tmp_path)).run_id
        execute(db, "UPDATE blobs SET legal_hold = true WHERE run_id = :r", r=run_id)
        db.write_manifest(
            "r-orphan", {"run_id": "r-orphan", "started_at": datetime.now(UTC).isoformat()}
        )
        db.save_report("r-orphan", "x")
        db.save_error("r-orphan", {"code": "X", "message": ""})
        execute(
            db, "UPDATE blobs SET created_at = now() - interval '2 days' WHERE run_id = 'r-orphan'"
        )
        execute(
            db,
            "UPDATE runs SET started_at = now() - interval '9 days', manifest = jsonb_set("
            "manifest, '{started_at}', to_jsonb((now() - interval '9 days')::text))",
        )
        import typer

        with pytest.raises(typer.Exit) as found:
            _prune(db, db, 1, 1, True)
        assert found.value.exit_code == 3
        assert db._all_blobs("r-orphan") == [], "the orphan was swept despite the held run"
        assert db.exists(run_id)


class TestImportResumes:
    async def test_an_import_cut_short_is_replaced_by_the_next(
        self, db: DbRunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        files = FileStore(tmp_path / "reports")
        result = await demo(files, tmp_path)

        def power_cut(_: SuiteResult) -> None:
            raise RuntimeError("power cut")

        def database_gone(_: str) -> bool:
            raise BlobStoreError("the database is gone too")

        monkeypatch.setattr(db, "save_result", power_cut)
        monkeypatch.setattr(db, "delete_run", database_gone)
        with pytest.raises(RuntimeError, match="power cut") as found:
            import_run(files, result.run_id, db)
        notes = " ".join(getattr(found.value, "__notes__", []))
        assert "could not be removed" in notes and "next import replaces it" in notes
        monkeypatch.undo()
        assert db.importing(result.run_id) and not db.has_result(result.run_id)
        again = import_run(files, result.run_id, db)
        assert again.outcome == "imported" and "cut short" in again.detail
        assert db.load_result(result.run_id) == result and not db.importing(result.run_id)
        assert db.orphans(datetime.now(UTC) + timedelta(days=2)) == []
        assert import_run(files, result.run_id, db).outcome == "unchanged"


class TestNoSilentLoss:
    async def test_an_unreadable_result_is_named_not_dropped(
        self, db: DbRunStore, pg_url: str, tmp_path: Path
    ) -> None:
        result = await demo(db, tmp_path)
        run_id = result.run_id
        row = db._blob(run_id, "result.json")
        assert row is not None
        db.blobs.delete(row.key)
        db._results.clear()
        finished = db.read_finished()
        assert finished.results == [] and [u.run_id for u in finished.unreadable] == [run_id]
        detail = views.run_detail(db, run_id)
        assert detail is not None and "RUN_UNREADABLE" in (detail.summary.error or "")

    async def test_another_blob_folder_is_named_as_such(
        self, db: DbRunStore, pg_url: str, tmp_path: Path
    ) -> None:
        result = await demo(db, tmp_path)
        elsewhere = DbRunStore.connect(
            settings_for(pg_url, tmp_path),
            FileBlobStore(tmp_path / "elsewhere"),
            project=PROJECT,
            task="default",
        )
        try:
            with pytest.raises(StorageError, match="BLOB_STORE_MISMATCH") as found:
                elsewhere.load_records(result.run_id)
            assert str((tmp_path / "blobs").resolve()) in str(found.value)
            finished = elsewhere.read_finished()
            assert "BLOB_STORE_MISMATCH" in finished.unreadable[0].reason
        finally:
            elsewhere.close()


class TestListing:
    async def test_listing_n_runs_takes_a_bounded_number_of_queries(
        self, db: DbRunStore, tmp_path: Path
    ) -> None:
        from sqlalchemy import event

        for n in range(4):
            await demo(db, tmp_path / str(n))
        db.write_manifest(
            "r-live", {"run_id": "r-live", "started_at": datetime.now(UTC).isoformat()}
        )
        statements: list[str] = []

        def count(*args: Any) -> None:
            statements.append(str(args[2]))

        event.listen(db.engine.sync_engine, "before_cursor_execute", count)
        try:
            rows = views.list_runs(db)
        finally:
            event.remove(db.engine.sync_engine, "before_cursor_execute", count)
        assert len(rows) == 5 and [r.state for r in rows].count("finished") == 4
        assert len(statements) <= 3, statements
        assert not any("FROM blobs" in q for q in statements), "no result downloaded"


class TestStorageFailures:
    async def test_a_locked_row_times_out_as_a_storage_error(
        self, pg_url: str, tmp_path: Path
    ) -> None:
        import asyncpg  # type: ignore[import-untyped]

        await prepare(pg_url, tmp_path)
        settings = settings_for(pg_url, tmp_path)
        settings.storage_timeout_seconds = 1
        store = DbRunStore.connect(
            settings, FileBlobStore(tmp_path / "blobs"), project=PROJECT, task="default"
        )
        conn = await asyncpg.connect(pg_url)
        try:
            store.write_manifest("r-lock", {"run_id": "r-lock"})
            tx = conn.transaction()
            await tx.start()
            await conn.execute("SELECT 1 FROM runs WHERE id = 'r-lock' FOR UPDATE")
            began = asyncio.get_running_loop().time()
            with pytest.raises(StorageError):
                await asyncio.to_thread(store.request_cancel, "r-lock", by="t")
            assert asyncio.get_running_loop().time() - began < 5
            await tx.rollback()
            assert store.request_cancel("r-lock", by="t")
        finally:
            await conn.close()
            store.close()
        store.close()  # idempotent
        with pytest.raises(StorageError, match="closed"):
            store.exists("r-lock")


class TestAudioExportFromTheDatabase:
    async def test_export_and_list_by_run_id_and_project(
        self, db: DbRunStore, pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from opseval.cli import app
        from opseval.reporting import audio_note

        record = voice_run(db, "r-exp")
        db.save_error("r-exp", {"code": "X", "message": "a voice run"})
        note = audio_note(record, db.audio("r-exp"))
        assert note is not None and note.command is not None
        assert note.command.startswith("opseval audio export r-exp --project acme/bot ")
        for name, value in {
            "OPSEVAL_DATABASE_URL": pg_url,
            "OPSEVAL_ALLOWED_HOSTS": "127.0.0.1",
            "OPSEVAL_BLOB_DIR": str(tmp_path / "blobs"),
            "OPSEVAL_AUDIO_ENCRYPTION_KEY": KEY,
            "OPSEVAL_DOTENV": "false",
        }.items():
            monkeypatch.setenv(name, value)
        out = tmp_path / "call.wav"
        runner = CliRunner()
        args = [
            "audio",
            "export",
            "r-exp",
            "--project",
            PROJECT,
            "--test",
            "v-001",
            "--out",
            str(out),
        ]
        done = await asyncio.to_thread(runner.invoke, app, args)
        assert done.exit_code == 0, done.output
        assert (
            out.read_bytes() == whole_call(db.audio("r-exp"), record, AudioKey.from_text(KEY)).wav
        )
        listed = await asyncio.to_thread(
            runner.invoke, app, ["audio", "list", "r-exp", "--project", PROJECT]
        )
        assert listed.exit_code == 0 and "v-001  attempt 0" in listed.output
        missing = await asyncio.to_thread(
            runner.invoke, app, ["audio", "list", "r-nope", "--project", PROJECT]
        )
        assert missing.exit_code == 3 and "no run" in missing.output
