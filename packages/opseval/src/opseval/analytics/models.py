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
