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
from opseval.errors import BlobStoreError, ConfigError
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
        assert db.finished_results() == []
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
                task, _ = await ensure_task(
                    s, "acme/other", "voice", kind="voice", modality="text"
                )
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
