# SPDX-License-Identifier: Apache-2.0
"""Flow map: expected versus observed phase transitions (FR-RPT-05).

The flow map explains the shape of a conversation, which a pass/fail score cannot. It
has no verdict of its own, so the rule it must hold is about honesty of presentation:

- a profile with no phases, or a conversation with no phase markers, is *not measured*
  with the reason — never an empty diagram, which would read as "the agent did nothing";
- a phase the profile does not declare is shown as unknown, not silently dropped;
- it never changes a status, a score or an exit code.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opseval.flow import build_flow_map, flow_maps
from opseval.models.flow import Step
from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import FrameRecord, RunRecord, TerminalState

T0 = datetime(2026, 10, 3, tzinfo=UTC)
PHASES = ["intake", "lookup", "proposal", "confirmation"]


def profile_with(phases: list[str] | None = None) -> SutProfile:
    return SutProfile(name="p", phases=PHASES if phases is None else phases)


def record(*phases: str | None, test_id: str = "t-001", attempt: int = 0) -> RunRecord:
    frames = tuple(
        FrameRecord(
            seq=i,
            at=T0,
            direction="received",
            kind=FrameKind.MESSAGE,
            payload={},
            phase=phase,
            text="hi",
        )
        for i, phase in enumerate(phases)
    )
    return RunRecord(
        run_id="r",
        test_id=test_id,
        attempt_index=attempt,
        seed=1,
        target="x",
        started_at=T0,
        ended_at=T0,
        terminal_state=TerminalState.GOAL_REACHED,
        frames=frames,
    )


def kinds(*phases: str | None) -> list[str]:
    return [t.kind.value for t in build_flow_map(record(*phases), profile_with()).transitions]


# --- not measured, rather than an empty diagram ---------------------------------------


class TestNotMeasured:
    def test_a_profile_without_phases_is_not_measured(self) -> None:
        flow = build_flow_map(record("anything"), profile_with([]))
        assert flow.measured is False
        assert "declares no phases" in (flow.reason or "")
        assert flow.transitions == []

    def test_no_phase_markers_is_not_measured(self) -> None:
        """Not "the agent did nothing": we did not observe the flow."""
        flow = build_flow_map(record(None, None), profile_with())
        assert flow.measured is False
        assert "no phase markers" in (flow.reason or "")

    def test_an_unobserved_flow_still_lists_what_was_expected(self) -> None:
        flow = build_flow_map(record(), profile_with())
        assert flow.expected == PHASES
        assert flow.never_reached == PHASES

    def test_unmeasured_is_never_clean(self) -> None:
        assert build_flow_map(record(), profile_with()).clean is False


# --- classifying transitions ----------------------------------------------------------


class TestClassification:
    def test_the_declared_path_is_all_expected(self) -> None:
        flow = build_flow_map(record(*PHASES), profile_with())
        assert [t.kind for t in flow.transitions] == [Step.EXPECTED] * 4
        assert flow.clean is True
        assert flow.never_reached == []
        assert flow.ended_at == "confirmation"

    def test_a_forward_jump_is_a_skip_and_names_what_was_missed(self) -> None:
        flow = build_flow_map(record("intake", "confirmation"), profile_with())
        skip = flow.transitions[1]
        assert skip.kind is Step.SKIPPED
        assert skip.over == ["lookup", "proposal"]
        assert flow.never_reached == ["lookup", "proposal"]

    def test_going_back_is_backward(self) -> None:
        assert kinds("intake", "lookup", "intake")[-1] == "backward"

    def test_revisiting_a_phase_is_backward(self) -> None:
        """There is no separate "repeated" step: a transition's two sides can never be
        the same phase, because consecutive frames in one phase are a single visit."""
        assert kinds("intake", "lookup", "intake", "intake") == [
            "expected",
            "expected",
            "backward",
        ]

    def test_consecutive_frames_in_one_phase_are_one_visit(self) -> None:
        """Three frames in `intake` is not two transitions."""
        assert kinds("intake", "intake", "intake") == ["expected"]

    def test_starting_mid_flow_is_not_a_skip(self) -> None:
        """There is no evidence about what preceded the first marker, and an early phase
        may simply not be one the agent marks. The reference agent starts at `lookup`, so
        calling this a skip would fire on every healthy run."""
        flow = build_flow_map(record("proposal"), profile_with())
        assert flow.transitions[0].kind is Step.EXPECTED
        assert flow.transitions[0].frm is None
        assert flow.unexpected == []

    def test_but_the_phases_before_it_are_reported_unreached(self) -> None:
        """The honest statement, without crying skip."""
        flow = build_flow_map(record("proposal"), profile_with())
        assert flow.never_reached == ["intake", "lookup", "confirmation"]
        assert flow.clean is False

    def test_starting_at_the_first_phase_is_expected(self) -> None:
        assert kinds("intake") == ["expected"]

    def test_an_undeclared_phase_is_unknown_not_dropped(self) -> None:
        flow = build_flow_map(record("intake", "mystery", "lookup"), profile_with())
        assert [t.kind.value for t in flow.transitions] == ["expected", "unknown", "unknown"]
        assert flow.unknown_phases == ["mystery"]
        assert "mystery" in flow.observed

    def test_the_transition_records_the_frame_it_was_seen_on(self) -> None:
        flow = build_flow_map(record("intake", "lookup"), profile_with())
        assert [t.at_seq for t in flow.transitions] == [0, 1]

    @pytest.mark.parametrize("step", list(Step))
    def test_only_expected_is_not_unexpected(self, step: Step) -> None:
        assert step.unexpected is (step is not Step.EXPECTED)


class TestSummaryProperties:
    def test_unexpected_lists_only_the_odd_ones(self) -> None:
        flow = build_flow_map(record("intake", "confirmation", "lookup"), profile_with())
        assert [t.kind.value for t in flow.unexpected] == ["skipped", "backward"]

    def test_a_run_that_never_finishes_is_not_clean(self) -> None:
        """Every transition was expected, but the flow stopped early."""
        flow = build_flow_map(record("intake", "lookup"), profile_with())
        assert flow.unexpected == []
        assert flow.never_reached == ["proposal", "confirmation"]
        assert flow.clean is False

    def test_observed_is_deduplicated_in_first_seen_order(self) -> None:
        flow = build_flow_map(record("intake", "lookup", "intake"), profile_with())
        assert flow.observed == ["intake", "lookup"]


class TestManyRecords:
    def test_maps_are_keyed_by_test_and_attempt(self) -> None:
        records = [
            record("intake", test_id="t-001", attempt=0),
            record("intake", "lookup", test_id="t-001", attempt=1),
            record("proposal", test_id="t-002", attempt=0),
        ]
        maps = flow_maps(records, profile_with())
        assert set(maps) == {("t-001", 0), ("t-001", 1), ("t-002", 0)}
        assert maps[("t-002", 0)].never_reached == ["intake", "lookup", "confirmation"]

    def test_no_records_is_no_maps(self) -> None:
        assert flow_maps([], profile_with()) == {}


# --- it changes nothing, and it reaches the report ------------------------------------


class TestReport:
    def html(self, *phases: str | None, profile: SutProfile | None = None) -> str:
        from opseval.reporting import render_html

        from .test_degraded_visibility import attempt, score, suite, verdict

        rec = record(*phases)
        result = suite(verdict("t-001", attempt(score("flow.phase_order"))))
        return render_html(result, [rec], profile or profile_with())

    def test_the_flow_map_appears(self) -> None:
        out = self.html(*PHASES)
        assert "Flow map" in out
        assert "as expected" in out

    def test_an_unexpected_transition_is_called_out(self) -> None:
        out = self.html("intake", "confirmation")
        assert "unexpected transition" in out
        assert "skipped" in out
        assert "over lookup, proposal" in out

    def test_only_unreached_phases_are_not_called_unexpected_transitions(self) -> None:
        """ "0 unexpected transition(s)" would be a confusing thing to print."""
        out = self.html("intake", "lookup")
        assert "never reached" in out
        assert "0 unexpected" not in out

    def test_an_unmeasured_map_says_so_rather_than_showing_nothing(self) -> None:
        out = self.html(None, profile=profile_with([]))
        assert "not measured" in out
        assert "declares no phases" in out

    def test_without_a_profile_there_is_no_flow_section(self) -> None:
        """render_html's profile is optional, and absence must not invent an order."""
        from opseval.reporting import render_html

        from .test_degraded_visibility import attempt, score, suite, verdict

        result = suite(verdict("t-001", attempt(score("flow.phase_order"))))
        out = render_html(result, [record("intake")])
        assert "Flow map" not in out

    def test_it_does_not_alter_the_verdict_or_exit_code(self) -> None:
        from .test_degraded_visibility import attempt, score, suite, verdict

        result = suite(verdict("t-001", attempt(score("flow.phase_order"))))
        before = result.model_dump_json()
        self.html("intake", "confirmation")
        assert result.model_dump_json() == before
        assert result.exit_code == 0
