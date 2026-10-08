# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
import pytest

from opseval.llm import ChatClient
from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import FrameRecord, InterruptRecord, RunRecord, TerminalState
from opseval.models.results import (
    AttemptVerdict,
    Axis,
    ScoreResult,
    ScoreStatus,
    TestVerdict,
    VerdictStatus,
)
from opseval.models.scenario import Criticality, ScorerSpec
from opseval.scoring import REGISTRY, Scorer, ScoringContext, ScoringEngine, effective_specs
from opseval.verdicts import (
    EXIT_FAIL,
    EXIT_HARNESS,
    EXIT_NOT_JUDGED,
    EXIT_PASS,
    attempt_verdict,
    exit_code,
    fold_attempts,
    release_gate,
)

from .conftest import make_scenario

NOW = datetime.now(UTC)


def frame(
    seq: int,
    direction: str,
    kind: FrameKind,
    payload: Any = None,
    phase: str | None = None,
    text: str | None = None,
) -> FrameRecord:
    return FrameRecord(
        seq=seq,
        at=NOW,
        direction=direction,
        kind=kind,
        payload=payload or {},  # type: ignore[arg-type]
        phase=phase,
        text=text,
    )


def record(
    frames: list[FrameRecord],
    interrupts: list[InterruptRecord] | None = None,
    state: TerminalState = TerminalState.STOPPED_AT_CHECKPOINT,
    **kw: Any,
) -> RunRecord:
    return RunRecord(
        run_id="r",
        test_id="t-001",
        attempt_index=kw.pop("attempt_index", 0),
        seed=1,
        target="x",
        started_at=NOW,
        ended_at=NOW,
        terminal_state=state,
        frames=tuple(frames),
        interrupts=tuple(interrupts or []),
        **kw,
    )


def score(sid: str, status: ScoreStatus, axis: Axis = Axis.OUTPUT) -> ScoreResult:
    value = (
        None
        if status is ScoreStatus.INFRA_ERROR
        else (1.0 if status is ScoreStatus.PASSED else 0.0)
    )
    return ScoreResult(
        scorer_id=sid,
        axis=axis,
        status=status,
        score=value,
        threshold=1.0,
        reason="x" if value is None else None,
    )


class TestDeterministicScorers:
    async def run(self, sid: str, rec: RunRecord, profile: SutProfile, **spec: Any) -> ScoreResult:
        s = make_scenario()
        return await REGISTRY[sid]().score(
            rec, ScorerSpec(id=sid, **spec), ScoringContext(s, profile)
        )

    async def test_phase_order_detects_skip(self, profile: SutProfile) -> None:
        rec = record(
            [
                frame(0, "received", FrameKind.MESSAGE, phase="lookup"),
                frame(1, "received", FrameKind.RESULT, phase="confirmation"),
            ]
        )
        res = await self.run("flow.phase_order", rec, profile)
        assert res.status is ScoreStatus.FAILED and "skipped" in res.details["problems"][0]
        assert (
            await self.run("flow.phase_order", rec, profile, params={"allow_gaps": True})
        ).status is ScoreStatus.PASSED

    async def test_phase_order_unmeasurable_is_infra(self, profile: SutProfile) -> None:
        res = await self.run("flow.phase_order", record([]), profile)
        assert res.status is ScoreStatus.INFRA_ERROR and res.score is None

    async def test_rejection_routing(self, profile: SutProfile) -> None:
        itr = InterruptRecord(
            seq=0,
            checkpoint="proposal_approval",
            occurrence=1,
            action="reject",
            expected_target_phase="proposal",
            decision_seq=1,
        )
        good = record([frame(2, "received", FrameKind.MESSAGE, phase="proposal")], [itr])
        bad = record([frame(2, "received", FrameKind.MESSAGE, phase="lookup")], [itr])
        assert (
            await self.run("flow.rejection_routing", good, profile)
        ).status is ScoreStatus.PASSED
        assert (await self.run("flow.rejection_routing", bad, profile)).status is ScoreStatus.FAILED

    async def test_forbidden_before_uses_approval_order(self, profile: SutProfile) -> None:
        refund = frame(3, "received", FrameKind.RESULT, {"data": {"refund_status": {}}})
        early = record(
            [refund],
            [
                InterruptRecord(
                    seq=5,
                    checkpoint="supervisor_review",
                    occurrence=1,
                    action="approve",
                    decision_seq=6,
                )
            ],
        )
        late = record(
            [frame(7, "received", FrameKind.RESULT, {"data": {"refund_status": {}}})],
            [
                InterruptRecord(
                    seq=5,
                    checkpoint="supervisor_review",
                    occurrence=1,
                    action="approve",
                    decision_seq=6,
                )
            ],
        )
        assert (
            await self.run("tools.forbidden_not_called", early, profile)
        ).status is ScoreStatus.FAILED
        assert (
            await self.run("tools.forbidden_not_called", late, profile)
        ).status is ScoreStatus.PASSED

    async def test_expected_called_is_degraded(self, profile: SutProfile) -> None:
        rec = record([frame(1, "received", FrameKind.MESSAGE, {"data": {"order_summary": {}}})])
        res = await self.run("tools.expected_called", rec, profile)
        assert (
            res.degraded
            and res.status is ScoreStatus.FAILED
            and res.details["missing"] == ["check_eligibility"]
        )

    async def test_language_script(self, profile: SutProfile) -> None:
        rec = record(
            [
                frame(1, "received", FrameKind.MESSAGE, text="تمام، هعدّل العرض"),
                frame(2, "received", FrameKind.MESSAGE, text="Okay"),
            ]
        )
        res = await self.run(
            "output.language_script", rec, profile, params={"script": "arabic", "min_fraction": 0.9}
        )
        assert res.status is ScoreStatus.FAILED and res.score == 0.5

    async def test_bad_params_are_infra(self, profile: SutProfile) -> None:
        res = await self.run("output.payload_equals", record([]), profile, params={"path": "x"})
        assert res.status is ScoreStatus.INFRA_ERROR and "BAD_PARAMS" in (res.reason or "")


class TestEngine:
    async def test_scorer_exception_becomes_infra_error(self, profile: SutProfile) -> None:
        class Boom(Scorer):
            id: ClassVar[str] = "test.boom"
            axis: ClassVar[Axis] = Axis.FLOW

            async def score(
                self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext
            ) -> ScoreResult:
                raise RuntimeError("scorer bug")

        REGISTRY[Boom.id] = Boom
        try:
            s = make_scenario(scorers=[{"id": "test.boom"}, {"id": "conversation.no_errors"}])
            results = await ScoringEngine().score(record([]), s, profile)
        finally:
            del REGISTRY[Boom.id]
        assert results[0].status is ScoreStatus.INFRA_ERROR and "scorer bug" in (
            results[0].reason or ""
        )
        assert results[1].status is ScoreStatus.PASSED

    def test_default_pack_and_invariants(self) -> None:
        s = make_scenario(
            scorers="default", expectations={"invariants": ["no_errors", "phase_order"]}
        )
        ids = [x.id for x in effective_specs(s)]
        assert ids == [
            "conversation.no_errors",
            "flow.phase_order",
            "output.terminal_state",
            "tools.forbidden_not_called",
        ]


def judge_client(content: str) -> ChatClient:
    body = {"choices": [{"message": {"content": content}}]}
    return ChatClient(
        base_url="https://j.example.com/v1",
        model="judge-m",
        api_key=None,
        role="judge",
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
        ),
    )


class TestJudge:
    REC = record(
        [
            frame(0, "sent", FrameKind.USER_MESSAGE, text="I want my money back on my card"),
            frame(
                1,
                "received",
                FrameKind.MESSAGE,
                text="I can refund 89.99 GBP to your original card.",
            ),
        ]
    )

    def ballot(
        self, quote: str = "refund 89.99 GBP to your original card", grade: float = 1
    ) -> str:
        crit = {
            c: {"grade": grade, "quote": quote}
            for c in ("request_understood", "resolution_matches", "consistent", "complete")
        }
        crit["resolution_matches"] = {"grade": 0.5, "quote": "I want my money back"}
        import json

        return json.dumps({"criteria": crit})

    async def test_harness_computes_weighted_score(self, profile: SutProfile) -> None:
        s = make_scenario(scorers=[{"id": "judge.goal_achievement", "threshold": 0.8}])
        res = (await ScoringEngine(judge=judge_client(self.ballot())).score(self.REC, s, profile))[
            0
        ]
        # weights 1,2,1,1 → (1 + 0.5*2 + 1 + 1) / 5 = 0.8
        assert res.score == 0.8 and res.status is ScoreStatus.PASSED
        assert res.details["prompt_hash"] and res.details["model"] == "judge-m"

    async def test_invented_quote_invalidates_ballot(self, profile: SutProfile) -> None:
        s = make_scenario(scorers=[{"id": "judge.goal_achievement"}])
        res = (
            await ScoringEngine(judge=judge_client(self.ballot(quote="words never said"))).score(
                self.REC, s, profile
            )
        )[0]
        assert res.status is ScoreStatus.INFRA_ERROR and "QUOTE" in (res.reason or "")

    async def test_missing_judge_is_infra(self, profile: SutProfile) -> None:
        s = make_scenario(scorers=[{"id": "judge.goal_achievement"}])
        res = (await ScoringEngine().score(self.REC, s, profile))[0]
        assert res.status is ScoreStatus.INFRA_ERROR and "JUDGE_UNAVAILABLE" in (res.reason or "")


def av(
    passed: bool | None,
    state: TerminalState = TerminalState.STOPPED_AT_CHECKPOINT,
    scores: list[ScoreResult] | None = None,
) -> AttemptVerdict:
    return AttemptVerdict(
        attempt_index=0,
        passed=passed,
        terminal_state=state,
        scores=scores or [],
        record_path="x",
        invalid_reason=None if passed is not None else "infra",
    )


class TestVerdicts:
    def test_attempt_invalidation_rules(self) -> None:
        rec_ok = record([])
        assert attempt_verdict(rec_ok, [score("a", ScoreStatus.PASSED)], "x").passed is True
        assert (
            attempt_verdict(
                rec_ok, [score("a", ScoreStatus.FAILED), score("b", ScoreStatus.INFRA_ERROR)], "x"
            ).passed
            is False
        )
        assert attempt_verdict(rec_ok, [score("a", ScoreStatus.INFRA_ERROR)], "x").passed is None
        only_harness = [score("judge.persona_consistency", ScoreStatus.PASSED, Axis.HARNESS)]
        assert attempt_verdict(rec_ok, only_harness, "x").passed is None
        rec_infra = record([], state=TerminalState.TRANSPORT_FAILURE, stop_reason="down")
        assert attempt_verdict(rec_infra, [score("a", ScoreStatus.FAILED)], "x").passed is None

    @pytest.mark.parametrize(
        ("results", "status", "flaky"),
        [
            ([True, True, True], VerdictStatus.PASSED, False),
            ([True, False, True], VerdictStatus.FAILED, True),
            ([True, None, None], VerdictStatus.INCONCLUSIVE, False),
            ([None, True, False], VerdictStatus.FAILED, True),
        ],
    )
    def test_fold(self, results: list[bool | None], status: VerdictStatus, flaky: bool) -> None:
        s = make_scenario(execution={"repeats": 3})
        v = fold_attempts(s, [av(r) for r in results])
        assert v.status is status and v.flaky is flaky

    def test_pass_hat_k(self) -> None:
        s = make_scenario(execution={"repeats": 4, "consistency_threshold": 0.5})
        v = fold_attempts(s, [av(True), av(True), av(True), av(False)])
        assert v.status is VerdictStatus.PASSED and v.pass_rate == 0.75 and v.pass_hat_k == 0.0

    def tv(
        self,
        status: VerdictStatus,
        crit: Criticality = Criticality.NORMAL,
        attempts: list[AttemptVerdict] | None = None,
    ) -> TestVerdict:
        return TestVerdict(
            test_id=f"t-{status.value}-{crit.value}",
            name="n",
            criticality=crit,
            status=status,
            attempts=attempts or [],
        )

    def test_gate_is_criticality_aware(self) -> None:
        gate = release_gate(
            [self.tv(VerdictStatus.PASSED, Criticality.CRITICAL), self.tv(VerdictStatus.FAILED)]
        )
        assert gate.status == "pass" and gate.warnings
        gate = release_gate([self.tv(VerdictStatus.FAILED, Criticality.CRITICAL)])
        assert gate.status == "block"
        gate = release_gate([self.tv(VerdictStatus.FAILED)])  # no criticality declared anywhere
        assert gate.status == "block"
        assert release_gate([self.tv(VerdictStatus.INCONCLUSIVE)]).status == "block"
        assert release_gate([]).status == "block"

    def test_gate_never_says_safe_about_a_suite_that_judged_nothing(self) -> None:
        # Every scenario skipped (judge down) or quarantined: nothing was proven.
        S, B = VerdictStatus.SKIPPED_PRECONDITION, VerdictStatus.BLOCKED
        gate = release_gate([self.tv(S), self.tv(B)])
        assert gate.status == "block"
        assert "no scenario was judged" in gate.blocking
        assert "Safe to release" not in gate.summary

    def test_safe_to_release_only_when_everything_passed(self) -> None:
        P, F = VerdictStatus.PASSED, VerdictStatus.FAILED
        assert release_gate([self.tv(P)]).summary.endswith("Safe to release.")
        # A normal failure is a warning, so the gate passes, but it is not "safe".
        gate = release_gate([self.tv(P, Criticality.CRITICAL), self.tv(F)])
        assert gate.status == "pass"
        assert "Safe to release" not in gate.summary
        assert "No blocking issues, but not everything passed" in gate.summary
        assert "t-failed-normal: failed" in gate.summary

    def test_a_skip_is_a_named_gap_not_a_block(self) -> None:
        """A skip may be the operator's choice (the commit guard), not an outage, and the
        gate cannot tell them apart yet: blocking would push teams to --allow-commit."""
        P, S = VerdictStatus.PASSED, VerdictStatus.SKIPPED_PRECONDITION
        gate = release_gate([self.tv(P, Criticality.CRITICAL), self.tv(S, Criticality.BLOCKER)])
        assert gate.status == "pass"
        assert any("skipped_precondition" in w for w in gate.warnings)
        assert "Safe to release" not in gate.summary
        assert "t-skipped_precondition-blocker: skipped_precondition" in gate.summary

    def test_unmeasured_checks_and_load_errors_are_gaps(self) -> None:
        unmeasured = AttemptVerdict(
            attempt_index=0,
            passed=True,
            terminal_state=TerminalState.GOAL_REACHED,
            scores=[score("security.x", ScoreStatus.INFRA_ERROR, Axis.SECURITY)],
            record_path="a0.json",
        )
        gate = release_gate([self.tv(VerdictStatus.PASSED, attempts=[unmeasured])])
        assert gate.status == "pass" and "not measured: security.x" in gate.summary
        gate = release_gate([self.tv(VerdictStatus.PASSED)], load_errors=2)
        assert gate.status == "pass" and "2 scenario file(s) could not be loaded" in gate.summary

    def test_undeclared_criticality_is_named_when_it_blocks(self) -> None:
        gate = release_gate([self.tv(VerdictStatus.FAILED)])
        assert gate.blocking == ["t-failed-normal: failed (no criticality declared)"]

    def test_exit_codes(self) -> None:
        P, F, I = VerdictStatus.PASSED, VerdictStatus.FAILED, VerdictStatus.INCONCLUSIVE  # noqa: E741
        assert exit_code([self.tv(P)]) == EXIT_PASS
        assert exit_code([self.tv(P), self.tv(F)]) == EXIT_FAIL
        assert exit_code([self.tv(P), self.tv(I, attempts=[av(True)])]) == EXIT_NOT_JUDGED
        assert exit_code([self.tv(P)], load_errors=1) == EXIT_NOT_JUDGED
        assert exit_code([]) == EXIT_NOT_JUDGED
        harness = [av(None, TerminalState.TRANSPORT_FAILURE)]
        assert exit_code([self.tv(I, attempts=harness)]) == EXIT_HARNESS
