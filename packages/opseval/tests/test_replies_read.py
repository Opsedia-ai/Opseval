# SPDX-License-Identifier: Apache-2.0
"""ADR-0042: Opseval must read what the agent said before anything can pass, and must
time the agent on the agent's own clock.

Both halves of the core rule, for a wrong profile mapping and for request/response
transports:

- a profile that reads nothing from the agent's replies is a harness problem: never a pass,
  and a run where every attempt was unreadable exits 3;
- an agent that answers with an empty message, an error or only a notification was read:
  its failure stays the agent's;
- the scenario's turn timeout bounds a request/response turn (not the transport's default),
  and a sent frame is stamped when sending began, so a tool the agent ran *after* an
  approval is never placed before it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from opseval.config import Settings
from opseval.driver import Driver
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord, TerminalState, ToolCall
from opseval.models.results import Axis, ScoreStatus, VerdictStatus
from opseval.models.scenario import Scenario
from opseval.runner import RunOptions, launch
from opseval.scaffold import scaffold
from opseval.scoring import ALWAYS_ON, REGISTRY, ScoringContext, ScoringEngine
from opseval.simulator import ScriptedSimulator
from opseval.transport import HttpJsonTransport, PythonTransport, Transport
from opseval.verdicts import EXIT_HARNESS, attempt_verdict

from .conftest import make_scenario

CHAT = SutProfile.model_validate(
    {"name": "chat", "kinds": {"message": "message"}, "phase_field": None}
)
"""A plain chatbot profile: replies are ``{"type": "message", "text": ...}``."""


def chat_profile(**update: Any) -> SutProfile:
    return SutProfile.model_validate({**CHAT.model_dump(), **update})


def chat_scenario(**overrides: Any) -> Scenario:
    base: dict[str, Any] = {
        "conversation": {
            "scripted_turns": ["What are your opening hours?"],
            "stop_at": "goal_reached",
            "checkpoint_policy": {},
        },
        "expectations": {},
        # The frame checks a chatbot scaffold ships: none of them reads the agent's words.
        "scorers": [
            {"id": "conversation.no_errors"},
            {"id": "output.terminal_state"},
            {"id": "conversation.turn_budget"},
        ],
    }
    base.update(overrides)
    return make_scenario(**base)


def agent_replying(*frames: Any) -> PythonTransport:
    def agent(_frame: dict[str, Any]) -> Any:
        return list(frames)

    return PythonTransport(lambda: agent, CHAT)


async def attempt(
    scenario: Scenario, transport: Transport, profile: SutProfile = CHAT
) -> RunRecord:
    return await Driver(
        scenario=scenario,
        profile=profile,
        transport=transport,
        simulator=ScriptedSimulator(scenario, 1),
        run_id="r",
        attempt_index=0,
        seed=1,
    ).run()


async def verdict(record: RunRecord, scenario: Scenario, profile: SutProfile = CHAT) -> Any:
    scores = await ScoringEngine().score(record, scenario, profile)
    return attempt_verdict(record, scores, "x"), {s.scorer_id: s for s in scores}


class TestAWrongMappingIsNeverAPass:
    async def test_unread_replies_invalidate_the_attempt(self) -> None:
        # The agent answers {"reply": ...}; the profile looks for "type" and "text".
        scenario = chat_scenario()
        record = await attempt(scenario, agent_replying({"reply": "We open at 9."}))
        assert record.terminal_state is TerminalState.GOAL_REACHED
        assert record.transcript() == [("customer", "What are your opening hours?")]
        result, scores = await verdict(record, scenario)
        # Every check the scenario lists passes on nothing...
        assert all(
            scores[s].status is ScoreStatus.PASSED
            for s in ("conversation.no_errors", "output.terminal_state", "conversation.turn_budget")
        )
        # ...so the always-on harness check is what keeps it from passing.
        read = scores["protocol.replies_read"]
        assert read.axis is Axis.HARNESS and read.status is ScoreStatus.FAILED
        assert read.details["reply_keys"] == ["reply"]
        assert result.passed is None
        assert "read nothing" in (result.invalid_reason or "")
        assert "'reply'" in (result.invalid_reason or "")

    async def test_replies_that_are_not_json_are_unread(self) -> None:
        scenario = chat_scenario()
        record = await attempt(scenario, agent_replying({"_raw": "<html>login</html>"}))
        result, scores = await verdict(record, scenario)
        assert scores["protocol.replies_read"].status is ScoreStatus.FAILED
        assert "not JSON" in scores["protocol.replies_read"].details["reason"]
        assert result.passed is None

    async def test_it_runs_even_when_the_scenario_lists_its_own_scorers(self) -> None:
        assert "protocol.replies_read" in ALWAYS_ON
        assert REGISTRY["protocol.replies_read"].axis is Axis.HARNESS
        scenario = chat_scenario(scorers=[{"id": "conversation.no_errors"}])
        record = await attempt(scenario, agent_replying({"reply": "hi"}))
        _result, scores = await verdict(record, scenario)
        assert set(scores) == {"conversation.no_errors", "protocol.replies_read"}

    async def test_listing_it_explicitly_does_not_score_it_twice(self) -> None:
        scenario = chat_scenario(scorers=[{"id": "protocol.replies_read"}])
        record = await attempt(scenario, agent_replying({"type": "message", "text": "hi"}))
        scores = await ScoringEngine().score(record, scenario, CHAT)
        assert [s.scorer_id for s in scores] == ["protocol.replies_read"]

    async def test_a_run_where_nothing_was_ever_read_exits_3(self, tmp_path: Path) -> None:
        scaffold(tmp_path / "cat")
        settings = Settings(allowed_hosts="agent.example.com", reports_dir=str(tmp_path / "r"))

        def agent(_frame: dict[str, Any]) -> Any:
            return {"answer": {"content": "We open at 9."}}

        options = RunOptions(
            target="https://agent.example.com/v1",
            transport="openai-chat",
            profile=tmp_path / "cat" / "profile.yaml",
            transport_factory=lambda p: PythonTransport(lambda: agent, p),
        )
        result = await launch(tmp_path / "cat" / "suites" / "smoke.yaml", options, settings)
        assert result.exit_code == EXIT_HARNESS
        assert all(t.status is VerdictStatus.INCONCLUSIVE for t in result.tests)
        assert not result.safe_to_release


class TestARealAgentFailureStaysTheAgents:
    async def test_an_empty_answer_was_read(self) -> None:
        # The text field is there and empty: the agent said nothing, which is its failure.
        scenario = chat_scenario()
        record = await attempt(scenario, agent_replying({"type": "message", "text": ""}))
        result, scores = await verdict(record, scenario)
        assert scores["protocol.replies_read"].status is ScoreStatus.PASSED
        assert result.passed is not None

    async def test_an_error_frame_is_read_and_fails_the_agent(self) -> None:
        profile = chat_profile(kinds={"message": "message", "error": "error"})
        scenario = chat_scenario()

        def agent(_frame: dict[str, Any]) -> Any:
            return {"type": "error", "code": "boom"}

        record = await attempt(scenario, PythonTransport(lambda: agent, profile), profile)
        assert record.terminal_state is TerminalState.ERROR_FRAME
        result, scores = await verdict(record, scenario, profile)
        assert scores["protocol.replies_read"].status is ScoreStatus.PASSED
        assert result.passed is False

    async def test_an_http_500_is_an_agent_error_not_an_unread_reply(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream down")

        tr = HttpJsonTransport(
            "https://agent.example.com/chat",
            CHAT,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        scenario = chat_scenario()
        record = await attempt(scenario, tr)
        result, scores = await verdict(record, scenario)
        assert scores["protocol.replies_read"].status is ScoreStatus.PASSED
        assert result.passed is False

    async def test_a_notification_counts_as_read(self) -> None:
        profile = chat_profile(kinds={"message": "message", "typing": "notification"})
        record = await attempt(
            chat_scenario(),
            PythonTransport(lambda: lambda _f: [{"type": "typing"}], profile),
            profile,
        )
        read = await REGISTRY["protocol.replies_read"]().score(
            record,
            chat_scenario().scorers[0],  # type: ignore[index]
            ScoringContext(scenario=chat_scenario(), profile=profile),
        )
        assert read.status is ScoreStatus.PASSED

    async def test_partly_unread_replies_still_pass_and_are_counted(self) -> None:
        scenario = chat_scenario()
        record = await attempt(
            scenario,
            agent_replying({"type": "message", "text": "We open at 9."}, {"debug": True}),
        )
        _result, scores = await verdict(record, scenario)
        read = scores["protocol.replies_read"]
        assert read.status is ScoreStatus.PASSED
        assert read.details == {"received": 2, "unread": 1}

    async def test_an_agent_that_never_answered_is_a_timeout(self) -> None:
        # Nothing arrived at all: that is the agent not answering (TIMEOUT), not a mapping.
        scenario = chat_scenario()
        record = await attempt(scenario, agent_replying())
        assert record.terminal_state is TerminalState.TIMEOUT
        result, scores = await verdict(record, scenario)
        assert scores["protocol.replies_read"].status is ScoreStatus.PASSED
        assert result.passed is False


class TestTheTurnIsTimedOnTheAgentsClock:
    async def test_the_scenario_turn_timeout_reaches_the_http_request(self) -> None:
        seen: list[dict[str, float]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions["timeout"])
            return httpx.Response(200, json={"type": "message", "text": "hi"})

        tr = HttpJsonTransport(
            "https://agent.example.com/chat",
            CHAT,
            request_timeout=120.0,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        scenario = chat_scenario(execution={"repeats": 1, "turn_timeout_seconds": 300})
        await attempt(scenario, tr)
        # It was the transport's 120 s whatever the scenario said; a 300 s agent timed out.
        assert seen and seen[0]["read"] == 300

    async def test_a_slow_answer_is_the_agents_timeout_and_the_question_is_kept(self) -> None:
        async def agent(_frame: dict[str, Any]) -> Any:
            await asyncio.sleep(1)
            return {"type": "message", "text": "late"}

        scenario = chat_scenario(execution={"repeats": 1, "turn_timeout_seconds": 0.05})
        record = await attempt(scenario, PythonTransport(lambda: agent, CHAT))
        assert record.terminal_state is TerminalState.TIMEOUT
        assert "0.1s" in (record.stop_reason or "") or "0.0s" in (record.stop_reason or "")
        assert record.transcript() == [("customer", "What are your opening hours?")]
        result, _ = await verdict(record, scenario)
        assert result.passed is False

    async def test_an_unanswered_probe_on_http_is_a_failed_probe_not_the_end(self) -> None:
        calls = 0

        async def agent(frame: dict[str, Any]) -> Any:
            nonlocal calls
            calls += 1
            if frame.get("type") == "ping":
                await asyncio.sleep(1)
            return {"type": "message", "text": "hello"}

        profile = chat_profile(
            probes=[{"name": "ping", "send": {"type": "ping"}, "timeout_seconds": 0.05}]
        )
        scenario = chat_scenario()
        record = await attempt(scenario, PythonTransport(lambda: agent, profile), profile)
        assert record.terminal_state is TerminalState.GOAL_REACHED
        assert len(record.probes) == 1 and not record.probes[0].passed
        assert not record.probes[0].answered
        assert record.probes[0].sent_seq == 0 and record.frames[0].payload == {"type": "ping"}
        assert calls == 2  # the conversation went on after the probe

    async def test_latency_is_the_round_trip(self) -> None:
        async def agent(_frame: dict[str, Any]) -> Any:
            await asyncio.sleep(0.05)
            return {"type": "message", "text": "hi"}

        record = await attempt(chat_scenario(), PythonTransport(lambda: agent, CHAT))
        reply = record.received()[0]
        assert reply.latency_ms is not None and reply.latency_ms >= 50


GATED = chat_profile(
    name="gated",
    kinds={"message": "message", "needs_human": "needs_human"},
    checkpoints={"approval": {}},
    tools={"pay": {}},
    traces={"clock_skew_ms": 0},
)


class TestAToolRunAfterAnApprovalIsPlacedAfterIt:
    async def test_with_traces_the_approval_is_stamped_when_it_was_sent(self) -> None:
        handled: list[datetime] = []

        async def agent(frame: dict[str, Any]) -> Any:
            if frame.get("type") == "resume":
                # The agent receives the approval, then runs the tool, then answers.
                handled.append(datetime.now(UTC))
                await asyncio.sleep(0.05)
                return {"type": "message", "text": "Paid."}
            return {"type": "needs_human", "checkpoint": "approval", "payload": {}}

        scenario = chat_scenario(
            conversation={
                "scripted_turns": ["Pay my bill."],
                "stop_at": "goal_reached",
                "checkpoint_policy": {"approval": {"action": "approve"}},
            },
            expectations={
                "expected_tools": {"forbidden_before": [{"tool": "pay", "checkpoint": "approval"}]}
            },
            scorers=[{"id": "tools.forbidden_not_called"}],
        )
        record = await attempt(scenario, PythonTransport(lambda: agent, GATED), GATED)
        assert record.interrupts and record.interrupts[0].action == "approve"
        traced = record.model_copy(
            update={
                "trace_coverage": "full",
                "tool_calls": (
                    ToolCall(name="pay", started_at=handled[0] + timedelta(milliseconds=1)),
                ),
            }
        )
        _result, scores = await verdict(traced, scenario, GATED)
        gate = scores["tools.forbidden_not_called"]
        # Stamped after the round trip, the approval sat 50 ms after the tool call it
        # caused, and the agent was failed for running `pay` before it was approved.
        assert gate.status is ScoreStatus.PASSED, gate.details


ORDERED = chat_profile(
    name="ordered",
    tools={"lookup": {}, "act": {}},
    traces={"clock_skew_ms": 2000},
)


async def ordering(*calls: tuple[str, float]) -> Any:
    """Score tools.ordering (lookup before act) on trace spans ``ms`` after a fixed start."""
    scenario = chat_scenario(
        expectations={"expected_tools": {"ordering": [{"before": "lookup", "after": "act"}]}},
        scorers=[{"id": "tools.ordering"}],
    )
    record = await attempt(scenario, agent_replying({"type": "message", "text": "ok"}), ORDERED)
    start = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    traced = record.model_copy(
        update={
            "trace_coverage": "full",
            "tool_calls": tuple(
                ToolCall(name=n, started_at=start + timedelta(milliseconds=ms)) for n, ms in calls
            ),
        }
    )
    _result, scores = await verdict(traced, scenario, ORDERED)
    return scores["tools.ordering"]


class TestTwoSpansOfTheAgentsTraceShareOneClock:
    async def test_a_quick_lookup_then_act_is_in_order(self) -> None:
        # 100 ms apart, well inside the 2 s window meant for Opseval's clock vs the agent's.
        score = await ordering(("lookup", 0), ("act", 100))
        assert score.status is ScoreStatus.PASSED, score.reason
        assert score.scorer_version == "2"

    async def test_acting_before_the_lookup_inside_the_window_is_a_failure(self) -> None:
        # The agent's own failure, hidden as "not measured" while the window applied.
        score = await ordering(("act", 0), ("lookup", 100))
        assert score.status is ScoreStatus.FAILED

    async def test_the_same_instant_cannot_be_ordered(self) -> None:
        score = await ordering(("lookup", 0), ("act", 0))
        assert score.status is ScoreStatus.INFRA_ERROR


@pytest.mark.parametrize("cls", ["protocol.replies_read"])
def test_documented_as_always_on(cls: str) -> None:
    assert REGISTRY[cls].description.startswith("Always on.")
