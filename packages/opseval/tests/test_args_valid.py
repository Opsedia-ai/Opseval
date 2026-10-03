# SPDX-License-Identifier: Apache-2.0
"""`tools.args_valid` (FR-SCR-04) and visible vacuous passes.

Fault-injection view of the core rule:
- harness half: no declared schema is *not measurable* (a pass would claim the arguments
  were verified against a rule that does not exist); no trace is `TRACE_UNAVAILABLE`; a
  broken schema is the author's fault and is reported as such, never as the agent sending
  bad arguments; and a pass on no evidence is named as vacuous wherever it is read;
- agent half: arguments that genuinely break a declared schema fail the agent, and the
  failure names the field and the reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from opseval.models.profile import FrameKind, SutProfile, ToolSpec, TraceConfig
from opseval.models.record import FrameRecord, RunRecord, TerminalState, ToolCall
from opseval.models.results import ScoreStatus
from opseval.models.scenario import ScorerSpec
from opseval.scoring import ScoringEngine
from opseval.scoring.base import ScoringContext
from opseval.scoring.tools import ArgsValid

from .conftest import make_scenario

T0 = datetime(2026, 10, 3, tzinfo=UTC)

REFUND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["order_id", "amount"],
    "properties": {
        "order_id": {"type": "string"},
        "amount": {"type": "number", "minimum": 0},
        "method": {"enum": ["card", "credit"]},
    },
    "additionalProperties": False,
}


def profile(
    *, schema: dict[str, Any] | None = REFUND_SCHEMA, traced: bool = True, **tools: ToolSpec
) -> SutProfile:
    declared = {"create_refund": ToolSpec(args_schema=schema), **tools}
    return SutProfile(
        name="p",
        tools=declared,
        traces=TraceConfig() if traced else None,
    )


def record(*calls: ToolCall, coverage: str = "full") -> RunRecord:
    return RunRecord(
        run_id="r",
        test_id="t-001",
        attempt_index=0,
        seed=1,
        target="x",
        started_at=T0,
        ended_at=T0,
        terminal_state=TerminalState.GOAL_REACHED,
        frames=(
            FrameRecord(
                seq=0, at=T0, direction="received", kind=FrameKind.MESSAGE, payload={}, text="hi"
            ),
        ),
        trace_coverage=coverage,  # type: ignore[arg-type]
        tool_calls=calls,
    )


def call(name: str = "create_refund", **arguments: Any) -> ToolCall:
    return ToolCall(name=name, started_at=T0, arguments=arguments or None)


async def score(rec: RunRecord, prof: SutProfile, **params: Any) -> Any:
    ctx = ScoringContext(scenario=make_scenario(), profile=prof)
    return await ArgsValid().score(rec, ScorerSpec(id=ArgsValid.id, params=params), ctx)


# --- the harness half -----------------------------------------------------------------


class TestNotTheAgentsFault:
    async def test_no_declared_schema_is_not_measurable(self) -> None:
        """A pass would claim the arguments were checked against a rule that does not exist."""
        res = await score(record(call(order_id="1", amount=5)), profile(schema=None))
        assert res.status is ScoreStatus.INFRA_ERROR
        assert res.score is None
        assert "no tool in this profile declares an args_schema" in (res.reason or "")

    async def test_frames_only_evidence_is_trace_unavailable(self) -> None:
        """FR-SCR-04 names this reason code."""
        res = await score(record(coverage="frames_only"), profile())
        assert res.status is ScoreStatus.INFRA_ERROR
        assert "TRACE_UNAVAILABLE" in (res.reason or "")
        assert "only visible in traces" in (res.reason or "")

    async def test_a_broken_schema_is_reported_as_the_schema_being_broken(self) -> None:
        """The author's mistake must not read as the agent sending bad arguments."""
        res = await score(
            record(call(order_id="1")), profile(schema={"type": "not-a-json-schema-type"})
        )
        assert res.status is ScoreStatus.FAILED
        problem = res.details["invalid"][0]["problem"]
        assert "not valid JSON Schema" in problem

    async def test_an_undeclared_trace_call_is_ignored_not_failed(self) -> None:
        res = await score(record(call("mystery_tool", x=1)), profile())
        assert res.details.get("vacuous") is True
        assert res.status is ScoreStatus.PASSED

    async def test_a_called_tool_without_a_schema_is_counted_unchecked(self) -> None:
        prof = profile(lookup_order=ToolSpec())
        res = await score(record(call("lookup_order", id="1")), prof)
        assert res.details["unchecked"] == ["lookup_order"]
        assert res.details.get("vacuous") is True

    async def test_the_scorer_never_raises_on_odd_arguments(self) -> None:
        for arguments in (None, [], "string", 7):
            rec = record(ToolCall(name="create_refund", started_at=T0, arguments=arguments))
            res = await score(rec, profile())
            assert res.status in (ScoreStatus.PASSED, ScoreStatus.FAILED)


# --- the agent half -------------------------------------------------------------------


class TestAgentFaults:
    async def test_valid_arguments_pass(self) -> None:
        res = await score(record(call(order_id="A-1", amount=12.5, method="card")), profile())
        assert res.status is ScoreStatus.PASSED
        assert res.score == 1.0
        assert res.details["checked"] == 1

    async def test_a_missing_required_field_fails_and_says_which(self) -> None:
        res = await score(record(call(order_id="A-1")), profile())
        assert res.status is ScoreStatus.FAILED
        assert "amount" in res.details["invalid"][0]["problem"]

    async def test_a_wrong_type_fails(self) -> None:
        res = await score(record(call(order_id="A-1", amount="loads")), profile())
        assert res.status is ScoreStatus.FAILED

    async def test_a_value_outside_the_schema_fails(self) -> None:
        res = await score(record(call(order_id="A-1", amount=-5)), profile())
        assert res.status is ScoreStatus.FAILED
        assert "amount" in res.details["invalid"][0]["problem"]

    async def test_an_unexpected_argument_fails_when_additional_is_forbidden(self) -> None:
        res = await score(record(call(order_id="A-1", amount=1, sneaky=True)), profile())
        assert res.status is ScoreStatus.FAILED

    async def test_the_score_is_the_share_of_valid_calls(self) -> None:
        res = await score(
            record(
                call(order_id="A-1", amount=1),
                call(order_id="A-2", amount=2),
                call(order_id="A-3"),
                call(order_id="A-4", amount=4),
            ),
            profile(),
        )
        assert res.details["checked"] == 4
        assert res.score == pytest.approx(0.75, abs=1e-4)
        assert res.status is ScoreStatus.FAILED

    async def test_the_failure_names_the_tool(self) -> None:
        res = await score(record(call(order_id="A-1")), profile())
        assert res.details["invalid"][0]["tool"] == "create_refund"

    async def test_a_trace_name_alias_is_resolved(self) -> None:
        prof = SutProfile(
            name="p",
            tools={"create_refund": ToolSpec(trace_name="refund.v2", args_schema=REFUND_SCHEMA)},
            traces=TraceConfig(),
        )
        res = await score(record(call("refund.v2", order_id="A-1")), prof)
        assert res.status is ScoreStatus.FAILED
        assert res.details["invalid"][0]["tool"] == "create_refund"


# --- vacuous passes are named ---------------------------------------------------------


class TestVacuousIsVisible:
    """A pass on no evidence looked identical to a real pass in every output a human
    reads. The pass is honest; presenting it without a word is not."""

    async def test_no_matching_call_is_a_vacuous_pass(self) -> None:
        res = await score(record(), profile())
        assert res.status is ScoreStatus.PASSED
        assert res.details["vacuous"] is True
        assert res.details["note"] == "no call to a tool with an args_schema"

    async def test_the_verdict_collects_vacuous_scorers(self) -> None:
        prof = profile()
        scenario = make_scenario(scorers=[{"id": "tools.args_valid"}])
        scores = await ScoringEngine().score(record(), scenario, prof)
        from opseval.models.record import TerminalState as TS
        from opseval.verdicts import attempt_verdict

        verdict = attempt_verdict(record(), scores, "p")
        assert verdict.passed is True
        from opseval.models.results import TestVerdict, VerdictStatus
        from opseval.models.scenario import Criticality

        test = TestVerdict(
            test_id="t-001",
            name="n",
            criticality=Criticality.NORMAL,
            status=VerdictStatus.PASSED,
            attempts=[verdict],
        )
        assert test.vacuous_scorers == ["tools.args_valid"]
        assert TS.GOAL_REACHED  # keep the import meaningful

    def test_every_output_names_it(self) -> None:
        from opseval.reporting import console_summary, render_html
        from opseval.reporting.junit import render_junit
        from opseval.reporting.markdown import markdown_summary

        from .test_degraded_visibility import attempt, suite, verdict
        from .test_degraded_visibility import score as mk

        passing = mk("tools.args_valid")
        vacuous = passing.model_copy(update={"details": {"vacuous": True}})
        result = suite(verdict("t-001", attempt(vacuous)))
        assert result.vacuous_scorers == ["tools.args_valid"]
        assert "vacuous" in console_summary(result)
        assert "Passed vacuously" in markdown_summary(result)
        assert "evidence of nothing" in markdown_summary(result)
        assert "Passed vacuously" in render_html(result, [])
        assert 'name="vacuous_scorers"' in render_junit(result)

    def test_a_real_pass_is_not_labelled(self) -> None:
        from opseval.reporting.markdown import markdown_summary

        from .test_degraded_visibility import attempt, suite, verdict
        from .test_degraded_visibility import score as mk

        result = suite(verdict("t-001", attempt(mk("tools.args_valid"))))
        assert result.vacuous_scorers == []
        assert "vacuously" not in markdown_summary(result)

    def test_it_changes_no_status_or_exit_code(self) -> None:
        """Vacuous is a caveat on a pass, not a verdict of its own."""
        from .test_degraded_visibility import attempt, suite, verdict
        from .test_degraded_visibility import score as mk

        vacuous = mk("tools.args_valid").model_copy(update={"details": {"vacuous": True}})
        result = suite(verdict("t-001", attempt(vacuous)))
        assert result.exit_code == 0
        assert result.tests[0].status.value == "passed"
