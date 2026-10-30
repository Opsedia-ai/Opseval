# SPDX-License-Identifier: Apache-2.0
"""Scores, verdicts and suite results.

Contract (ADR-0006): every score is passed, failed or infra_error, and ``score`` is
``None`` exactly when the status is ``infra_error``. Unmeasured is never zero.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opseval.models.gate import ResolvedGatePolicy
from opseval.models.preflight import PreflightReport
from opseval.models.record import TerminalState
from opseval.models.scenario import Criticality
from opseval.models.stop import StopPolicy
from opseval.models.usage import AttemptUsage, CostParts, UsageEvent, UsageReported


class ScoreStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INFRA_ERROR = "infra_error"


UNREAD_REPLIES = "protocol.replies_read"
"""The always-on harness check that the profile read the agent's replies (ADR-0042). Named
here, in the models, so the scorer, the engine and the verdict rules share one spelling."""

VOICE_INTELLIGIBLE = "voice.intelligible"
"""The always-on check for voice attempts (ADR-0053): agent speech with energy that no STT
could read, while the STT canary proved Opseval could hear, fails it (output axis)."""

ASR_NO_TRANSCRIPT = "ASR_NO_TRANSCRIPT"
"""The reason code of a text-reading score that could not be measured because Opseval's own
listening was proven broken on that attempt (ADR-0053)."""


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
    session_id: str | None = None
    """The agent session this attempt judged, so a verdict can be correlated back to the
    trace it came from without re-reading the record (ADR-0027)."""
    invalid_reason: str | None = None


class VerdictStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    SKIPPED_PRECONDITION = "skipped_precondition"
    BLOCKED = "blocked"


SkipKind = Literal[
    "dependency", "simulator", "commit_guard", "quarantine", "fail_fast", "stopped", "cancelled"
]
"""``stopped`` is a stop policy other than fail-fast; ``cancelled`` is an operator's cancel
request (ADR-0035)."""

StopCause = Literal[
    "fail_fast",
    "max_consecutive_failures",
    "stop_on_critical_failure",
    "stop_on_rate_limit",
    "max_wall_clock_seconds",
    "cancelled",
]


class StoppedEarly(BaseModel):
    """Why a run ended before every scenario ran. Each scenario it left out is a
    ``skipped_precondition`` verdict naming it, so a stopped run is never "safe"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    by: StopCause
    after: str | None = None
    """The last scenario that ran (None: none had finished)."""
    detail: str


class TestVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    __test__ = False  # not a pytest test class

    test_id: str
    name: str
    criticality: Criticality
    status: VerdictStatus
    reason: str | None = None
    skip_kind: SkipKind | None = None
    """Why a skipped scenario was not run, so a gate policy can treat an outage and the
    operator's own choice (the commit guard) differently (ADR-0032)."""
    pass_rate: float | None = None
    pass_hat_k: float | None = None
    flaky: bool = False
    unstable_scorers: list[str] = Field(default_factory=list)
    attempts: list[AttemptVerdict] = Field(default_factory=list)
    cut_short_of: int | None = None
    """Set when a cancel interrupted this scenario: the repeats that were planned. Its
    ``attempts`` are the ones that ran, and ``opseval rescore`` folds them by the same rule
    (``verdicts.fold_cut_short``), so a re-score can never turn them into a pass (ADR-0035)."""

    @property
    def degraded_scorers(self) -> list[str]:
        """Scorers that produced a score on weaker evidence than they wanted.

        A degraded score still counts toward the verdict, so it has to be visible
        wherever the verdict is: a reader must not mistake it for a full measurement.
        """
        return sorted({s.scorer_id for a in self.attempts for s in a.scores if s.degraded})

    @property
    def vacuous_scorers(self) -> list[str]:
        """Scorers that passed because the situation they check never arose.

        "No forbidden tool was called" is trivially true when nothing is forbidden. The
        pass is honest, but it is evidence of nothing, and it looks identical to a real
        pass unless it is named.
        """
        return sorted(
            {
                s.scorer_id
                for a in self.attempts
                for s in a.scores
                if s.details.get("vacuous") is True
            }
        )

    @property
    def unmeasured_scorers(self) -> list[str]:
        """Agent-facing scorers that returned ``infra_error`` in an attempt that was judged.

        An ``infra_error`` is not the agent's fault, so it is left out of the attempt's
        verdict, and the attempt can pass on the scorers that did run. That is honest only
        if the reader can see which checks were missing from the pass: a security judge
        whose votes disagreed must not read the same as one that voted "safe" (ADR-0030).
        """
        return sorted(
            {
                s.scorer_id
                for a in self.attempts
                if a.passed is not None
                for s in a.scores
                if s.axis.agent_facing and s.status is ScoreStatus.INFRA_ERROR
            }
        )


class ModelUsage(BaseModel):
    """One ``(role, model)``'s usage in a run, folded from its usage events (ADR-0066).

    A total covers the calls that were answered, and is ``None`` unless every one of them
    reported that count; ``reported`` says how many did. ``reported`` is ``None`` on results
    written before per-call usage: their totals are kept as written (a missing count was then
    added as 0), and are shown as such."""

    model_config = ConfigDict(extra="forbid")
    role: str
    model: str
    calls: int = 0
    input_tokens: int | None = 0
    """Tokens sent to an LLM role. ``None`` for a speech role (TTS, STT), which has no tokens,
    and when an answered call did not report it: not measured, never 0 (ADR-0054/0066)."""
    output_tokens: int | None = 0
    """Tokens an LLM role returned; ``None`` for a speech role or when not reported."""
    rate_limited_calls: int = 0
    """HTTP 429 answers received, one per response (a call retried three times counts 3)."""
    cost_usd: float | None = None
    """None = no price known (or a priced count not reported), never 0."""
    reasoning_tokens: int | None = None
    """Part of ``output_tokens`` spent reasoning, as the provider reported it
    (``completion_tokens_details.reasoning_tokens``). None: not reported, never 0."""
    cached_input_tokens: int | None = None
    """Part of ``input_tokens`` served from the provider's cache
    (``prompt_tokens_details.cached_tokens``). None: not reported, never 0."""
    failed_calls: int = 0
    """Calls that got no usable answer after their retries: nothing to count or price."""
    reported: UsageReported | None = None
    """How many answered calls reported each count. None: written before per-call usage."""
    audio_in_seconds: float | None = None
    """Audio sent to a speech-to-text role (ADR-0054). None for a role that sends none, and
    on results written before speech providers: not measured, never 0."""
    audio_out_seconds: float | None = None
    """Audio a text-to-speech role returned."""
    characters: int | None = None
    """Characters a text-to-speech role synthesised. Audio seconds and characters count only
    calls that succeeded: a failed call produced nothing to count."""
    cost_known_usd: float | None = None
    """The cost of the answered calls that had a price (layout 9): the known part, beside
    ``unpriced_calls``. ``cost_usd`` is this figure only when no call was left unpriced, so a
    run with some unpriced calls shows what is known and how much is missing, never one total
    that pretends to be complete (ADR-0066). ``None``: no call was priced."""
    unpriced_calls: int | None = None
    """Answered calls with no price (no row in force, or a priced count not reported).
    ``None`` on a row written before layout 9 whose split cannot be told from the row (some
    calls priced, some not): not recorded."""

    @model_validator(mode="after")
    def _split_cost(self) -> ModelUsage:
        """Fill the split for rows written before layout 9 where the row itself tells it: a
        total means every answered call was priced; no total and no per-count tally (written
        before per-call usage) means none was. Otherwise it stays not recorded."""
        if self.unpriced_calls is None:
            answered = self.calls - self.failed_calls
            if self.cost_usd is not None:
                self.cost_known_usd, self.unpriced_calls = self.cost_usd, 0
            elif self.reported is None or self.reported.cost == 0:
                self.unpriced_calls = (
                    self.reported.answered if self.reported is not None else answered
                )
        return self

    def cost_parts(self) -> CostParts | None:
        """The row's cost split (:class:`~opseval.models.usage.CostParts`), or ``None`` when it
        was not recorded."""
        if self.unpriced_calls is None:
            return None
        answered = self.reported.answered if self.reported is not None else (
            self.calls - self.failed_calls
        )
        return CostParts(
            known_usd=self.cost_known_usd,
            priced=max(answered - self.unpriced_calls, 0),
            unpriced=self.unpriced_calls,
        )


class Gate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pass", "block"]
    blocking: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""
    ignored: list[str] = Field(default_factory=list)
    """What the policy chose to ignore. Listed, so "ignored" is never "not there"."""
    agent_blocked: bool = False
    """At least one blocking reason is agent behaviour (a failure or flakiness), rather than
    something that could not be judged."""
    safe: bool | None = None
    """True when nothing blocks, nothing warns, and (under ``exit_code: strict``) every
    scenario passed. ``None`` on results written before gate policies (ADR-0032)."""
    policy: ResolvedGatePolicy | None = None
    """The policy this gate was decided with. ``None`` on results written before it."""


class ScorerChange(BaseModel):
    """A scorer whose logic (version) or judge prompt (hash) differs from the original run."""

    model_config = ConfigDict(extra="forbid")
    scorer_id: str
    what: str  # "version" | "prompt_hash" | "model" | "added" | "removed"
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


class SinkOutcome(BaseModel):
    """Whether a finished run reached a sink (ADR-0027).

    Recorded on the result so a publish that failed is visible after the fact, not only
    in whatever console scrollback the operator happened to keep.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    sink: str
    published: bool
    detail: str
    """What was sent, or why it was not."""


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
    usage_events: list[UsageEvent] = Field(default_factory=list)
    """Calls made outside any attempt (ADR-0066). An attempt's own calls live in its record."""
    attempt_usage: list[AttemptUsage] = Field(default_factory=list)
    """A re-score's calls, per attempt it re-graded (ADR-0066). Empty on a run, whose attempts
    keep their calls in their records."""
    config_digest: dict[str, Any] = Field(default_factory=dict)
    load_errors: list[str] = Field(default_factory=list)
    rescore: RescoreInfo | None = None
    preflight: PreflightReport | None = None
    """What the dependency checks found before the run (ADR-0020)."""
    sinks: list[SinkOutcome] = Field(default_factory=list)
    """Where this run was published, and anything that failed (ADR-0027)."""
    stop: StopPolicy | None = None
    """The stop policy in force, when one was (ADR-0035)."""
    stopped: StoppedEarly | None = None
    """Set when a stop policy or a cancel request ended the run early."""

    @property
    def failed_sinks(self) -> list[str]:
        return [s.sink for s in self.sinks if not s.published]

    @property
    def degraded_scorers(self) -> list[str]:
        """Every scorer that scored on degraded evidence anywhere in the run."""
        return sorted({sid for t in self.tests for sid in t.degraded_scorers})

    @property
    def vacuous_scorers(self) -> list[str]:
        """Every scorer that passed vacuously anywhere in the run."""
        return sorted({sid for t in self.tests for sid in t.vacuous_scorers})

    @property
    def unmeasured_scorers(self) -> list[str]:
        """Every agent-facing scorer left out of a judged attempt by an ``infra_error``."""
        return sorted({sid for t in self.tests for sid in t.unmeasured_scorers})

    @property
    def safe_to_release(self) -> bool:
        """The one condition under which an output may say "Safe to release" (ADR-0030).

        The gate passed, every scenario passed (exit 0, so nothing failed, was skipped or
        failed to load), and every agent-facing check produced a score.
        """
        if self.gate.safe is not None:  # decided under a gate policy (ADR-0032)
            return self.gate.status == "pass" and self.gate.safe
        return self.gate.status == "pass" and self.exit_code == 0 and not self.unmeasured_scorers
