# SPDX-License-Identifier: Apache-2.0
"""``FileAnalytics``: the dashboards' aggregates from the run folders (local mode, ADR-0052).

Usage comes from the usage events in each attempt's record, the calls a run made outside any
attempt, and the calls its latest re-score made (ADR-0066). A call's cost is the one stored
with it when it was made: the price in force that day (or the provider's own figure). Nothing
is repriced afterwards, so a price row added later, even with a past ``effective_from``, never
changes an earlier run, and the dashboards agree with the run's own usage rows. A call stored
with no price stays "not measured". A run written before per-call usage contributes its
per-model totals as written and is named in ``scope.legacy_runs``; it has no per-attempt
figures.

Every value carries how many calls (or attempts) it covers. Nothing measured is ``None``,
never 0; a value covering part of what it should is marked incomplete. Token aggregates count
only roles that have tokens (:func:`~opseval.models.usage.has_tokens`): a TTS, STT or
telephony call is not a call whose tokens are unknown.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from opseval.analytics.base import (
    NOT_RECORDED_FILTERS,
    SERVER_ONLY_FILTERS,
    UnsupportedFilter,
    UsageFilter,
)
from opseval.analytics.dashboards import (
    NOT_CALIBRATED,
    AttemptFacts,
    CallFacts,
    RunFacts,
    VerdictFacts,
    bucket_of,
    fold_latency,
    fold_quality,
    fold_usage,
    fold_voice,
)
from opseval.analytics.models import (
    BucketOut,
    CostAnswer,
    DashboardOptions,
    DashboardScope,
    FilterOptions,
    GateRun,
    LatencyAnswer,
    Measured,
    QualityAnswer,
    Ratio,
    RunCost,
    ScenarioCost,
    ScenarioTokens,
    Series,
    TokensAnswer,
    TokenTotals,
    UsageCountsAnswer,
    UsageScope,
    VoiceAnswer,
)
from opseval.errors import HarnessError
from opseval.models.record import RunRecord
from opseval.models.results import ModelUsage, SuiteResult, VerdictStatus
from opseval.models.usage import UsageEvent, has_tokens
from opseval.storage import KNOWN_LAYOUTS, PRE_USAGE_LAYOUTS, RunStore
from opseval.verdicts import gate_state

AttemptKey = tuple[str, int]
RESCORE = " (rescore)"
"""Appended to a re-score call's role in the by-role series: re-grading spend, shown apart."""


@dataclass
class Acc:
    """A running sum that remembers how much of it was measured. Summed with
    :func:`math.fsum`, exact whatever the order, so every store adds the same calls to the
    same figure (the known-part arithmetic of :mod:`opseval.analytics.costs`)."""

    values: list[float] = field(default_factory=list)
    measured: int = 0
    of: int = 0

    @property
    def total(self) -> float:
        return math.fsum(self.values)

    def add(self, value: float | None, of: int = 1, measured: int | None = None) -> None:
        self.of += of
        if value is not None:
            self.values.append(value)
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
class TestHead:
    test_id: str
    status: VerdictStatus


@dataclass(frozen=True)
class RunHead:
    """What the usage folds read of a run's result: whichever store it came from."""

    run_id: str
    suite: str
    started_at: datetime
    tests: tuple[TestHead, ...]
    usage: tuple[ModelUsage, ...]
    """The run's usage rows as written (read for runs written before per-call usage)."""

    @classmethod
    def of(cls, result: SuiteResult) -> RunHead:
        return cls(
            run_id=result.run_id,
            suite=result.suite,
            started_at=result.started_at,
            tests=tuple(TestHead(t.test_id, t.status) for t in result.tests),
            usage=tuple(result.usage),
        )


@dataclass(frozen=True)
class RunUsage:
    """One finished run's usage, as read from its folder (cached per result file) or from the
    database's rows."""

    result: RunHead
    legacy: bool
    unreadable: bool
    attempts: dict[AttemptKey, tuple[UsageEvent, ...] | None]
    outside: tuple[UsageEvent, ...]
    rescore: tuple[UsageEvent, ...] = ()
    """Every call the latest re-score made (per attempt and outside any)."""
    rescored: bool = False
    rescore_unreadable: bool = False
    facts: RunFacts | None = None
    """What the D2 dashboards read (``None``: the records could not be read)."""

    def measured(self, test_id: str) -> bool:
        """Every attempt of ``test_id`` in this run has per-call usage."""
        keys = [k for k in self.attempts if k[0] == test_id]
        return bool(keys) and all(self.attempts[k] is not None for k in keys)


Stamp = tuple[float, str, float]
"""What a cached run is valid for: result.json's mtime, the latest re-score and its mtime."""


@dataclass
class _Tally:
    """One scenario across the runs: its attempts, those measured, the runs it passed."""

    attempts: int = 0
    measured: int = 0
    passed: int = 0
    passed_measured: int = 0


@dataclass
class _Scope:
    runs: int = 0
    calls: int = 0
    failed: int = 0
    rescore_calls: int = 0
    attempts: int = 0
    without: int = 0
    legacy: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    rescored: list[str] = field(default_factory=list)
    unreadable_rescores: list[str] = field(default_factory=list)

    def out(self) -> UsageScope:
        return UsageScope(
            runs=self.runs,
            calls=self.calls,
            failed_calls=self.failed,
            rescore_calls=self.rescore_calls,
            attempts=self.attempts,
            attempts_without_usage=self.without,
            legacy_runs=self.legacy,
            unreadable_runs=self.unreadable,
            rescored_runs=self.rescored,
            unreadable_rescores=self.unreadable_rescores,
        )


def _bucket_of(result: RunHead, bucket: str) -> tuple[str, str, datetime]:
    return bucket_of(result.run_id, result.started_at, bucket)


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
    """The cost stored when the call was made (ADR-0066): never repriced."""
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


def _ratio(num: Measured, measured: int, of: int) -> Ratio:
    """``num`` over the ``measured`` units of a denominator of ``of``. ``None`` when the
    numerator was not measured or no unit was; partial when the numerator is a floor."""
    value = num.value / measured if num.value is not None and measured > 0 else None
    return Ratio(value=value, measured=measured, of=of, complete=num.complete)


class UsageFold:
    """The Cost and Tokens dashboards' arithmetic over runs' usage (ADR-0066 D1), shared by
    every store: ``FileAnalytics`` reads the runs from folders, ``PgAnalytics`` from the
    database's rows, and both fold them here, so a total can never mean two things."""

    @staticmethod
    def options(finished: list[RunUsage]) -> FilterOptions:
        suites: set[str] = set()
        roles: set[str] = set()
        models: set[str] = set()
        for run in finished:
            suites.add(run.result.suite)
            if run.unreadable:
                continue
            rows: list[tuple[str, str]] = [(u.role, u.model) for u in run.result.usage]
            for events in [*run.attempts.values(), run.outside, run.rescore]:
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
    def _wanted(filters: UsageFilter, role: str, model: str, tokens: bool) -> bool:
        if tokens and not has_tokens(role):
            return False
        return (filters.role is None or role == filters.role) and (
            filters.model is None or model == filters.model
        )

    def _events(
        self, run: RunUsage, filters: UsageFilter, tokens: bool
    ) -> Iterator[tuple[UsageEvent, bool]]:
        """Every call of a per-call run that passes the filters, failed calls included, with
        whether the latest re-score made it."""
        for events in run.attempts.values():
            for e in events or ():
                if self._wanted(filters, e.role, e.model, tokens):
                    yield e, False
        for e in run.outside:
            if self._wanted(filters, e.role, e.model, tokens):
                yield e, False
        for e in run.rescore:
            if self._wanted(filters, e.role, e.model, tokens):
                yield e, True

    def _attempt_events(
        self, run: RunUsage, filters: UsageFilter, tokens: bool
    ) -> dict[AttemptKey, list[UsageEvent] | None]:
        """Each attempt's own calls (a re-score's are not the attempt as it ran)."""
        return {
            key: None
            if events is None
            else [e for e in events if self._wanted(filters, e.role, e.model, tokens)]
            for key, events in run.attempts.items()
        }

    def _legacy_rows(self, run: RunUsage, filters: UsageFilter, tokens: bool) -> list[ModelUsage]:
        return [u for u in run.result.usage if self._wanted(filters, u.role, u.model, tokens)]

    def _scope(
        self, runs: list[RunUsage], left_out: list[str], filters: UsageFilter, tokens: bool
    ) -> _Scope:
        scope = _Scope(runs=len(runs), unreadable=list(left_out))
        for run in runs:
            run_id = run.result.run_id
            if run.legacy:
                scope.legacy.append(run_id)
                scope.calls += sum(u.calls for u in self._legacy_rows(run, filters, tokens))
            if run.rescored:
                scope.rescored.append(run_id)
            if run.rescore_unreadable:
                scope.unreadable_rescores.append(run_id)
            scope.attempts += len(run.attempts)
            scope.without += sum(1 for v in run.attempts.values() if v is None)
            for e, again in self._events(run, filters, tokens):
                if run.legacy and not again:
                    continue  # counted from its rows above
                scope.calls += 1
                scope.failed += 0 if e.answered else 1
                scope.rescore_calls += again
        return scope

    def _series(
        self,
        runs: list[RunUsage],
        filters: UsageFilter,
        groups: Callable[[UsageEvent, bool], str],
        legacy_group: Callable[[ModelUsage], str],
        metrics: dict[str, tuple[Metric, str]],
        digits: int | None,
        tokens: bool,
    ) -> tuple[list[BucketOut], dict[str, list[Series]]]:
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
                    for row in self._legacy_rows(run, filters, tokens):
                        answered = row.calls - row.failed_calls
                        group = legacy_group(row) if name.startswith("*") else name
                        value = _legacy_value(row, legacy_metric)
                        for acc in (cells[(name, group, key)], totals[f"{name}|{group}"]):
                            acc.add(value, of=answered)
                for e, again in self._events(run, filters, tokens):
                    if not e.answered or (run.legacy and not again):
                        continue
                    group = groups(e, again) if name.startswith("*") else name
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
        return [buckets[k] for k in bucket_keys], series

    def _scenario_rows(
        self,
        run: RunUsage,
        filters: UsageFilter,
        scenarios: dict[str, _Tally],
        accs: dict[str, Acc],
        metric: Metric,
        tokens: bool,
    ) -> None:
        verdicts = {t.test_id: t.status for t in run.result.tests}
        seen: set[str] = set()
        for (sid, _), events in self._attempt_events(run, filters, tokens).items():
            tally = scenarios[sid]
            tally.attempts += 1
            if sid not in seen:
                seen.add(sid)
                if verdicts.get(sid) is VerdictStatus.PASSED:
                    tally.passed += 1
                    tally.passed_measured += run.measured(sid)
            if events is None:
                continue
            tally.measured += 1
            for e in events:
                if e.answered:
                    accs[sid].add(metric(e))

    def fold_cost(
        self,
        filters: UsageFilter,
        options: FilterOptions,
        runs: list[RunUsage],
        left_out: list[str],
    ) -> CostAnswer:
        buckets, series = self._series(
            runs,
            filters,
            groups=lambda e, again: e.role + (RESCORE if again else ""),
            legacy_group=lambda u: u.role,
            metrics={"*role": (_cost, "cost")},
            digits=6,
            tokens=False,
        )
        _, by_model = self._series(
            runs,
            filters,
            groups=lambda e, _again: e.model,
            legacy_group=lambda u: u.model,
            metrics={"*model": (_cost, "cost")},
            digits=6,
            tokens=False,
        )
        total = Acc()
        sources: dict[str, int] = defaultdict(int)
        run_rows: list[RunCost] = []
        passed_total = passed_measured = 0
        scenarios: dict[str, _Tally] = defaultdict(_Tally)
        per_scenario: dict[str, Acc] = defaultdict(Acc)
        for run in runs:
            acc = Acc()
            if run.legacy:
                for row in self._legacy_rows(run, filters, tokens=False):
                    acc.add(row.cost_usd, of=row.calls - row.failed_calls)
            for e, again in self._events(run, filters, tokens=False):
                if e.answered and (again or not run.legacy):
                    acc.add(e.cost_usd)
                    sources[e.cost_source] += 1
            total.add(acc.total if acc.measured else None, of=acc.of, measured=acc.measured)
            passed = [t.test_id for t in run.result.tests if t.status is VerdictStatus.PASSED]
            # A passed test whose attempts have no per-call usage is not in the numerator, so
            # it is not in the denominator. A run before per-call usage: its totals cover it.
            measured = len(passed) if run.legacy else sum(run.measured(t) for t in passed)
            passed_total += len(passed)
            passed_measured += measured
            run_cost = acc.out(6)
            run_rows.append(
                RunCost(
                    run=run.result.run_id,
                    suite=run.result.suite,
                    started_at=run.result.started_at,
                    cost_usd=run_cost,
                    passed_tests=len(passed),
                    cost_per_passed_test=_ratio(run_cost, measured, len(passed)),
                    per_call=not run.legacy,
                )
            )
            self._scenario_rows(run, filters, scenarios, per_scenario, _cost, tokens=False)
        total_out = total.out(6)
        return CostAnswer(
            filters=filters.echo(),
            options=options,
            scope=self._scope(runs, left_out, filters, tokens=False).out(),
            buckets=buckets,
            by_role=series["*role"],
            by_model=by_model["*model"],
            total_usd=total_out,
            cost_per_passed_test=_ratio(total_out, passed_measured, passed_total),
            passed_tests=passed_total,
            runs=list(reversed(run_rows)),
            scenarios=[
                ScenarioCost(
                    scenario=sid,
                    attempts=info.attempts,
                    attempts_measured=info.measured,
                    cost_usd=(cost := per_scenario[sid].out(6)),
                    cost_per_attempt=_ratio(cost, info.measured, info.attempts),
                    passed_runs=info.passed_measured,
                    cost_per_passed_test=_ratio(cost, info.passed_measured, info.passed),
                )
                for sid, info in sorted(scenarios.items())
            ],
            sources=dict(sorted(sources.items())),
        )

    def fold_tokens(
        self,
        filters: UsageFilter,
        options: FilterOptions,
        runs: list[RunUsage],
        left_out: list[str],
    ) -> TokensAnswer:
        buckets, by_kind = self._series(
            runs,
            filters,
            groups=lambda e, _again: e.role,
            legacy_group=lambda u: u.role,
            metrics={k: (m, k) for k, m in TOKEN_SEGMENTS.items()},
            digits=None,
            tokens=True,
        )
        _, by_model = self._series(
            runs,
            filters,
            groups=lambda e, _again: e.model,
            legacy_group=lambda u: u.model,
            metrics={"*model": (_total_tokens, "total")},
            digits=None,
            tokens=True,
        )
        totals: dict[str, Acc] = defaultdict(Acc)
        share_reasoning = share_output = 0
        share_measured = answered = 0
        per_attempt: list[int] = []
        attempts_in_scope = 0
        attempt_calls = 0
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
                for row in self._legacy_rows(run, filters, tokens=True):
                    answered += row.calls - row.failed_calls
                    for name in whole:
                        totals[name].add(_legacy_value(row, name), of=row.calls - row.failed_calls)
            for e, again in self._events(run, filters, tokens=True):
                if not e.answered or (run.legacy and not again):
                    continue
                answered += 1
                for name, metric in whole.items():
                    totals[name].add(metric(e))
                if e.output_tokens is not None and e.reasoning_tokens is not None:
                    share_measured += 1
                    share_output += e.output_tokens
                    share_reasoning += min(e.reasoning_tokens, e.output_tokens)
            for events in self._attempt_events(run, filters, tokens=True).values():
                if events is None:
                    continue
                counted = [_total_tokens(e) for e in events if e.answered]
                attempt_calls += len(counted)
                if all(c is not None for c in counted):
                    per_attempt.append(int(sum(c or 0 for c in counted)))
            self._scenario_rows(run, filters, scenarios, per_scenario, _total_tokens, tokens=True)
        # No token call in any attempt: nothing was counted, so no mean (never 0).
        attempts_total = Measured(
            value=float(sum(per_attempt)) if per_attempt and attempt_calls else None,
            measured=len(per_attempt),
            of=len(per_attempt),
            complete=True,  # each attempt in it reported every call's tokens
        )
        return TokensAnswer(
            filters=filters.echo(),
            options=options,
            scope=self._scope(runs, left_out, filters, tokens=True).out(),
            buckets=buckets,
            by_kind=[s for k in TOKEN_SEGMENTS for s in by_kind.get(k, [])],
            by_model=by_model["*model"],
            totals=TokenTotals(**{name: acc.out() for name, acc in _all(totals, whole).items()}),
            reasoning_share=Ratio(
                value=share_reasoning / share_output if share_output else None,
                measured=share_measured,
                of=answered,
                # A share of the calls that reported both counts: a sample, never a floor.
                complete=True,
            ),
            tokens_per_attempt=_ratio(attempts_total, len(per_attempt), attempts_in_scope),
            scenarios=[
                ScenarioTokens(
                    scenario=sid,
                    attempts=info.attempts,
                    attempts_measured=info.measured,
                    tokens=(tokens := per_scenario[sid].out()),
                    tokens_per_attempt=_ratio(tokens, info.measured, info.attempts),
                )
                for sid, info in sorted(scenarios.items())
            ],
        )


class FileAnalytics(UsageFold):
    """The analytics store over a :class:`~opseval.storage.RunStore`: run folders in local
    mode, and the run store in server mode until analytics has tables of its own (ADR-0052).
    It reads through the store's contract only."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self._cache: dict[str, tuple[Stamp, RunUsage]] = {}
        self._unreadable: list[str] = []
        """Finished runs whose result could not be read on the last pass: named in every
        answer's scope, never silently left out (review of #93)."""

    # --- reading ------------------------------------------------------------------------

    def _latest_rescore(self, run_id: str) -> tuple[str, float]:
        found = self.store.rescores(run_id)
        if not found:
            return "", -1.0
        at = self.store.changed_at(run_id, found[-1])
        return found[-1], -1.0 if at is None else at

    def _finished(self) -> list[RunUsage]:
        """Every finished run, newest first, each parsed once and then reused until its
        ``result.json`` (or its latest re-score) changes. One request reads them once and
        shares them between the filter options and the answer."""
        runs: list[RunUsage] = []
        unreadable: list[str] = []
        for stored in self.store.listing():  # one query in the database
            run_id = stored.run_id
            if not stored.finished or stored.layout not in KNOWN_LAYOUTS:
                continue  # listed by the console as an error row: shown, never misread
            mtime = stored.changed_at
            if mtime is None:
                unreadable.append(run_id)
                continue
            stamp: Stamp = (mtime, *self._latest_rescore(run_id))
            hit = self._cache.get(run_id)
            if hit is not None and hit[0] == stamp:
                runs.append(hit[1])
                continue
            try:
                result = self.store.load_result(run_id)
            except (ValueError, OSError, HarnessError):
                unreadable.append(run_id)  # named in the scope, never silently dropped
                continue
            usage = self._read(result, stamp[1])
            self._cache[run_id] = (stamp, usage)
            runs.append(usage)
        self._unreadable = sorted(unreadable)
        return sorted(runs, key=lambda r: (r.result.started_at, r.result.run_id), reverse=True)

    def _read(self, result: SuiteResult, rescore: str) -> RunUsage:
        run_id = result.run_id
        attempts: dict[AttemptKey, tuple[UsageEvent, ...] | None] = {
            (t.test_id, i): None for t in result.tests for i, _ in enumerate(t.attempts)
        }
        unreadable = False
        records: list[RunRecord] = []
        try:
            records = self.store.load_records(run_id)
            for record in records:
                attempts[(record.test_id, record.attempt_index)] = record.usage_events
        except (ValueError, OSError, HarnessError):
            unreadable = True
        rescore_events: tuple[UsageEvent, ...] = ()
        rescore_unreadable = False
        if rescore:
            try:
                again = self.store.load_rescore(run_id, rescore)
                rescore_events = (
                    *(e for a in again.attempt_usage for e in a.events),
                    *again.usage_events,
                )
            except (ValueError, OSError, HarnessError):
                rescore_unreadable = True
        return RunUsage(
            facts=None if unreadable else _facts(result, records),
            result=RunHead.of(result),
            legacy=self.store.layout(run_id) in PRE_USAGE_LAYOUTS,
            unreadable=unreadable,
            attempts=attempts,
            outside=tuple(result.usage_events),
            rescore=rescore_events,
            rescored=bool(rescore) and not rescore_unreadable,
            rescore_unreadable=rescore_unreadable,
        )

    def _runs(
        self, finished: list[RunUsage], filters: UsageFilter
    ) -> tuple[list[RunUsage], list[str]]:
        """The runs the filters pick, oldest first, and the picked runs left out because
        their records could not be read. Such a run contributes nothing at all: half a run
        (its result's outside calls and passed tests without its attempts) would be a total
        that looks whole."""
        picked: list[RunUsage] = []
        # A run whose result cannot be read cannot be filtered either: named, whatever the
        # filters, rather than left out of the scope without a word.
        left_out: list[str] = list(self._unreadable)
        for run in reversed(finished):  # oldest first
            result = run.result
            day = result.started_at.astimezone(UTC).date()
            if filters.suite is not None and result.suite != filters.suite:
                continue
            if filters.since is not None and day < filters.since:
                continue
            if filters.until is not None and day > filters.until:
                continue
            if run.unreadable:
                left_out.append(result.run_id)
                continue
            picked.append(run)
        return picked, left_out

    # --- the questions ------------------------------------------------------------------

    def cost(self, filters: UsageFilter) -> CostAnswer:
        _check(filters, "cost", "language", "dialect")
        finished = self._finished()
        runs, left_out = self._runs(finished, filters)
        return self.fold_cost(filters, self.options(finished), runs, left_out)

    def tokens(self, filters: UsageFilter) -> TokensAnswer:
        _check(filters, "tokens", "language", "dialect")
        finished = self._finished()
        runs, left_out = self._runs(finished, filters)
        return self.fold_tokens(filters, self.options(finished), runs, left_out)

    # --- D2: latency, quality, voice and usage (ADR-0066) ---------------------------------

    def _facts(
        self, filters: UsageFilter, question: str, *refused: str
    ) -> tuple[list[RunUsage], list[RunFacts], list[str]]:
        """The finished runs (every one, for the options), the facts of those the filters
        pick, oldest first, and the picked runs left out because their records could not be
        read."""
        _check(filters, question, *refused)
        finished = self._finished()
        picked, left_out = self._runs(finished, filters)
        return finished, [r.facts for r in picked if r.facts is not None], left_out

    @staticmethod
    def dashboard_options(finished: list[RunUsage]) -> DashboardOptions:
        languages: set[str] = set()
        dialects: set[str] = set()
        for run in finished:
            for a in run.facts.attempts if run.facts is not None else ():
                if a.language is not None:
                    languages.add(a.language)
                if a.dialect is not None:
                    dialects.add(a.dialect)
        return DashboardOptions(
            suites=sorted({r.result.suite for r in finished}),
            tasks=[],
            languages=sorted(languages),
            dialects=sorted(dialects),
            server_only=[f for f in SERVER_ONLY_FILTERS if f not in NOT_RECORDED_FILTERS],
            not_recorded=list(NOT_RECORDED_FILTERS),
        )

    def latency(self, filters: UsageFilter) -> LatencyAnswer:
        finished, runs, left_out = self._facts(filters, "latency", "model", "role")
        buckets, agent, llm, attempts = fold_latency(
            runs, filters.bucket, filters.language, filters.dialect
        )
        return LatencyAnswer(
            filters=filters.echo(),
            options=self.dashboard_options(finished),
            scope=DashboardScope(runs=len(runs), attempts=attempts, unreadable_runs=left_out),
            buckets=buckets,
            turn_latency=agent["turn"],
            voice_turn_latency=agent["voice_turn"],
            time_to_first_audio=agent["ttfa"],
            llm_latency=llm,
        )

    def quality(self, filters: UsageFilter, *, limit: int = 20) -> QualityAnswer:
        finished, runs, left_out = self._facts(filters, "quality", "model", "role")
        buckets, points, total, checks, flaky, matrix, attempts = fold_quality(
            runs, filters.bucket, filters.language, filters.dialect, limit
        )
        return QualityAnswer(
            filters=filters.echo(),
            options=self.dashboard_options(finished),
            scope=DashboardScope(runs=len(runs), attempts=attempts, unreadable_runs=left_out),
            buckets=buckets,
            pass_rate=points,
            pass_rate_total=total,
            gate_history=[
                GateRun(run=r.run_id, suite=r.suite, started_at=r.started_at, gate=r.gate)
                for r in runs
            ],
            flaky=flaky,
            checks=checks,
            matrix=matrix,
            judge_agreement_note=NOT_CALIBRATED,
        )

    def voice(self, filters: UsageFilter) -> VoiceAnswer:
        finished, runs, left_out = self._facts(filters, "voice", "model", "role")
        metrics, voice, attempts = fold_voice(runs, filters.language, filters.dialect)
        return VoiceAnswer(
            filters=filters.echo(),
            options=self.dashboard_options(finished),
            scope=DashboardScope(runs=len(runs), attempts=attempts, unreadable_runs=left_out),
            voice_attempts=voice,
            metrics=metrics,
        )

    def usage(self, filters: UsageFilter) -> UsageCountsAnswer:
        finished, runs, left_out = self._facts(
            filters, "usage", "model", "role", "language", "dialect"
        )
        buckets, attempts, audio, by_task, total = fold_usage(runs, filters.bucket)
        return UsageCountsAnswer(
            filters=filters.echo(),
            options=self.dashboard_options(finished),
            scope=DashboardScope(runs=len(runs), attempts=total, unreadable_runs=left_out),
            buckets=buckets,
            attempts=attempts,
            audio_minutes=audio,
            by_task=by_task,
        )


def _check(filters: UsageFilter, question: str, *refused: str) -> None:
    """Refuse what no store records (a label), then what a folder cannot filter by
    (server-only), then what does not apply."""
    filters.refuse_not_recorded()
    server_only = [n for n in filters.server_only() if n not in NOT_RECORDED_FILTERS]
    if server_only:
        raise UnsupportedFilter(
            f"filter by {', '.join(server_only)} needs server mode: local run folders have "
            "no organisation, project, task or label"
        )
    filters.refuse(question, *refused)


def _facts(result: SuiteResult, records: list[RunRecord]) -> RunFacts:
    """What the D2 dashboards read from one run: its verdicts, each recorded attempt (with
    its scores from the result) and the calls it made as it ran."""
    scores = {
        (t.test_id, a.attempt_index): tuple(
            (x.scorer_id, x.axis.value, x.status.value, x.score) for x in a.scores
        )
        for t in result.tests
        for a in t.attempts
    }
    attempts = []
    calls: list[UsageEvent] = list(result.usage_events)
    for record in records:
        language, dialect = record.language()
        voice = record.voice
        attempts.append(
            AttemptFacts(
                test_id=record.test_id,
                index=record.attempt_index,
                language=language,
                dialect=dialect,
                voice=voice is not None,
                latencies=tuple(record.turn_latencies()),
                metrics=tuple(
                    (m.name, m.value, m.degraded)
                    for m in (voice.metrics or () if voice is not None else ())
                ),
                scores=scores.get((record.test_id, record.attempt_index), ()),
            )
        )
        calls.extend(record.usage_events or ())
    return RunFacts(
        run_id=result.run_id,
        suite=result.suite,
        started_at=result.started_at,
        gate=gate_state(result),
        verdicts=tuple(
            VerdictFacts(
                test_id=t.test_id,
                name=t.name,
                status=t.status.value,
                pass_rate=t.pass_rate,
                flaky=t.flaky,
            )
            for t in result.tests
        ),
        attempts=tuple(sorted(attempts, key=lambda a: (a.test_id, a.index))),
        calls=tuple(
            CallFacts(
                role=e.role,
                answered=e.answered,
                latency_ms=e.latency_ms,
                audio_in_seconds=e.audio_in_seconds,
                audio_out_seconds=e.audio_out_seconds,
            )
            for e in calls
        ),
    )


def _all(totals: dict[str, Acc], names: Iterable[str]) -> dict[str, Acc]:
    return {name: totals.get(name) or Acc() for name in names}


__all__ = ["FileAnalytics", "RunHead", "RunUsage", "TestHead", "UnsupportedFilter", "UsageFold"]
