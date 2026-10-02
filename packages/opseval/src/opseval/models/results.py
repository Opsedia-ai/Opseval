# SPDX-License-Identifier: Apache-2.0
"""Scores, verdicts and suite results.

Contract (ADR-0006): every score is passed, failed or infra_error, and ``score`` is
``None`` exactly when the status is ``infra_error``. Unmeasured is never zero.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opseval.models.preflight import PreflightReport
from opseval.models.record import TerminalState
from opseval.models.scenario import Criticality


class ScoreStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INFRA_ERROR = "infra_error"


class Axis(StrEnum):
    OUTPUT = "output"
    FLOW = "flow"
    TOOLS = "tools"
    SECURITY = "security"
    HARNESS = "harness"  # grades the simulator; never counts against the agent

    @property
    def agent_facing(self) -> bool:
        return self is not Axis.HARNESS


class ScoreResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scorer_id: str
    axis: Axis
    status: ScoreStatus
    score: float | None
    threshold: float
    weight: float = 1.0
    degraded: bool = False
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    scorer_version: str | None = None
    """Bumped when a scorer's logic changes, so re-scoring can tell scorer drift apart."""

    @model_validator(mode="after")
    def _contract(self) -> ScoreResult:
        if (self.score is None) != (self.status is ScoreStatus.INFRA_ERROR):
            raise ValueError("score must be None exactly when status is infra_error")
        if self.score is not None and not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be within [0, 1]")
        if self.status is ScoreStatus.INFRA_ERROR and not self.reason:
            raise ValueError("an infra_error score needs a reason")
        return self


class AttemptVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_index: int
    passed: bool | None  # None = invalidated (not judged)
    terminal_state: TerminalState
    scores: list[ScoreResult]
    record_path: str
    invalid_reason: str | None = None


class VerdictStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    SKIPPED_PRECONDITION = "skipped_precondition"
    BLOCKED = "blocked"


class TestVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    __test__ = False  # not a pytest test class

    test_id: str
    name: str
    criticality: Criticality
    status: VerdictStatus
    reason: str | None = None
    pass_rate: float | None = None
    pass_hat_k: float | None = None
    flaky: bool = False
    unstable_scorers: list[str] = Field(default_factory=list)
    attempts: list[AttemptVerdict] = Field(default_factory=list)

    @property
    def degraded_scorers(self) -> list[str]:
        """Scorers that produced a score on weaker evidence than they wanted.

        A degraded score still counts toward the verdict, so it has to be visible
        wherever the verdict is: a reader must not mistake it for a full measurement.
        """
        return sorted({s.scorer_id for a in self.attempts for s in a.scores if s.degraded})


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    rate_limited_calls: int = 0
    cost_usd: float | None = None  # None = no price known, never 0


class Gate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str  # "pass" | "block"
    blocking: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""


class ScorerChange(BaseModel):
    """A scorer whose logic (version) or judge prompt (hash) differs from the original run."""

    model_config = ConfigDict(extra="forbid")
    scorer_id: str
    what: str  # "version" | "prompt_hash" | "model"
    before: str | None
    after: str | None


class VerdictChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_id: str
    before: VerdictStatus
    after: VerdictStatus


class RescoreInfo(BaseModel):
    """Marks a result produced by ``opseval rescore`` from saved records (no agent contact)."""

    model_config = ConfigDict(extra="forbid")
    of_run_id: str
    rescored_at: datetime
    scenario_source: str  # "snapshot" (saved with the run) | "catalogue" (current files)
    scorer_changes: list[ScorerChange] = Field(default_factory=list)
    verdict_changes: list[VerdictChange] = Field(default_factory=list)
    reused_judge_scores: int = 0
    """Judge scores copied from the original run because no judge is configured and the
    prompt hash is unchanged."""


class SuiteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "opseval/v1"
    opseval_version: str
    run_id: str
    suite: str
    target: str
    profile: str
    started_at: datetime
    ended_at: datetime
    tests: list[TestVerdict]
    gate: Gate
    exit_code: int
    usage: list[ModelUsage] = Field(default_factory=list)
    config_digest: dict[str, Any] = Field(default_factory=dict)
    load_errors: list[str] = Field(default_factory=list)
    rescore: RescoreInfo | None = None
    preflight: PreflightReport | None = None
    """What the dependency checks found before the run (ADR-0020)."""

    @property
    def degraded_scorers(self) -> list[str]:
        """Every scorer that scored on degraded evidence anywhere in the run."""
        return sorted({sid for t in self.tests for sid in t.degraded_scorers})
