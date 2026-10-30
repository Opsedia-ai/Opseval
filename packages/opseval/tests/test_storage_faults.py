# SPDX-License-Identifier: Apache-2.0
"""Storage faults never look like agent failures (review of #93, ADR-0051).

A slow store is off the event loop and off the agent's clock; a failing one is a harness
error (exit 3), never a TIMEOUT or a fail; an unreadable run is named, never dropped; the
store's own loop has a timeout and a clean close; the console says STORAGE_UNAVAILABLE.
These run without a database: the stores here are run folders with injected faults.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from opseval.blobs import FileBlobStore, S3BlobStore, blob_dir, blob_store, host_header
from opseval.config import Settings
from opseval.errors import BlobStoreError, ConfigError, HarnessError, StorageError
from opseval.models.record import TerminalState
from opseval.models.results import SuiteResult
from opseval.runner import RunOptions, launch
from opseval.storage import FileRunAudio, FileStore
from opseval.voice.recording import AudioWriter

# --- the voice demo through a store with faults ------------------------------------------------


class FaultyAudio(FileRunAudio):
    def __init__(self, folder: Path, run_id: str, fault: Callable[[], None]) -> None:
        super().__init__(folder, run_id)
        self.fault = fault

    def write(self, relative: str, data: bytes) -> None:
        self.fault()
        super().write(relative, data)


class FaultyStore(FileStore):
    """Run folders whose audio writes (and, if asked, record saves) are slow or fail."""

    def __init__(
        self,
        root: Path,
        *,
        audio: Callable[[], None] = lambda: None,
        record: Callable[[], None] = lambda: None,
    ) -> None:
        super().__init__(root)
        self.audio_fault, self.record_fault = audio, record
        self.writers: set[str] = set()

    def audio(self, run_id: str) -> FileRunAudio:
        return FaultyAudio(self.run_dir(run_id), run_id, self._audio)

    def _audio(self) -> None:
        self.writers.add(threading.current_thread().name)
        self.audio_fault()

    def save_record(self, record: Any) -> str:
        self.record_fault()
        return super().save_record(record)


def voice_catalogue(tmp_path: Path, turn_timeout: float) -> Path:
    from opseval.demo import voice_catalogue_dir

    cat = tmp_path / "voice-catalogue"
    shutil.copytree(voice_catalogue_dir(), cat)
    for path in (cat / "scenarios").glob("*.yaml"):
        text = path.read_text()
        path.write_text(
            text.replace("turn_timeout_seconds: 4", f"turn_timeout_seconds: {turn_timeout}")
        )
    return cat


def voice_settings(tmp_path: Path, speech_url: str) -> Settings:
    settings = Settings(
        _env_file=None, allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "reports")
    )
    settings.tts_provider = settings.stt_provider = "openai"
    settings.tts_url = settings.stt_url = speech_url
    settings.tts_model, settings.stt_model = "demo-tts", "demo-stt"
    settings.tts_voice_map = {"default": "demo"}
    settings.tts_format = "wav"
    settings.tts_timeout_seconds = settings.stt_timeout_seconds = 5.0
    settings.record_audio = "files"
    return settings


async def voice_demo(store: FileStore, tmp_path: Path, *, turn_timeout: float = 4) -> SuiteResult:
    from opseval.demo.voice_agent import run_voice_demo

    cat = voice_catalogue(tmp_path, turn_timeout)
    async with run_voice_demo(None) as (url, speech_url):
        options = RunOptions(
            target=url,
            transport="audio-ws",
            profile=cat / "profile.yaml",
            repeats=1,
            synthetic_voices=True,
            only_ids=["vr-001"],
            store=store,
        )
        return await launch(
            cat / "suites" / "smoke.yaml", options, voice_settings(tmp_path, speech_url)
        )


def latencies(store: FileStore, result: SuiteResult) -> list[float]:
    out = []
    for record in store.load_records(result.run_id):
        for m in record.voice.metrics if record.voice and record.voice.metrics else ():
            if m.name == "voice.turn_latency_ms" and m.value is not None:
                out.append(m.value)
    return out


SLOW = 1.5
"""Seconds per stored audio file: longer than the turn timeout below."""


class TestStorageIsOffTheClock:
    async def test_a_slow_blob_store_is_never_the_agents_timeout(self, tmp_path: Path) -> None:
        store = FaultyStore(tmp_path / "reports", audio=lambda: time.sleep(SLOW))
        result = await voice_demo(store, tmp_path / "slow", turn_timeout=1)
        states = {r.terminal_state for r in store.load_records(result.run_id)}
        assert TerminalState.TIMEOUT not in states and result.exit_code == 0, states
        assert all(v < SLOW * 1000 for v in latencies(store, result)), "storage is not latency"
        assert store.writers and "MainThread" not in store.writers, "never on the event loop"
        (record,) = store.load_records(result.run_id)
        stored = [f.audio for f in record.frames if f.audio is not None and f.audio.path]
        assert stored and all(store.audio(result.run_id).size(a.path or "") for a in stored)

    async def test_a_slow_store_does_not_stall_another_lane(self, tmp_path: Path) -> None:
        """Two runs on one event loop, as lanes are: the slow store's run waits on its own
        writes and saves, never on the loop, so the other run's turns are untouched."""

        def slow() -> None:
            time.sleep(SLOW)

        slow_store = FaultyStore(tmp_path / "a", audio=slow, record=slow)
        fast_store = FaultyStore(tmp_path / "b")
        done: dict[str, float] = {}

        async def timed(name: str, store: FaultyStore) -> SuiteResult:
            result = await voice_demo(store, tmp_path / name, turn_timeout=1)
            done[name] = time.monotonic()
            return result

        slow_result, fast_result = await asyncio.gather(
            timed("slow", slow_store), timed("fast", fast_store)
        )
        assert slow_result.exit_code == 0 and fast_result.exit_code == 0
        assert done["fast"] < done["slow"], "the fast lane never waited for the slow store"
        for store, result in ((slow_store, slow_result), (fast_store, fast_result)):
            states = {r.terminal_state for r in store.load_records(result.run_id)}
            assert TerminalState.TIMEOUT not in states
            assert all(v < SLOW * 1000 for v in latencies(store, result))

    async def test_a_failing_blob_store_is_a_harness_error(self, tmp_path: Path) -> None:
        def down() -> None:
            raise BlobStoreError("the blob store at s3.example.test refused (HTTP 503)")

        store = FaultyStore(tmp_path / "reports", audio=down)
        result = await voice_demo(store, tmp_path / "down")
        assert result.exit_code == 3, "not judged: never an agent failure"
        (record,) = store.load_records(result.run_id)
        assert record.terminal_state is TerminalState.HARNESS_FAILURE
        assert "could not be stored" in (record.stop_reason or "")
        assert "goal_reached" in (record.stop_reason or ""), "how the call itself had ended"
        assert any("BlobStoreError" in e for e in record.infra_errors)
        (test,) = result.tests
        assert test.status.value != "failed"

    async def test_a_record_that_cannot_be_stored_stops_the_run_as_a_harness_error(
        self, tmp_path: Path
    ) -> None:
        def down() -> None:
            raise StorageError("the database did not answer within 60s")

        store = FaultyStore(tmp_path / "reports", record=down)
        with pytest.raises(HarnessError):
            await voice_demo(store, tmp_path / "down")


class TestAudioWriter:
    def test_files_are_written_on_another_thread_in_order(self, tmp_path: Path) -> None:
        seen: list[tuple[str, str]] = []

        class Audio(FileRunAudio):
            def write(self, relative: str, data: bytes) -> None:
                seen.append((relative, threading.current_thread().name))
                super().write(relative, data)

        writer = AudioWriter(Audio(tmp_path / "r", "r"))
        for n in range(3):
            writer.submit(f"audio/a/attempt0/00{n}-caller.wav", b"x")
        writer.flush()
        assert [s[0][-14:] for s in seen] == [f"00{n}-caller.wav" for n in range(3)]
        assert {s[1] for s in seen} == {"opseval-audio-writer"}
        assert writer.files == 3
        writer.flush()  # idempotent

    def test_a_failure_is_raised_by_flush_as_a_storage_error(self, tmp_path: Path) -> None:
        class Broken(FileRunAudio):
            def write(self, relative: str, data: bytes) -> None:
                raise PermissionError("read-only file system")

        writer = AudioWriter(Broken(tmp_path / "r", "r"))
        writer.submit("audio/a/attempt0/001-caller.wav", b"x")
        with pytest.raises(StorageError, match="PermissionError"):
            writer.flush()

    def test_too_much_waiting_audio_is_refused_never_dropped_silently(self, tmp_path: Path) -> None:
        gate = threading.Event()

        class Stuck(FileRunAudio):
            def write(self, relative: str, data: bytes) -> None:
                gate.wait(5)

        writer = AudioWriter(Stuck(tmp_path / "r", "r"), limit_bytes=10)
        writer.submit("audio/a/attempt0/001-caller.wav", b"12345678")
        writer.submit("audio/a/attempt0/002-agent.wav", b"12345678")
        gate.set()
        with pytest.raises(StorageError, match="OPSEVAL_AUDIO_WRITE_QUEUE_MB"):
            writer.flush()


# --- the store's own event loop ------------------------------------------------------------


class TestStoreLoop:
    def test_a_hung_call_times_out_as_a_storage_error(self) -> None:
        from opseval.db.run_store import _Loop

        loop = _Loop(timeout=0.2)
        try:
            with pytest.raises(StorageError, match="OPSEVAL_STORAGE_TIMEOUT_SECONDS"):
                loop.run(asyncio.sleep(30))
            assert loop.run(asyncio.sleep(0, result=7)) == 7, "the loop still works after it"
        finally:
            loop.close()

    def test_a_call_after_close_fails_at_once_and_close_is_idempotent(self) -> None:
        from opseval.db.run_store import _Loop

        loop = _Loop(timeout=5)
        loop.close()
        loop.close()
        assert loop.loop.is_closed()
        started = time.monotonic()
        with pytest.raises(StorageError, match="closed"):
            loop.run(asyncio.sleep(0))
        assert time.monotonic() - started < 1

    def test_errors_from_the_coroutine_are_its_own(self) -> None:
        from opseval.db.run_store import _Loop

        async def boom() -> None:
            raise TimeoutError("its own")

        loop = _Loop(timeout=5)
        try:
            with pytest.raises(TimeoutError, match="its own"):
                loop.run(boom())
        finally:
            loop.close()


# --- blob store configuration ------------------------------------------------------------------


class TestBlobFolder:
    def test_a_relative_blob_dir_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="absolute path"):
            blob_store(Settings(_env_file=None, blob_dir="blobs"))

    def test_the_default_is_absolute_inside_the_reports_folder(self, tmp_path: Path) -> None:
        found = blob_dir(Settings(_env_file=None, reports_dir=str(tmp_path / "reports")))
        assert found.is_absolute() and found == (tmp_path / "reports").resolve() / ".blobs"
        assert FileStore(tmp_path / "reports").listing() == [], "never listed as a run"

    def test_the_location_is_recorded_absolute(self, tmp_path: Path) -> None:
        store = FileBlobStore(tmp_path / "x" / ".." / "blobs")
        assert store.location == str((tmp_path / "blobs").resolve())


class TestS3Host:
    def _store(self, endpoint: str, seen: list[httpx.Request]) -> S3BlobStore:
        from tests.test_blobs import FakeS3

        fake = FakeS3()

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return fake(request)

        return S3BlobStore(
            endpoint=endpoint,
            bucket="runs",
            region="eu-west-1",
            access_key=SecretStr("AK"),
            secret_key=SecretStr("SK"),
            allowlist={"s3.example.test", "127.0.0.1"},
            client=httpx.Client(transport=httpx.MockTransport(answer)),
        )

    @pytest.mark.parametrize(
        ("endpoint", "host"),
        [
            ("https://s3.example.test:443", "s3.example.test"),
            ("https://S3.Example.test", "s3.example.test"),
            ("https://s3.example.test:9000", "s3.example.test:9000"),
            ("http://127.0.0.1:9000", "127.0.0.1:9000"),
            ("http://127.0.0.1:80", "127.0.0.1"),
        ],
    )
    def test_the_host_is_signed_and_sent_as_one(self, endpoint: str, host: str) -> None:
        assert host_header(endpoint) == host
        seen: list[httpx.Request] = []
        store = self._store(endpoint, seen)
        store.put("runs/r/k", b"x", media_type="text/plain")
        assert store.get("runs/r/k") == b"x", "the signature checked against the sent Host"
        assert {r.headers["host"] for r in seen} == {host}
        signed = seen[0].headers["authorization"].split("SignedHeaders=")[1].split(",")[0]
        assert "host" in signed.split(";")

    def test_an_endpoint_with_a_user_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="must not carry a user"):
            self._store("https://key:secret@s3.example.test", [])

    @pytest.mark.parametrize(
        ("status", "body"),
        [(501, "<Code>NotImplemented</Code>"), (400, "<Code>NotImplemented</Code>")],
    )
    def test_a_store_without_sse_is_named_with_the_setting(self, status: int, body: str) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, text=body)

        store = S3BlobStore(
            endpoint="http://127.0.0.1:9000",
            bucket="runs",
            region="us-east-1",
            access_key=SecretStr("AK"),
            secret_key=SecretStr("SK"),
            allowlist={"127.0.0.1"},
            client=httpx.Client(transport=httpx.MockTransport(refuse)),
        )
        with pytest.raises(BlobStoreError, match="OPSEVAL_S3_SERVER_SIDE_ENCRYPTION=false"):
            store.put("k", b"x", media_type="x/y")


# --- reads never drop a run ----------------------------------------------------------------------


async def text_demo(store: FileStore, tmp_path: Path) -> SuiteResult:
    from opseval.demo import catalogue_dir
    from opseval.demo.refund_agent import run_agent

    cat = catalogue_dir()
    settings = Settings(
        _env_file=None, allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "reports")
    )
    async with run_agent() as url:
        options = RunOptions(target=url, profile=cat / "profile.yaml", store=store)
        return await launch(cat / "suites" / "smoke.yaml", options, settings)


class TestUnreadableRuns:
    async def test_a_broken_result_is_listed_never_dropped(self, tmp_path: Path) -> None:
        from opseval.analytics import FileAnalytics, UsageFilter
        from opseval.server import views

        store = FileStore(tmp_path / "reports")
        good = await text_demo(store, tmp_path)
        bad = await text_demo(store, tmp_path)
        (tmp_path / "reports" / bad.run_id / "result.json").write_text("{broken")
        finished = store.read_finished()
        assert [r.run_id for r in finished.results] == [good.run_id]
        assert [u.run_id for u in finished.unreadable] == [bad.run_id]
        rows = {r.id: r for r in views.list_runs(store)}
        assert rows[bad.run_id].state == "harness_error"
        assert (rows[bad.run_id].error or "").startswith("RUN_UNREADABLE")
        assert rows[bad.run_id].passed is None, "never drawn as zeros"
        detail = views.run_detail(store, bad.run_id)
        assert detail is not None and detail.exit_code == 3
        assert "RUN_UNREADABLE" in (detail.summary.error or "")
        cost = FileAnalytics(store).cost(UsageFilter())
        assert bad.run_id in cost.scope.unreadable_runs


# --- the console on a store that is down -----------------------------------------------------


class DownStore(FileStore):
    """Every read fails as the database would when it is down."""

    down = True

    def listing(self) -> list[Any]:
        if self.down:
            raise StorageError("the database did not answer within 60s")
        return super().listing()

    def has_result(self, run_id: str) -> bool:
        if self.down:
            raise StorageError("the database did not answer within 60s")
        return super().has_result(run_id)

    def save_error(self, run_id: str, error: dict[str, Any]) -> None:
        if self.down:
            raise StorageError("the database did not answer within 60s")
        super().save_error(run_id, error)


class TestConsoleOnAStoreThatIsDown:
    def _client(self, store: FileStore, tmp_path: Path) -> Any:
        from fastapi.testclient import TestClient

        from opseval.server import create_app

        settings = Settings(_env_file=None, reports_dir=str(tmp_path / "reports"))
        app = create_app(settings=settings, token="t", store=store)
        return app, TestClient(
            app, base_url="http://127.0.0.1:8000", headers={"Authorization": "Bearer t"}
        )

    def test_a_read_is_503_storage_unavailable(self, tmp_path: Path) -> None:
        _, client = self._client(DownStore(tmp_path / "reports"), tmp_path)
        for path in ("/api/v1/runs", "/api/v1/runs/r-1"):
            answer = client.get(path)
            assert answer.status_code == 503, path
            assert answer.json()["code"] == "STORAGE_UNAVAILABLE"

    def test_a_failure_the_store_could_not_take_is_shown_then_written(self, tmp_path: Path) -> None:
        from opseval.server import actions

        store = DownStore(tmp_path / "reports")
        app, client = self._client(store, tmp_path)
        ctx = app.state.ctx
        actions.record_failure(ctx, "r-lost", StorageError("the database is down"))
        assert "r-lost" in ctx.failures
        store.down = False  # the store answers again
        rows = {r["id"]: r for r in client.get("/api/v1/runs").json()}
        assert rows["r-lost"]["state"] == "harness_error"
        assert "STORAGE_UNAVAILABLE" in rows["r-lost"]["error"]
        assert ctx.failures == {} and store.has_error("r-lost"), "written once it could be"

    def test_while_still_down_the_run_is_a_harness_error_not_running(self, tmp_path: Path) -> None:
        from opseval.server import actions, views

        store = DownStore(tmp_path / "reports")
        app, client = self._client(store, tmp_path)
        ctx = app.state.ctx
        actions.record_failure(ctx, "r-lost", RuntimeError("boom"))
        rows = views.with_unrecorded([], ctx.unrecorded())
        assert [(r.id, r.state) for r in rows] == [("r-lost", "harness_error")]
        detail = client.get("/api/v1/runs/r-lost").json()
        assert detail["exitCode"] == 3 and "harness" in detail["gateSummary"]


# --- retention goes on past a run it cannot delete -----------------------------------------------


class TestRetentionGoesOn:
    async def test_one_failing_run_never_keeps_the_others(self, tmp_path: Path) -> None:
        from opseval.retention import apply, plan

        store = FileStore(tmp_path / "reports")
        first = await text_demo(store, tmp_path)
        second = await text_demo(store, tmp_path)

        class Flaky(FileStore):
            def delete_run(self, run_id: str) -> bool:
                if run_id == first.run_id:
                    raise BlobStoreError("the blob store at s3.example.test answered 503")
                return super().delete_run(run_id)

        flaky = Flaky(tmp_path / "reports")
        items = plan(flaky, older_than_days=1, now=datetime.now(UTC) + timedelta(days=3))
        outcome = apply(flaky, items)
        assert outcome.deleted == [second.run_id]
        assert [f[0] for f in outcome.failed] == [first.run_id]
        assert "503" in outcome.failed[0][1]


class TestServeClosesItsStore:
    @pytest.mark.parametrize("owned", [True, False])
    def test_the_store_is_closed_on_shutdown_only_when_it_is_the_servers(
        self, tmp_path: Path, owned: bool
    ) -> None:
        from fastapi.testclient import TestClient

        from opseval.server import create_app

        closed: list[bool] = []

        class Closing(FileStore):
            def close(self) -> None:
                closed.append(True)

        app = create_app(
            settings=Settings(_env_file=None, reports_dir=str(tmp_path / "reports")),
            token="t",
            store=Closing(tmp_path / "reports"),
            close_store=owned,
        )
        with TestClient(app, base_url="http://127.0.0.1:8000"):
            assert closed == []
        assert closed == ([True] if owned else [])
