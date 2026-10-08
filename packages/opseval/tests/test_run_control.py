# SPDX-License-Identifier: Apache-2.0
"""Run control: stop policies and cancel requests (FR-CTL, ADR-0035).

Both halves of the core rule, as they apply to stopping early:
- a stopped or cancelled run never reads as a pass: every scenario it did not run is a
  ``skipped_precondition`` verdict naming why, so the result is never "safe to release";
- a stop never hides a failure that happened: the scenarios that ran keep their verdicts,
  and a scenario cut short is never folded from part of its repeats.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from opseval.cli import _stop_flags, app
from opseval.config import Settings
from opseval.demo.refund_agent import run_agent
from opseval.errors import ConfigError
from opseval.models.gate import resolve as resolve_gate
from opseval.models.results import SuiteResult, TestVerdict, VerdictStatus
from opseval.models.scenario import Criticality
from opseval.models.stop import StopPolicy
from opseval.reporting import console_summary
from opseval.reporting.junit import render_junit
from opseval.reporting.markdown import markdown_summary
from opseval.runner import RunOptions, _RunControl, launch
from opseval.storage import FileStore
from opseval.verdicts import EXIT_FAIL, EXIT_NOT_JUDGED

from .conftest import make_scenario

F, P, S = VerdictStatus.FAILED, VerdictStatus.PASSED, VerdictStatus.SKIPPED_PRECONDITION


def settings_for(tmp_path: Path) -> Settings:
    return Settings(reports_dir=str(tmp_path / "reports"), allowed_hosts="127.0.0.1")


def suite_with(tmp_path: Path, cat_dir: Path, stop: dict[str, Any] | None) -> Path:
    """A copy of the demo catalogue whose smoke suite carries ``stop``."""
    import shutil

    root = tmp_path / "cat"
    shutil.copytree(cat_dir, root)
    suite = yaml.safe_load((root / "suites" / "smoke.yaml").read_text("utf-8"))
    if stop is not None:
        suite["stop"] = stop
    path = root / "suites" / "smoke.yaml"
    path.write_text(yaml.safe_dump(suite), encoding="utf-8")
    return path


async def run(tmp_path: Path, suite: Path, bug: str | None = None, **options: Any) -> SuiteResult:
    profile = suite.parent.parent / "profile.yaml"
    async with run_agent(bug=bug) as url:  # type: ignore[arg-type]
        return await launch(
            suite,
            RunOptions(target=url, profile=profile, repeats=options.pop("repeats", 1), **options),
            settings_for(tmp_path),
        )


def kinds(result: SuiteResult) -> dict[str, tuple[VerdictStatus, str | None]]:
    return {t.test_id: (t.status, t.skip_kind) for t in result.tests}


def assert_never_safe(result: SuiteResult) -> None:
    assert result.stopped is not None
    assert not result.safe_to_release
    assert result.exit_code != 0


# --- the policy model and the flags ------------------------------------------------------


class TestPolicy:
    def test_a_flag_replaces_only_its_own_field(self) -> None:
        suite = StopPolicy(max_consecutive_failures=3, stop_on_rate_limit=True)
        merged = suite.merged(StopPolicy(stop_on_rate_limit=False))
        assert merged == StopPolicy(max_consecutive_failures=3, stop_on_rate_limit=False)

    def test_no_flags_keep_the_suite_policy(self) -> None:
        assert _stop_flags(fail_fast=None, max_wall_clock_seconds=None) is None
        suite = StopPolicy(fail_fast=True)
        assert suite.merged(None) is suite

    def test_no_fail_fast_turns_a_suites_fail_fast_off(self) -> None:
        override = _stop_flags(fail_fast=False)
        assert StopPolicy(fail_fast=True).merged(override).fail_fast is False

    def test_nonsense_limits_are_load_errors(self) -> None:
        with pytest.raises(ValueError):
            StopPolicy(max_consecutive_failures=0)
        with pytest.raises(ValueError):
            StopPolicy(max_wall_clock_seconds=0)

    async def test_a_misspelt_policy_in_a_suite_fails_to_load(
        self, tmp_path: Path, cat_dir: Path
    ) -> None:
        suite = suite_with(tmp_path, cat_dir, {"max_consecutive_failure": 2})
        with pytest.raises(ConfigError, match="invalid suite"):
            await run(tmp_path, suite)


class TestCounting:
    """Only a pass resets the run of failures: a skip says nothing about recovery."""

    def control(self, tmp_path: Path, **stop: Any) -> _RunControl:
        policy = resolve_gate(None, preset=None, fail_on_flaky=False)
        return _RunControl(StopPolicy(**stop), FileStore(tmp_path), "r", policy, declared=True)

    def verdict(self, status: VerdictStatus) -> TestVerdict:
        return TestVerdict(
            test_id="x", name="x", criticality=Criticality.NORMAL, status=status, reason="r"
        )

    def test_a_skip_between_two_failures_does_not_reset_the_count(self, tmp_path: Path) -> None:
        control = self.control(tmp_path, max_consecutive_failures=2)
        s = make_scenario()
        control.after_scenario(s, self.verdict(F), rate_limited=False)
        control.after_scenario(s, self.verdict(S), rate_limited=False)
        assert control.stopped is None
        control.after_scenario(s, self.verdict(F), rate_limited=False)
        assert control.stopped is not None and control.stopped.by == "max_consecutive_failures"

    def test_a_pass_resets_it(self, tmp_path: Path) -> None:
        control = self.control(tmp_path, max_consecutive_failures=2)
        s = make_scenario()
        for status in (F, P, F):
            control.after_scenario(s, self.verdict(status), rate_limited=False)
        assert control.stopped is None

    def test_a_rate_limit_stops_only_when_asked(self, tmp_path: Path) -> None:
        s = make_scenario()
        off = self.control(tmp_path)
        off.after_scenario(s, self.verdict(P), rate_limited=True)
        assert off.stopped is None
        on = self.control(tmp_path, stop_on_rate_limit=True)
        on.after_scenario(s, self.verdict(P), rate_limited=True)
        assert on.stopped is not None and on.stopped.by == "stop_on_rate_limit"
        assert "t-001" in on.stopped.detail


# --- stop policies against the reference agent ---------------------------------------


class TestStopPolicies:
    async def test_max_consecutive_failures_from_the_suite(
        self, tmp_path: Path, cat_dir: Path
    ) -> None:
        suite = suite_with(tmp_path, cat_dir, {"max_consecutive_failures": 2})
        result = await run(tmp_path, suite, bug="skip-approval")
        assert kinds(result) == {"rf-001": (F, None), "rf-002": (F, None), "rf-003": (S, "stopped")}
        assert result.stopped is not None and result.stopped.by == "max_consecutive_failures"
        assert result.stopped.after == "rf-002"
        assert result.stop == StopPolicy(max_consecutive_failures=2)
        assert result.exit_code == EXIT_FAIL
        assert_never_safe(result)
        rest = next(t for t in result.tests if t.test_id == "rf-003")
        assert rest.reason is not None and "max_consecutive_failures" in rest.reason

    async def test_a_critical_failure_stops_the_run(self, tmp_path: Path, cat_dir: Path) -> None:
        suite = suite_with(tmp_path, cat_dir, None)
        result = await run(
            tmp_path, suite, bug="skip-approval", stop=StopPolicy(stop_on_critical_failure=True)
        )
        assert kinds(result) == {
            "rf-001": (F, None),
            "rf-002": (S, "stopped"),
            "rf-003": (S, "stopped"),
        }
        assert result.stopped is not None and result.stopped.by == "stop_on_critical_failure"
        assert_never_safe(result)

    async def test_a_flag_switches_off_the_suites_policy(
        self, tmp_path: Path, cat_dir: Path
    ) -> None:
        suite = suite_with(tmp_path, cat_dir, {"stop_on_critical_failure": True})
        result = await run(
            tmp_path, suite, bug="skip-approval", stop=StopPolicy(stop_on_critical_failure=False)
        )
        assert result.stopped is None
        assert all(t.status is not S for t in result.tests)

    async def test_the_wall_clock_budget_is_checked_between_scenarios(
        self, tmp_path: Path, cat_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first scenario starts on time; by the second the budget is spent."""
        ticks = iter([0.0, 0.0])  # the run starts; the first scenario is checked
        clock = SimpleNamespace(monotonic=lambda: next(ticks, 1000.0))
        monkeypatch.setattr("opseval.runner.time", clock)
        suite = suite_with(tmp_path, cat_dir, {"max_wall_clock_seconds": 60})
        result = await run(tmp_path, suite)
        assert kinds(result) == {
            "rf-001": (P, None),
            "rf-002": (S, "stopped"),
            "rf-003": (S, "stopped"),
        }
        assert result.stopped is not None and result.stopped.by == "max_wall_clock_seconds"
        assert result.exit_code == EXIT_NOT_JUDGED
        assert_never_safe(result)

    async def test_a_rate_limited_attempt_stops_the_run(
        self, tmp_path: Path, cat_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The runner reads the driver's note, never the error text."""
        from opseval.driver import RATE_LIMITED_NOTE, Driver

        real = Driver.run

        async def rate_limited(self: Driver) -> Any:
            record = await real(self)
            return record.model_copy(update={"notes": (*record.notes, RATE_LIMITED_NOTE)})

        monkeypatch.setattr(Driver, "run", rate_limited)
        suite = suite_with(tmp_path, cat_dir, {"stop_on_rate_limit": True})
        result = await run(tmp_path, suite)
        assert kinds(result)["rf-002"] == (S, "stopped")
        assert result.stopped is not None and result.stopped.by == "stop_on_rate_limit"


# --- cancel requests ---------------------------------------------------------------


class TestCancel:
    async def test_a_cancel_before_the_start_runs_nothing_and_says_so(
        self, tmp_path: Path, cat_dir: Path
    ) -> None:
        store = FileStore(tmp_path / "reports")
        store.request_cancel("r-cancelled", by="test")
        result = await run(tmp_path, suite_with(tmp_path, cat_dir, None), run_id="r-cancelled")
        assert {k for k in kinds(result).values()} == {(S, "cancelled")}
        assert result.stopped is not None and result.stopped.by == "cancelled"
        assert result.gate.status == "block", "a suite that judged nothing blocks"
        assert_never_safe(result)

    async def test_a_scenario_cut_short_is_not_folded_from_part_of_its_repeats(
        self, tmp_path: Path, cat_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = FileStore.save_record

        def cancel_after_the_first(self: FileStore, record: Any) -> Path:
            path = real(self, record)
            self.request_cancel(record.run_id, by="test")
            return path

        monkeypatch.setattr(FileStore, "save_record", cancel_after_the_first)
        result = await run(tmp_path, suite_with(tmp_path, cat_dir, None), repeats=3, run_id="r-mid")
        first = result.tests[0]
        assert (first.status, first.skip_kind) == (S, "cancelled")
        assert first.reason is not None and "after 1 of 3 attempts" in first.reason
        assert first.pass_rate is None, "a pass rate from 1 of 3 repeats was never measured"
        assert {kinds(result)[t] for t in ("rf-002", "rf-003")} == {(S, "cancelled")}
        records = list((tmp_path / "reports" / "r-mid" / "records").glob("*.json"))
        assert len(records) == 1, "the attempt that ran is kept"
        assert_never_safe(result)

    async def test_a_failure_before_the_cancel_still_fails_the_run(
        self, tmp_path: Path, cat_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancelling can never launder a failure that already happened."""
        from opseval.runner import _RunControl as Control

        real = Control.after_scenario

        def cancel_after_the_first(self: Control, *args: Any, **kwargs: Any) -> None:
            real(self, *args, **kwargs)
            self.store.request_cancel(self.run_id, by="test")

        monkeypatch.setattr(Control, "after_scenario", cancel_after_the_first)
        result = await run(
            tmp_path, suite_with(tmp_path, cat_dir, None), bug="skip-approval", run_id="r-f"
        )
        assert kinds(result)["rf-001"] == (F, None)
        assert result.exit_code == EXIT_FAIL


class TestCancelCommand:
    runner = CliRunner()

    def run_folder(self, tmp_path: Path, *files: str) -> Path:
        folder = tmp_path / "reports" / "r-1"
        folder.mkdir(parents=True)
        for name in ("manifest.json", *files):
            (folder / name).write_text("{}", encoding="utf-8")
        return folder

    def test_it_records_the_request_once(self, tmp_path: Path) -> None:
        folder = self.run_folder(tmp_path)
        first = self.runner.invoke(app, ["cancel", str(folder)])
        assert first.exit_code == 0 and "Cancel requested" in first.output
        assert json.loads((folder / "cancel.json").read_text("utf-8"))["by"] == "cli"
        again = self.runner.invoke(app, ["cancel", str(folder)])
        assert again.exit_code == 0 and "already requested" in again.output

    @pytest.mark.parametrize("finished", ["result.json", "error.json"])
    def test_a_finished_run_cannot_be_cancelled(self, tmp_path: Path, finished: str) -> None:
        folder = self.run_folder(tmp_path, finished)
        res = self.runner.invoke(app, ["cancel", str(folder)])
        assert res.exit_code == 3 and not (folder / "cancel.json").exists()

    def test_a_folder_that_is_not_a_run_is_exit_3(self, tmp_path: Path) -> None:
        assert self.runner.invoke(app, ["cancel", str(tmp_path)]).exit_code == 3


# --- every output says the run stopped early ------------------------------------------


async def test_every_output_names_the_stop(tmp_path: Path, cat_dir: Path) -> None:
    suite = suite_with(tmp_path, cat_dir, {"stop_on_critical_failure": True})
    result = await run(tmp_path, suite, bug="skip-approval")
    assert "Stopped early: stop_on_critical_failure" in console_summary(result)
    assert "Stopped early" in markdown_summary(result)
    assert 'name="stopped_early"' in render_junit(result)
    report = (tmp_path / "reports" / result.run_id / "report.html").read_text("utf-8")
    assert "Stopped early" in report
