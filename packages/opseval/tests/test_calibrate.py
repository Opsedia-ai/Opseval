# SPDX-License-Identifier: Apache-2.0
"""`opseval calibrate`: measuring a scorer against human labels.

The core rule applies to calibration itself:
- harness half: a score that was never measured (``infra_error``) cannot agree or
  disagree, too few usable labels is *not measured* (exit 2) and never "agrees", and a
  rate over an empty denominator is ``None`` rather than 0;
- agent half: a judge that really does disagree with the humans is reported as
  disagreeing (exit 1), and passing what a human failed is called out separately from
  failing what a human passed, because only the first ships a broken agent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from opseval.calibrate import (
    calibrate_run,
    calibration_summary,
    label_template,
    load_labels,
    save_calibration,
)
from opseval.cli import app
from opseval.errors import ConfigError
from opseval.models.calibration import AgreementStatus
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

NOW = datetime(2026, 10, 2, tzinfo=UTC)
SCORER = "judge.goal_achievement"


def score(status: ScoreStatus, scorer_id: str = SCORER) -> ScoreResult:
    return ScoreResult(
        scorer_id=scorer_id,
        axis=Axis.OUTPUT,
        status=status,
        score=None if status is ScoreStatus.INFRA_ERROR else (1.0 if status else 0.0),
        threshold=1.0,
        weight=1.0,
        reason="JUDGE_ERROR: boom" if status is ScoreStatus.INFRA_ERROR else None,
    )


def run_with(statuses: list[ScoreStatus], *, scorer_id: str = SCORER) -> SuiteResult:
    """One scenario, one attempt per status."""
    attempts = [
        AttemptVerdict(
            attempt_index=i,
            passed=status is ScoreStatus.PASSED,
            terminal_state=TerminalState.GOAL_REACHED,
            scores=[score(status, scorer_id)],
            record_path=f"a{i}.json",
        )
        for i, status in enumerate(statuses)
    ]
    return SuiteResult(
        opseval_version="0.1.0",
        run_id="20261002T000000Z-abc123",
        suite="smoke",
        target="ws://x",
        profile="p",
        started_at=NOW,
        ended_at=NOW,
        tests=[
            TestVerdict(
                test_id="t-001",
                name="a scenario",
                criticality=Criticality.CRITICAL,
                status=VerdictStatus.PASSED,
                attempts=attempts,
            )
        ],
        gate=Gate(status="pass", summary="ok"),
        exit_code=0,
    )


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "reports" / "20261002T000000Z-abc123"
    folder.mkdir(parents=True)
    return folder


def write_run(run_dir: Path, result: SuiteResult) -> Path:
    (run_dir / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return run_dir


def write_labels(path: Path, verdicts: list[str], *, scorer_id: str = SCORER) -> Path:
    rows = [
        {
            "test_id": "t-001",
            "attempt_index": i,
            "scorer_id": scorer_id,
            "verdict": v,
        }
        for i, v in enumerate(verdicts)
    ]
    path.write_text(yaml.safe_dump({"schema": "opseval/v1", "labels": rows}), encoding="utf-8")
    return path


P, F, E = ScoreStatus.PASSED, ScoreStatus.FAILED, ScoreStatus.INFRA_ERROR


class TestLabelLoading:
    def test_a_missing_file_is_a_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="cannot read"):
            load_labels(tmp_path / "nope.yaml")

    def test_bad_yaml_is_a_config_error(self, tmp_path: Path) -> None:
        path = tmp_path / "l.yaml"
        path.write_text("labels: [\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_labels(path)

    def test_an_unknown_field_fails_at_load_time(self, tmp_path: Path) -> None:
        path = tmp_path / "l.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "labels": [
                        {
                            "test_id": "t",
                            "attempt_index": 0,
                            "scorer_id": "s",
                            "verdict": "passed",
                            "tyop": 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_labels(path)

    def test_an_invalid_verdict_fails_at_load_time(self, tmp_path: Path) -> None:
        path = tmp_path / "l.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "labels": [
                        {
                            "test_id": "t",
                            "attempt_index": 0,
                            "scorer_id": "s",
                            "verdict": "probably",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_labels(path)

    def test_a_duplicate_label_is_rejected(self, tmp_path: Path) -> None:
        """Two humans disagreeing is real, but silently keeping one is not an answer."""
        path = tmp_path / "l.yaml"
        row: dict[str, Any] = {
            "test_id": "t",
            "attempt_index": 0,
            "scorer_id": "s",
            "verdict": "passed",
        }
        path.write_text(yaml.safe_dump({"labels": [row, {**row, "verdict": "failed"}]}), "utf-8")
        with pytest.raises(ConfigError, match="duplicate label"):
            load_labels(path)

    def test_an_empty_label_list_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "l.yaml"
        path.write_text(yaml.safe_dump({"labels": []}), encoding="utf-8")
        with pytest.raises(ConfigError):
            load_labels(path)


class TestAgreement:
    def calibrate(
        self, run_dir: Path, statuses: list[ScoreStatus], verdicts: list[str], **kw: Any
    ) -> Any:
        write_run(run_dir, run_with(statuses))
        labels = write_labels(run_dir.parent / "l.yaml", verdicts)
        kw.setdefault("min_labels", 4)
        return calibrate_run(run_dir, labels, **kw)

    def test_total_agreement_passes(self, run_dir: Path) -> None:
        result = self.calibrate(run_dir, [P, P, F, F], ["passed", "passed", "failed", "failed"])
        only = result.scorers[0]
        assert only.status is AgreementStatus.AGREES
        assert only.agreement == 1.0
        assert only.kappa == 1.0
        assert only.false_pass == 0 and only.false_fail == 0
        assert result.exit_code == 0

    def test_a_judge_that_disagrees_fails(self, run_dir: Path) -> None:
        """Agent half: real disagreement is reported, not smoothed over."""
        result = self.calibrate(run_dir, [P, P, P, P], ["failed", "failed", "failed", "passed"])
        only = result.scorers[0]
        assert only.status is AgreementStatus.DISAGREES
        assert only.agreement == 0.25
        assert result.exit_code == 1
        assert "Do not trust" in result.summary

    def test_a_false_pass_is_counted_separately_from_a_false_fail(self, run_dir: Path) -> None:
        result = self.calibrate(run_dir, [P, F, P, F], ["failed", "passed", "passed", "failed"])
        only = result.scorers[0]
        assert only.false_pass == 1, "judge passed what a human failed"
        assert only.false_fail == 1, "judge failed what a human passed"

    def test_a_low_false_pass_rate_can_fail_on_its_own(self, run_dir: Path) -> None:
        """High overall agreement can still hide the dangerous direction."""
        statuses = [P] * 9 + [P]
        verdicts = ["passed"] * 9 + ["failed"]
        result = self.calibrate(run_dir, statuses, verdicts, min_labels=4, min_agreement=0.5)
        only = result.scorers[0]
        assert only.agreement == 0.9
        assert only.false_pass_rate == 1.0, "it passed the single attempt humans failed"
        assert only.status is AgreementStatus.DISAGREES
        assert "humans failed" in (only.reason or "")

    def test_an_infra_error_score_is_excluded_not_counted_as_agreement(self, run_dir: Path) -> None:
        """Harness half: a score that was never measured cannot agree."""
        result = self.calibrate(
            run_dir, [P, P, E, E], ["passed", "passed", "passed", "passed"], min_labels=2
        )
        only = result.scorers[0]
        assert only.labels == 4
        assert only.usable == 2
        assert only.not_measured == 2
        assert only.agreement == 1.0, "computed over the 2 usable labels only"

    def test_too_few_usable_labels_is_not_measured(self, run_dir: Path) -> None:
        result = self.calibrate(
            run_dir, [P, E, E, E], ["passed", "passed", "passed", "passed"], min_labels=4
        )
        only = result.scorers[0]
        assert only.status is AgreementStatus.NOT_MEASURED
        assert only.agreement is None, "never 0.0"
        assert only.kappa is None
        assert result.exit_code == 2
        assert "Not measured" in result.summary

    def test_an_empty_denominator_is_none_not_zero(self, run_dir: Path) -> None:
        """Humans failed nothing, so there is no false-pass rate to report."""
        result = self.calibrate(run_dir, [P, P, P, P], ["passed", "passed", "passed", "passed"])
        only = result.scorers[0]
        assert only.false_pass_rate is None
        assert only.false_pass == 0

    def test_kappa_is_undefined_when_a_rater_used_one_verdict(self, run_dir: Path) -> None:
        """0.0 would read as "no better than chance"; the truth is "cannot tell"."""
        result = self.calibrate(run_dir, [P, P, P, P], ["passed", "passed", "passed", "passed"])
        only = result.scorers[0]
        assert only.agreement == 1.0
        assert only.kappa is None
        assert "only one verdict" in (only.kappa_note or "")

    def test_chance_agreement_gives_a_low_kappa(self, run_dir: Path) -> None:
        result = self.calibrate(
            run_dir,
            [P, P, F, F, P, P, F, F],
            ["passed", "failed", "passed", "failed", "passed", "failed", "passed", "failed"],
            min_agreement=0.0,
            max_false_pass_rate=1.0,
        )
        only = result.scorers[0]
        assert only.kappa == 0.0
        assert only.agreement == 0.5

    def test_scorers_are_reported_separately(self, run_dir: Path) -> None:
        result = run_with([P, P])
        for attempt in result.tests[0].attempts:
            attempt.scores.append(score(ScoreStatus.FAILED, "judge.efficiency"))
        write_run(run_dir, result)
        rows = [
            {"test_id": "t-001", "attempt_index": i, "scorer_id": s, "verdict": "passed"}
            for i in range(2)
            for s in (SCORER, "judge.efficiency")
        ]
        labels = run_dir.parent / "l.yaml"
        labels.write_text(yaml.safe_dump({"labels": rows}), encoding="utf-8")
        out = calibrate_run(run_dir, labels, min_labels=2, max_false_pass_rate=1.0)
        by_id = {a.scorer_id: a for a in out.scorers}
        assert by_id[SCORER].agreement == 1.0
        assert by_id["judge.efficiency"].agreement == 0.0


class TestConfigErrors:
    def test_an_unfinished_run_is_a_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not a finished run"):
            calibrate_run(tmp_path, tmp_path / "l.yaml")

    def test_a_label_for_an_unknown_attempt_is_a_config_error(self, run_dir: Path) -> None:
        """A typo must not quietly shrink the sample the judge is measured on."""
        write_run(run_dir, run_with([P, P]))
        labels = run_dir.parent / "l.yaml"
        labels.write_text(
            yaml.safe_dump(
                {
                    "labels": [
                        {
                            "test_id": "t-999",
                            "attempt_index": 0,
                            "scorer_id": SCORER,
                            "verdict": "passed",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="not in run"):
            calibrate_run(run_dir, labels)

    def test_a_label_for_an_unscored_scorer_is_reported_as_unmatched(self, run_dir: Path) -> None:
        """The attempt exists, so this is a usable-sample problem, not a bad file."""
        write_run(run_dir, run_with([P, P]))
        labels = write_labels(
            run_dir.parent / "l.yaml", ["passed", "passed"], scorer_id="judge.efficiency"
        )
        out = calibrate_run(run_dir, labels, min_labels=1)
        only = out.scorers[0]
        assert only.unmatched == 2
        assert only.status is AgreementStatus.NOT_MEASURED


class TestOutput:
    def test_the_run_is_never_modified(self, run_dir: Path) -> None:
        write_run(run_dir, run_with([P, P, F, F]))
        before = (run_dir / "result.json").read_text("utf-8")
        labels = write_labels(run_dir.parent / "l.yaml", ["passed", "passed", "failed", "failed"])
        out = calibrate_run(run_dir, labels, min_labels=4)
        save_calibration(run_dir, out)
        assert (run_dir / "result.json").read_text("utf-8") == before
        assert (run_dir / "calibration.json").is_file()

    def test_the_written_json_round_trips(self, run_dir: Path) -> None:
        write_run(run_dir, run_with([P, P, F, F]))
        labels = write_labels(run_dir.parent / "l.yaml", ["passed", "passed", "failed", "failed"])
        out = calibrate_run(run_dir, labels, min_labels=4, labelled_by="aya")
        path = save_calibration(run_dir, out)
        data = json.loads(path.read_text("utf-8"))
        assert data["labelled_by"] == "aya"
        assert data["scorers"][0]["agreement"] == 1.0

    def test_the_summary_never_prints_a_missing_number_as_zero(self, run_dir: Path) -> None:
        write_run(run_dir, run_with([P, E, E, E]))
        labels = write_labels(run_dir.parent / "l.yaml", ["passed"] * 4)
        text = calibration_summary(calibrate_run(run_dir, labels, min_labels=4))
        assert "not measured" in text
        assert "agreement 0%" not in text

    def test_the_summary_reports_kappa_as_unavailable_not_zero(self, run_dir: Path) -> None:
        write_run(run_dir, run_with([P, P, P, P]))
        labels = write_labels(run_dir.parent / "l.yaml", ["passed"] * 4)
        text = calibration_summary(calibrate_run(run_dir, labels, min_labels=4))
        assert "kappa not measured" in text
        assert "only one verdict" in text


class TestLabelTemplate:
    def test_it_lists_every_attempt_that_has_that_score(self) -> None:
        text = label_template(run_with([P, F, E]), SCORER)
        parsed = yaml.safe_load(text)
        assert [row["attempt_index"] for row in parsed["labels"]] == [0, 1, 2]
        assert {row["verdict"] for row in parsed["labels"]} == {"passed"}

    def test_it_round_trips_through_the_loader(self, tmp_path: Path) -> None:
        path = tmp_path / "l.yaml"
        path.write_text(label_template(run_with([P, F]), SCORER), encoding="utf-8")
        assert len(load_labels(path).labels) == 2

    def test_an_unknown_scorer_is_a_config_error(self) -> None:
        with pytest.raises(ConfigError, match="no 'judge.nope' scores"):
            label_template(run_with([P]), "judge.nope")

    def test_it_warns_against_guessing(self) -> None:
        assert "guessed label is worse" in label_template(run_with([P]), SCORER)


class TestCli:
    runner = CliRunner()

    def test_exit_codes_follow_the_suite(self, run_dir: Path) -> None:
        write_run(run_dir, run_with([P, P, F, F]))
        labels = write_labels(run_dir.parent / "l.yaml", ["passed", "passed", "failed", "failed"])
        res = self.runner.invoke(
            app, ["calibrate", str(run_dir), "--labels", str(labels), "--min-labels", "4"]
        )
        assert res.exit_code == 0, res.output
        assert "AGREES" in res.output

    def test_disagreement_is_exit_1(self, run_dir: Path) -> None:
        write_run(run_dir, run_with([P, P, P, P]))
        labels = write_labels(run_dir.parent / "l.yaml", ["failed"] * 4)
        res = self.runner.invoke(
            app, ["calibrate", str(run_dir), "--labels", str(labels), "--min-labels", "4"]
        )
        assert res.exit_code == 1
        assert "DISAGREES" in res.output

    def test_a_missing_run_is_exit_3(self, tmp_path: Path) -> None:
        res = self.runner.invoke(app, ["calibrate", str(tmp_path), "--labels", "x.yaml"])
        assert res.exit_code == 3

    def test_no_labels_and_no_template_is_exit_3(self, run_dir: Path) -> None:
        write_run(run_dir, run_with([P]))
        res = self.runner.invoke(app, ["calibrate", str(run_dir)])
        assert res.exit_code == 3
        assert "--template-for" in res.output

    def test_the_template_flag_prints_a_label_file(self, run_dir: Path) -> None:
        write_run(run_dir, run_with([P, F]))
        res = self.runner.invoke(app, ["calibrate", str(run_dir), "--template-for", SCORER])
        assert res.exit_code == 0
        assert "scorer_id: judge.goal_achievement" in res.output

    def test_the_template_flag_rejects_an_unknown_scorer(self, run_dir: Path) -> None:
        write_run(run_dir, run_with([P]))
        res = self.runner.invoke(app, ["calibrate", str(run_dir), "--template-for", "judge.nope"])
        assert res.exit_code == 3
