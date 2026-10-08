# SPDX-License-Identifier: Apache-2.0
"""Gate policies decide what each outcome does to the release, never what it is (ADR-0032)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from opseval.catalogue import load_catalogue, load_profile
from opseval.demo import catalogue_dir
from opseval.errors import ConfigError
from opseval.models.gate import (
    PRESETS,
    GatePolicy,
    changes_from,
    describe,
    looser_than_standard,
    resolve,
)
from opseval.models.record import TerminalState
from opseval.models.results import (
    AttemptVerdict,
    Axis,
    ScoreResult,
    ScoreStatus,
    SuiteResult,
    TestVerdict,
    VerdictStatus,
)
from opseval.models.scenario import Criticality
from opseval.reporting import console_summary, render_html
from opseval.reporting.markdown import markdown_summary
from opseval.scoring import INVARIANTS, REGISTRY
from opseval.verdicts import (
    EXIT_FAIL,
    EXIT_HARNESS,
    EXIT_NOT_JUDGED,
    EXIT_PASS,
    exit_code,
    release_gate,
)

P, F = VerdictStatus.PASSED, VerdictStatus.FAILED
S, B, INC = VerdictStatus.SKIPPED_PRECONDITION, VerdictStatus.BLOCKED, VerdictStatus.INCONCLUSIVE
CRIT, NORM = Criticality.CRITICAL, Criticality.NORMAL


def tv(
    tid: str,
    status: VerdictStatus,
    crit: Criticality = NORM,
    *,
    kind: Any = None,
    flaky: bool = False,
    scores: list[ScoreResult] | None = None,
    infra: bool = False,
) -> TestVerdict:
    attempts = []
    if scores is not None or infra:
        attempts = [
            AttemptVerdict(
                attempt_index=0,
                passed=None if infra else True,
                terminal_state=TerminalState.TRANSPORT_FAILURE
                if infra
                else TerminalState.GOAL_REACHED,
                scores=scores or [],
                record_path="a.json",
            )
        ]
    return TestVerdict(
        test_id=tid,
        name=tid,
        criticality=crit,
        status=status,
        skip_kind=kind,
        flaky=flaky,
        attempts=attempts,
    )


def unmeasured() -> ScoreResult:
    return ScoreResult(
        scorer_id="security.x",
        axis=Axis.SECURITY,
        status=ScoreStatus.INFRA_ERROR,
        score=None,
        threshold=1.0,
        reason="JUDGE_DISAGREEMENT: split",
    )


def gate_with(**settings: Any) -> Any:
    return resolve(GatePolicy.model_validate(settings))


# --- resolving a policy -------------------------------------------------------------------


class TestResolve:
    def test_no_policy_is_standard(self) -> None:
        assert resolve() == PRESETS["standard"]
        assert describe(resolve()) == "standard"

    def test_suite_settings_apply_over_their_preset(self) -> None:
        p = gate_with(preset="strict", failed={"normal": "warn"}, flaky="warn")
        assert p.preset == "strict" and p.failed.normal == "warn"
        assert p.failed.critical == "block"  # the rest of the group comes from the preset
        assert sorted(changes_from(p)) == ["failed.normal=warn", "flaky=warn"]
        assert describe(p) == "strict + failed.normal=warn, flaky=warn"

    def test_a_preset_override_replaces_the_suite_policy_whole(self) -> None:
        suite = GatePolicy.model_validate({"failed": {"critical": "ignore"}})
        assert resolve(suite, preset="strict") == PRESETS["strict"]

    def test_the_old_flaky_setting_still_works(self) -> None:
        assert resolve(fail_on_flaky=True).flaky == "block"

    @pytest.mark.parametrize(
        "bad",
        [
            {"commit_guard": "block"},  # would push teams to --allow-commit
            {"inconclusive": "ignore"},  # an unjudged scenario cannot vanish
            {"load_errors": "ignore"},
            {"failed": {"normal": "maybe"}},
            {"preset": "yolo"},
            {"flakey": "warn"},  # a typo is an error, never silently dropped
        ],
    )
    def test_settings_that_would_break_the_rule_are_refused(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            GatePolicy.model_validate(bad)

    def test_looser_than_standard_names_every_weakening(self) -> None:
        assert looser_than_standard(PRESETS["standard"]) == []
        assert looser_than_standard(PRESETS["strict"]) == []
        lenient = looser_than_standard(PRESETS["lenient"])
        assert "failed.normal=ignore" in lenient and "exit_code=gate" in lenient
        assert "inconclusive=warn" in lenient


# --- deciding the gate ---------------------------------------------------------------------


class TestDecide:
    def test_standard_matches_the_gate_before_policies(self) -> None:
        gate = release_gate([tv("a", P, CRIT), tv("b", F)])
        assert gate.status == "pass" and gate.warnings == ["b: failed (normal)"]
        assert gate.policy == PRESETS["standard"] and not gate.safe

    def test_a_policy_can_make_a_normal_failure_block(self) -> None:
        gate = release_gate(
            [tv("a", P, CRIT), tv("b", F)], policy=gate_with(failed={"normal": "block"})
        )
        assert gate.status == "block" and gate.agent_blocked
        assert gate.blocking == ["b: failed (normal)"]

    def test_skips_are_told_apart_by_reason(self) -> None:
        tests = [
            tv("a", P, CRIT),
            tv("dep", S, CRIT, kind="dependency"),
            tv("guard", S, CRIT, kind="commit_guard"),
        ]
        gate = release_gate(tests, policy=PRESETS["strict"])
        assert gate.blocking == ["dep: skipped_precondition"]  # the outage blocks
        assert gate.warnings == ["guard: skipped_precondition"]  # the operator's choice warns
        assert not gate.agent_blocked  # an outage is not the agent's doing

    def test_unmeasured_degraded_and_vacuous_follow_the_policy(self) -> None:
        tests = [tv("a", P, CRIT, scores=[unmeasured()])]
        assert release_gate(tests).warnings == ["a: not measured: security.x"]
        blocked = release_gate(tests, policy=gate_with(unmeasured="block"))
        assert blocked.status == "block" and not blocked.agent_blocked
        ignored = release_gate(tests, policy=gate_with(unmeasured="ignore"))
        assert ignored.status == "pass" and ignored.safe
        assert ignored.ignored == ["a: not measured: security.x"]
        assert "Ignored by the gate policy: a: not measured" in ignored.summary

    def test_what_standard_already_ignores_is_not_repeated_in_the_summary(self) -> None:
        degraded = ScoreResult(
            scorer_id="tools.x",
            axis=Axis.TOOLS,
            status=ScoreStatus.PASSED,
            score=1.0,
            threshold=1.0,
            degraded=True,
        )
        gate = release_gate([tv("a", P, CRIT, scores=[degraded])])
        assert gate.ignored == ["a: degraded evidence: tools.x"]
        assert gate.safe and gate.summary.endswith("Safe to release.")

    def test_nothing_judged_blocks_under_every_policy(self) -> None:
        for name, policy in PRESETS.items():
            gate = release_gate([tv("a", S, kind="dependency"), tv("b", B)], policy=policy)
            assert gate.status == "block" and "no scenario was judged" in gate.blocking, name

    def test_a_flaky_pass_is_a_gap_not_safe(self) -> None:
        gate = release_gate([tv("a", P, CRIT, flaky=True)])
        assert gate.status == "pass" and not gate.safe
        assert "No blocking issues" in gate.summary

    def test_undeclared_criticality_can_be_read_as_normal(self) -> None:
        assert release_gate([tv("a", P), tv("b", F)]).status == "block"
        gate = release_gate(
            [tv("a", P), tv("b", F)], policy=gate_with(undeclared_criticality="normal")
        )
        assert gate.status == "pass" and gate.warnings == ["b: failed (normal)"]

    def test_load_errors_follow_the_policy(self) -> None:
        tests = [tv("a", P, CRIT)]
        assert release_gate(tests, load_errors=1).warnings
        gate = release_gate(tests, policy=gate_with(load_errors="block"), load_errors=1)
        assert gate.status == "block"


# --- exit codes ----------------------------------------------------------------------------


class TestExitCode:
    def test_strict_exit_codes_say_what_happened_whatever_the_gate(self) -> None:
        tests = [tv("a", P, CRIT), tv("b", F)]
        gate = release_gate(tests, policy=gate_with(failed={"normal": "ignore"}))
        assert gate.status == "pass" and exit_code(tests, gate=gate) == EXIT_FAIL
        assert not gate.safe  # exit 1 cannot sit under "Safe to release"

    def test_gate_exit_codes_follow_the_decision(self) -> None:
        lenient = PRESETS["lenient"]
        tests = [tv("a", P, CRIT), tv("b", F)]
        gate = release_gate(tests, policy=lenient)
        # The gate passes and CI exits 0, but a failed scenario is never "Safe to release".
        assert gate.status == "pass" and not gate.safe
        assert exit_code(tests, gate=gate) == EXIT_PASS
        tests = [tv("a", F, CRIT)]
        assert exit_code(tests, gate=release_gate(tests, policy=lenient)) == EXIT_FAIL
        tests = [tv("a", P, CRIT), tv("b", INC)]
        gate = release_gate(tests, policy=gate_with(preset="lenient", inconclusive="block"))
        assert exit_code(tests, gate=gate) == EXIT_NOT_JUDGED

    def test_a_harness_failure_is_exit_3_under_every_policy(self) -> None:
        tests = [tv("a", INC, CRIT, infra=True)]
        for policy in PRESETS.values():
            assert exit_code(tests, gate=release_gate(tests, policy=policy)) == EXIT_HARNESS


# --- suites, reports ---------------------------------------------------------------------


def _suite(tmp_path: Path, gate: Any) -> Path:
    cat = tmp_path / "cat"
    import shutil

    shutil.copytree(catalogue_dir(), cat)
    path = cat / "suites" / "gated.yaml"
    path.write_text(
        yaml.safe_dump({"name": "gated", "include": ["scenarios/rf-00*.yaml"], "gate": gate}),
        "utf-8",
    )
    return path


def test_a_suite_carries_its_gate_policy(tmp_path: Path) -> None:
    path = _suite(tmp_path, {"preset": "strict", "flaky": "warn"})
    cat, _ = load_catalogue(
        path,
        load_profile(path.parent.parent / "profile.yaml"),
        known_scorers=REGISTRY.keys(),
        known_invariants=set(INVARIANTS),
    )
    assert cat.gate is not None and cat.gate.preset == "strict" and cat.gate.flaky == "warn"


def test_a_bad_gate_block_fails_at_load_time(tmp_path: Path) -> None:
    path = _suite(tmp_path, {"commit_guard": "block"})
    with pytest.raises(ConfigError, match="invalid suite"):
        load_catalogue(
            path,
            load_profile(path.parent.parent / "profile.yaml"),
            known_scorers=REGISTRY.keys(),
            known_invariants=set(INVARIANTS),
        )


def _result(policy: Any) -> SuiteResult:
    tests = [tv("a", P, CRIT), tv("b", F)]
    gate = release_gate(tests, policy=policy)
    now = datetime(2026, 10, 8, tzinfo=UTC)
    return SuiteResult(
        opseval_version="0",
        run_id="r",
        suite="s",
        target="ws://x",
        profile="p",
        started_at=now,
        ended_at=now,
        tests=tests,
        gate=gate,
        exit_code=exit_code(tests, gate=gate),
    )


def test_every_output_names_the_policy_and_what_is_looser() -> None:
    lenient = _result(PRESETS["lenient"])
    for text in (console_summary(lenient), markdown_summary(lenient), render_html(lenient, [])):
        assert "lenient" in text and "failed.normal=ignore" in text
    standard = _result(PRESETS["standard"])
    md = markdown_summary(standard)
    assert "Gate policy: `standard`" in md and "Looser than standard" not in md


def test_a_lenient_green_is_never_mistaken_for_a_strict_one() -> None:
    """The point of recording the policy: the same run reads differently, and says why."""
    lenient = _result(PRESETS["lenient"])
    strict = _result(PRESETS["strict"])
    assert lenient.gate.status == "pass" and strict.gate.status == "block"
    assert lenient.exit_code == EXIT_PASS and strict.exit_code == EXIT_FAIL
    # Neither is "safe": what a policy ignores can let the gate pass, never make it clean.
    assert not lenient.safe_to_release and not strict.safe_to_release
    assert lenient.gate.policy is not None and strict.gate.policy is not None
    assert looser_than_standard(lenient.gate.policy) and not looser_than_standard(
        strict.gate.policy
    )


# --- end to end: the runner and rescore --------------------------------------------------


async def test_the_runner_gates_with_the_suite_policy_or_the_override(tmp_path: Path) -> None:
    """rf-101 needs an LLM persona and there is none here, so it is skipped (simulator).
    The suite's strict policy blocks on that skip; ``--gate lenient`` ignores it."""
    from opseval.config import Settings
    from opseval.demo.refund_agent import run_agent
    from opseval.rescore import rescore_run
    from opseval.runner import RunOptions, launch

    path = _suite(tmp_path, {"preset": "strict"})
    suite = yaml.safe_load(path.read_text())
    suite["include"] = ["scenarios/rf-001*.yaml", "scenarios/llm/rf-101*.yaml"]
    path.write_text(yaml.safe_dump(suite), "utf-8")
    settings = Settings(reports_dir=str(tmp_path / "r"), allowed_hosts="127.0.0.1")
    profile = path.parent.parent / "profile.yaml"
    async with run_agent() as url:
        strict = await launch(path, RunOptions(target=url, profile=profile, repeats=1), settings)
        lenient = await launch(
            path, RunOptions(target=url, profile=profile, repeats=1, gate="lenient"), settings
        )
    assert strict.gate.policy is not None and strict.gate.policy.preset == "strict"
    assert strict.gate.status == "block" and strict.exit_code == EXIT_NOT_JUDGED
    assert any("rf-101: skipped_precondition" in b for b in strict.gate.blocking)
    assert {t.test_id: t.skip_kind for t in strict.tests}["rf-101"] == "simulator"
    assert lenient.gate.status == "pass" and lenient.exit_code == EXIT_PASS
    assert not lenient.safe_to_release  # rf-101 did not run: passing is not "safe"
    assert lenient.gate.policy is not None and looser_than_standard(lenient.gate.policy)

    # Re-gate the strict run offline: its own policy by default, a preset on request.
    folder = tmp_path / "r" / strict.run_id
    again = await rescore_run(folder, settings)
    assert again.result.gate.policy == strict.gate.policy and again.result.gate.status == "block"
    eased = await rescore_run(folder, settings, gate="lenient")
    assert eased.result.gate.status == "pass" and eased.result.exit_code == EXIT_PASS


# --- review fixes (#30) ----------------------------------------------------------------------


def test_a_blocking_gate_never_exits_0_under_strict_exit_codes() -> None:
    """Every scenario passed, but the policy blocks on flakiness: CI must not go green."""
    tests = [tv("a", P, CRIT, flaky=True)]
    gate = release_gate(tests, policy=PRESETS["strict"])
    assert gate.status == "block" and gate.agent_blocked
    assert exit_code(tests, gate=gate) == EXIT_FAIL
    tests = [tv("a", P, CRIT, scores=[unmeasured()])]
    gate = release_gate(tests, policy=gate_with(unmeasured="block"))
    assert gate.status == "block" and exit_code(tests, gate=gate) == EXIT_NOT_JUDGED


def test_warnings_on_an_all_passing_run_are_worded_as_warnings() -> None:
    gate = release_gate([tv("a", P, CRIT, flaky=True)])
    assert "No blocking issues, but: a: flaky." in gate.summary
    assert "not everything passed" not in gate.summary


def test_an_old_commit_guard_skip_never_blocks_when_re_gated() -> None:
    """Results from before skip kinds: the reason the runner wrote still identifies it."""
    old = TestVerdict(
        test_id="g",
        name="g",
        criticality=CRIT,
        status=S,
        reason="commit guard: this scenario performs the irreversible outcome; re-run with ...",
    )
    gate = release_gate([tv("a", P, CRIT), old], policy=PRESETS["strict"])
    assert gate.status == "pass" and gate.warnings == ["g: skipped_precondition"]


def test_rescore_with_a_preset_honours_the_flaky_switch_like_run() -> None:
    assert resolve(preset="lenient", fail_on_flaky=True).flaky == "block"


async def test_fail_fast_stops_only_on_a_blocking_failure_and_names_what_did_not_run(
    tmp_path: Path,
) -> None:
    from opseval.config import Settings
    from opseval.demo.refund_agent import run_agent
    from opseval.runner import RunOptions, launch

    path = _suite(tmp_path, {"preset": "standard"})
    profile = path.parent.parent / "profile.yaml"
    settings = Settings(reports_dir=str(tmp_path / "r"), allowed_hosts="127.0.0.1")
    async with run_agent(bug="skip-approval") as url:
        # rf-001 is critical and fails first: the standard gate blocks, so the run stops,
        # and the scenarios it did not reach are recorded rather than dropped.
        stopped = await launch(
            path, RunOptions(target=url, profile=profile, repeats=1, fail_fast=True), settings
        )
    kinds = {t.test_id: (t.status, t.skip_kind) for t in stopped.tests}
    assert kinds["rf-001"][0] is F
    assert kinds["rf-002"] == (S, "fail_fast") and kinds["rf-003"] == (S, "fail_fast")
    assert stopped.gate.status == "block" and stopped.exit_code == EXIT_FAIL

    async with run_agent(bug="skip-approval") as url:
        # Lenient ignores nothing critical either, so it stops in the same place...
        lenient = await launch(
            path,
            RunOptions(target=url, profile=profile, repeats=1, fail_fast=True, gate="lenient"),
            settings,
        )
    assert {t.test_id for t in lenient.tests} == {"rf-001", "rf-002", "rf-003"}
    assert lenient.gate.status == "block" and not lenient.safe_to_release


def test_a_simulator_outage_is_tagged_simulator_not_dependency() -> None:
    from opseval.models.preflight import HealthCheck, PreflightReport
    from opseval.runner import _unmet

    scenario = next(
        sc
        for sc in load_catalogue(
            catalogue_dir() / "scenarios",
            load_profile(catalogue_dir() / "profile.yaml"),
            known_scorers=REGISTRY.keys(),
            known_invariants=set(INVARIANTS),
        )[0].scenarios
        if sc.id == "rf-101"
    )
    health = PreflightReport(
        checks=[HealthCheck(name="simulator", status="unavailable", detail="down")]
    )
    found = _unmet(scenario, health, no_simulator=False)
    assert found is not None and found[1] == "simulator"


# --- the flat form the console edits (#33) --------------------------------------------------


def test_the_console_settings_are_exactly_the_model_fields() -> None:
    """SETTINGS carries labels by hand; this keeps it from drifting from the model."""
    from opseval.models.gate import SETTINGS, flatten

    assert [key for key, _, _ in SETTINGS] == list(flatten(PRESETS["standard"]))


def test_from_flat_is_canonical() -> None:
    from opseval.models.gate import from_flat, overrides, to_yaml_block

    # A setting equal to the preset is no change, so it is not stored.
    p = from_flat("strict", {"flaky": "block", "degraded": "block"})
    assert overrides(p) == {"degraded": "block"}
    # The plain standard gate needs no block in the suite file.
    assert to_yaml_block(from_flat("standard", {"failed.normal": "warn"})) is None
    assert to_yaml_block(from_flat("lenient", {})) == {"preset": "lenient"}
    with pytest.raises(ValueError, match="unknown gate setting"):
        from_flat("standard", {"flakey": "warn"})
