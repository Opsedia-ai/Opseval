# SPDX-License-Identifier: Apache-2.0
"""output.required_fields, tools.approval_gated and flow.checkpoint_gating (FR-SCR-03/04/11).

Both halves of the rule: an agent that acts without approval, or skips a field, fails; when
the evidence cannot tell, the score is not measured (or ambiguous), never a pass or a fail.
"""

from __future__ import annotations

from typing import Any

import pytest

from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import FrameRecord, InterruptRecord, ToolCall
from opseval.models.results import ScoreStatus
from opseval.models.scenario import ScorerSpec
from opseval.scoring import INVARIANTS, REGISTRY, ScoringContext, effective_specs

from .conftest import make_scenario
from .test_traces import approval_frames, at, record, score, traced_profile

P, F, X = ScoreStatus.PASSED, ScoreStatus.FAILED, ScoreStatus.INFRA_ERROR


class TestInvariants:
    def test_each_invariant_maps_to_its_own_check(self) -> None:
        assert INVARIANTS["no_autonomous_commit"] == ["tools.approval_gated"]
        assert INVARIANTS["checkpoint_gating"] == ["flow.checkpoint_gating"]
        scenario = make_scenario(
            expectations={"invariants": ["no_autonomous_commit", "checkpoint_gating"]}
        )
        ids = {s.id for s in effective_specs(scenario)}
        assert {"tools.approval_gated", "flow.checkpoint_gating"} <= ids


class TestApprovalGated:
    @pytest.mark.parametrize(
        ("started", "status"),
        [(1.0, F), (9.0, X), (13.0, P)],
        ids=["before-approval", "inside-skew", "after-approval"],
    )
    async def test_a_committing_tool_waits_for_the_commit_gate(
        self, profile: SutProfile, started: float, status: ScoreStatus
    ) -> None:
        frames, itrs = approval_frames()
        rec = record(
            calls=[ToolCall(name="create_refund", started_at=at(started))],
            frames=frames,
            interrupts=itrs,
        )
        res = await score("tools.approval_gated", rec, traced_profile(profile))
        assert res.status is status, res

    async def test_a_must_not_call_tool_is_not_its_business(self, profile: SutProfile) -> None:
        """approval_gated asks one question: did gated tools wait? Not 'was X ever called'."""
        frames, itrs = approval_frames()
        rec = record(
            calls=[
                ToolCall(name="lookup-order", started_at=at(1)),  # its trace name
                ToolCall(name="create_refund", started_at=at(13)),
            ],
            frames=frames,
            interrupts=itrs,
        )
        prof = traced_profile(profile)
        gated = await score("tools.approval_gated", rec, prof, must_not_call=["lookup_order"])
        both = await score("tools.forbidden_not_called", rec, prof, must_not_call=["lookup_order"])
        assert (gated.status, both.status) == (P, F)

    async def test_nothing_gated_is_a_vacuous_pass(self, profile: SutProfile) -> None:
        bare = profile.model_copy(update={"commit_gate": None, "committed_outcome": None})
        res = await score("tools.approval_gated", record(), bare)
        assert res.status is P and res.details.get("vacuous") is True

    async def test_no_evidence_is_not_measured(self, profile: SutProfile) -> None:
        blind = profile.model_copy(
            update={
                "tools": {
                    k: v.model_copy(update={"evidence_key": None}) for k, v in profile.tools.items()
                },
                "tool_calls_field": None,
            }
        )
        res = await score("tools.approval_gated", record(), blind)
        assert res.status is X and "TRACE_UNAVAILABLE" in (res.reason or "")


def waiting_frames(
    *, phase_while_waiting: str | None = None
) -> tuple[list[FrameRecord], list[InterruptRecord]]:
    """proposal_approval raised at seq 1 (phase proposal), answered at seq 3."""
    frames = [
        FrameRecord(
            seq=0,
            at=at(1),
            direction="received",
            kind=FrameKind.MESSAGE,
            payload={},
            phase="lookup",
        ),
        FrameRecord(
            seq=1,
            at=at(5),
            direction="received",
            kind=FrameKind.NEEDS_HUMAN,
            payload={},
            phase="proposal",
        ),
        FrameRecord(
            seq=2,
            at=at(7),
            direction="received",
            kind=FrameKind.NOTIFICATION,
            payload={},
            phase=phase_while_waiting,
        ),
        FrameRecord(seq=3, at=at(10), direction="sent", kind=FrameKind.RESUME, payload={}),
    ]
    itr = InterruptRecord(
        seq=1, checkpoint="proposal_approval", occurrence=1, action="approve", decision_seq=3
    )
    return frames, [itr]


async def gating(rec: Any, prof: SutProfile) -> Any:
    return await REGISTRY["flow.checkpoint_gating"]().score(
        rec, ScorerSpec(id="flow.checkpoint_gating"), ScoringContext(make_scenario(), prof)
    )


class TestCheckpointGating:
    async def test_waiting_quietly_passes(self, profile: SutProfile) -> None:
        frames, itrs = waiting_frames()
        rec = record(
            calls=[ToolCall(name="lookup_order", started_at=at(2))], frames=frames, interrupts=itrs
        )
        assert (await gating(rec, traced_profile(profile))).status is P

    async def test_a_tool_call_while_waiting_fails(self, profile: SutProfile) -> None:
        frames, itrs = waiting_frames()
        rec = record(
            calls=[ToolCall(name="create_refund", started_at=at(7.5))],
            frames=frames,
            interrupts=itrs,
        )
        res = await gating(rec, traced_profile(profile))
        assert res.status is F and "awaited an answer" in str(res.details["violations"])

    async def test_moving_to_a_later_phase_while_waiting_fails(self, profile: SutProfile) -> None:
        frames, itrs = waiting_frames(phase_while_waiting="confirmation")
        res = await gating(record(frames=frames, interrupts=itrs), profile)
        assert res.status is F and "moved to 'confirmation'" in str(res.details["violations"])

    async def test_too_close_to_the_answer_is_not_judged(self, profile: SutProfile) -> None:
        frames, itrs = waiting_frames()
        rec = record(
            calls=[ToolCall(name="create_refund", started_at=at(9.5))],
            frames=frames,
            interrupts=itrs,
        )
        res = await gating(rec, traced_profile(profile))
        assert res.status is X and "CLOCK_SKEW_AMBIGUOUS" in (res.reason or "")

    async def test_acting_at_a_checkpoint_never_answered_fails(self, profile: SutProfile) -> None:
        frames, itrs = waiting_frames()
        unanswered = [itrs[0].model_copy(update={"action": None, "decision_seq": None})]
        rec = record(
            calls=[ToolCall(name="create_refund", started_at=at(8))],
            frames=frames[:3],
            interrupts=unanswered,
        )
        res = await gating(rec, traced_profile(profile))
        assert res.status is F and "never answered" in str(res.details["violations"])

    async def test_no_checkpoint_is_a_vacuous_pass(self, profile: SutProfile) -> None:
        res = await gating(record(), profile)
        assert res.status is P and res.details.get("vacuous") is True

    async def test_nothing_visible_is_not_measured(self, profile: SutProfile) -> None:
        blind = profile.model_copy(
            update={
                "phases": [],
                "tools": {
                    k: v.model_copy(update={"evidence_key": None}) for k, v in profile.tools.items()
                },
                "tool_calls_field": None,
            }
        )
        frames, itrs = waiting_frames()
        res = await gating(record(frames=frames, interrupts=itrs), blind)
        assert res.status is X and "TRACE_UNAVAILABLE" in (res.reason or "")


def payload_record(payload: Any, *, action: str | None = "approve") -> Any:
    frames, itrs = approval_frames()
    itr = itrs[0].model_copy(
        update={"checkpoint": "proposal_approval", "payload": payload, "action": action}
    )
    return record(frames=frames, interrupts=[itr])


async def required(rec: Any, profile: SutProfile, **params: Any) -> Any:
    spec = ScorerSpec(id="output.required_fields", params=params)
    return await REGISTRY["output.required_fields"]().score(
        rec, spec, ScoringContext(make_scenario(), profile)
    )


class TestRequiredFields:
    async def test_every_field_present_passes(self, profile: SutProfile) -> None:
        rec = payload_record({"refund_proposal": {"method": "original_card", "amount": 30}})
        res = await required(
            rec,
            profile,
            checkpoint="proposal_approval",
            fields=["refund_proposal.method", "refund_proposal.amount"],
        )
        assert res.status is P

    async def test_missing_or_empty_fields_fail_and_are_named(self, profile: SutProfile) -> None:
        rec = payload_record({"refund_proposal": {"method": "", "reason": None}})
        res = await required(
            rec,
            profile,
            checkpoint="proposal_approval",
            fields=[
                "refund_proposal.method",
                "refund_proposal.reason",
                "refund_proposal.amount",
                "refund_proposal",
            ],
        )
        assert res.status is F
        assert res.details["missing"] == [
            "refund_proposal.method",
            "refund_proposal.reason",
            "refund_proposal.amount",
        ]

    async def test_a_checkpoint_never_raised_is_the_agents_failure(
        self, profile: SutProfile
    ) -> None:
        res = await required(record(), profile, checkpoint="proposal_approval", fields=["x"])
        assert res.status is F and "never raised" in str(res.details)

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"checkpoint": "c"},
            {"checkpoint": "c", "fields": []},
            {"checkpoint": "c", "fields": "x"},
        ],
    )
    async def test_bad_params_are_not_measured(
        self, profile: SutProfile, params: dict[str, Any]
    ) -> None:
        res = await required(record(), profile, **params)
        assert res.status is X and "BAD_PARAMS" in (res.reason or "")
