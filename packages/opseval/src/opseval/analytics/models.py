# SPDX-License-Identifier: Apache-2.0
"""The analytics store's answers (ADR-0052, ADR-0066).

Every value says what it counted: ``measured`` calls (or attempts) had the value, out of
``of`` that should have. A value with nothing measured is ``None``, never 0, and a value that
covers only part of what it should is ``complete: false`` and drawn as partial: a floor, never
the whole bill.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Bucket = Literal["day", "week", "run"]


class Measured(BaseModel):
    """A sum over calls (or attempts) and how many of them it covers."""

    model_config = ConfigDict(extra="forbid")
    value: float | None
    """The sum of what was measured; ``None`` when nothing was (never 0). With ``of`` 0 there
    was nothing to count (no calls); with ``of`` above 0, the calls did not report it."""
    measured: int
    of: int
    complete: bool
    """Every one of ``of`` was measured. False with a value: the value is a floor."""


class Ratio(BaseModel):
    """A sum over the measured units of a denominator (attempts, passed tests, calls).

    The denominator counts only the units that were measured (``measured`` of ``of``), so a
    unit whose usage is unknown never dilutes the ratio. ``complete`` false: the numerator was
    a floor (a call in it was not priced or did not report its count), so the ratio is too."""

    model_config = ConfigDict(extra="forbid")
    value: float | None
    """``None`` when the numerator was not measured or no unit of the denominator was."""
    measured: int
    """Units of the denominator that were measured (and divided by)."""
    of: int
    """Units of the denominator there were."""
    complete: bool
    """The numerator covered every call it should have: the ratio is not a floor."""


class BucketOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    """An ISO date (day, or the Monday of a week) or a run id."""
    label: str
    start: datetime
    runs: int


class Series(BaseModel):
    """One group's values, one per bucket, in the order of the answer's ``buckets``."""

    model_config = ConfigDict(extra="forbid")
    group: str
    points: list[Measured]
    total: Measured


class UsageScope(BaseModel):
    """What an answer covered, so nothing left out is invisible."""

    model_config = ConfigDict(extra="forbid")
    runs: int
    """Finished runs that matched the filters."""
    calls: int
    """Calls counted (failed calls included, re-scoring calls included)."""
    failed_calls: int
    rescore_calls: int = 0
    """Of ``calls``, the calls the latest re-score of a run made (grouped as
    ``<role> (rescore)``). They are counted with their run, never as a run of their own."""
    attempts: int
    """Attempts in those runs."""
    attempts_without_usage: int
    """Attempts whose record has no per-call usage (written before it): not measured."""
    legacy_runs: list[str] = Field(default_factory=list)
    """Runs written before per-call usage. Their per-model totals are shown as written (a
    missing count was then added as 0); they have no per-attempt or per-scenario figures."""
    unreadable_runs: list[str] = Field(default_factory=list)
    """Runs whose records could not be read: left out entirely (no calls, no passed tests,
    no bucket), and named."""
    rescored_runs: list[str] = Field(default_factory=list)
    """Runs whose latest re-score's calls are counted."""
    unreadable_rescores: list[str] = Field(default_factory=list)
    """Runs whose latest re-score could not be read: its calls are left out, and named."""
    in_flight_excluded: bool = True
    """Runs still in flight are never counted: their usage is not final."""


class UsageFilterEcho(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite: str | None = None
    model: str | None = None
    role: str | None = None
    since: date | None = None
    until: date | None = None
    bucket: Bucket = "day"
    task: str | None = None
    language: str | None = None
    dialect: str | None = None


class FilterOptions(BaseModel):
    """What the filters can be set to: every suite, role and model in the finished runs."""

    model_config = ConfigDict(extra="forbid")
    suites: list[str]
    roles: list[str]
    models: list[str]
    server_only: list[str]
    """Filters only server mode can apply (organisation, project, task, label)."""


class RunCost(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: str
    suite: str
    started_at: datetime
    cost_usd: Measured
    passed_tests: int
    """Scenarios whose verdict is ``passed``."""
    cost_per_passed_test: Ratio
    """Over the passed tests whose attempts were measured (all of them in a run written
    before per-call usage: its totals cover the whole run)."""
    per_call: bool
    """False: written before per-call usage (totals as written)."""


class ScenarioCost(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str
    attempts: int
    attempts_measured: int
    """Attempts with per-call usage; the others are not measured."""
    cost_usd: Measured
    cost_per_attempt: Ratio
    """Over the measured attempts."""
    passed_runs: int
    """Runs in which this scenario passed and every attempt of it was measured."""
    cost_per_passed_test: Ratio
    """Over ``passed_runs`` (``of``: every run in which it passed)."""


class CostAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: UsageFilterEcho
    options: FilterOptions
    scope: UsageScope
    buckets: list[BucketOut]
    by_role: list[Series]
    by_model: list[Series]
    total_usd: Measured
    cost_per_passed_test: Ratio
    passed_tests: int
    runs: list[RunCost]
    scenarios: list[ScenarioCost]
    sources: dict[str, int]
    """Priced calls by where the cost came from (``provider``, ``price_table``, ``unknown``)."""


TokenKind = Literal["input", "cached_input", "output", "reasoning"]


class TokenTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Measured
    """All input tokens, cached included."""
    cached_input: Measured
    output: Measured
    """All output tokens, reasoning included."""
    reasoning: Measured
    total: Measured
    """Input plus output, over the calls that reported both."""


class ScenarioTokens(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str
    attempts: int
    attempts_measured: int
    tokens: Measured
    tokens_per_attempt: Ratio


class TokensAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: UsageFilterEcho
    options: FilterOptions
    scope: UsageScope
    buckets: list[BucketOut]
    by_kind: list[Series]
    """Stack segments: ``input`` is input not reported as cached, ``cached_input`` the cached
    part, ``output`` is output not reported as reasoning, ``reasoning`` the reasoning part.
    They add up to every token counted."""
    by_model: list[Series]
    """Input plus output tokens per model, over the calls that reported both."""
    totals: TokenTotals
    reasoning_share: Ratio
    """Reasoning tokens over output tokens, over the calls that reported both."""
    tokens_per_attempt: Ratio
    """Mean tokens per attempt, over the attempts whose every call reported its tokens."""
    scenarios: list[ScenarioTokens]


# --- D2: latency, quality, voice and usage (ADR-0066) -------------------------------------

PERCENTILE_RULE = "nearest-rank"
"""How every percentile is computed, by every store: the value at rank ``ceil(p * n)`` of the
sorted values (Postgres ``percentile_disc``). Never interpolated: a p95 is a value that was
measured."""


class Percentiles(BaseModel):
    """Percentiles of measured values (nearest rank). ``n`` values were counted; ``unmeasured``
    units had no value (not measured, never 0) and are left out of ``n``. With ``n`` 0 every
    figure is ``None``."""

    model_config = ConfigDict(extra="forbid")
    n: int
    unmeasured: int
    p50: float | None
    p95: float | None
    p99: float | None
    mean: float | None
    unanswered: int = 0
    """Agent turns that got no reply before the next turn or the end (a timeout on the agent):
    counted apart, never a number in the percentiles. Always 0 for a metric that is not about
    replies."""


class PercentileSeries(BaseModel):
    """One group's percentiles, one per bucket in the answer's ``buckets`` order."""

    model_config = ConfigDict(extra="forbid")
    group: str
    points: list[Percentiles]
    total: Percentiles


class Rate(BaseModel):
    """A pass rate over judged results only. ``value`` is ``passed / judged``; ``None`` when
    nothing was judged (never 0%). ``not_judged`` results (an outage, a skip, an
    ``infra_error`` score) are counted apart, never in the denominator: a harness outage must
    not pull a pass rate down (ADR-0052)."""

    model_config = ConfigDict(extra="forbid")
    value: float | None
    passed: int
    judged: int
    not_judged: int


class DashboardOptions(BaseModel):
    """What the D2 dashboards' filters can be set to, from the finished runs in scope."""

    model_config = ConfigDict(extra="forbid")
    suites: list[str]
    tasks: list[str]
    """Task slugs in scope (server mode); empty in local mode."""
    languages: list[str]
    dialects: list[str]
    server_only: list[str]
    """Filters only server mode can apply."""
    not_recorded: list[str]
    """Filters no store can apply yet, because runs do not record them (``label``)."""


class DashboardScope(BaseModel):
    """What a D2 answer covered, so nothing left out is invisible."""

    model_config = ConfigDict(extra="forbid")
    runs: int
    attempts: int
    """Attempts in those runs that pass the attempt filters (language, dialect)."""
    unreadable_runs: list[str] = Field(default_factory=list)
    """Runs whose records could not be read: left out entirely, and named."""
    in_flight_excluded: bool = True


class LatencyAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: UsageFilterEcho
    options: DashboardOptions
    scope: DashboardScope
    rule: str = PERCENTILE_RULE
    buckets: list[BucketOut]
    turn_latency: PercentileSeries
    """The agent's reply time: from each message sent to the first frame back (ms). A reply
    the clocks could not time (a voice barge-in) is ``unmeasured``."""
    voice_turn_latency: PercentileSeries
    """``voice.turn_latency_ms`` (ADR-0055): the caller's end of speech to the agent's audio."""
    time_to_first_audio: PercentileSeries
    """``voice.time_to_first_audio_ms``: one per voice attempt."""
    llm_latency: list[PercentileSeries]
    """The harness's own LLM, TTS and STT calls, per role (answered calls; ms, retries
    included). A call that reported no latency is ``unmeasured``. Under a language or dialect
    filter, only the calls made for the attempts that match."""
    llm_calls_unattributed: int = 0
    """Answered calls left out of ``llm_latency`` because a language or dialect filter is set
    and they were made outside any attempt (they have no language). 0 with no such filter."""


class GateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: str
    suite: str
    started_at: datetime
    gate: str
    """``safe``, ``gaps``, ``blocked``, ``not_judged`` or ``harness_error``."""


class FlakyScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str
    name: str
    judged_runs: int
    """Runs in which it was judged (passed or failed)."""
    flaky_runs: int
    """Runs whose verdict was marked flaky."""
    flips: int
    """Changes between consecutive judged verdicts, within each suite."""


class CheckStats(BaseModel):
    """One check's scores across the attempts in scope."""

    model_config = ConfigDict(extra="forbid")
    scorer_id: str
    axis: str
    pass_rate: Rate
    """Passed over passed + failed; ``infra_error`` scores are ``not_judged``."""
    mean_score: float | None
    """The mean of the scores that have one (``infra_error`` has none)."""


class MatrixRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: str
    label: str
    suite: str


class MatrixCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: str
    status: str | None
    """``None``: the scenario was not in that run (an absence, never a result)."""
    pass_rate: float | None
    flaky: bool = False


class MatrixRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    cells: list[MatrixCell]
    flips: int


class VerdictMatrix(BaseModel):
    """The scenario x run verdict matrix of the newest ``limit`` runs, oldest first, as
    ``views.scenario_trend`` draws it."""

    model_config = ConfigDict(extra="forbid")
    runs: list[MatrixRun]
    scenarios: list[MatrixRow]


class QualityAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: UsageFilterEcho
    options: DashboardOptions
    scope: DashboardScope
    buckets: list[BucketOut]
    pass_rate: list[Rate]
    """Scenario verdicts per bucket: passed over passed + failed."""
    pass_rate_total: Rate
    gate_history: list[GateRun]
    """Every run in scope, oldest first (the language filter does not apply to a run's gate)."""
    flaky: list[FlakyScenario]
    checks: list[CheckStats]
    matrix: VerdictMatrix
    judge_agreement: None = None
    """Not measured yet: calibrations (``opseval calibrate``) are written beside a run folder
    and no store reads them. Always ``None`` until one does, never a made-up agreement."""
    judge_agreement_note: str


VOICE_METRICS: tuple[tuple[str, str], ...] = (
    ("voice.barge_in_yield_ms", "ms"),
    ("voice.talk_over_ratio", "ratio"),
    ("voice.dead_air_ms", "ms"),
    ("voice.asr_confidence", "confidence"),
    ("voice.agent_asr_wer", "wer"),
)
"""The Voice dashboard's metrics (ADR-0055 names), in the order it draws them."""


class VoiceGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str | None
    dialect: str | None
    stats: Percentiles
    degraded: int
    """Of ``stats.n``, values measured on partial evidence."""


class VoiceMetricOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    unit: str
    total: Percentiles
    degraded: int
    by_language: list[VoiceGroup]


class VoiceAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: UsageFilterEcho
    options: DashboardOptions
    scope: DashboardScope
    voice_attempts: int
    rule: str = PERCENTILE_RULE
    metrics: list[VoiceMetricOut]


class TaskUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str | None
    """``org/project`` in server mode; ``None`` in local mode (a folder has no project)."""
    task: str | None
    runs: int
    attempts: int
    voice_attempts: int
    audio_minutes: Measured
    """Speech the harness synthesised or transcribed (TTS audio out, STT audio in), over the
    answered speech calls: ``None`` with no speech call, never 0."""


class UsageCountsAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: UsageFilterEcho
    options: DashboardOptions
    scope: DashboardScope
    buckets: list[BucketOut]
    attempts: list[int]
    """Attempts per bucket (runs per bucket are on ``buckets``)."""
    audio_minutes: list[Measured]
    by_task: list[TaskUsage]
