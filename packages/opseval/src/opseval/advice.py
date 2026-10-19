# SPDX-License-Identifier: Apache-2.0
"""Warnings about a valid scenario: it loads, but it will measure less than it looks (FR-AUT).

An error stops a file from loading. A warning does not, and never changes ``ok`` or an exit
code: it says what the scenario will *not* tell you, so nobody reads a green run as proof of
something it never checked. Two kinds:

- about the file itself (one repeat, a lowered pass threshold, a planted injection that no
  scorer grades, trace-backed checks with no trace source, the commit opt-in, quarantine);
- about this machine (judge checks with no judge configured, an LLM persona with no simulator).
  These are checked only when the caller says what is configured.
"""

from __future__ import annotations

from dataclasses import dataclass

from opseval.catalogue import LoadProblem
from opseval.models.profile import SutProfile
from opseval.models.scenario import Scenario
from opseval.scoring import REGISTRY, effective_specs


@dataclass(frozen=True)
class Configured:
    """What this machine has configured, for the warnings that depend on it."""

    judge: bool
    simulator: bool


def scenario_warnings(
    scenario: Scenario, where: str, profile: SutProfile, configured: Configured | None = None
) -> list[LoadProblem]:
    out: list[LoadProblem] = []

    def warn(field: str, message: str) -> None:
        out.append(LoadProblem(where, field, message))

    specs = effective_specs(scenario)
    needs = {spec.id: getattr(REGISTRY.get(spec.id), "requires", None) for spec in specs}
    if scenario.is_blocked:
        # Nothing else applies to a scenario that never runs.
        warn("tags", "tagged 'blocked': quarantined, it is reported but never run")
        return out
    if scenario.execution.repeats == 1:
        warn(
            "execution.repeats",
            "one attempt cannot show flakiness: a pass proves only that it passed once",
        )
    if scenario.execution.consistency_threshold < 1:
        warn(
            "execution.consistency_threshold",
            f"it passes when {scenario.execution.consistency_threshold:.0%} of attempts pass, "
            "so some attempts may fail and it still passes",
        )
    if scenario.conversation.allow_commit:
        warn(
            "conversation.allow_commit",
            "with --allow-commit on the run, this scenario performs the irreversible outcome: "
            "point that run at a test tenant",
        )
    if scenario.conversation.injections and "security.no_injection_takeover" not in needs:
        warn(
            "conversation.injections",
            "payloads are planted but nothing grades them: add security.no_injection_takeover",
        )
    traced = sorted(sid for sid, req in needs.items() if req == "trace")
    if traced and profile.traces is None:
        warn(
            "scorers",
            f"{', '.join(traced)} read traces, and the profile has no trace source: they will "
            "use frame evidence (degraded) or be not measured",
        )
    if configured is not None:
        judged = sorted(sid for sid, req in needs.items() if req == "llm")
        if judged and not configured.judge:
            warn(
                "scorers",
                f"no judge is configured here: {', '.join(judged)} will be not measured "
                "(set OPSEVAL_JUDGE_BASE_URL and OPSEVAL_JUDGE_MODEL)",
            )
        if not scenario.conversation.scripted_turns and not configured.simulator:
            warn(
                "conversation",
                "an LLM persona and no simulator configured here: it will be skipped "
                "(set OPSEVAL_SIMULATOR_BASE_URL and OPSEVAL_SIMULATOR_MODEL)",
            )
    return out


__all__ = ["Configured", "scenario_warnings"]
