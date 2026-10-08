# SPDX-License-Identifier: Apache-2.0
"""A scorer that could not run is named wherever the verdict is (ADR-0029).

An ``infra_error`` is never the agent's fault, so ``attempt_verdict`` leaves it out of the
attempt and the attempt can pass on the scorers that did run. Without a name attached, a
3-vote security judge whose votes disagreed (``JUDGE_DISAGREEMENT``, "never a pass") would
reach CI as the same green as one that voted "safe".
"""

from __future__ import annotations

from datetime import UTC, datetime
from xml.etree import ElementTree as ET  # nosec B405 - parsing our own output in a test

from opseval.models.record import RunRecord, TerminalState
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
from opseval.reporting import console_summary, render_html
from opseval.reporting.junit import render_junit
from opseval.reporting.markdown import markdown_summary
from opseval.verdicts import attempt_verdict

NOW = datetime(2026, 10, 8, tzinfo=UTC)
SECURITY = "security.no_injection_takeover"


def ok(scorer_id: str, axis: Axis = Axis.OUTPUT) -> ScoreResult:
    return ScoreResult(
        scorer_id=scorer_id, axis=axis, status=ScoreStatus.PASSED, score=1.0, threshold=1.0
    )


def infra(scorer_id: str, axis: Axis = Axis.SECURITY) -> ScoreResult:
    return ScoreResult(
        scorer_id=scorer_id,
        axis=axis,
        status=ScoreStatus.INFRA_ERROR,
        score=None,
        threshold=1.0,
        reason="JUDGE_DISAGREEMENT: votes split 2-1",
    )


def attempt(*scores: ScoreResult, passed: bool | None = True) -> AttemptVerdict:
    return AttemptVerdict(
        attempt_index=0,
        passed=passed,
        terminal_state=TerminalState.GOAL_REACHED,
        scores=list(scores),
        record_path="a0.json",
        invalid_reason=None if passed is not None else "the simulated customer broke character",
    )


def verdict(*attempts: AttemptVerdict) -> TestVerdict:
    return TestVerdict(
        test_id="t-001",
        name="a scenario",
        criticality=Criticality.CRITICAL,
        status=VerdictStatus.PASSED,
        pass_rate=1.0,
        attempts=list(attempts),
    )


def suite(*tests: TestVerdict) -> SuiteResult:
    return SuiteResult(
        opseval_version="0.1.0",
        run_id="20261008T000000Z-abc123",
        suite="smoke",
        target="ws://x",
        profile="p",
        started_at=NOW,
        ended_at=NOW,
        tests=list(tests),
        gate=Gate(status="pass", summary="1/1 scenarios passed. Safe to release."),
        exit_code=0,
    )


PASSING_WITHOUT_SECURITY = suite(verdict(attempt(ok("output.terminal_state"), infra(SECURITY))))
CLEAN = suite(verdict(attempt(ok("output.terminal_state"))))


class TestTheGapIsReal:
    def test_an_attempt_passes_with_its_security_judge_unmeasured(self) -> None:
        """The behaviour this file exists to make visible, pinned so it cannot drift."""
        rec = RunRecord(
            run_id="r",
            test_id="t-001",
            attempt_index=0,
            seed=1,
            target="x",
            started_at=NOW,
            ended_at=NOW,
            terminal_state=TerminalState.GOAL_REACHED,
        )
        av = attempt_verdict(rec, [ok("output.terminal_state"), infra(SECURITY)], "a0.json")
        assert av.passed is True
        assert verdict(av).unmeasured_scorers == [SECURITY]


class TestAggregation:
    def test_named_on_the_test_and_the_suite(self) -> None:
        assert PASSING_WITHOUT_SECURITY.tests[0].unmeasured_scorers == [SECURITY]
        assert PASSING_WITHOUT_SECURITY.unmeasured_scorers == [SECURITY]

    def test_harness_axis_is_not_listed(self) -> None:
        """The persona judge grades the simulator; its absence says nothing about the agent."""
        t = verdict(attempt(ok("output.terminal_state"), infra("judge.persona", Axis.HARNESS)))
        assert t.unmeasured_scorers == []

    def test_an_invalidated_attempt_is_not_listed(self) -> None:
        """An invalidated attempt was not judged at all, and already says why."""
        t = verdict(attempt(ok("output.terminal_state"), infra(SECURITY), passed=None))
        assert t.unmeasured_scorers == []

    def test_silent_when_everything_ran(self) -> None:
        assert CLEAN.unmeasured_scorers == []


class TestEveryOutputNamesThem:
    def test_markdown_summary(self) -> None:
        md = markdown_summary(PASSING_WITHOUT_SECURITY)
        assert "Safe to release" in md  # the case that matters: a green summary
        assert "Not measured" in md and SECURITY in md
        assert "Not measured" not in markdown_summary(CLEAN)

    def test_junit(self) -> None:
        root = ET.fromstring(render_junit(PASSING_WITHOUT_SECURITY))  # nosec B314 - our output
        suite_props = {p.get("name"): p.get("value") for p in root.findall("./properties/property")}
        assert suite_props["unmeasured_scorers"] == SECURITY
        case = root.find("./testcase")
        assert case is not None
        case_props = {p.get("name"): p.get("value") for p in case.findall("./properties/property")}
        assert case_props["unmeasured_scorers"] == SECURITY
        clean = ET.fromstring(render_junit(CLEAN))  # nosec B314 - our output
        assert clean.find(".//property[@name='unmeasured_scorers']") is None

    def test_console_summary(self) -> None:
        assert f"not measured: {SECURITY}" in console_summary(PASSING_WITHOUT_SECURITY)
        assert "not measured" not in console_summary(CLEAN)

    def test_html_report_banner_sits_above_the_scenarios(self) -> None:
        out = render_html(PASSING_WITHOUT_SECURITY, [])
        assert "Not measured" in out and SECURITY in out
        assert out.index("Not measured") < out.index("<h2>Scenarios</h2>")
        assert '<h3 class="inconclusive">Not measured' not in render_html(CLEAN, [])
