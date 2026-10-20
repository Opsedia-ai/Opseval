# SPDX-License-Identifier: Apache-2.0
"""Scorer registry and scoring engine.

The engine converts every problem it cannot attribute to the agent (unknown scorer,
exception, missing evidence, contract violation) into ``infra_error``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import ValidationError

from opseval.errors import ConfigError
from opseval.llm import ChatClient, model_family
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import Axis, ScoreResult, ScoreStatus
from opseval.models.scenario import Scenario, ScorerSpec


@dataclass
class ScoringContext:
    scenario: Scenario
    profile: SutProfile
    judge: ChatClient | None = None
    simulator_model: str | None = None
    """The persona model, for the independence guard on HARNESS-axis judges (ADR-0016)."""
    judge_independence: str = "error"
    """``error`` | ``warn`` | ``off`` (see ``OPSEVAL_JUDGE_INDEPENDENCE``)."""
    ballot_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    """Caps judge requests in flight. A multi-vote judge fans its ballots out
    concurrently, so without a shared bound peak concurrency would be
    ``judge_concurrency x votes`` and a 3-vote judge could triple the request rate."""

    def graded_model(self, axis: Axis) -> str | None:
        """The model a judge on ``axis`` grades: the simulator for HARNESS, else the agent."""
        if axis is Axis.HARNESS:
            return self.simulator_model
        return self.profile.declared_model()


class Scorer(ABC):
    id: ClassVar[str]
    axis: ClassVar[Axis]
    requires: ClassVar[str] = "frames"  # frames | trace | llm
    description: ClassVar[str] = ""
    version: ClassVar[str] = "1"
    """Bump when the scoring logic changes (``opseval rescore`` reports the difference)."""

    @abstractmethod
    async def score(
        self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext
    ) -> ScoreResult: ...

    # helpers ------------------------------------------------------------------------
    def result(
        self, spec: ScorerSpec, score: float, *, degraded: bool = False, **details: Any
    ) -> ScoreResult:
        status = ScoreStatus.PASSED if score >= spec.threshold else ScoreStatus.FAILED
        return ScoreResult(
            scorer_id=self.id,
            axis=self.axis,
            status=status,
            score=round(score, 4),
            threshold=spec.threshold,
            weight=spec.weight,
            degraded=degraded,
            details=details,
            scorer_version=self.version,
        )

    def infra(self, spec: ScorerSpec, reason: str, **details: Any) -> ScoreResult:
        return infra_error(self.id, self.axis, spec, reason, version=self.version, **details)


def infra_error(
    scorer_id: str,
    axis: Axis,
    spec: ScorerSpec,
    reason: str,
    *,
    version: str | None = None,
    **details: Any,
) -> ScoreResult:
    return ScoreResult(
        scorer_id=scorer_id,
        axis=axis,
        status=ScoreStatus.INFRA_ERROR,
        score=None,
        threshold=spec.threshold,
        weight=spec.weight,
        reason=reason,
        details=details,
        scorer_version=version,
    )


REGISTRY: dict[str, type[Scorer]] = {}


def register(cls: type[Scorer]) -> type[Scorer]:
    if cls.id in REGISTRY:
        raise ValueError(f"duplicate scorer id {cls.id!r}")
    REGISTRY[cls.id] = cls
    return cls


INVARIANTS: dict[str, list[str]] = {
    "phase_order": ["flow.phase_order"],
    "no_errors": ["conversation.no_errors"],
    # forbidden_not_called is the broad check (must_not_call, forbidden_before and the commit
    # gate). Both invariants keep it, so no scenario loses a check it had; effective_specs
    # removes the duplicate, so one defect is never scored twice.
    "no_autonomous_commit": ["tools.forbidden_not_called"],
    "checkpoint_gating": ["tools.forbidden_not_called", "flow.checkpoint_gating"],
}

DEFAULT_PACK = [
    "conversation.no_errors",
    "flow.phase_order",
    "output.terminal_state",
    "tools.forbidden_not_called",
]

ALWAYS_ON = ["protocol.replies_read"]
"""Harness checks the engine runs on every attempt, whatever the scenario lists (ADR-0042).
A profile that maps nothing of what the agent says would otherwise let an attempt pass on
checks that read nothing, so this cannot be left to each author to remember. They are not
in :func:`effective_specs`, which stays what the scenario asked for: a harness check
measures nothing about the agent, so it must not change which dependencies a scenario
needs."""


def effective_specs(scenario: Scenario) -> list[ScorerSpec]:
    specs = (
        [ScorerSpec(id=i) for i in DEFAULT_PACK]
        if scenario.scorers == "default"
        else list(scenario.scorers)
    )
    for inv in scenario.expectations.invariants:
        specs += [ScorerSpec(id=i) for i in INVARIANTS.get(inv, [])]
    seen: set[str] = set()
    out = []
    for spec in specs:
        if spec.id not in seen:
            seen.add(spec.id)
            out.append(spec)
    return out


def independence_conflicts(
    scenarios: Iterable[Scenario],
    profile: SutProfile,
    *,
    judge_model: str | None,
    simulator_model: str | None,
) -> list[str]:
    """Judges that would grade a model from their own vendor family (ADR-0016).

    This is pure configuration, knowable before the first frame is sent, which is why it
    is enforced at launch rather than per score: an ``infra_error`` judge is dropped from
    the measured set by :func:`opseval.verdicts.attempt_verdict`, so refusing a judge
    *during* scoring would quietly lose coverage instead of reporting it.
    """
    judge_family = model_family(judge_model)
    if judge_family is None:
        return []
    agent, simulator = profile.declared_model(), simulator_model
    graded_by_axis = {
        True: (simulator, model_family(simulator)),
        False: (agent, model_family(agent)),
    }
    conflicts: dict[str, str] = {}
    for scenario in scenarios:
        if scenario.is_blocked:
            continue  # skipped before scoring, so its judges never run
        for spec in effective_specs(scenario):
            cls = REGISTRY.get(spec.id)
            if cls is None or cls.requires != "llm":
                continue
            harness = cls.axis is Axis.HARNESS
            graded, graded_family = graded_by_axis[harness]
            if graded_family is None or graded_family != judge_family:
                continue
            subject = "the simulated customer" if harness else "the agent"
            conflicts[spec.id] = (
                f"{spec.id}: the judge ({judge_model}) and {subject} ({graded}) are both "
                f"{judge_family} models, so a pass would grade that family against itself"
            )
    return [conflicts[key] for key in sorted(conflicts)]


def check_judge_independence(
    scenarios: Iterable[Scenario],
    profile: SutProfile,
    *,
    judge_model: str | None,
    simulator_model: str | None,
    mode: str,
    log: Callable[[str], None] = lambda _msg: None,
) -> list[str]:
    """Refuse, or warn about, judges that would grade their own vendor family (ADR-0016).

    Checked before any conversation happens, so a misconfigured run costs nothing. An
    unrecognised ``mode`` is treated as ``error``: the strict reading is the safe one.
    """
    if mode == "off":
        return []
    conflicts = independence_conflicts(
        scenarios, profile, judge_model=judge_model, simulator_model=simulator_model
    )
    if not conflicts:
        return []
    listed = "\n".join(f"  - {c}" for c in conflicts)
    if mode == "warn":
        log(f"warning: judge independence (scores will be marked degraded):\n{listed}")
        return conflicts
    raise ConfigError(
        "judge independence (ADR-0016):\n"
        f"{listed}\n"
        "Use a judge from another vendor, or set OPSEVAL_JUDGE_INDEPENDENCE=warn to score "
        "anyway and mark the results degraded (off disables the check)."
    )


class ScoringEngine:
    def __init__(
        self,
        judge: ChatClient | None = None,
        judge_concurrency: int = 4,
        *,
        simulator_model: str | None = None,
        judge_independence: str = "error",
    ) -> None:
        self.judge = judge
        self.simulator_model = simulator_model
        self.judge_independence = judge_independence
        self._judge_slots = asyncio.Semaphore(max(1, judge_concurrency))
        # Shared by every ballot of every judge, so the fan-out inside a multi-vote
        # judge cannot push more requests at the provider than judge_concurrency.
        self._ballot_slots = asyncio.Semaphore(max(1, judge_concurrency))

    async def score(
        self, record: RunRecord, scenario: Scenario, profile: SutProfile
    ) -> list[ScoreResult]:
        ctx = ScoringContext(
            scenario=scenario,
            profile=profile,
            judge=self.judge,
            simulator_model=self.simulator_model,
            judge_independence=self.judge_independence,
            ballot_slots=self._ballot_slots,
        )
        specs = effective_specs(scenario)
        listed = {spec.id for spec in specs}
        specs += [ScorerSpec(id=i) for i in ALWAYS_ON if i not in listed]
        return list(await asyncio.gather(*(self._one(record, spec, ctx) for spec in specs)))

    async def _one(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        cls = REGISTRY.get(spec.id)
        if cls is None:
            return infra_error(spec.id, Axis.OUTPUT, spec, f"UNKNOWN_SCORER: {spec.id}")
        scorer = cls()
        try:
            if cls.requires == "llm":
                async with self._judge_slots:
                    return await scorer.score(record, spec, ctx)
            return await scorer.score(record, spec, ctx)
        except ValidationError as exc:
            return infra_error(
                cls.id,
                cls.axis,
                spec,
                f"CONTRACT_VIOLATION: {exc.errors()[0]['msg']}",
                version=cls.version,
            )
        except Exception as exc:  # noqa: BLE001 - a scorer bug is never the agent's fault
            return infra_error(
                cls.id,
                cls.axis,
                spec,
                f"SCORER_ERROR: {type(exc).__name__}: {exc}",
                version=cls.version,
            )
