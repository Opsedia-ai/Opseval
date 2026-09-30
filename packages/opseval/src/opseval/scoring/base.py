# SPDX-License-Identifier: Apache-2.0
"""Scorer registry and scoring engine.

The engine converts every problem it cannot attribute to the agent (unknown scorer,
exception, missing evidence, contract violation) into ``infra_error``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import ValidationError

from opseval.llm import ChatClient
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import Axis, ScoreResult, ScoreStatus
from opseval.models.scenario import Scenario, ScorerSpec


@dataclass
class ScoringContext:
    scenario: Scenario
    profile: SutProfile
    judge: ChatClient | None = None


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
    "no_autonomous_commit": ["tools.forbidden_not_called"],
    "checkpoint_gating": ["tools.forbidden_not_called"],
}

DEFAULT_PACK = [
    "conversation.no_errors",
    "flow.phase_order",
    "output.terminal_state",
    "tools.forbidden_not_called",
]


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


class ScoringEngine:
    def __init__(self, judge: ChatClient | None = None, judge_concurrency: int = 4) -> None:
        self.judge = judge
        self._judge_slots = asyncio.Semaphore(max(1, judge_concurrency))

    async def score(
        self, record: RunRecord, scenario: Scenario, profile: SutProfile
    ) -> list[ScoreResult]:
        ctx = ScoringContext(scenario=scenario, profile=profile, judge=self.judge)
        specs = effective_specs(scenario)
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
