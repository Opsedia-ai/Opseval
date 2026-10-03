# SPDX-License-Identifier: Apache-2.0
"""Build a flow map from a record (FR-RPT-05, ADR-0023).

Answers one question for whoever is investigating a failure: *where did the conversation
actually go, and where was it supposed to go?* The phase-order scorer already decides
pass or fail; this explains the shape of what happened, which a score cannot.

It reads the frozen record only, and it refuses to guess: a profile with no phases, or a
conversation with no phase markers, produces an unmeasured map with the reason. Rendering
an empty diagram there would read as "the agent did nothing", and an absent measurement
must never look like a zero.
"""

from __future__ import annotations

from opseval.models.flow import FlowMap, Step, Transition
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord


def _classify(previous: str | None, current: str, order: list[str]) -> tuple[Step, list[str]]:
    if current not in order or (previous is not None and previous not in order):
        return Step.UNKNOWN, []
    if previous is None:
        # The first marker is never a skip. There is no evidence about what happened
        # before it, and an early phase may simply not be one the agent marks - the
        # reference agent's first marker is `lookup`, not `intake`. Calling that
        # "skipped" would fire on every healthy run, and a signal that cries wolf on the
        # happy path teaches people to ignore it. Leading phases that were never seen
        # are reported in `never_reached`, which is the accurate statement.
        return Step.EXPECTED, []
    here, there = order.index(previous), order.index(current)
    # `there == here` cannot happen: consecutive frames in one phase are collapsed into a
    # single visit before classification, so the two sides of a transition always differ.
    if there < here:
        return Step.BACKWARD, []
    if there - here == 1:
        return Step.EXPECTED, []
    return Step.SKIPPED, order[here + 1 : there]


def build_flow_map(record: RunRecord, profile: SutProfile) -> FlowMap:
    """The path this attempt took through ``profile.phases``."""
    order = list(profile.phases)
    if not order:
        return FlowMap(
            measured=False, reason="the SUT profile declares no phases, so there is no flow to map"
        )

    marks: list[tuple[str, int]] = []
    for frame in record.received():
        phase = frame.phase
        if phase and (not marks or marks[-1][0] != phase):
            # Consecutive frames in the same phase are one visit, not a transition.
            marks.append((phase, frame.seq))
    if not marks:
        return FlowMap(
            measured=False,
            reason="the agent reported no phase markers, so the flow was not observed",
            expected=order,
            never_reached=order,
        )

    transitions: list[Transition] = []
    previous: str | None = None
    for phase, seq in marks:
        kind, over = _classify(previous, phase, order)
        transitions.append(Transition(frm=previous, to=phase, kind=kind, at_seq=seq, over=over))
        previous = phase

    seen = [p for p, _ in marks]
    unknown = sorted({p for p in seen if p not in order})
    return FlowMap(
        expected=order,
        observed=list(dict.fromkeys(seen)),
        transitions=transitions,
        never_reached=[p for p in order if p not in seen],
        unknown_phases=unknown,
        ended_at=seen[-1],
    )


def flow_maps(records: list[RunRecord], profile: SutProfile) -> dict[tuple[str, int], FlowMap]:
    """Flow maps keyed by ``(test_id, attempt_index)``, for the report."""
    return {(r.test_id, r.attempt_index): build_flow_map(r, profile) for r in records}


__all__ = ["FlowMap", "Step", "Transition", "build_flow_map", "flow_maps"]
