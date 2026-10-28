# SPDX-License-Identifier: Apache-2.0
"""``FileAnalytics``: the dashboards' aggregates from the run folders (local mode, ADR-0052).

Usage comes from the usage events in each attempt's record and the calls a run made outside
any attempt (ADR-0066). Costs are recomputed from the counts with the price in force when each
call was made, so a price added later never changes an earlier run; a provider-reported cost
is kept. A run written before per-call usage contributes its per-model totals as written and
is named in ``scope.legacy_runs``; it has no per-attempt figures.

Every value carries how many calls (or attempts) it covers. Nothing measured is ``None``,
never 0; a value covering part of what it should is marked incomplete.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from opseval.analytics.base import SERVER_ONLY_FILTERS, UnsupportedFilter, UsageFilter
from opseval.analytics.models import (
    BucketOut,
    CostAnswer,
    FilterOptions,
    Measured,
    Ratio,
    RunCost,
    ScenarioCost,
    ScenarioTokens,
    Series,
    TokensAnswer,
    TokenTotals,
    UsageScope,
)
from opseval.models.results import ModelUsage, SuiteResult, VerdictStatus
from opseval.models.usage import PriceTable, UsageEvent
from opseval.pricing import repriced
from opseval.storage import PRE_USAGE_LAYOUTS, FileStore

AttemptKey = tuple[str, int]


@dataclass
class Acc:
    """A running sum that remembers how much of it was measured."""

    total: float = 0.0
    measured: int = 0
    of: int = 0

    def add(self, value: float | None, of: int = 1, measured: int | None = None) -> None:
        self.of += of
        if value is not None:
            self.total += value
            self.measured += of if measured is None else measured

    def out(self, digits: int | None = None) -> Measured:
        value: float | None = None
        if self.measured:
            value = round(self.total, digits) if digits is not None else self.total
        return Measured(
            value=value,
            measured=self.measured,
            of=self.of,
            complete=self.measured == self.of,
        )


@dataclass(frozen=True)
class RunUsage:
    """One finished run's usage, as read from its folder (cached per result file)."""

    result: SuiteResult
    legacy: bool
    unreadable: bool
    attempts: dict[AttemptKey, tuple[UsageEvent, ...] | None]
    outside: tuple[UsageEvent, ...]


@dataclass
class _Tally:
    """One scenario across the runs: its attempts, those measured, the runs it passed."""

    attempts: int = 0
    measured: int = 0
    passed: int = 0


@dataclass
class _Scope:
    runs: int = 0
    calls: int = 0
    failed: int = 0
    attempts: int = 0
    without: int = 0
    legacy: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    def out(self) -> UsageScope:
        return UsageScope(
            runs=self.runs,
            calls=self.calls,
            failed_calls=self.failed,
            attempts=self.attempts,
            attempts_without_usage=self.without,
            legacy_runs=self.legacy,
            unreadable_runs=self.unreadable,
        )


def _bucket_of(result: SuiteResult, bucket: str) -> tuple[str, str, datetime]:
    started = result.started_at.astimezone(UTC)
    if bucket == "run":
        return result.run_id, started.strftime("%b %-d %H:%M"), started
    day = started.date()
    if bucket == "week":
        day = day - timedelta(days=day.weekday())
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return day.isoformat(), f"wk of {start:%b %-d}", start
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return day.isoformat(), f"{start:%b %-d}", start


Metric = Callable[[UsageEvent], float | None]


def _sum(*parts: int | None) -> int | None:
    return None if any(p is None for p in parts) else sum(p or 0 for p in parts)


def _less(whole: int | None, part: int | None) -> int | None:
    """``whole`` without the reported ``part``; all of it when the part was not reported."""
    if whole is None:
        return None
    return whole - min(part, whole) if part is not None else whole


TOKEN_SEGMENTS: dict[str, Metric] = {
    "input": lambda e: _less(e.input_tokens, e.cached_input_tokens),
    "cached_input": lambda e: e.cached_input_tokens,
    "output": lambda e: _less(e.output_tokens, e.reasoning_tokens),
    "reasoning": lambda e: e.reasoning_tokens,
}


def _cost(e: UsageEvent) -> float | None:
    return e.cost_usd


def _total_tokens(e: UsageEvent) -> float | None:
    return _sum(e.input_tokens, e.output_tokens)


def _legacy_value(row: ModelUsage, metric: str) -> int | float | None:
    """A pre-usage-event row's figure for ``metric``, as written."""
    if metric == "cost":
        return row.cost_usd
    if metric == "total":
        if row.input_tokens is None or row.output_tokens is None:
            return None
        return row.input_tokens + row.output_tokens
    if metric in ("input", "output"):
        return getattr(row, f"{metric}_tokens")  # type: ignore[no-any-return]
    return None  # cached and reasoning tokens were never recorded


class FileAnalytics:
    """The analytics store over a :class:`FileStore` (local mode)."""

    def __init__(self, store: FileStore, prices: PriceTable | None = None) -> None:
        self.store = store
        self.prices = prices
        self._cache: dict[str, tuple[float, RunUsage]] = {}

    # --- reading ------------------------------------------------------------------------

    def _usage(self, result: SuiteResult) -> RunUsage:
        run_id = result.run_id
        try:
            stamp = (self.store.run_dir(run_id) / "result.json").stat().st_mtime
        except OSError:
            stamp = -1.0
        hit = self._cache.get(run_id)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        legacy = self.store.layout(run_id) in PRE_USAGE_LAYOUTS
        attempts: dict[AttemptKey, tuple[UsageEvent, ...] | None] = {
            (t.test_id, i): None for t in result.tests for i, _ in enumerate(t.attempts)
        }
        unreadable = False
        try:
            for record in self.store.load_records(run_id):
                events = record.usage_events
                attempts[(record.test_id, record.attempt_index)] = (
                    None if events is None else tuple(repriced(e, self.prices) for e in events)
                )
        except (ValueError, OSError):
            unreadable = True
        usage = RunUsage(
            result=result,
            legacy=legacy,
            unreadable=unreadable,
            attempts=attempts,
            outside=tuple(repriced(e, self.prices) for e in result.usage_events),
        )
        self._cache[run_id] = (stamp, usage)
        return usage

    def _runs(self, filters: UsageFilter) -> list[RunUsage]:
        server_only = filters.server_only()
        if server_only:
            raise UnsupportedFilter(
                f"filter by {', '.join(server_only)} needs server mode: local run folders have "
                "no organisation, project, task or label"
            )
        picked = []
        for result in reversed(self.store.finished_results()):  # oldest first
            day = result.started_at.astimezone(UTC).date()
            if filters.suite is not None and result.suite != filters.suite:
                continue
            if filters.since is not None and day < filters.since:
                continue
            if filters.until is not None and day > filters.until:
                continue
            picked.append(self._usage(result))
        return picked

    def options(self) -> FilterOptions:
        suites: set[str] = set()
        roles: set[str] = set()
        models: set[str] = set()
        for result in self.store.finished_results():
            suites.add(result.suite)
            run = self._usage(result)
            rows: list[tuple[str, str]] = [(u.role, u.model) for u in result.usage]
            for events in [*run.attempts.values(), run.outside]:
                rows.extend((e.role, e.model) for e in events or ())
            roles.update(r for r, _ in rows)
            models.update(m for _, m in rows)
        return FilterOptions(
            suites=sorted(suites),
            roles=sorted(roles),
            models=sorted(models),
            server_only=list(SERVER_ONLY_FILTERS),
        )

    @staticmethod
    def _wanted(filters: UsageFilter, role: str, model: str) -> bool:
        return (filters.role is None or role == filters.role) and (
            filters.model is None or model == filters.model
        )

    def _events(self, run: RunUsage, filters: UsageFilter) -> Iterable[UsageEvent]:
        """Every call of a per-call run that passes the filters, failed calls included."""
        for events in run.attempts.values():
            for e in events or ():
                if self._wanted(filters, e.role, e.model):
                    yield e
        for e in run.outside:
            if self._wanted(filters, e.role, e.model):
                yield e

    def _attempt_events(
        self, run: RunUsage, filters: UsageFilter
    ) -> dict[AttemptKey, list[UsageEvent] | None]:
        return {
            key: None
            if events is None
            else [e for e in events if self._wanted(filters, e.role, e.model)]
            for key, events in run.attempts.items()
        }

    def _legacy_rows(self, run: RunUsage, filters: UsageFilter) -> list[ModelUsage]:
        return [u for u in run.result.usage if self._wanted(filters, u.role, u.model)]

    def _scope(self, runs: list[RunUsage], filters: UsageFilter) -> _Scope:
        scope = _Scope(runs=len(runs))
        for run in runs:
            if run.legacy:
                scope.legacy.append(run.result.run_id)
                rows = self._legacy_rows(run, filters)
                scope.calls += sum(u.calls for u in rows)
            if run.unreadable:
                scope.unreadable.append(run.result.run_id)
            scope.attempts += len(run.attempts)
            scope.without += sum(1 for v in run.attempts.values() if v is None)
            if not run.legacy:
                for e in self._events(run, filters):
                    scope.calls += 1
                    scope.failed += 0 if e.answered else 1
        return scope

    def _series(
        self,
        runs: list[RunUsage],
        filters: UsageFilter,
        groups: Callable[[UsageEvent], str],
        legacy_group: Callable[[ModelUsage], str],
        metrics: dict[str, tuple[Metric, str]],
        digits: int | None,
    ) -> tuple[list[BucketOut], dict[str, list[Series]], dict[str, Acc]]:
        """Bucket every answered call's metrics.

        ``metrics`` maps an answer name to (the per-call metric, the legacy row's metric);
        a name of ``"*"`` groups by ``groups`` (the call's role, model...), any other name is
        one series of its own. Failed calls produced nothing to count and are left out of
        every ``of``, as in the run's own usage rows."""
        bucket_keys: list[str] = []
        buckets: dict[str, BucketOut] = {}
        cells: dict[tuple[str, str, str], Acc] = defaultdict(Acc)
        totals: dict[str, Acc] = defaultdict(Acc)
        for run in runs:
            key, label, start = _bucket_of(run.result, filters.bucket)
            if key not in buckets:
                bucket_keys.append(key)
                buckets[key] = BucketOut(key=key, label=label, start=start, runs=0)
            buckets[key] = buckets[key].model_copy(update={"runs": buckets[key].runs + 1})
            for name, (metric, legacy_metric) in metrics.items():
                if run.legacy:
                    for row in self._legacy_rows(run, filters):
                        answered = row.calls - row.failed_calls
                        group = legacy_group(row) if name.startswith("*") else name
                        value = _legacy_value(row, legacy_metric)
                        for acc in (cells[(name, group, key)], totals[f"{name}|{group}"]):
                            acc.add(value, of=answered)
                    continue
                for e in self._events(run, filters):
                    if not e.answered:
                        continue
                    group = groups(e) if name.startswith("*") else name
                    value = metric(e)
                    for acc in (cells[(name, group, key)], totals[f"{name}|{group}"]):
                        acc.add(value)
        series: dict[str, list[Series]] = defaultdict(list)
        names = sorted({(n, g) for n, g, _ in cells})
        for name, group in names:
            series[name].append(
                Series(
                    group=group,
                    points=[
                        (cells.get((name, group, k)) or Acc()).out(digits) for k in bucket_keys
                    ],
                    total=totals[f"{name}|{group}"].out(digits),
                )
            )
        return [buckets[k] for k in bucket_keys], series, totals

    # --- the questions ------------------------------------------------------------------

    def cost(self, filters: UsageFilter) -> CostAnswer:
        runs = self._runs(filters)
        buckets, series, _ = self._series(
            runs,
            filters,
            groups=lambda e: e.role,
            legacy_group=lambda u: u.role,
            metrics={"*role": (_cost, "cost")},
            digits=6,
        )
        _, by_model, _ = self._series(
            runs,
            filters,
            groups=lambda e: e.model,
            legacy_group=lambda u: u.model,
            metrics={"*model": (_cost, "cost")},
            digits=6,
        )
        total = Acc()
        sources: dict[str, int] = defaultdict(int)
        run_rows: list[RunCost] = []
        passed_total = 0
        scenarios: dict[str, _Tally] = defaultdict(_Tally)
        per_scenario: dict[str, Acc] = defaultdict(Acc)
        for run in runs:
            acc = Acc()
            if run.legacy:
                for row in self._legacy_rows(run, filters):
                    acc.add(row.cost_usd, of=row.calls - row.failed_calls)
            else:
                for e in self._events(run, filters):
                    if e.answered:
                        acc.add(e.cost_usd)
                        sources[e.cost_source] += 1
            total.add(acc.total if acc.measured else None, of=acc.of, measured=acc.measured)
            passed = sum(1 for t in run.result.tests if t.status is VerdictStatus.PASSED)
            passed_total += passed
            measured = acc.out(6)
            run_rows.append(
                RunCost(
                    run=run.result.run_id,
                    suite=run.result.suite,
                    started_at=run.result.started_at,
                    cost_usd=measured,
                    passed_tests=passed,
                    cost_per_passed_test=_per(measured.value, passed),
                    per_call=not run.legacy,
                )
            )
            self._scenario_rows(run, filters, scenarios, per_scenario, _cost)
        total_out = total.out(6)
        return CostAnswer(
            filters=filters.echo(),
            options=self.options(),
            scope=self._scope(runs, filters).out(),
            buckets=buckets,
            by_role=series["*role"],
            by_model=by_model["*model"],
            total_usd=total_out,
            cost_per_passed_test=_per(total_out.value, passed_total),
            passed_tests=passed_total,
            runs=list(reversed(run_rows)),
            scenarios=[
                ScenarioCost(
                    scenario=sid,
                    attempts=info.attempts,
                    attempts_measured=info.measured,
                    cost_usd=(cost := per_scenario[sid].out(6)),
                    cost_per_attempt=_per(cost.value, info.measured),
                    passed_runs=info.passed,
                    cost_per_passed_test=_per(cost.value, info.passed),
                )
                for sid, info in sorted(scenarios.items())
            ],
            sources=dict(sorted(sources.items())),
        )

    def _scenario_rows(
        self,
        run: RunUsage,
        filters: UsageFilter,
        scenarios: dict[str, _Tally],
        accs: dict[str, Acc],
        metric: Metric,
    ) -> None:
        verdicts = {t.test_id: t.status for t in run.result.tests}
        seen: set[str] = set()
        for (sid, _), events in self._attempt_events(run, filters).items():
            tally = scenarios[sid]
            tally.attempts += 1
            if sid not in seen:
                seen.add(sid)
                tally.passed += verdicts.get(sid) is VerdictStatus.PASSED
            if events is None:
                continue
            tally.measured += 1
            for e in events:
                if e.answered:
                    accs[sid].add(metric(e))

    def tokens(self, filters: UsageFilter) -> TokensAnswer:
        runs = self._runs(filters)
        buckets, by_kind, _ = self._series(
            runs,
            filters,
            groups=lambda e: e.role,
            legacy_group=lambda u: u.role,
            metrics={k: (m, k) for k, m in TOKEN_SEGMENTS.items()},
            digits=None,
        )
        _, by_model, _ = self._series(
            runs,
            filters,
            groups=lambda e: e.model,
            legacy_group=lambda u: u.model,
            metrics={"*model": (_total_tokens, "total")},
            digits=None,
        )
        totals: dict[str, Acc] = defaultdict(Acc)
        share_reasoning = share_output = 0
        share_measured = answered = 0
        per_attempt: list[int] = []
        attempts_in_scope = 0
        scenarios: dict[str, _Tally] = defaultdict(_Tally)
        per_scenario: dict[str, Acc] = defaultdict(Acc)
        whole: dict[str, Metric] = {
            "input": lambda e: e.input_tokens,
            "cached_input": lambda e: e.cached_input_tokens,
            "output": lambda e: e.output_tokens,
            "reasoning": lambda e: e.reasoning_tokens,
            "total": _total_tokens,
        }
        for run in runs:
            attempts_in_scope += len(run.attempts)
            if run.legacy:
                for row in self._legacy_rows(run, filters):
                    for name in whole:
                        totals[name].add(_legacy_value(row, name), of=row.calls - row.failed_calls)
                continue
            for e in self._events(run, filters):
                if not e.answered:
                    continue
                answered += 1
                for name, metric in whole.items():
                    totals[name].add(metric(e))
                if e.output_tokens is not None and e.reasoning_tokens is not None:
                    share_measured += 1
                    share_output += e.output_tokens
                    share_reasoning += min(e.reasoning_tokens, e.output_tokens)
            for events in self._attempt_events(run, filters).values():
                if events is None:
                    continue
                counted = [_total_tokens(e) for e in events if e.answered]
                if all(c is not None for c in counted):
                    per_attempt.append(int(sum(c or 0 for c in counted)))
            self._scenario_rows(run, filters, scenarios, per_scenario, _total_tokens)
        legacy_answered = sum(
            u.calls - u.failed_calls
            for run in runs
            if run.legacy
            for u in self._legacy_rows(run, filters)
        )
        return TokensAnswer(
            filters=filters.echo(),
            options=self.options(),
            scope=self._scope(runs, filters).out(),
            buckets=buckets,
            by_kind=[s for k in TOKEN_SEGMENTS for s in by_kind.get(k, [])],
            by_model=by_model["*model"],
            totals=TokenTotals(**{name: acc.out() for name, acc in _all(totals, whole).items()}),
            reasoning_share=Ratio(
                value=share_reasoning / share_output if share_output else None,
                measured=share_measured,
                of=answered + legacy_answered,
            ),
            tokens_per_attempt=Ratio(
                # No call anywhere: nothing was counted, so no mean (never 0).
                value=sum(per_attempt) / len(per_attempt) if per_attempt and answered else None,
                measured=len(per_attempt),
                of=attempts_in_scope,
            ),
            scenarios=[
                ScenarioTokens(
                    scenario=sid,
                    attempts=info.attempts,
                    attempts_measured=info.measured,
                    tokens=(tokens := per_scenario[sid].out()),
                    tokens_per_attempt=_per(tokens.value, info.measured),
                )
                for sid, info in sorted(scenarios.items())
            ],
        )


def _all(totals: dict[str, Acc], names: Iterable[str]) -> dict[str, Acc]:
    return {name: totals.get(name) or Acc() for name in names}


def _per(value: float | None, n: int) -> float | None:
    """``value`` per one of ``n``; ``None`` when either is missing or ``n`` is 0."""
    if value is None or n <= 0:
        return None
    return value / n


__all__ = ["FileAnalytics", "RunUsage", "UnsupportedFilter"]
