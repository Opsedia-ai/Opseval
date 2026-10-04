# SPDX-License-Identifier: Apache-2.0
"""Protocol probes (FR-RUN-07) and the PROBES state (FR-RUN-04).

Fault-injection view of the core rule:
- agent half: an agent that answers a raw protocol frame wrongly, or not at all, fails.
  A probe timeout is the agent not answering, so it is a failed probe rather than an
  invalidated attempt;
- harness half: a transport error during probing is the harness losing the session, so
  the attempt is *not judged*; and a profile that declares probes whose attempt died
  before they ran is `NOT_MEASURABLE`, never a pass.

Probes never abort the conversation: a protocol fault and whatever it causes downstream
are both worth seeing in one record.
"""

from __future__ import annotations

from typing import Any

import pytest

from opseval.driver import Driver
from opseval.models.profile import FrameKind, Probe, ProbeExpect, SutProfile
from opseval.models.record import RunRecord, TerminalState
from opseval.models.results import ScoreStatus
from opseval.models.scenario import ScorerSpec
from opseval.scoring.base import ScoringContext
from opseval.scoring.deterministic import ProbesPassed
from opseval.simulator import ScriptedSimulator
from opseval.transport import PythonTransport

from .conftest import make_scenario

PING = Probe(name="ping", send={"type": "ping"}, expect=ProbeExpect(fields={"type": "pong"}))


def profile(*probes: Probe, **kw: Any) -> SutProfile:
    base: dict[str, Any] = {
        "name": "p",
        "kinds": {
            "user_message": FrameKind.USER_MESSAGE,
            "message": FrameKind.MESSAGE,
            "pong": FrameKind.HANDSHAKE,
            "error": FrameKind.ERROR,
        },
        "session_id_field": None,
        "probes": list(probes),
    }
    return SutProfile.model_validate({**base, **kw})


def agent(replies: dict[str, Any] | None = None, *, silent_to: set[str] | None = None) -> Any:
    """An in-process agent that answers by frame type."""
    table = replies or {}
    quiet = silent_to or set()

    def factory() -> Any:
        def handle(frame: dict[str, Any]) -> Any:
            kind = frame.get("type")
            if kind in quiet:
                return None
            if kind in table:
                return table[kind]
            return {"type": "message", "text": "hello"}

        return handle

    return factory


async def drive(prof: SutProfile, factory: Any, **kw: Any) -> RunRecord:
    scenario = make_scenario(
        conversation={"scripted_turns": ["hi"], "stop_at": "goal_reached", "checkpoint_policy": {}},
        expectations={"expected_tools": {}},
        **kw,
    )
    return await Driver(
        scenario=scenario,
        profile=prof,
        transport=PythonTransport(factory, prof),
        simulator=ScriptedSimulator(scenario, 1),
        run_id="r",
        attempt_index=0,
        seed=1,
    ).run()


async def grade(record: RunRecord, prof: SutProfile) -> Any:
    ctx = ScoringContext(scenario=make_scenario(), profile=prof)
    return await ProbesPassed().score(record, ScorerSpec(id=ProbesPassed.id), ctx)


# --- the PROBES state runs before the conversation ------------------------------------


class TestProbesRunFirst:
    async def test_a_probe_is_sent_before_the_opening_message(self) -> None:
        prof = profile(PING)
        rec = await drive(prof, agent({"ping": {"type": "pong"}}))
        sent = [f.payload.get("type") for f in rec.frames if f.direction == "sent"]
        assert sent[0] == "ping"
        assert "user_message" in sent[1:]

    async def test_the_probe_frames_are_in_the_record(self) -> None:
        rec = await drive(profile(PING), agent({"ping": {"type": "pong"}}))
        result = rec.probes[0]
        assert result.sent_seq == 0
        assert result.received_seq == 1
        assert rec.frames[result.received_seq].payload == {"type": "pong"}

    async def test_no_probes_declared_sends_nothing_extra(self) -> None:
        rec = await drive(profile(), agent())
        assert rec.probes == ()
        assert rec.frames[0].payload.get("type") == "user_message"

    async def test_several_probes_run_in_order(self) -> None:
        prof = profile(
            PING,
            Probe(name="whoami", send={"type": "whoami"}, expect=ProbeExpect(has_fields=["id"])),
        )
        rec = await drive(
            prof, agent({"ping": {"type": "pong"}, "whoami": {"type": "message", "id": "a"}})
        )
        assert [p.name for p in rec.probes] == ["ping", "whoami"]
        assert all(p.passed for p in rec.probes)

    async def test_a_failing_probe_does_not_abort_the_conversation(self) -> None:
        """A protocol fault and what it causes are both worth seeing in one record."""
        rec = await drive(profile(PING), agent({"ping": {"type": "nope"}}))
        assert rec.probes[0].passed is False
        assert rec.terminal_state is TerminalState.GOAL_REACHED
        assert any(f.kind is FrameKind.USER_MESSAGE for f in rec.frames)


# --- matching --------------------------------------------------------------------------


class TestMatching:
    async def test_a_matching_field_passes(self) -> None:
        rec = await drive(profile(PING), agent({"ping": {"type": "pong"}}))
        assert rec.probes[0].passed is True
        assert rec.probes[0].reason is None

    async def test_a_wrong_value_fails_and_says_what_it_got(self) -> None:
        rec = await drive(profile(PING), agent({"ping": {"type": "nope"}}))
        assert rec.probes[0].passed is False
        assert "'nope'" in (rec.probes[0].reason or "")
        assert "'pong'" in (rec.probes[0].reason or "")

    async def test_a_missing_field_fails(self) -> None:
        rec = await drive(profile(PING), agent({"ping": {"other": 1}}))
        assert "missing field 'type'" in (rec.probes[0].reason or "")

    async def test_extra_fields_are_allowed(self) -> None:
        """A subset match: the agent may send more than the probe asks about."""
        rec = await drive(profile(PING), agent({"ping": {"type": "pong", "extra": True}}))
        assert rec.probes[0].passed is True

    async def test_has_fields_checks_presence_only(self) -> None:
        prof = profile(
            Probe(name="v", send={"type": "v"}, expect=ProbeExpect(has_fields=["version"]))
        )
        ok = await drive(prof, agent({"v": {"type": "message", "version": "9"}}))
        assert ok.probes[0].passed is True
        bad = await drive(prof, agent({"v": {"type": "message"}}))
        assert "missing field 'version'" in (bad.probes[0].reason or "")

    async def test_a_kind_expectation_uses_the_profiles_own_mapping(self) -> None:
        prof = profile(
            Probe(
                name="hand",
                send={"type": "hand"},
                expect=ProbeExpect(kind=FrameKind.HANDSHAKE),
            )
        )
        ok = await drive(prof, agent({"hand": {"type": "pong"}}))
        assert ok.probes[0].passed is True
        bad = await drive(prof, agent({"hand": {"type": "message"}}))
        assert "expected a handshake frame, got message" in (bad.probes[0].reason or "")

    async def test_an_empty_expectation_asserts_it_answered(self) -> None:
        prof = profile(Probe(name="any", send={"type": "any"}))
        rec = await drive(prof, agent({"any": {"literally": "anything"}}))
        assert rec.probes[0].passed is True

    async def test_an_error_reply_fails_even_an_empty_expectation(self) -> None:
        """Found against the real demo agent, which answers an unknown frame with
        `error: unknown_frame`. It has demonstrably not handled the probe, so passing
        would make the loosest expectation the most misleading one."""
        prof = profile(Probe(name="any", send={"type": "any"}))
        rec = await drive(prof, agent({"any": {"type": "error", "code": "unknown_frame"}}))
        assert rec.probes[0].passed is False
        assert "unknown_frame" in (rec.probes[0].reason or "")
        assert "expect.kind: error" in (rec.probes[0].reason or "")

    async def test_a_probe_may_expect_an_error_on_purpose(self) -> None:
        """Asserting the agent rejects a malformed frame is a real protocol test."""
        prof = profile(
            Probe(
                name="malformed",
                send={"type": "garbage"},
                expect=ProbeExpect(kind=FrameKind.ERROR, has_fields=["code"]),
            )
        )
        rec = await drive(prof, agent({"garbage": {"type": "error", "code": "bad_request"}}))
        assert rec.probes[0].passed is True

    async def test_a_non_object_answer_fails_a_field_expectation(self) -> None:
        rec = await drive(profile(PING), agent({"ping": [1, 2, 3]}))
        assert "expected an object with fields" in (rec.probes[0].reason or "")


# --- fault injection -------------------------------------------------------------------


class TestAgentFaults:
    async def test_no_answer_is_a_failed_probe_not_a_dead_attempt(self) -> None:
        """The agent failing to answer counts against it."""
        prof = profile(Probe(name="ping", send={"type": "ping"}, timeout_seconds=0.05))
        rec = await drive(prof, agent(silent_to={"ping"}))
        assert rec.probes[0].passed is False
        assert rec.probes[0].answered is False
        assert "no answer within" in (rec.probes[0].reason or "")
        assert rec.terminal_state is TerminalState.GOAL_REACHED, "the conversation still ran"

    async def test_an_unanswered_probe_records_what_was_sent(self) -> None:
        prof = profile(Probe(name="ping", send={"type": "ping"}, timeout_seconds=0.05))
        rec = await drive(prof, agent(silent_to={"ping"}))
        assert rec.probes[0].sent_seq == 0
        assert rec.probes[0].received_seq is None


class TestHarnessFaults:
    async def test_a_transport_error_invalidates_the_attempt(self) -> None:
        """Losing the session is never the agent's fault."""
        from opseval.errors import TransportError

        def factory() -> Any:
            def handle(frame: dict[str, Any]) -> Any:
                raise TransportError("socket died")

            return handle

        rec = await drive(profile(PING), factory)
        assert rec.terminal_state is TerminalState.TRANSPORT_FAILURE
        assert rec.probes == ()


# --- the scorer ------------------------------------------------------------------------


class TestScorer:
    async def test_all_passing_is_a_pass(self) -> None:
        prof = profile(PING)
        rec = await drive(prof, agent({"ping": {"type": "pong"}}))
        res = await grade(rec, prof)
        assert res.status is ScoreStatus.PASSED
        assert res.score == 1.0
        assert res.details["probes"] == 1

    async def test_a_failing_probe_fails_the_agent(self) -> None:
        prof = profile(PING)
        rec = await drive(prof, agent({"ping": {"type": "nope"}}))
        res = await grade(rec, prof)
        assert res.status is ScoreStatus.FAILED
        assert res.details["failed"][0]["probe"] == "ping"

    async def test_the_score_is_the_share_that_passed(self) -> None:
        prof = profile(
            PING, Probe(name="two", send={"type": "two"}, expect=ProbeExpect(fields={"ok": True}))
        )
        rec = await drive(prof, agent({"ping": {"type": "pong"}, "two": {"ok": False}}))
        res = await grade(rec, prof)
        assert res.score == pytest.approx(0.5, abs=1e-4)

    async def test_no_declared_probes_is_a_vacuous_pass(self) -> None:
        """Consistent with the other scorers, and now visible in every output."""
        prof = profile()
        rec = await drive(prof, agent())
        res = await grade(rec, prof)
        assert res.status is ScoreStatus.PASSED
        assert res.details["vacuous"] is True

    async def test_declared_but_never_run_is_not_measurable(self) -> None:
        """The attempt died before PROBES, which is the harness, not a wrong answer."""
        prof = profile(PING)
        dead = RunRecord(
            run_id="r",
            test_id="t-001",
            attempt_index=0,
            seed=1,
            target="x",
            started_at=__import__("datetime").datetime(
                2026, 1, 1, tzinfo=__import__("datetime").UTC
            ),
            ended_at=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC),
            terminal_state=TerminalState.TRANSPORT_FAILURE,
            stop_reason="socket died",
        )
        res = await grade(dead, prof)
        assert res.status is ScoreStatus.INFRA_ERROR
        assert res.score is None
        assert "before the probes ran" in (res.reason or "")
        assert res.details["declared"] == ["ping"]

    async def test_an_unanswered_probe_is_reported_as_unanswered(self) -> None:
        prof = profile(Probe(name="ping", send={"type": "ping"}, timeout_seconds=0.05))
        rec = await drive(prof, agent(silent_to={"ping"}))
        res = await grade(rec, prof)
        assert res.details["failed"][0]["answered"] is False
