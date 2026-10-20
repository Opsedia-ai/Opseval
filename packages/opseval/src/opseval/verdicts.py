# SPDX-License-Identifier: Apache-2.0
"""Fold attempts into verdicts, verdicts into a release gate and an exit code (ADR-0006).

Exit codes: 0 all passed · 1 any failed · 2 not judged / inconclusive / skipped / empty ·
3 harness or configuration error (including a run where every attempt was invalidated
by the harness).
"""

from __future__ import annotations

import math
from collections import defaultdict

from opseval.models.gate import PRESETS, ByCriticality, ResolvedGatePolicy, resolve
from opseval.models.record import RunRecord
from opseval.models.results import (
    AttemptVerdict,
    Gate,
    ScoreResult,
    ScoreStatus,
    SkipKind,
    TestVerdict,
    VerdictStatus,
)
from opseval.models.scenario import Criticality, Scenario

EXIT_PASS, EXIT_FAIL, EXIT_NOT_JUDGED, EXIT_HARNESS = 0, 1, 2, 3


TENANT_LEASE_LOST = "tenant_lease_lost"
"""Noted on a record whose tenant lease was taken by another run while the attempt held it."""


def attempt_verdict(
    record: RunRecord, scores: list[ScoreResult], record_path: str
) -> AttemptVerdict:
    """An attempt passes only if every measured agent-facing score passed.

    It is invalidated (``passed=None``) only for a fixed, short list of reasons.
    """
    reason: str | None = None
    agent_facing = [s for s in scores if s.axis.agent_facing]
    measured = [s for s in agent_facing if s.status is not ScoreStatus.INFRA_ERROR]
    persona = [s for s in scores if s.scorer_id == "judge.persona_consistency"]
    if record.terminal_state.is_infra:
        reason = f"{record.terminal_state.value}: {record.stop_reason}"
    elif TENANT_LEASE_LOST in record.notes:
        # Another run may have used the same test tenant during this attempt: shared state
        # is the harness's problem, never the agent's verdict (ADR-0041).
        reason = "tenant lease lost: the test tenant may have been shared with another run"
    elif persona and persona[0].status is ScoreStatus.FAILED:
        reason = "the simulated customer broke character"
    elif scores and all(s.status is ScoreStatus.INFRA_ERROR for s in scores):
        reason = "every scorer returned infra_error"
    elif not measured:
        reason = "no agent-facing scorer produced a score"
    passed = None if reason else all(s.status is ScoreStatus.PASSED for s in measured)
    return AttemptVerdict(
        attempt_index=record.attempt_index,
        passed=passed,
        terminal_state=record.terminal_state,
        scores=scores,
        record_path=record_path,
        session_id=record.session_id,
        invalid_reason=reason,
    )


def _pass_hat_k(n: int, c: int, k: int) -> float | None:
    """pass^k: probability that k i.i.d. attempts all pass (tau-bench definition)."""
    if n == 0 or k > n:
        return None
    return math.comb(c, k) / math.comb(n, k)


def fold_attempts(scenario: Scenario, attempts: list[AttemptVerdict]) -> TestVerdict:
    valid = [a for a in attempts if a.passed is not None]
    min_valid = math.ceil(scenario.execution.repeats / 2)
    passed = sum(1 for a in valid if a.passed)
    unstable = _unstable_scorers(valid)
    base = {
        "test_id": scenario.id,
        "name": scenario.name,
        "criticality": scenario.criticality,
        "attempts": attempts,
        "unstable_scorers": unstable,
    }
    if len(valid) < min_valid:
        reasons = sorted({a.invalid_reason or "" for a in attempts if a.passed is None})
        return TestVerdict(
            **base,
            status=VerdictStatus.INCONCLUSIVE,
            reason=f"only {len(valid)}/{len(attempts)} attempts could be judged "
            f"(need {min_valid}): {'; '.join(reasons)}",
        )
    rate = passed / len(valid)
    status = (
        VerdictStatus.PASSED
        if rate >= scenario.execution.consistency_threshold
        else VerdictStatus.FAILED
    )
    failing = sorted(
        {
            s.scorer_id
            for a in valid
            if not a.passed
            for s in a.scores
            if s.status is ScoreStatus.FAILED
        }
    )
    return TestVerdict(
        **base,
        status=status,
        reason=None if status is VerdictStatus.PASSED else f"failed checks: {', '.join(failing)}",
        pass_rate=round(rate, 4),
        pass_hat_k=_pass_hat_k(len(valid), passed, len(valid)),
        flaky=0 < passed < len(valid),
    )


def fold_cut_short(scenario: Scenario, attempts: list[AttemptVerdict]) -> TestVerdict:
    """The verdict for a scenario a cancel interrupted (ADR-0035).

    A verdict from some of the repeats would claim a pass rate nobody measured, so the
    scenario is skipped as cancelled. Unless it has already failed for certain: if it would
    fail even had every attempt it did not run passed, that is a real agent failure, and it
    is reported as one rather than hidden as "not run".
    """
    planned = scenario.execution.repeats
    valid = [a for a in attempts if a.passed is not None]
    passed = sum(1 for a in valid if a.passed)
    remaining = planned - len(attempts)
    best = (passed + remaining) / (len(valid) + remaining) if valid or remaining else 0.0
    failed = len(valid) - passed
    if failed and best < scenario.execution.consistency_threshold:
        folded = fold_attempts(scenario, attempts)
        checks = folded.reason if folded.status is VerdictStatus.FAILED else None
        return folded.model_copy(
            update={
                "cut_short_of": planned,
                "status": VerdictStatus.FAILED,
                "reason": f"cancelled after {len(attempts)} of {planned} attempts, but already "
                f"failed: {failed} failed attempt(s) cannot reach the "
                f"{scenario.execution.consistency_threshold:g} pass threshold"
                + (f" ({checks})" if checks else ""),
            }
        )
    note = f", {failed} failed: not enough to decide" if failed else ""
    return skipped(
        scenario,
        VerdictStatus.SKIPPED_PRECONDITION,
        f"cancelled after {len(attempts)} of {planned} attempts{note} (their records are kept)",
        "cancelled",
    ).model_copy(update={"attempts": attempts, "cut_short_of": planned})


def _unstable_scorers(valid: list[AttemptVerdict]) -> list[str]:
    outcomes: dict[str, set[ScoreStatus]] = defaultdict(set)
    for attempt in valid:
        for s in attempt.scores:
            if s.status is not ScoreStatus.INFRA_ERROR:
                outcomes[s.scorer_id].add(s.status)
    return sorted(k for k, v in outcomes.items() if len(v) > 1)


def skipped(
    scenario: Scenario, status: VerdictStatus, reason: str, kind: SkipKind | None = None
) -> TestVerdict:
    return TestVerdict(
        test_id=scenario.id,
        name=scenario.name,
        criticality=scenario.criticality,
        status=status,
        reason=reason,
        skip_kind=kind,
    )


class _Decision:
    """Collects what a policy does with each finding: block, warn or ignore."""

    def __init__(self) -> None:
        self.blocking: list[str] = []
        self.warnings: list[str] = []
        self.ignored: list[str] = []
        self.ignored_beyond_standard: list[str] = []
        self.agent_blocked = False

    def apply(self, action: str, standard: str, finding: str, *, agent: bool = False) -> None:
        """``standard`` is what the standard preset does with the same finding, so the
        summary can name what *this* policy chose to ignore, without repeating what every
        run ignores (degraded and vacuous scores have banners of their own)."""
        if action == "block":
            self.blocking.append(finding)
            self.agent_blocked |= agent
        elif action == "warn":
            self.warnings.append(finding)
        else:
            self.ignored.append(finding)
            if standard != "ignore":
                self.ignored_beyond_standard.append(finding)


def _at(group: ByCriticality, level: str) -> str:
    return str(getattr(group, level))


def release_gate(
    tests: list[TestVerdict],
    *,
    policy: ResolvedGatePolicy | None = None,
    load_errors: int = 0,
) -> Gate:
    """Decide the release under a gate policy (ADR-0032; ``standard`` by default).

    What each outcome *is* is fixed; the policy decides whether it blocks, warns or is
    ignored. Two things are not up to the policy: a suite where nothing was judged always
    blocks, and "Safe to release" is said only when nothing blocks or warns and every
    scenario passed and loaded, whatever the policy ignored.
    """
    policy = policy or resolve()
    std = PRESETS["standard"]
    declared = any(t.criticality is not Criticality.NORMAL for t in tests)
    d = _Decision()
    for t in tests:
        if declared or policy.undeclared_criticality == "normal":
            level, label = t.criticality.value, t.criticality.value
        else:
            level, label = "critical", "no criticality declared"
        tid = t.test_id
        if t.status is VerdictStatus.INCONCLUSIVE:
            d.apply(policy.inconclusive, std.inconclusive, f"{tid}: inconclusive")
        elif t.status is VerdictStatus.FAILED:
            d.apply(
                _at(policy.failed, level),
                _at(std.failed, level),
                f"{tid}: failed ({label})",
                agent=True,
            )
        elif t.status is VerdictStatus.BLOCKED:
            d.apply(policy.quarantined, std.quarantined, f"{tid}: blocked")
        elif t.status is VerdictStatus.SKIPPED_PRECONDITION:
            action: str
            standard: str
            if skip_kind(t) == "commit_guard":
                action, standard = policy.commit_guard, std.commit_guard
            else:
                action, standard = _at(policy.skipped, level), _at(std.skipped, level)
            d.apply(action, standard, f"{tid}: skipped_precondition")
        if t.flaky:
            d.apply(policy.flaky, std.flaky, f"{tid}: flaky", agent=True)
        for kind, ids, action, standard in (
            ("not measured", t.unmeasured_scorers, policy.unmeasured, std.unmeasured),
            ("degraded evidence", t.degraded_scorers, policy.degraded, std.degraded),
            ("passed vacuously", t.vacuous_scorers, policy.vacuous, std.vacuous),
        ):
            if ids:
                d.apply(action, standard, f"{tid}: {kind}: {', '.join(ids)}")
    if load_errors:
        d.apply(
            policy.load_errors,
            std.load_errors,
            f"{load_errors} scenario file(s) could not be loaded",
        )
    judged = [t for t in tests if t.status in (VerdictStatus.PASSED, VerdictStatus.FAILED)]
    if not tests:
        d.blocking.append("no scenarios were run")  # not a policy choice
    elif not judged:
        d.blocking.append("no scenario was judged")  # not a policy choice
    everything_passed = (
        bool(tests) and not load_errors and all(t.status is VerdictStatus.PASSED for t in tests)
    )
    # "Safe to release" is never said beside a failed, skipped or unloaded scenario, under
    # any policy: what a policy ignores can let the gate pass, never make the run clean.
    safe = not d.blocking and not d.warnings and everything_passed
    passed = sum(1 for t in tests if t.status is VerdictStatus.PASSED)
    if d.blocking:
        verdict = "Release is BLOCKED by: " + "; ".join(d.blocking) + "."
    elif safe:
        verdict = "Safe to release."
    elif everything_passed:  # every scenario passed, but something warns (e.g. flaky)
        verdict = "No blocking issues, but: " + "; ".join(d.warnings) + "."
    else:
        gaps = d.warnings or [
            f"{t.test_id}: {t.status.value}" for t in tests if t.status is not VerdictStatus.PASSED
        ]
        if load_errors and not d.warnings:
            gaps.append(f"{load_errors} scenario file(s) could not be loaded")
        verdict = "No blocking issues, but not everything passed: " + "; ".join(gaps) + "."
    summary = f"{passed}/{len(tests)} scenarios passed. {verdict}"
    if d.ignored_beyond_standard:
        summary += " Ignored by the gate policy: " + "; ".join(d.ignored_beyond_standard) + "."
    return Gate(
        status="block" if d.blocking else "pass",
        blocking=d.blocking,
        warnings=d.warnings,
        ignored=d.ignored,
        agent_blocked=d.agent_blocked,
        safe=safe,
        summary=summary,
        policy=policy,
    )


def exit_code(tests: list[TestVerdict], *, load_errors: int = 0, gate: Gate | None = None) -> int:
    """0 pass · 1 fail · 2 not judged · 3 harness. Exit 3 is a fact under every policy.

    Under ``exit_code: gate`` the other codes follow the gate's decision. Under ``strict``
    they say what happened, with one floor: a gate that blocks never exits 0, or CI would
    go green on a release the gate refused (a flaky or unmeasured block on a passing run).
    """
    attempts = [a for t in tests for a in t.attempts]
    all_infra = bool(attempts) and all(a.terminal_state.is_infra for a in attempts)
    if gate is not None and gate.policy is not None and gate.policy.exit_code == "gate":
        if all_infra:
            return EXIT_HARNESS
        if gate.status != "pass":
            return EXIT_FAIL if gate.agent_blocked else EXIT_NOT_JUDGED
        return EXIT_PASS
    if any(t.status is VerdictStatus.FAILED for t in tests):
        return EXIT_FAIL
    if all_infra:
        return EXIT_HARNESS
    if not tests or load_errors or any(t.status is not VerdictStatus.PASSED for t in tests):
        return EXIT_NOT_JUDGED
    if gate is not None and gate.status != "pass":
        return EXIT_FAIL if gate.agent_blocked else EXIT_NOT_JUDGED
    return EXIT_PASS


def skip_kind(t: TestVerdict) -> SkipKind | None:
    """Why a scenario was skipped. Results from before skip kinds were recorded (ADR-0032)
    are read from the reason the runner wrote, so re-gating an old run under a policy can
    never make a commit-guard skip block."""
    if t.skip_kind is not None or t.reason is None:
        return t.skip_kind
    if t.reason.startswith("commit guard"):
        return "commit_guard"
    if t.status is VerdictStatus.BLOCKED:
        return "quarantine"
    return None


def failure_blocks(policy: ResolvedGatePolicy, criticality: Criticality, declared: bool) -> bool:
    """Whether the policy blocks a failure of this criticality (for ``--fail-fast``)."""
    level = (
        criticality.value if declared or policy.undeclared_criticality == "normal" else "critical"
    )
    return _at(policy.failed, level) == "block"
