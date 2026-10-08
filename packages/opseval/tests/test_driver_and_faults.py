# SPDX-License-Identifier: Apache-2.0
"""Driver behaviour against the reference agent, plus the fault-injection suite.

The fault-injection tests encode the product's core promise: a harness problem must
never be reported as an agent failure.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve

from opseval.demo.refund_agent import Conversation, run_agent
from opseval.driver import Driver
from opseval.llm import ChatClient
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord, TerminalState
from opseval.models.results import ScoreStatus
from opseval.models.scenario import Scenario
from opseval.scoring import ScoringEngine
from opseval.simulator import LLMSimulator, ScriptedSimulator, SimTurn, Simulator
from opseval.transport import HttpJsonTransport, PythonTransport, WebSocketTransport
from opseval.verdicts import attempt_verdict

from .conftest import make_scenario


async def drive(
    scenario: Scenario, profile: SutProfile, url: str, sim: Simulator | None = None, **kw: Any
) -> RunRecord:
    driver = Driver(
        scenario=scenario,
        profile=profile,
        transport=WebSocketTransport(url, profile),
        simulator=sim or ScriptedSimulator(scenario, 1),
        run_id="r",
        attempt_index=0,
        seed=1,
        **kw,
    )
    return await driver.run()


class SlowSimulator(ScriptedSimulator):
    """A scripted customer behind a slow (rate-limited, retrying) simulator LLM."""

    def __init__(self, scenario: Scenario, delay: float) -> None:
        super().__init__(scenario, 1)
        self.delay = delay

    async def opening(self) -> SimTurn:
        await asyncio.sleep(self.delay)
        return await super().opening()

    async def next_turn(self, transcript: list[tuple[str, str]]) -> SimTurn:
        await asyncio.sleep(self.delay)
        return await super().next_turn(transcript)


async def judged(record: RunRecord, scenario: Scenario, profile: SutProfile) -> bool | None:
    scores = await ScoringEngine().score(record, scenario, profile)
    return attempt_verdict(record, scores, "x").passed


class TestDriver:
    async def test_happy_path_stops_at_checkpoint(self, profile: SutProfile) -> None:
        s = make_scenario()
        async with run_agent() as url:
            rec = await drive(s, profile, url)
        assert rec.terminal_state is TerminalState.STOPPED_AT_CHECKPOINT
        assert [i.checkpoint for i in rec.interrupts] == ["proposal_approval", "supervisor_review"]
        assert rec.interrupts[-1].action is None  # stopped before deciding
        assert rec.session_id and rec.session_id.startswith("s-")
        assert await judged(rec, s, profile) is True

    async def test_commit_guard_blocks_irreversible_step(self, profile: SutProfile) -> None:
        s = make_scenario(
            conversation={
                "stop_at": "goal_reached",
                "checkpoint_policy": {
                    "proposal_approval": {"action": "approve"},
                    "supervisor_review": {"action": "approve"},
                },
            }
        )
        async with run_agent() as url:
            rec = await drive(s, profile, url, allow_commit=True)  # run flag alone is not enough
        assert rec.terminal_state is TerminalState.STOPPED_AT_CHECKPOINT
        assert "commit_guard" in rec.notes
        assert not any(f.payload.get("outcome") == "refund_issued" for f in rec.received())
        scores = await ScoringEngine().score(rec, s, profile)
        terminal = next(x for x in scores if x.scorer_id == "output.terminal_state")
        assert terminal.status is ScoreStatus.INFRA_ERROR and "COMMIT_GUARD" in (
            terminal.reason or ""
        )

    async def test_double_opt_in_commits(self, profile: SutProfile) -> None:
        s = make_scenario(
            conversation={
                "stop_at": "outcome_committed",
                "allow_commit": True,
                "checkpoint_policy": {
                    "proposal_approval": {"action": "approve"},
                    "supervisor_review": {"action": "approve"},
                },
            }
        )
        async with run_agent() as url:
            rec = await drive(s, profile, url, allow_commit=True)
        assert rec.terminal_state is TerminalState.OUTCOME_COMMITTED
        assert await judged(rec, s, profile) is True

    async def test_rejection_loop_guard(self, profile: SutProfile) -> None:
        s = make_scenario(
            conversation={
                "checkpoint_policy": {
                    "proposal_approval": {"action": "reject", "feedback": "no", "max_rejections": 2}
                },
            }
        )
        async with run_agent() as url:
            rec = await drive(s, profile, url)
        assert rec.terminal_state is TerminalState.MAX_TURNS_EXHAUSTED
        assert "rejection loop" in (rec.stop_reason or "")

    async def test_reject_via_chat_message(self, profile: SutProfile) -> None:
        s = make_scenario(
            conversation={
                "scripted_turns": ["Order 48213 arrived broken, I want a refund."],
                "checkpoint_policy": {
                    "proposal_approval": {
                        "action": "sequence",
                        "via": "user_message",
                        "expected_target_phase": "proposal",
                        "sequence": [
                            {"action": "reject", "feedback": "Refund to my card please."},
                            {"action": "approve"},
                        ],
                    }
                },
            }
        )
        s = s.model_copy(
            update={
                "scorers": [
                    *s.scorers,
                    *make_scenario(scorers=[{"id": "flow.rejection_routing"}]).scorers,
                ]
            }
        )  # type: ignore[misc]
        async with run_agent() as url:
            rec = await drive(s, profile, url)
        rejects = [i for i in rec.interrupts if i.action == "reject"]
        assert rejects and rejects[0].via == "user_message"
        assert await judged(rec, s, profile) is True

    async def test_secrets_are_redacted_in_records(self, profile: SutProfile) -> None:
        def agent_factory():  # type: ignore[no-untyped-def]
            conv = Conversation(bug=None)

            def handle(frame: dict[str, Any]) -> list[dict[str, Any]]:
                replies = (
                    conv.on_user_message(frame["text"])
                    if frame["type"] == "user_message"
                    else conv.on_resume(frame["checkpoint"], frame["action"], frame.get("feedback"))
                )
                return [
                    {**r, "data": {**r.get("data", {}), "api_key": "sk-live-123"}} for r in replies
                ]

            return handle

        s = make_scenario()
        rec = await Driver(
            scenario=s,
            profile=profile,
            transport=PythonTransport(agent_factory, profile),
            simulator=ScriptedSimulator(s, 1),
            run_id="r",
            attempt_index=0,
            seed=1,
        ).run()
        assert "sk-live-123" not in rec.model_dump_json()
        assert rec.terminal_state is TerminalState.STOPPED_AT_CHECKPOINT


class TestFaultInjection:
    """Every harness fault must end in an infra outcome — never in a failed attempt."""

    async def _assert_not_blamed(self, rec: RunRecord, s: Scenario, profile: SutProfile) -> None:
        assert rec.terminal_state.is_infra, rec.terminal_state
        assert rec.infra_errors
        assert await judged(rec, s, profile) is None  # invalidated, not failed

    async def test_sut_unreachable(self, profile: SutProfile) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        s = make_scenario()
        rec = await drive(s, profile, f"ws://127.0.0.1:{port}")
        await self._assert_not_blamed(rec, s, profile)

    async def test_connection_dropped_mid_conversation(self, profile: SutProfile) -> None:
        s = make_scenario()
        async with run_agent(bug="crash") as url:
            rec = await drive(s, profile, url)
        await self._assert_not_blamed(rec, s, profile)

    async def test_no_handshake(self, profile: SutProfile) -> None:
        async def silent(conn: ServerConnection) -> None:
            await asyncio.sleep(5)

        server = await serve(silent, "127.0.0.1", 0)
        port = next(iter(server.sockets)).getsockname()[1]
        s = make_scenario()
        try:
            driver = Driver(
                scenario=s,
                profile=profile,
                transport=WebSocketTransport(
                    f"ws://127.0.0.1:{port}", profile, handshake_timeout=0.3
                ),
                simulator=ScriptedSimulator(s, 1),
                run_id="r",
                attempt_index=0,
                seed=1,
            )
            rec = await driver.run()
        finally:
            server.close()
        await self._assert_not_blamed(rec, s, profile)

    @pytest.mark.parametrize(
        "llm_reply",
        [
            httpx.Response(503),
            httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": '{"wrong": "shape"}'}}]}),
        ],
        ids=["provider-outage", "invalid-json", "wrong-shape"],
    )
    async def test_simulator_llm_failures(
        self, profile: SutProfile, llm_reply: httpx.Response
    ) -> None:
        async def no_sleep(_: float) -> None:
            return None

        client = ChatClient(
            base_url="https://llm.example.com/v1",
            model="m",
            api_key=None,
            role="simulator",
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: llm_reply)),
            sleep=no_sleep,
            max_retries=1,
        )
        s = make_scenario(
            conversation={
                "scripted_turns": [],
                "flow": "Ask for a refund.",
                "checkpoint_policy": {},
            }
        )
        async with run_agent() as url:
            rec = await drive(s, profile, url, sim=LLMSimulator(s, 1, client))
        await self._assert_not_blamed(rec, s, profile)

    async def test_http_transport_connection_error(self, profile: SutProfile) -> None:
        def boom(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        s = make_scenario()
        transport = HttpJsonTransport(
            "http://127.0.0.1:1/chat",
            profile,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        )
        rec = await Driver(
            scenario=s,
            profile=profile,
            transport=transport,
            simulator=ScriptedSimulator(s, 1),
            run_id="r",
            attempt_index=0,
            seed=1,
        ).run()
        await self._assert_not_blamed(rec, s, profile)


class TestSimulatorTimeIsNotTheAgents:
    """I1: the attempt deadline measures the agent, not the harness's simulator."""

    async def test_a_slow_simulator_does_not_use_up_the_agents_deadline(
        self, profile: SutProfile
    ) -> None:
        """One simulator call alone takes longer than the whole attempt deadline."""
        s = make_scenario(execution={"attempt_timeout_seconds": 1.0})
        async with run_agent() as url:
            rec = await drive(s, profile, url, sim=SlowSimulator(s, 1.3))
        assert rec.terminal_state is TerminalState.STOPPED_AT_CHECKPOINT, rec.stop_reason
        assert await judged(rec, s, profile) is True

    async def test_a_simulator_that_never_answers_is_a_harness_failure(
        self, profile: SutProfile
    ) -> None:
        s = make_scenario()
        async with run_agent() as url:
            rec = await drive(
                s, profile, url, sim=SlowSimulator(s, 30), simulator_timeout_seconds=0.2
            )
        assert rec.terminal_state is TerminalState.HARNESS_FAILURE
        assert "OPSEVAL_SIMULATOR_TIMEOUT_SECONDS" in (rec.stop_reason or "")
        assert rec.infra_errors
        assert await judged(rec, s, profile) is None  # invalidated, not failed

    async def test_the_agents_own_time_still_counts_after_a_slow_simulator(
        self, profile: SutProfile
    ) -> None:
        """The deadline is paused for the simulator, not removed: a silent agent still
        runs out of time, and that is the agent's TIMEOUT."""

        async def mute(conn: ServerConnection) -> None:
            await conn.send(json.dumps({"type": "session_created", "session_id": "s-1"}))
            async for _ in conn:
                pass

        server = await serve(mute, "127.0.0.1", 0)
        port = next(iter(server.sockets)).getsockname()[1]
        s = make_scenario(execution={"attempt_timeout_seconds": 0.8, "turn_timeout_seconds": 30})
        try:
            started = asyncio.get_running_loop().time()
            rec = await drive(s, profile, f"ws://127.0.0.1:{port}", sim=SlowSimulator(s, 0.5))
            took = asyncio.get_running_loop().time() - started
        finally:
            server.close()
        assert rec.terminal_state is TerminalState.TIMEOUT
        assert rec.stop_reason == "attempt deadline exceeded"
        assert took >= 1.2, "the simulator's 0.5s was added back to the deadline"
        assert await judged(rec, s, profile) is False


class TestAgentFaultsCountAgainstTheAgent:
    """The converse promise: real agent failures are never laundered into 'not measured'."""

    async def test_silent_agent_is_a_timeout_failure(self, profile: SutProfile) -> None:
        async def mute(conn: ServerConnection) -> None:
            await conn.send(json.dumps({"type": "session_created", "session_id": "s-1"}))
            async for _ in conn:
                pass

        server = await serve(mute, "127.0.0.1", 0)
        port = next(iter(server.sockets)).getsockname()[1]
        s = make_scenario(execution={"turn_timeout_seconds": 0.5})
        try:
            rec = await drive(s, profile, f"ws://127.0.0.1:{port}")
        finally:
            server.close()
        assert rec.terminal_state is TerminalState.TIMEOUT
        assert await judged(rec, s, profile) is False

    async def test_agent_http_500_is_an_agent_error(self, profile: SutProfile) -> None:
        s = make_scenario(scorers=[{"id": "conversation.no_errors"}])
        transport = HttpJsonTransport(
            "http://agent.local/chat",
            profile,
            client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(500))
            ),
        )
        rec = await Driver(
            scenario=s,
            profile=profile,
            transport=transport,
            simulator=ScriptedSimulator(s, 1),
            run_id="r",
            attempt_index=0,
            seed=1,
        ).run()
        assert rec.terminal_state is TerminalState.ERROR_FRAME
        assert await judged(rec, s, profile) is False

    @pytest.mark.parametrize("bug", ["skip-approval", "forget-method"])
    async def test_injected_bugs_fail(self, profile: SutProfile, bug: str) -> None:
        s = make_scenario(
            conversation={
                "scripted_turns": ["My blender from order 48213 arrived cracked, I want a refund."],
                "checkpoint_policy": {
                    "proposal_approval": {
                        "action": "sequence",
                        "expected_target_phase": "proposal",
                        "sequence": [
                            {"action": "reject", "feedback": "Refund to my original card."},
                            {"action": "approve"},
                        ],
                    }
                },
            },
            expectations={
                "expected_tools": {"must_call": ["lookup_order"]},
                "invariants": ["no_autonomous_commit", "phase_order"],
            },
            scorers=[
                {"id": "output.terminal_state"},
                {
                    "id": "output.payload_equals",
                    "params": {
                        "checkpoint": "proposal_approval",
                        "path": "refund_proposal.method",
                        "equals": "original_card",
                    },
                },
            ],
        )
        async with run_agent(bug=bug) as url:  # type: ignore[arg-type]
            rec = await drive(s, profile, url)
        assert await judged(rec, s, profile) is False


class TestLLMPersona:
    """An LLM persona (mocked) drives the agent, decides at a checkpoint, and is judged."""

    @staticmethod
    def fake_llm(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        system = body["messages"][0]["content"]
        assert "Egyptian Arabic" in system  # the language pack reached the persona prompt
        if "asking for your approval" in prompt:
            reply: dict[str, Any] = (
                {"action": "reject", "feedback": "No store credit, refund to my original card."}
                if "store_credit" in prompt
                else {"action": "approve", "feedback": None}
            )
        else:
            reply = {"message": "الطلب 48213 وصل مكسور، عايز فلوسي", "goal_reached": False}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    async def test_llm_persona_rejects_then_approves(self, profile: SutProfile) -> None:
        client = ChatClient(
            base_url="https://llm.example.com/v1",
            model="sim",
            api_key=None,
            role="simulator",
            http=httpx.AsyncClient(transport=httpx.MockTransport(self.fake_llm)),
        )
        s = make_scenario(
            persona={"role": "customer from Cairo", "language": "ar", "dialect": "egyptian"},
            conversation={
                "scripted_turns": [],
                "flow": "Ask for a refund to the card.",
                "checkpoint_policy": {
                    "proposal_approval": {"action": "script", "expected_target_phase": "proposal"}
                },
            },
            scorers=[{"id": "output.terminal_state"}, {"id": "flow.rejection_routing"}],
        )
        async with run_agent() as url:
            rec = await drive(s, profile, url, sim=LLMSimulator(s, 1, client))
        assert [i.action for i in rec.interrupts] == ["reject", "approve", None]
        assert rec.interrupts[1].payload["refund_proposal"]["method"] == "original_card"
        assert await judged(rec, s, profile) is True
