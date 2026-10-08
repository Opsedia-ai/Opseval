# SPDX-License-Identifier: Apache-2.0
"""Fold attempts into verdicts, verdicts into a release gate and an exit code (ADR-0006).

Exit codes: 0 all passed · 1 any failed · 2 not judged / inconclusive / skipped / empty ·
3 harness or configuration error (including a run where every attempt was invalidated
by the harness).
"""

from __future__ import annotations

import math
from collections import defaultdict

from opseval.models.record import RunRecord
from opseval.models.results import (
    AttemptVerdict,
    Gate,
    ScoreResult,
    ScoreStatus,
    TestVerdict,
    VerdictStatus,
)
from opseval.models.scenario import Criticality, Scenario

EXIT_PASS, EXIT_FAIL, EXIT_NOT_JUDGED, EXIT_HARNESS = 0, 1, 2, 3


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


def _unstable_scorers(valid: list[AttemptVerdict]) -> list[str]:
    outcomes: dict[str, set[ScoreStatus]] = defaultdict(set)
    for attempt in valid:
        for s in attempt.scores:
            if s.status is not ScoreStatus.INFRA_ERROR:
                outcomes[s.scorer_id].add(s.status)
    return sorted(k for k, v in outcomes.items() if len(v) > 1)


def skipped(scenario: Scenario, status: VerdictStatus, reason: str) -> TestVerdict:
    return TestVerdict(
        test_id=scenario.id,
        name=scenario.name,
        criticality=scenario.criticality,
        status=status,
        reason=reason,
    )


def release_gate(tests: list[TestVerdict], *, fail_on_flaky: bool = False) -> Gate:
    """Pass only if something was judged and nothing that gates the release failed.

    A skip is a warning for a normal scenario. A skipped critical scenario is an
    unmeasured critical scenario, and a suite where nothing was judged has proven nothing:
    both block, or the gate would say "Safe to release" about coverage it does not have
    (ADR-0029). A ``blocked`` scenario was quarantined on purpose and stays a warning.
    """
    declared = any(t.criticality is not Criticality.NORMAL for t in tests)
    blocking, warnings = [], []
    for t in tests:
        gating = t.criticality in (Criticality.BLOCKER, Criticality.CRITICAL) or not declared
        if t.status is VerdictStatus.INCONCLUSIVE:
            blocking.append(f"{t.test_id}: inconclusive")
        elif t.status is VerdictStatus.FAILED:
            if gating:
                blocking.append(f"{t.test_id}: failed ({t.criticality.value})")
            else:
                warnings.append(f"{t.test_id}: failed (normal)")
        elif t.status is VerdictStatus.SKIPPED_PRECONDITION and gating:
            blocking.append(f"{t.test_id}: skipped_precondition ({t.criticality.value})")
        elif t.status in (VerdictStatus.SKIPPED_PRECONDITION, VerdictStatus.BLOCKED):
            warnings.append(f"{t.test_id}: {t.status.value}")
        if t.flaky and fail_on_flaky:
            blocking.append(f"{t.test_id}: flaky")
        elif t.flaky:
            warnings.append(f"{t.test_id}: flaky")
    judged = [t for t in tests if t.status in (VerdictStatus.PASSED, VerdictStatus.FAILED)]
    if not tests:
        blocking.append("no scenarios were run")
    elif not judged:
        blocking.append("no scenario was judged")
    status = "block" if blocking else "pass"
    passed = sum(1 for t in tests if t.status is VerdictStatus.PASSED)
    # "Safe to release" only when everything passed: a passing gate with a normal failure or
    # a skip is honest, but it is not the same sentence (ADR-0029).
    gaps = [f"{t.test_id}: {t.status.value}" for t in tests if t.status is not VerdictStatus.PASSED]
    if blocking:
        verdict = "Release is BLOCKED by: " + "; ".join(blocking) + "."
    elif gaps:
        verdict = "No blocking issues, but not everything passed: " + "; ".join(gaps) + "."
    else:
        verdict = "Safe to release."
    summary = f"{passed}/{len(tests)} scenarios passed. {verdict}"
    return Gate(status=status, blocking=blocking, warnings=warnings, summary=summary)


def exit_code(tests: list[TestVerdict], *, load_errors: int = 0) -> int:
    if any(t.status is VerdictStatus.FAILED for t in tests):
        return EXIT_FAIL
    attempts = [a for t in tests for a in t.attempts]
    if attempts and all(a.terminal_state.is_infra for a in attempts):
        return EXIT_HARNESS
    if not tests or load_errors or any(t.status is not VerdictStatus.PASSED for t in tests):
        return EXIT_NOT_JUDGED
    return EXIT_PASS
