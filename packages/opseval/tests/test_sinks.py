# SPDX-License-Identifier: Apache-2.0
"""Sinks: webhook and Langfuse score push (FR-INT-04, FR-INT-07).

Publishing happens after the verdict, so the rules are about not corrupting it:

- a sink can **never** change a status, a pass rate or an exit code, however it fails;
- but a failure is **reported** — in the result, the console and the gate summary.
  Telemetry may fail silently because nobody asked for it; a sink was asked for, and an
  operator believing a notification went out when it did not is the failure mode;
- a destination that is not allowlisted, or plain HTTP off loopback, is a configuration
  error raised *before* the run, not after it has something to publish;
- a verdict that was never measured is pushed as a category, not as a number. A 0 in a
  dashboard is indistinguishable from "the agent failed".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from opseval.config import Settings
from opseval.errors import AllowedHostError, ConfigError
from opseval.models.record import TerminalState
from opseval.models.results import (
    AttemptVerdict,
    Axis,
    Gate,
    ScoreResult,
    ScoreStatus,
    SuiteResult,
    TestVerdict,
    VerdictStatus,
)
from opseval.models.scenario import Criticality
from opseval.sinks import LangfuseScoreSink, Sink, WebhookSink, build_sinks, publish_all

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def attempt(index: int = 0, *, session: str | None = "sess-1") -> AttemptVerdict:
    return AttemptVerdict(
        attempt_index=index,
        passed=True,
        terminal_state=TerminalState.GOAL_REACHED,
        scores=[
            ScoreResult(
                scorer_id="output.terminal_state",
                axis=Axis.OUTPUT,
                status=ScoreStatus.PASSED,
                score=1.0,
                threshold=1.0,
                weight=1.0,
            )
        ],
        record_path=f"a{index}.json",
        session_id=session,
    )


def result(*tests: TestVerdict, exit_code: int = 0, gate: str = "pass", **kw: Any) -> SuiteResult:
    base: dict[str, Any] = {
        "opseval_version": "0.1.0",
        "run_id": "20261005T000000Z-abc123",
        "suite": "smoke",
        "target": "wss://agent.example.com/ws",
        "profile": "p",
        "started_at": NOW,
        "ended_at": NOW,
        "tests": list(tests)
        or [
            TestVerdict(
                test_id="t-001",
                name="a scenario",
                criticality=Criticality.CRITICAL,
                status=VerdictStatus.PASSED,
                pass_rate=1.0,
                attempts=[attempt()],
            )
        ],
        "gate": Gate(status=gate, summary="ok"),
        "exit_code": exit_code,
    }
    return SuiteResult(**{**base, **kw})


def verdict(status: VerdictStatus, *, session: str | None = "sess-1", **kw: Any) -> TestVerdict:
    return TestVerdict(
        test_id=kw.pop("test_id", "t-001"),
        name="a scenario",
        criticality=Criticality.CRITICAL,
        status=status,
        attempts=[attempt(session=session)],
        **kw,
    )


class Spy:
    """Records what was posted."""

    def __init__(self, status: int = 200) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.status = status

    def factory(self) -> httpx.AsyncClient:
        async def handler(request: httpx.Request) -> httpx.Response:
            import json

            self.calls.append((str(request.url), json.loads(request.content)))
            return httpx.Response(self.status, json={"ok": True})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Boom(Sink):
    name = "boom"

    async def publish(self, result: SuiteResult) -> str:
        raise RuntimeError("the collector exploded")


class Fine(Sink):
    name = "fine"

    async def publish(self, result: SuiteResult) -> str:
        return "sent"


# --- a sink can never corrupt the verdict ---------------------------------------------


class TestCannotCorruptTheVerdict:
    async def test_a_failing_sink_is_caught(self) -> None:
        outcomes = await publish_all([Boom()], result())
        assert outcomes[0].published is False
        assert "the collector exploded" in outcomes[0].detail

    async def test_a_failing_sink_leaves_the_result_untouched(self) -> None:
        res = result()
        before = res.model_dump_json()
        await publish_all([Boom()], res)
        assert res.model_dump_json() == before
        assert res.exit_code == 0
        assert res.tests[0].status is VerdictStatus.PASSED

    async def test_one_failure_does_not_stop_the_others(self) -> None:
        outcomes = await publish_all([Boom(), Fine()], result())
        assert [o.sink for o in outcomes] == ["boom", "fine"]
        assert [o.published for o in outcomes] == [False, True]

    async def test_no_sinks_is_no_outcomes(self) -> None:
        assert await publish_all([], result()) == []

    async def test_failures_are_logged_not_swallowed(self) -> None:
        """An operator must not believe a notification went out when it did not."""
        lines: list[str] = []
        await publish_all([Boom()], result(), log=lines.append)
        assert any("FAILED to publish" in line for line in lines)

    def test_failed_sinks_are_listed_on_the_result(self) -> None:
        from opseval.models.results import SinkOutcome

        res = result()
        res = res.model_copy(
            update={
                "sinks": [
                    SinkOutcome(sink="webhook", published=False, detail="boom"),
                    SinkOutcome(sink="fine", published=True, detail="sent"),
                ]
            }
        )
        assert res.failed_sinks == ["webhook"]

    def test_a_failure_is_named_in_the_gate_summary_and_console(self) -> None:
        from opseval.models.results import SinkOutcome
        from opseval.reporting import console_summary
        from opseval.reporting.markdown import markdown_summary

        res = result().model_copy(
            update={"sinks": [SinkOutcome(sink="webhook", published=False, detail="HTTP 500")]}
        )
        assert "Not published" in markdown_summary(res)
        assert "verdict above is unaffected" in markdown_summary(res)
        assert "NOT PUBLISHED" in console_summary(res)

    def test_a_clean_run_says_nothing_about_sinks(self) -> None:
        from opseval.reporting.markdown import markdown_summary

        assert "Not published" not in markdown_summary(result())


# --- the webhook -----------------------------------------------------------------------


class TestWebhook:
    async def test_it_posts_the_whole_result(self) -> None:
        spy = Spy()
        detail = await WebhookSink(
            "https://hooks.example.com/opseval", client_factory=spy.factory
        ).publish(result())
        url, body = spy.calls[0]
        assert url == "https://hooks.example.com/opseval"
        assert body["run_id"] == "20261005T000000Z-abc123"
        assert body["exit_code"] == 0
        assert "posted run" in detail

    async def test_a_4xx_is_a_failure(self) -> None:
        spy = Spy(status=404)
        with pytest.raises(RuntimeError, match="HTTP 404"):
            await WebhookSink("https://hooks.example.com/x", client_factory=spy.factory).publish(
                result()
            )

    async def test_a_connection_error_propagates_to_publish_all(self) -> None:
        def factory() -> httpx.AsyncClient:
            async def handler(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("refused")

            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        sink = WebhookSink("https://hooks.example.com/x", client_factory=factory)
        outcomes = await publish_all([sink], result())
        assert outcomes[0].published is False
        assert "ConnectError" in outcomes[0].detail

    async def test_custom_headers_are_sent(self) -> None:
        seen: list[str] = []

        def factory() -> httpx.AsyncClient:
            async def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request.headers.get("x-token", ""))
                return httpx.Response(200)

            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        await WebhookSink(
            "https://hooks.example.com/x", headers={"x-token": "abc"}, client_factory=factory
        ).publish(result())
        assert seen == ["abc"]


# --- Langfuse score push ---------------------------------------------------------------


class TestLangfuseScores:
    def sink(self, spy: Spy, **kw: Any) -> LangfuseScoreSink:
        return LangfuseScoreSink(
            "https://cloud.langfuse.com", "pk", "sk", client_factory=spy.factory, **kw
        )

    async def test_a_passed_verdict_pushes_one(self) -> None:
        spy = Spy()
        detail = await self.sink(spy).publish(result(verdict(VerdictStatus.PASSED)))
        _url, body = spy.calls[0]
        assert body["value"] == 1.0
        assert body["dataType"] == "NUMERIC"
        assert body["sessionId"] == "sess-1"
        assert "1 measured" in detail

    async def test_a_failed_verdict_pushes_zero(self) -> None:
        spy = Spy()
        await self.sink(spy).publish(result(verdict(VerdictStatus.FAILED)))
        assert spy.calls[0][1]["value"] == 0.0

    @pytest.mark.parametrize(
        "status",
        [
            VerdictStatus.INCONCLUSIVE,
            VerdictStatus.SKIPPED_PRECONDITION,
            VerdictStatus.BLOCKED,
        ],
    )
    async def test_an_unmeasured_verdict_is_not_pushed_as_a_number(
        self, status: VerdictStatus
    ) -> None:
        """A 0 in a dashboard is indistinguishable from "the agent failed"."""
        spy = Spy()
        detail = await self.sink(spy).publish(result(verdict(status)))
        body = spy.calls[0][1]
        assert "value" not in body
        assert body["dataType"] == "CATEGORICAL"
        assert body["stringValue"] == status.value
        assert "1 not judged" in detail

    async def test_the_comment_carries_the_verdict_and_reason(self) -> None:
        spy = Spy()
        await self.sink(spy).publish(
            result(verdict(VerdictStatus.FAILED, reason="rf-001 broke the policy"))
        )
        assert "broke the policy" in spy.calls[0][1]["comment"]

    async def test_metadata_links_back_to_the_run(self) -> None:
        spy = Spy()
        await self.sink(spy).publish(result(verdict(VerdictStatus.PASSED)))
        meta = spy.calls[0][1]["metadata"]
        assert meta["opseval_run_id"] == "20261005T000000Z-abc123"
        assert meta["test_id"] == "t-001"
        assert meta["criticality"] == "critical"

    async def test_the_score_name_is_configurable(self) -> None:
        spy = Spy()
        await self.sink(spy, score_name="release-gate").publish(
            result(verdict(VerdictStatus.PASSED))
        )
        assert spy.calls[0][1]["name"] == "release-gate"

    async def test_an_attempt_without_a_session_is_skipped(self) -> None:
        """Nothing to attach a score to, and inventing one would be worse."""
        spy = Spy()
        detail = await self.sink(spy).publish(result(verdict(VerdictStatus.PASSED, session=None)))
        assert spy.calls == []
        assert "no attempt recorded a session id" in detail

    async def test_an_http_error_is_a_failure(self) -> None:
        spy = Spy(status=401)
        with pytest.raises(RuntimeError, match="HTTP 401"):
            await self.sink(spy).publish(result(verdict(VerdictStatus.PASSED)))

    async def test_credentials_are_sent_as_basic_auth(self) -> None:
        seen: list[str] = []

        def factory() -> httpx.AsyncClient:
            async def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request.headers.get("authorization", ""))
                return httpx.Response(200)

            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        await LangfuseScoreSink(
            "https://cloud.langfuse.com", "pk", "sk", client_factory=factory
        ).publish(result(verdict(VerdictStatus.PASSED)))
        assert seen[0].startswith("Basic ")


# --- configuration is checked before the run ------------------------------------------


class TestBuildSinks:
    def test_nothing_configured_is_no_sinks(self) -> None:
        assert build_sinks(Settings()) == []

    def test_a_webhook_is_built_when_allowlisted(self) -> None:
        settings = Settings(
            webhook_url="https://hooks.example.com/x", allowed_hosts="hooks.example.com"
        )
        assert [s.name for s in build_sinks(settings)] == ["webhook"]

    def test_an_unallowlisted_webhook_is_refused(self) -> None:
        """Found before the run, not after it has something to publish."""
        with pytest.raises(AllowedHostError):
            build_sinks(Settings(webhook_url="https://hooks.example.com/x"))

    def test_plain_http_is_refused_off_loopback(self) -> None:
        with pytest.raises(AllowedHostError):
            build_sinks(
                Settings(
                    webhook_url="http://hooks.example.com/x", allowed_hosts="hooks.example.com"
                )
            )

    def test_loopback_http_is_allowed(self) -> None:
        settings = Settings(webhook_url="http://127.0.0.1:9000/x", allowed_hosts="127.0.0.1")
        assert [s.name for s in build_sinks(settings)] == ["webhook"]

    def test_langfuse_push_without_credentials_is_a_config_error(self) -> None:
        with pytest.raises(ConfigError, match="OPSEVAL_LANGFUSE_HOST"):
            build_sinks(Settings(langfuse_push_scores=True))

    def test_langfuse_push_is_built_when_configured(self) -> None:
        settings = Settings(
            langfuse_push_scores=True,
            langfuse_host="https://cloud.langfuse.com",
            langfuse_public_key="pk",
            langfuse_secret_key=SecretStr("sk"),
            allowed_hosts="cloud.langfuse.com",
        )
        assert [s.name for s in build_sinks(settings)] == ["langfuse-scores"]

    def test_both_can_be_configured_together(self) -> None:
        settings = Settings(
            webhook_url="https://hooks.example.com/x",
            langfuse_push_scores=True,
            langfuse_host="https://cloud.langfuse.com",
            langfuse_public_key="pk",
            langfuse_secret_key=SecretStr("sk"),
            allowed_hosts="hooks.example.com,cloud.langfuse.com",
        )
        assert [s.name for s in build_sinks(settings)] == ["webhook", "langfuse-scores"]


# --- third-party sinks over entry points ----------------------------------------------


class TestSinkPlugins:
    """Same fail-closed contract as scorers (ADR-0022): an operator who installed a
    publisher and got silence would reasonably assume it had published."""

    def discover(self, *entries: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        from opseval.plugins import load_sinks, reset_for_tests

        reset_for_tests()
        monkeypatch.setattr("opseval.plugins._iter_entry_points", lambda group: iter(entries))
        return load_sinks()

    def test_a_compliant_sink_is_discovered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from .test_plugins import FakeEntryPoint

        found = self.discover(FakeEntryPoint("fine", Fine), monkeypatch=monkeypatch)
        assert found == {"fine": Fine}

    def test_an_unimportable_sink_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from .test_plugins import FakeEntryPoint

        with pytest.raises(ConfigError, match="could not be imported"):
            self.discover(FakeEntryPoint("broken", ImportError("nope")), monkeypatch=monkeypatch)

    def test_a_non_sink_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from .test_plugins import FakeEntryPoint

        class NotASink:
            name = "nope"

        with pytest.raises(ConfigError, match="deriving from opseval.sinks.Sink"):
            self.discover(FakeEntryPoint("nope", NotASink), monkeypatch=monkeypatch)

    def test_the_default_name_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two sinks both called "sink" would be indistinguishable in the outcomes."""
        from .test_plugins import FakeEntryPoint

        class Unnamed(Sink):
            async def publish(self, result: SuiteResult) -> str:
                return "x"

        with pytest.raises(ConfigError, match="needs its own 'name'"):
            self.discover(FakeEntryPoint("unnamed", Unnamed), monkeypatch=monkeypatch)

    def test_a_synchronous_publish_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from .test_plugins import FakeEntryPoint

        class SyncPublish(Sink):
            name = "sync"

            def publish(self, result: SuiteResult) -> str:  # type: ignore[override]
                return "x"

        with pytest.raises(ConfigError, match="async def publish"):
            self.discover(FakeEntryPoint("sync", SyncPublish), monkeypatch=monkeypatch)
