# SPDX-License-Identifier: Apache-2.0
"""Langfuse trace source, trace enrichment and trace-backed tool scorers."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from opseval.catalogue import load_profile
from opseval.config import Settings
from opseval.demo.refund_agent import run_agent
from opseval.errors import ConfigError, TraceUnavailable
from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import FrameRecord, InterruptRecord, RunRecord, TerminalState, ToolCall
from opseval.models.results import ScoreStatus, VerdictStatus
from opseval.models.scenario import ScorerSpec
from opseval.runner import RunOptions, launch
from opseval.scoring import REGISTRY, ScoringContext
from opseval.traces import TraceSource, enrich, trace_session_id
from opseval.traces.langfuse import LangfuseTraceSource

from .conftest import make_scenario

T0 = datetime(2026, 9, 23, 10, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def traced_profile(profile: SutProfile, **traces: Any) -> SutProfile:
    data = profile.model_dump()
    data["traces"] = {"source": "langfuse", "wait_seconds": 0, **traces}
    data["tools"]["lookup_order"]["trace_name"] = "lookup-order"
    return SutProfile.model_validate(data)


def record(
    *,
    calls: list[ToolCall] | None = None,
    frames: list[FrameRecord] | None = None,
    interrupts: list[InterruptRecord] | None = None,
    session_id: str | None = "s-1",
    state: TerminalState = TerminalState.STOPPED_AT_CHECKPOINT,
) -> RunRecord:
    return RunRecord(
        run_id="r",
        test_id="t-001",
        attempt_index=0,
        seed=1,
        session_id=session_id,
        target="x",
        started_at=T0,
        ended_at=at(60),
        terminal_state=state,
        frames=tuple(frames or []),
        interrupts=tuple(interrupts or []),
        trace_coverage="full" if calls is not None else "frames_only",
        tool_calls=tuple(calls or []),
    )


def observation(
    name: str, start: float, *, type_: str = "SPAN", level: str = "DEFAULT"
) -> dict[str, Any]:
    return {
        "id": f"obs-{name}-{start}",
        "traceId": "t1",
        "type": type_,
        "name": name,
        "startTime": at(start).isoformat().replace("+00:00", "Z"),
        "endTime": at(start + 0.2).isoformat().replace("+00:00", "Z"),
        "level": level,
        "input": {"order": "10482"},
    }


class TestLangfuseSource:
    async def test_reads_pages_and_maps_tools(self, profile: SutProfile) -> None:
        prof = traced_profile(profile)
        pages = {
            None: {
                "data": [
                    observation("lookup-order", 1),
                    observation("llm-call", 2, type_="GENERATION"),
                    observation("send_email", 3, type_="TOOL"),
                ],
                "meta": {"cursor": "c2"},
            },
            "c2": {"data": [observation("create_refund", 4, level="ERROR")], "meta": {}},
        }
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json=pages[req.url.params.get("cursor")])

        src = LangfuseTraceSource(
            "https://cloud.langfuse.com",
            "pk",
            "sk",
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        calls = await src.fetch("s-1", record(), prof)
        assert [(c.name, c.ok) for c in calls] == [
            ("lookup-order", True),
            ("send_email", True),
            ("create_refund", False),
        ]
        first = seen[0]
        assert first.url.path == "/api/public/v2/observations"
        assert first.headers["authorization"] == "Basic " + base64.b64encode(b"pk:sk").decode()
        flt = json.loads(first.url.params["filter"])
        assert flt == [{"type": "string", "column": "sessionId", "operator": "=", "value": "s-1"}]
        assert first.url.params["fromStartTime"] == "2026-09-23T09:50:00Z"
        assert len(seen) == 2

    @pytest.mark.parametrize(
        ("response", "match"),
        [
            (httpx.Response(401), "rejected the API keys"),
            (httpx.Response(500), "HTTP 500"),
            (httpx.Response(200, json={"data": [], "meta": {}}), "no observations"),
            (httpx.Response(200, text="<html>"), "invalid JSON"),
            (httpx.Response(200, json={"data": "x"}), "unexpected"),
        ],
    )
    async def test_failures_are_trace_unavailable(
        self, profile: SutProfile, response: httpx.Response, match: str
    ) -> None:
        src = LangfuseTraceSource(
            "https://lf.example.com",
            "pk",
            "sk",
            client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: response)
            ),
        )
        with pytest.raises(TraceUnavailable, match=match):
            await src.fetch("s-1", record(), traced_profile(profile))


class FakeSource(TraceSource):
    name = "fake"

    def __init__(self, results: list[list[ToolCall] | Exception]) -> None:
        self.results = results
        self.calls = 0

    async def fetch(
        self, session_id: str, record: RunRecord, profile: SutProfile
    ) -> list[ToolCall]:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


class TestEnrich:
    async def test_polls_until_stable(self, profile: SutProfile) -> None:
        prof = traced_profile(profile, wait_seconds=10, poll_interval_seconds=2)
        one = [ToolCall(name="lookup-order", started_at=at(1))]
        two = [*one, ToolCall(name="check_eligibility", started_at=at(2))]
        src = FakeSource([TraceUnavailable("not yet"), one, two, two])
        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            sleeps.append(s)

        rec = await enrich(record(), src, prof, sleep=fake_sleep)
        assert rec.trace_coverage == "full" and len(rec.tool_calls) == 2
        assert sleeps == [2, 2, 2]

    async def test_gives_up_with_a_note(self, profile: SutProfile) -> None:
        prof = traced_profile(profile, wait_seconds=4, poll_interval_seconds=2)

        async def no_sleep(_: float) -> None:
            return None

        rec = await enrich(record(), FakeSource([TraceUnavailable("down")]), prof, sleep=no_sleep)
        assert rec.trace_coverage == "frames_only"
        assert rec.notes == ("trace_unavailable: fake: down",)

    async def test_no_session_id(self, profile: SutProfile) -> None:
        rec = await enrich(record(session_id=None), FakeSource([[]]), traced_profile(profile))
        assert rec.trace_coverage == "frames_only" and "no session id" in rec.notes[0]

    async def test_infra_attempts_are_left_alone(self, profile: SutProfile) -> None:
        src = FakeSource([[]])
        rec = record(state=TerminalState.TRANSPORT_FAILURE)
        assert await enrich(rec, src, traced_profile(profile)) is rec and src.calls == 0

    def test_session_template(self, profile: SutProfile) -> None:
        cfg = traced_profile(profile, session_id="{test_id}-{attempt_index}").traces
        assert cfg is not None and trace_session_id(cfg, record()) == "t-001-0"


def approval_frames() -> tuple[list[FrameRecord], list[InterruptRecord]]:
    frames = [
        FrameRecord(seq=0, at=at(5), direction="received", kind=FrameKind.NEEDS_HUMAN, payload={}),
        FrameRecord(seq=1, at=at(10), direction="sent", kind=FrameKind.RESUME, payload={}),
    ]
    itr = InterruptRecord(
        seq=0, checkpoint="supervisor_review", occurrence=1, action="approve", decision_seq=1
    )
    return frames, [itr]


async def score(sid: str, rec: RunRecord, prof: SutProfile, **expectations: Any) -> Any:
    s = make_scenario(expectations={"expected_tools": expectations} if expectations else {})
    return await REGISTRY[sid]().score(rec, ScorerSpec(id=sid), ScoringContext(s, prof))


class TestTraceBackedScorers:
    @pytest.mark.parametrize(
        ("started", "status"),
        [(1.0, ScoreStatus.FAILED), (9.0, ScoreStatus.INFRA_ERROR), (13.0, ScoreStatus.PASSED)],
        ids=["clearly-before", "inside-skew", "clearly-after"],
    )
    async def test_commit_tool_vs_approval(
        self, profile: SutProfile, started: float, status: ScoreStatus
    ) -> None:
        frames, itrs = approval_frames()
        rec = record(
            calls=[ToolCall(name="create_refund", started_at=at(started))],
            frames=frames,
            interrupts=itrs,
        )
        res = await score("tools.forbidden_not_called", rec, traced_profile(profile))
        assert res.status is status, res
        if status is ScoreStatus.INFRA_ERROR:
            assert "CLOCK_SKEW_AMBIGUOUS" in (res.reason or "")
        else:
            assert not res.degraded

    async def test_expected_called_uses_trace_names(self, profile: SutProfile) -> None:
        rec = record(calls=[ToolCall(name="lookup-order", started_at=at(1))])
        res = await score(
            "tools.expected_called",
            rec,
            traced_profile(profile),
            must_call=["lookup_order", "check_eligibility"],
        )
        assert res.status is ScoreStatus.FAILED and not res.degraded
        assert res.details["missing"] == ["check_eligibility"]
        assert res.details["source"] == "trace"

    async def test_ordering(self, profile: SutProfile) -> None:
        prof = traced_profile(profile)
        good = record(
            calls=[
                ToolCall(name="lookup-order", started_at=at(1)),
                ToolCall(name="check_eligibility", started_at=at(5)),
            ]
        )
        bad = record(calls=[ToolCall(name="check_eligibility", started_at=at(1))])
        rule = {"ordering": [{"before": "lookup_order", "after": "check_eligibility"}]}
        assert (await score("tools.ordering", good, prof, **rule)).status is ScoreStatus.PASSED
        assert (await score("tools.ordering", bad, prof, **rule)).status is ScoreStatus.FAILED
        assert (await score("tools.ordering", good, prof)).details["vacuous"] is True

    async def test_call_success(self, profile: SutProfile) -> None:
        prof = traced_profile(profile)
        rec = record(
            calls=[
                ToolCall(name="lookup-order", started_at=at(1)),
                ToolCall(name="check_eligibility", started_at=at(2), ok=False),
                ToolCall(name="mystery_tool", started_at=at(3)),
            ]
        )
        res = await score("tools.call_success", rec, prof)
        assert res.status is ScoreStatus.FAILED and res.score == 0.5
        assert res.details["undeclared_trace_calls"] == ["mystery_tool"]
        frames_only = await score("tools.call_success", record(), prof)
        assert frames_only.status is ScoreStatus.INFRA_ERROR

    async def test_frames_ordering_same_frame_is_ambiguous(self, profile: SutProfile) -> None:
        frame = FrameRecord(
            seq=3,
            at=at(1),
            direction="received",
            kind=FrameKind.MESSAGE,
            payload={"data": {"order_summary": {}, "eligibility": {}}},
        )
        rule = {"ordering": [{"before": "lookup_order", "after": "check_eligibility"}]}
        res = await score("tools.ordering", record(frames=[frame]), profile, **rule)
        assert res.status is ScoreStatus.INFRA_ERROR and res.details["source"] == "frames"


class TestRunnerWithTraces:
    async def test_trace_backed_run_against_demo_agent(
        self, cat_dir: Path, profile: SutProfile, tmp_path: Path
    ) -> None:
        prof_path = tmp_path / "profile.json"
        prof_path.write_text(traced_profile(profile).model_dump_json(), encoding="utf-8")

        class DemoTrace(TraceSource):
            name = "demo"

            async def fetch(
                self, session_id: str, record: RunRecord, profile: SutProfile
            ) -> list[ToolCall]:
                assert session_id.startswith("s-")
                return [
                    ToolCall(name="lookup-order", started_at=record.started_at),
                    ToolCall(name="check_eligibility", started_at=record.started_at),
                ]

        settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "r"))
        async with run_agent() as url:
            result = await launch(
                cat_dir / "scenarios" / "rf-001-happy-path.yaml",
                RunOptions(target=url, profile=prof_path, repeats=1, trace_source=DemoTrace()),
                settings,
            )
        verdict = result.tests[0]
        assert verdict.status is VerdictStatus.PASSED, verdict
        tool_scores = [s for s in verdict.attempts[0].scores if s.scorer_id.startswith("tools.")]
        assert tool_scores and not any(s.degraded for s in tool_scores)
        html = (Path(settings.reports_dir) / result.run_id / "report.html").read_text("utf-8")
        assert "trace-backed" in html

    def test_missing_langfuse_settings_is_a_config_error(
        self, cat_dir: Path, profile: SutProfile, tmp_path: Path
    ) -> None:
        import asyncio

        prof_path = tmp_path / "profile.json"
        prof_path.write_text(traced_profile(profile).model_dump_json(), encoding="utf-8")
        settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "r"))
        options = RunOptions(target="ws://127.0.0.1:9", profile=prof_path)
        with pytest.raises(ConfigError, match="OPSEVAL_LANGFUSE_HOST"):
            asyncio.run(launch(cat_dir / "suites" / "smoke.yaml", options, settings))
        assert load_profile(prof_path).traces is not None
