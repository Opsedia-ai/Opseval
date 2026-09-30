# SPDX-License-Identifier: Apache-2.0
"""``opseval rescore``: offline re-scoring of saved runs.

Re-scoring must reproduce verdicts when nothing changed (PRD G4), never touch the
original run, and keep both halves of the core rule: harness failures stay not judged
and real agent failures stay failed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from opseval.cli import app
from opseval.config import Settings
from opseval.demo.refund_agent import run_agent
from opseval.errors import ConfigError
from opseval.models.results import Axis, ScoreResult, ScoreStatus, SuiteResult, VerdictStatus
from opseval.models.scenario import ScorerSpec
from opseval.rescore import _Rescorer, rescore_run
from opseval.runner import RunOptions, launch
from opseval.scoring import REGISTRY, ScoringEngine
from opseval.scoring.judges import GoalAchievement


def settings_for(tmp_path: Path) -> Settings:
    return Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "reports"))


async def demo_run(cat_dir: Path, tmp_path: Path, bug: str | None = None) -> Path:
    settings = settings_for(tmp_path)
    async with run_agent(bug=bug) as url:  # type: ignore[arg-type]
        result = await launch(
            cat_dir / "suites" / "smoke.yaml",
            RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1),
            settings,
        )
    return Path(settings.reports_dir) / result.run_id


def digest(folder: Path) -> dict[str, str]:
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and not p.relative_to(folder).parts[0].startswith("rescore-")
    }


class TestRescore:
    async def test_same_scorers_same_verdicts_and_the_original_is_untouched(
        self, cat_dir: Path, tmp_path: Path
    ) -> None:
        run = await demo_run(cat_dir, tmp_path)
        before = digest(run)
        original = SuiteResult.model_validate_json((run / "result.json").read_text("utf-8"))

        outcome = await rescore_run(run, settings_for(tmp_path))  # the agent is gone

        assert digest(run) == before  # records, result and report unchanged
        assert outcome.out_dir.parent == run and outcome.out_dir.name.startswith("rescore-")
        assert {p.name for p in outcome.out_dir.iterdir()} == {
            "result.json",
            "report.html",
            "junit.xml",
        }
        new = outcome.result
        assert [t.status for t in new.tests] == [t.status for t in original.tests]
        assert new.exit_code == original.exit_code == 0
        assert outcome.info.of_run_id == original.run_id
        assert outcome.info.scenario_source == "snapshot"
        assert outcome.info.scorer_changes == [] and outcome.info.verdict_changes == []
        saved = SuiteResult.model_validate_json(
            (outcome.out_dir / "result.json").read_text("utf-8")
        )
        assert saved.rescore is not None and saved.run_id.startswith(original.run_id + ".rescore-")
        html = (outcome.out_dir / "report.html").read_text("utf-8")
        assert "Re-scored offline" in html and "No scorer version or judge prompt changed" in html

    async def test_runs_save_a_snapshot_of_what_was_run(
        self, cat_dir: Path, tmp_path: Path
    ) -> None:
        run = await demo_run(cat_dir, tmp_path)
        assert (run / "profile.json").is_file()
        snap = json.loads(next((run / "scenarios").glob("*.json")).read_text("utf-8"))
        assert snap["schema"] == "opseval/v1"
        assert snap["execution"]["repeats"] == 1  # the --repeats override is what ran

    async def test_harness_failures_stay_not_judged(self, cat_dir: Path, tmp_path: Path) -> None:
        run = await demo_run(cat_dir, tmp_path, bug="crash")
        outcome = await rescore_run(run, settings_for(tmp_path))
        assert outcome.result.exit_code == 3
        assert all(a.passed is None for t in outcome.result.tests for a in t.attempts)

    async def test_agent_failures_stay_failed(self, cat_dir: Path, tmp_path: Path) -> None:
        run = await demo_run(cat_dir, tmp_path, bug="skip-approval")
        outcome = await rescore_run(run, settings_for(tmp_path))
        assert outcome.result.exit_code == 1
        assert any(t.status is VerdictStatus.FAILED for t in outcome.result.tests)

    async def test_reports_changed_scorer_versions(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await demo_run(cat_dir, tmp_path)
        monkeypatch.setattr(REGISTRY["output.terminal_state"], "version", "2")
        outcome = await rescore_run(run, settings_for(tmp_path))
        changes = {(c.scorer_id, c.what, c.before, c.after) for c in outcome.info.scorer_changes}
        assert ("output.terminal_state", "version", "1", "2") in changes
        html = (outcome.out_dir / "report.html").read_text("utf-8")
        assert "output.terminal_state" in html and "version" in html

    async def test_current_catalogue_can_move_verdicts(self, cat_dir: Path, tmp_path: Path) -> None:
        run = await demo_run(cat_dir, tmp_path)
        edited = tmp_path / "catalogue"
        shutil.copytree(cat_dir, edited)
        for path in (edited / "scenarios").glob("*.yaml"):
            data = yaml.safe_load(path.read_text("utf-8"))
            tools = data.setdefault("expectations", {}).setdefault("expected_tools", {})
            tools["must_not_call"] = ["lookup_order"]  # the agent always looks the order up
            if data.get("scorers") != "default":
                data["scorers"] = [*data.get("scorers", []), {"id": "tools.forbidden_not_called"}]
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

        outcome = await rescore_run(
            run,
            settings_for(tmp_path),
            catalogue=edited / "suites" / "smoke.yaml",
            profile=edited / "profile.yaml",
        )
        assert outcome.info.scenario_source == "catalogue"
        moved = {(v.before, v.after) for v in outcome.info.verdict_changes}
        assert (VerdictStatus.PASSED, VerdictStatus.FAILED) in moved
        assert outcome.result.exit_code == 1

    async def test_bad_inputs_are_configuration_errors(self, cat_dir: Path, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not a finished run"):
            await rescore_run(tmp_path, settings_for(tmp_path))
        run = await demo_run(cat_dir, tmp_path)
        with pytest.raises(ConfigError, match="both --catalogue and --profile"):
            await rescore_run(run, settings_for(tmp_path), catalogue=cat_dir)

        no_snapshot = tmp_path / "old" / run.name
        shutil.copytree(run, no_snapshot)
        (no_snapshot / "profile.json").unlink()
        with pytest.raises(ConfigError, match="no saved scenarios"):
            await rescore_run(no_snapshot, settings_for(tmp_path))

        broken = tmp_path / "broken"
        shutil.copytree(cat_dir, broken)
        (broken / "scenarios" / "zz-bad.yaml").write_text("id: bad\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="catalogue has problems"):
            await rescore_run(
                run, settings_for(tmp_path), catalogue=broken, profile=broken / "profile.yaml"
            )

        next(p for p in (run / "records").glob("*.json")).unlink()
        with pytest.raises(ConfigError, match="record missing"):
            await rescore_run(run, settings_for(tmp_path))


def judge_score(prompt_hash: str) -> ScoreResult:
    return ScoreResult(
        scorer_id="judge.goal_achievement",
        axis=Axis.OUTPUT,
        status=ScoreStatus.PASSED,
        score=1.0,
        threshold=0.7,
        details={"prompt_hash": prompt_hash, "model": "m"},
        scorer_version="1",
    )


class TestJudgesWithoutAnLLM:
    def test_reused_only_when_the_prompt_is_unchanged(self) -> None:
        rescorer = _Rescorer(ScoringEngine(), judge_configured=False)
        spec = ScorerSpec(id="judge.goal_achievement", threshold=0.7)
        fresh = ScoreResult(
            scorer_id="judge.goal_achievement",
            axis=Axis.OUTPUT,
            status=ScoreStatus.INFRA_ERROR,
            score=None,
            threshold=0.7,
            reason="JUDGE_UNAVAILABLE: x",
        )
        same = rescorer._reuse(fresh, judge_score(GoalAchievement.prompt_hash()), spec)
        assert same.status is ScoreStatus.PASSED and same.details["reused"] is True
        assert rescorer.reused == 1

        changed = rescorer._reuse(fresh, judge_score("old-hash"), spec)
        assert changed.status is ScoreStatus.INFRA_ERROR
        assert "prompt changed" in (changed.reason or "")
        never = rescorer._reuse(fresh, None, spec)
        assert never.status is ScoreStatus.INFRA_ERROR

    def test_deterministic_scores_are_always_recomputed(self) -> None:
        rescorer = _Rescorer(ScoringEngine(), judge_configured=False)
        fresh = ScoreResult(
            scorer_id="output.terminal_state",
            axis=Axis.OUTPUT,
            status=ScoreStatus.FAILED,
            score=0.0,
            threshold=1.0,
        )
        spec = ScorerSpec(id="output.terminal_state")
        old = fresh.model_copy(update={"status": ScoreStatus.PASSED, "score": 1.0})
        assert rescorer._reuse(fresh, old, spec) is fresh


class TestCli:
    def test_rescore_command(self, cat_dir: Path, tmp_path: Path) -> None:
        run = asyncio.run(demo_run(cat_dir, tmp_path))
        junit = tmp_path / "out" / "junit.xml"
        res = CliRunner().invoke(app, ["rescore", str(run), "--junit", str(junit)])
        assert res.exit_code == 0, res.output
        assert (
            "Re-scored" in res.output and "no scorer version or judge prompt changed" in res.output
        )
        assert junit.is_file()

    def test_missing_run_is_exit_3(self, tmp_path: Path) -> None:
        res = CliRunner().invoke(app, ["rescore", str(tmp_path / "nope")])
        assert res.exit_code == 3
