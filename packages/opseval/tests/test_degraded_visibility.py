# SPDX-License-Identifier: Apache-2.0
"""Degraded evidence is visible wherever the verdict is.

A degraded score still counts toward the verdict, so if it is only a badge in the HTML
report then a score made on weaker evidence than the scorer wanted — frame evidence
instead of traces, or a judge from the graded model's own vendor family — reaches CI as
an ordinary green. The gate summary, the JUnit XML and the console summary therefore all
name it, and the case that matters most is a suite that **passed**.
"""

from __future__ import annotations

from datetime import UTC, datetime
from xml.etree import ElementTree as ET  # nosec B405 - parsing our own output in a test

from opseval.models.record import TerminalState
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
from opseval.reporting import console_summary
from opseval.reporting.junit import render_junit
from opseval.reporting.markdown import markdown_summary

NOW = datetime(2026, 10, 2, tzinfo=UTC)


def score(scorer_id: str, *, degraded: bool = False, passed: bool = True) -> ScoreResult:
    return ScoreResult(
        scorer_id=scorer_id,
        axis=Axis.OUTPUT,
        status=ScoreStatus.PASSED if passed else ScoreStatus.FAILED,
        score=1.0 if passed else 0.0,
        threshold=1.0,
        weight=1.0,
        degraded=degraded,
    )


def attempt(*scores: ScoreResult, index: int = 0, passed: bool | None = True) -> AttemptVerdict:
    return AttemptVerdict(
        attempt_index=index,
        passed=passed,
        terminal_state=TerminalState.GOAL_REACHED,
        scores=list(scores),
        record_path=f"a{index}.json",
    )


def verdict(
    test_id: str = "t-001",
    *attempts: AttemptVerdict,
    status: VerdictStatus = VerdictStatus.PASSED,
) -> TestVerdict:
    return TestVerdict(
        test_id=test_id,
        name="a scenario",
        criticality=Criticality.CRITICAL,
        status=status,
        pass_rate=1.0 if status is VerdictStatus.PASSED else 0.0,
        attempts=list(attempts),
    )


def suite(*tests: TestVerdict, exit_code: int = 0, gate: str = "pass") -> SuiteResult:
    return SuiteResult(
        opseval_version="0.1.0",
        run_id="20261002T000000Z-abc123",
        suite="smoke",
        target="ws://x",
        profile="p",
        started_at=NOW,
        ended_at=NOW,
        tests=list(tests),
        gate=Gate(status=gate, summary="1/1 scenarios passed."),
        exit_code=exit_code,
    )


class TestAggregation:
    def test_collected_across_attempts_deduped_and_sorted(self) -> None:
        t = verdict(
            "t-001",
            attempt(score("tools.ordering", degraded=True), index=0),
            attempt(
                score("tools.ordering", degraded=True),
                score("judge.content_quality", degraded=True),
                index=1,
            ),
        )
        assert t.degraded_scorers == ["judge.content_quality", "tools.ordering"]

    def test_a_clean_score_is_not_listed(self) -> None:
        t = verdict("t-001", attempt(score("output.terminal_state")))
        assert t.degraded_scorers == []

    def test_suite_unions_its_tests(self) -> None:
        s = suite(
            verdict("t-001", attempt(score("tools.ordering", degraded=True))),
            verdict("t-002", attempt(score("judge.efficiency", degraded=True))),
        )
        assert s.degraded_scorers == ["judge.efficiency", "tools.ordering"]

    def test_a_failed_score_can_also_be_degraded(self) -> None:
        t = verdict(
            "t-001",
            attempt(score("tools.ordering", degraded=True, passed=False)),
            status=VerdictStatus.FAILED,
        )
        assert t.degraded_scorers == ["tools.ordering"]


class TestMarkdownSummary:
    def test_a_passing_suite_still_names_degraded_evidence(self) -> None:
        """The case that matters: nothing else in a green summary hints at it."""
        md = markdown_summary(
            suite(verdict("t-001", attempt(score("judge.content_quality", degraded=True))))
        )
        assert "Safe to release" in md
        assert "Degraded evidence" in md
        assert "judge.content_quality" in md
        assert "counted toward the verdict" in md

    def test_silent_when_nothing_was_degraded(self) -> None:
        md = markdown_summary(suite(verdict("t-001", attempt(score("output.terminal_state")))))
        assert "Degraded" not in md

    def test_every_degraded_scorer_is_listed(self) -> None:
        md = markdown_summary(
            suite(
                verdict(
                    "t-001",
                    attempt(
                        score("tools.ordering", degraded=True),
                        score("judge.efficiency", degraded=True),
                    ),
                )
            )
        )
        assert "tools.ordering" in md
        assert "judge.efficiency" in md

    def test_a_scorer_id_cannot_break_out_of_its_code_span(self) -> None:
        """Scorer ids come from the catalogue, so they get the same treatment as
        scenario text: rendered inside a code span with backticks neutralised, which
        makes link and mention syntax inside it inert."""
        md = markdown_summary(
            suite(verdict("t-001", attempt(score("evil`id](http://x) @team", degraded=True))))
        )
        assert "`evil'id](http://x) @team`" in md  # backticks cannot close the span
        assert "[" not in md.split("Degraded evidence")[1].split("\n")[0]


class TestJunit:
    def parse(self, result: SuiteResult) -> ET.Element:
        return ET.fromstring(render_junit(result))  # nosec B314 - our own output

    def test_suite_property_lists_them(self) -> None:
        root = self.parse(suite(verdict("t-001", attempt(score("tools.ordering", degraded=True)))))
        props = {p.get("name"): p.get("value") for p in root.findall("./properties/property")}
        assert props["degraded_scorers"] == "tools.ordering"

    def test_a_passed_case_says_so_in_system_out(self) -> None:
        """A passed case has no failure or error element, so without this it is invisible."""
        root = self.parse(
            suite(verdict("t-001", attempt(score("judge.content_quality", degraded=True))))
        )
        case = root.find("./testcase")
        assert case is not None
        out = case.find("system-out")
        assert out is not None and out.text is not None
        assert "judge.content_quality" in out.text
        assert "counted" in out.text

    def test_case_property_is_set(self) -> None:
        root = self.parse(suite(verdict("t-001", attempt(score("tools.ordering", degraded=True)))))
        case = root.find("./testcase")
        assert case is not None
        props = {p.get("name"): p.get("value") for p in case.findall("./properties/property")}
        assert props["degraded_scorers"] == "tools.ordering"

    def test_nothing_added_when_nothing_degraded(self) -> None:
        root = self.parse(suite(verdict("t-001", attempt(score("output.terminal_state")))))
        assert root.find(".//property[@name='degraded_scorers']") is None
        assert root.find(".//system-out") is None

    def test_counts_are_unchanged_by_degradation(self) -> None:
        """Degraded evidence is a caveat on a score, not a verdict of its own."""
        root = self.parse(suite(verdict("t-001", attempt(score("tools.ordering", degraded=True)))))
        assert root.get("tests") == "1"
        assert root.get("failures") == "0"
        assert root.get("errors") == "0"
        assert root.get("skipped") == "0"


class TestConsoleSummary:
    def test_degraded_scorers_are_shown(self) -> None:
        text = console_summary(
            suite(verdict("t-001", attempt(score("tools.ordering", degraded=True))))
        )
        assert "degraded: tools.ordering" in text

    def test_silent_when_nothing_was_degraded(self) -> None:
        text = console_summary(suite(verdict("t-001", attempt(score("output.terminal_state")))))
        assert "degraded" not in text
