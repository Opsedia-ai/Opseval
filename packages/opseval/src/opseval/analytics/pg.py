# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: S608
# Every SQL string here is assembled from this module's own constants (column names, the
# bucket expression for a ``Literal`` bucket, the scope clause); every value a caller gives is a
# bound parameter (``:suite``, ``:since``...). Nothing a request sends is ever spliced in.
"""``PgAnalytics``: the dashboards' aggregates from the database (ADR-0052, server mode).

It answers every question ``FileAnalytics`` answers, from the ADR-0051 rows and never from a
blob, and a golden test holds the two to identical answers, ``null``s included:

- **Latency, quality, voice, usage (D2)** are SQL: the runs the filters pick (an indexed scan
  of ``runs`` by project, task and start), then ``GROUP BY ... GROUPING SETS`` per bucket and
  in total over ``attempt_metrics``, ``usage_events``, ``verdicts`` and ``scores``. Percentiles
  are ``percentile_disc`` (nearest rank), the rule :mod:`opseval.analytics.dashboards` writes
  for the folders. Only the order of rows (Python's, not the database's collation), the flips
  between consecutive verdicts and the buckets' labels are placed in Python.
- **Cost and tokens (D1)** read the picked runs' usage rows in a few queries and fold them with
  :class:`~opseval.analytics.files.UsageFold`, the arithmetic ``FileAnalytics`` uses: the cost
  stored with each call, never repriced, with the known part and the unpriced calls apart.

The store's scope is the store's: a project, and a task when it serves one task's reads
(``DbRunStore(task_reads=True)``, accounts mode). A filter naming another project is refused.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opseval.analytics.base import NOT_RECORDED_FILTERS, UnsupportedFilter, UsageFilter
from opseval.analytics.dashboards import (
    DIGITS,
    NOT_CALIBRATED,
    TURN_LATENCY,
    Buckets,
    RunFacts,
    VerdictFacts,
    flaky_and_matrix,
    rate,
)
from opseval.analytics.files import RunHead, RunUsage, TestHead, UsageFold
from opseval.analytics.models import (
    VOICE_METRICS,
    CheckStats,
    CostAnswer,
    DashboardOptions,
    DashboardScope,
    FilterOptions,
    GateRun,
    LatencyAnswer,
    Measured,
    Percentiles,
    PercentileSeries,
    QualityAnswer,
    Rate,
    TaskUsage,
    TokensAnswer,
    UsageCountsAnswer,
    VoiceAnswer,
    VoiceGroup,
    VoiceMetricOut,
)
from opseval.models.results import ModelUsage, VerdictStatus
from opseval.models.usage import UsageEvent
from opseval.storage import KNOWN_LAYOUTS, PRE_USAGE_LAYOUTS, RESCORE_PREFIX

if TYPE_CHECKING:
    from opseval.db.run_store import DbRunStore

OPTIONS_TTL = 30.0
"""Seconds the filter choices are reused: a new suite or language shows within half a minute."""
IS_NO_REPLY = "m.reason LIKE 'NO_REPLY%'"
NOT_NO_REPLY = "(m.reason IS NULL OR m.reason NOT LIKE 'NO_REPLY%')"
"""A null reply time is an unanswered turn when its reason says ``NO_REPLY`` (written by
``DbRunStore.save_record``), and not measured otherwise."""
PCT = "percentile_disc(ARRAY[0.5, 0.95, 0.99]) WITHIN GROUP (ORDER BY {col})"
EVENT_COLUMNS = (
    "at, role, provider, model, answered, input_tokens, output_tokens, reasoning_tokens, "
    "cached_input_tokens, audio_in_seconds, audio_out_seconds, characters, latency_ms, "
    "rate_limited, retries, cost_usd, cost_source, price_from"
)


class FilterOutsideScope(UnsupportedFilter):
    """A filter naming a project this server does not serve."""

    code = "FILTER_OUTSIDE_SCOPE"


@dataclass(frozen=True)
class _Picked:
    """The runs the filters pick: the SQL that selects them and its parameters."""

    where: str
    params: dict[str, Any]
    bucket: str

    def key(self, alias: str = "r") -> str:
        """The bucket a run falls in, as ``bucket_of`` keys it."""
        if self.bucket == "run":
            return f"{alias}.id"
        day = f"({alias}.started_at AT TIME ZONE 'UTC')::date"
        if self.bucket == "week":
            day = f"date_trunc('week', {alias}.started_at AT TIME ZONE 'UTC')::date"
        return f"to_char({day}, 'YYYY-MM-DD')"


def _pcts(n: int, unmeasured: int, values: Any, mean: Any, unanswered: int = 0) -> Percentiles:
    if not n:
        return Percentiles(
            n=0,
            unmeasured=unmeasured,
            unanswered=unanswered,
            p50=None,
            p95=None,
            p99=None,
            mean=None,
        )
    p50, p95, p99 = (float(v) for v in values)
    return Percentiles(
        n=n,
        unmeasured=unmeasured,
        unanswered=unanswered,
        p50=p50,
        p95=p95,
        p99=p99,
        mean=round(float(mean), DIGITS),
    )


EMPTY = Percentiles(n=0, unmeasured=0, p50=None, p95=None, p99=None, mean=None)


class PgAnalytics(UsageFold):
    """The analytics store on the database a :class:`~opseval.db.run_store.DbRunStore` uses,
    within its scope."""

    def __init__(self, store: DbRunStore) -> None:
        self.store = store
        self._options: tuple[float, DashboardOptions] | None = None

    # --- plumbing -------------------------------------------------------------------------

    def _rows(self, sql: str, params: dict[str, Any]) -> list[Any]:
        async def go(s: AsyncSession) -> list[Any]:
            return list((await s.execute(text(sql), params)).all())

        return self.store.run_in_session(go)

    def _check(self, filters: UsageFilter, question: str, *refused: str) -> None:
        """Refuse a filter outside this store's scope, then what does not apply."""
        ref = self.store.project_ref
        if filters.project is not None and filters.project != ref:
            raise FilterOutsideScope(
                f"this server serves project {ref or 'every project'}; it cannot filter by "
                f"project {filters.project}"
            )
        if filters.org is not None and (ref is None or ref.split("/", 1)[0] != filters.org):
            raise FilterOutsideScope(
                f"this server serves project {ref or 'every project'}; it cannot filter by "
                f"organisation {filters.org}"
            )
        filters.refuse(question, *refused)

    def _scope_sql(self, params: dict[str, Any]) -> str:
        where = (
            "r.result_blob_id IS NOT NULL AND r.status <> 'deleting' AND r.layout = ANY(:layouts)"
        )
        params["layouts"] = sorted(KNOWN_LAYOUTS)
        if self.store.project_id is not None:
            where += " AND r.project_id = :scope_project"
            params["scope_project"] = self.store.project_id
        if self.store.task_reads and self.store.task_id is not None:
            where += " AND r.task_id = :scope_task"
            params["scope_task"] = self.store.task_id
        return where

    def _picked(self, filters: UsageFilter) -> _Picked:
        params: dict[str, Any] = {}
        where = self._scope_sql(params)
        if filters.suite is not None:
            where += " AND r.suite = :suite"
            params["suite"] = filters.suite
        if filters.task is not None:
            where += " AND r.task_id IN (SELECT id FROM tasks WHERE slug = :task)"
            params["task"] = filters.task
        # Bounds as UTC-midnight instants, so the comparison is on the indexed column itself
        # (``ix_runs_project_started``): the same days as "started on or after/before" in UTC.
        if filters.since is not None:
            where += " AND r.started_at >= :since_at"
            params["since_at"] = _midnight(filters.since)
        if filters.until is not None:
            where += " AND r.started_at < :until_at"
            params["until_at"] = _midnight(filters.until + timedelta(days=1))
        return _Picked(where, params, filters.bucket)

    @staticmethod
    def _attempt_filter(filters: UsageFilter, params: dict[str, Any], alias: str = "a") -> str:
        out = ""
        if filters.language is not None:
            out += f" AND {alias}.language = :language"
            params["language"] = filters.language
        if filters.dialect is not None:
            out += f" AND {alias}.dialect = :dialect"
            params["dialect"] = filters.dialect
        return out

    def _runs(self, picked: _Picked) -> list[tuple[str, str, datetime, str | None]]:
        """(id, suite, started_at, gate) of the picked runs, oldest first."""
        rows = self._rows(
            f"SELECT r.id, r.suite, r.started_at, r.gate_state FROM runs r WHERE {picked.where}",  # nosec B608
            dict(picked.params),
        )
        return sorted(((r[0], r[1], _utc(r[2]), r[3]) for r in rows), key=lambda r: (r[2], r[0]))

    def _buckets(self, runs: Sequence[tuple[str, str, datetime, str | None]], kind: str) -> Buckets:
        buckets = Buckets(kind)
        for run_id, suite, started, gate in runs:
            buckets.add(RunFacts(run_id, suite, started, gate or "", (), (), ()))
        return buckets

    def dashboard_options(self) -> DashboardOptions:
        """The filters' choices for this store's scope, kept for :data:`OPTIONS_TTL` seconds:
        every dashboard request returns them, and three ``DISTINCT`` scans per request would
        cost more than the answer itself."""
        now = time.monotonic()
        if self._options is not None and now - self._options[0] < OPTIONS_TTL:
            return self._options[1]
        options = self._read_options()
        self._options = (now, options)
        return options

    def _read_options(self) -> DashboardOptions:
        params: dict[str, Any] = {}
        where = self._scope_sql(params)
        suites = self._rows(f"SELECT DISTINCT r.suite FROM runs r WHERE {where}", dict(params))  # nosec B608
        tasks = self._rows(
            f"SELECT DISTINCT t.slug FROM runs r JOIN tasks t ON t.id = r.task_id WHERE {where}",  # nosec B608
            dict(params),
        )
        langs = self._rows(
            "SELECT DISTINCT a.language, a.dialect FROM runs r JOIN attempts a "  # nosec B608
            f"ON a.run_id = r.id AND a.project_id = r.project_id WHERE {where}",
            dict(params),
        )
        return DashboardOptions(
            suites=sorted(r[0] for r in suites),
            tasks=sorted(r[0] for r in tasks),
            languages=sorted({r[0] for r in langs if r[0] is not None}),
            dialects=sorted({r[1] for r in langs if r[1] is not None}),
            server_only=[],
            not_recorded=list(NOT_RECORDED_FILTERS),
        )

    def _percentile_rows(
        self, sql: str, params: dict[str, Any]
    ) -> dict[tuple[str, str | None], Percentiles]:
        """Rows of ``(group, bucket or None for the total, n, unmeasured, pcts, mean,
        unanswered)``."""
        out: dict[tuple[str, str | None], Percentiles] = {}
        for group, bucket, n, unmeasured, pcts, mean, unanswered in self._rows(sql, params):
            out[(group, bucket)] = _pcts(n, unmeasured, pcts, mean, unanswered)
        return out

    @staticmethod
    def _pseries(
        group: str, name: str, cells: dict[tuple[str, str | None], Percentiles], keys: list[str]
    ) -> PercentileSeries:
        return PercentileSeries(
            group=group,
            points=[cells.get((name, k), EMPTY) for k in keys],
            total=cells.get((name, None), EMPTY),
        )

    # --- D2 ---------------------------------------------------------------------------------

    def latency(self, filters: UsageFilter) -> LatencyAnswer:
        self._check(filters, "latency", "model", "role")
        picked = self._picked(filters)
        runs = self._runs(picked)
        buckets = self._buckets(runs, filters.bucket)
        keys = buckets.keys
        params = dict(picked.params)
        lang = self._attempt_filter(filters, params)
        params["names"] = [TURN_LATENCY, "voice.turn_latency_ms", "voice.time_to_first_audio_ms"]
        bucket = picked.key()
        agent = self._percentile_rows(
            f"SELECT m.name, CASE WHEN GROUPING({bucket}) = 0 THEN {bucket} END, "  # nosec B608
            f"count(m.value), count(*) FILTER (WHERE m.value IS NULL AND {NOT_NO_REPLY}), "
            f"{PCT.format(col='m.value')}, avg(m.value), "
            f"count(*) FILTER (WHERE m.value IS NULL AND {IS_NO_REPLY}) "
            "FROM runs r JOIN attempts a ON a.run_id = r.id AND a.project_id = r.project_id "
            "JOIN attempt_metrics m ON m.attempt_id = a.id "
            f"WHERE {picked.where} AND m.name = ANY(:names){lang} "
            f"GROUP BY GROUPING SETS ((m.name, {bucket}), (m.name))",
            params,
        )
        params = dict(picked.params)
        filtered = filters.language is not None or filters.dialect is not None
        # Under a language filter a call counts when the attempt it was made for matches; a
        # call outside any attempt has no language and is counted apart (as the folders do).
        through = ""
        if filtered:
            through = (
                " JOIN attempts a ON a.id = e.attempt_id AND a.project_id = e.project_id"
                + self._attempt_filter(filters, params)
            )
        llm = self._percentile_rows(
            f"SELECT e.role, CASE WHEN GROUPING({bucket}) = 0 THEN {bucket} END, "  # nosec B608
            f"count(e.latency_ms), count(*) - count(e.latency_ms), "
            f"{PCT.format(col='e.latency_ms')}, avg(e.latency_ms), 0 "
            "FROM runs r JOIN usage_events e ON e.run_id = r.id AND e.project_id = r.project_id"
            f"{through} "
            f"WHERE {picked.where} AND e.answered AND e.rescore IS NULL "
            f"GROUP BY GROUPING SETS ((e.role, {bucket}), (e.role))",
            params,
        )
        unattributed = 0
        if filtered:
            unattributed = self._count(
                "SELECT count(*) FROM runs r JOIN usage_events e ON e.run_id = r.id "  # nosec B608
                f"AND e.project_id = r.project_id WHERE {picked.where} AND e.answered "
                "AND e.rescore IS NULL AND e.attempt_id IS NULL",
                dict(picked.params),
            )
        attempts, _voice = self._attempts(picked, filters)
        return LatencyAnswer(
            filters=filters.echo(),
            options=self.dashboard_options(),
            scope=DashboardScope(runs=len(runs), attempts=attempts),
            buckets=buckets.list(),
            turn_latency=self._pseries("agent", TURN_LATENCY, agent, keys),
            voice_turn_latency=self._pseries("agent", "voice.turn_latency_ms", agent, keys),
            time_to_first_audio=self._pseries("agent", "voice.time_to_first_audio_ms", agent, keys),
            llm_latency=[
                self._pseries(role, role, llm, keys) for role in sorted({g for g, _ in llm})
            ],
            llm_calls_unattributed=unattributed,
        )

    def _attempts(self, picked: _Picked, filters: UsageFilter) -> tuple[int, int]:
        """(attempts, voice attempts) of the picked runs under the attempt filters: one query,
        the one every D2 answer's scope reads."""
        params = dict(picked.params)
        lang = self._attempt_filter(filters, params)
        rows = self._rows(
            "SELECT count(*), count(*) FILTER (WHERE a.voice) FROM runs r "  # nosec B608
            "JOIN attempts a ON a.run_id = r.id AND a.project_id = r.project_id "
            f"WHERE {picked.where}{lang}",
            params,
        )
        return (int(rows[0][0] or 0), int(rows[0][1] or 0)) if rows else (0, 0)

    def _count(self, sql: str, params: dict[str, Any]) -> int:
        rows = self._rows(sql, params)
        return int(rows[0][0] or 0) if rows else 0

    def quality(self, filters: UsageFilter, *, limit: int = 20) -> QualityAnswer:
        self._check(filters, "quality", "model", "role")
        picked = self._picked(filters)
        runs = self._runs(picked)
        buckets = self._buckets(runs, filters.bucket)
        keys = buckets.keys
        bucket = picked.key()
        params = dict(picked.params)
        first = ""
        if filters.language is not None or filters.dialect is not None:
            # A verdict's language is its scenario's first attempt's (as the folders read it).
            first = (
                " AND EXISTS (SELECT 1 FROM attempts a WHERE a.run_id = v.run_id "  # nosec B608
                "AND a.project_id = v.project_id AND a.test_id = v.test_id "
                "AND a.attempt_index = (SELECT min(b.attempt_index) FROM attempts b "
                "WHERE b.run_id = v.run_id AND b.project_id = v.project_id "
                f"AND b.test_id = v.test_id){self._attempt_filter(filters, params)})"
            )
        tallies: dict[str | None, Rate] = {}
        for key, passed, failed, other in self._rows(
            f"SELECT CASE WHEN GROUPING({bucket}) = 0 THEN {bucket} END, "  # nosec B608
            "count(*) FILTER (WHERE v.status = 'passed'), "
            "count(*) FILTER (WHERE v.status = 'failed'), "
            "count(*) FILTER (WHERE v.status NOT IN ('passed', 'failed')) "
            "FROM runs r JOIN verdicts v ON v.run_id = r.id AND v.project_id = r.project_id "
            f"WHERE {picked.where}{first} GROUP BY GROUPING SETS (({bucket}), ())",
            params,
        ):
            tallies[key] = rate(passed, failed, other)
        none = rate(0, 0, 0)
        params = dict(picked.params)
        lang = self._attempt_filter(filters, params)
        check_rows = self._rows(
            "SELECT x.scorer_id, x.axis, "  # nosec B608
            "count(*) FILTER (WHERE x.status = 'passed'), "
            "count(*) FILTER (WHERE x.status = 'failed'), "
            "count(*) FILTER (WHERE x.status NOT IN ('passed', 'failed')), avg(x.score) "
            "FROM runs r JOIN attempts a ON a.run_id = r.id AND a.project_id = r.project_id "
            "JOIN scores x ON x.attempt_id = a.id "
            f"WHERE {picked.where}{lang} GROUP BY x.scorer_id, x.axis",
            params,
        )
        checks = sorted(
            (
                CheckStats(
                    scorer_id=scorer,
                    axis=axis,
                    pass_rate=rate(passed, failed, other),
                    mean_score=None if mean is None else round(float(mean), DIGITS),
                )
                for scorer, axis, passed, failed, other, mean in check_rows
            ),
            key=lambda c: (c.scorer_id, c.axis),
        )
        params = dict(picked.params)
        verdict_rows = self._rows(
            "SELECT v.run_id, v.test_id, coalesce(v.name, v.test_id), v.status, v.pass_rate, "  # nosec B608
            "v.flaky FROM runs r JOIN verdicts v ON v.run_id = r.id "
            f"AND v.project_id = r.project_id WHERE {picked.where}{first}",
            {**params, **self._lang_params(filters)},
        )
        by_run: dict[str, list[VerdictFacts]] = defaultdict(list)
        for run_id, test_id, name, status, pass_rate, flaky in verdict_rows:
            by_run[run_id].append(VerdictFacts(test_id, name, status, pass_rate, flaky))
        facts = [
            (
                RunFacts(run_id, suite, started, gate or "", (), (), ()),
                sorted(by_run.get(run_id, []), key=lambda v: v.test_id),
            )
            for run_id, suite, started, gate in runs
        ]
        flaky, matrix = flaky_and_matrix(facts, limit)
        attempts, _voice = self._attempts(picked, filters)
        return QualityAnswer(
            filters=filters.echo(),
            options=self.dashboard_options(),
            scope=DashboardScope(runs=len(runs), attempts=attempts),
            buckets=buckets.list(),
            pass_rate=[tallies.get(k, none) for k in keys],
            pass_rate_total=tallies.get(None, none),
            gate_history=[
                GateRun(run=run_id, suite=suite, started_at=started, gate=gate or "harness_error")
                for run_id, suite, started, gate in runs
            ],
            flaky=flaky,
            checks=checks,
            matrix=matrix,
            judge_agreement_note=NOT_CALIBRATED,
        )

    @staticmethod
    def _lang_params(filters: UsageFilter) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if filters.language is not None:
            out["language"] = filters.language
        if filters.dialect is not None:
            out["dialect"] = filters.dialect
        return out

    def voice(self, filters: UsageFilter) -> VoiceAnswer:
        self._check(filters, "voice", "model", "role")
        picked = self._picked(filters)
        runs = self._runs(picked)
        params = dict(picked.params)
        lang = self._attempt_filter(filters, params)
        params["names"] = [name for name, _ in VOICE_METRICS]
        rows = self._rows(
            "SELECT m.name, GROUPING(a.language, a.dialect), a.language, a.dialect, "  # nosec B608
            f"count(m.value), count(*) - count(m.value), {PCT.format(col='m.value')}, "
            "avg(m.value), count(*) FILTER (WHERE m.degraded AND m.value IS NOT NULL) "
            "FROM runs r JOIN attempts a ON a.run_id = r.id AND a.project_id = r.project_id "
            "JOIN attempt_metrics m ON m.attempt_id = a.id "
            f"WHERE {picked.where} AND m.name = ANY(:names){lang} "
            "GROUP BY GROUPING SETS ((m.name), (m.name, a.language, a.dialect))",
            params,
        )
        totals: dict[str, tuple[Percentiles, int]] = {}
        groups: dict[str, list[VoiceGroup]] = defaultdict(list)
        for name, grouping, language, dialect, n, unmeasured, pcts, mean, degraded in rows:
            stats = _pcts(n, unmeasured, pcts, mean)
            if grouping:
                totals[name] = (stats, degraded)
            else:
                groups[name].append(
                    VoiceGroup(language=language, dialect=dialect, stats=stats, degraded=degraded)
                )
        attempts, voice = self._attempts(picked, filters)
        return VoiceAnswer(
            filters=filters.echo(),
            options=self.dashboard_options(),
            scope=DashboardScope(runs=len(runs), attempts=attempts),
            voice_attempts=voice,
            metrics=[
                VoiceMetricOut(
                    name=name,
                    unit=unit,
                    total=totals.get(name, (EMPTY, 0))[0],
                    degraded=totals.get(name, (EMPTY, 0))[1],
                    by_language=sorted(
                        groups.get(name, []), key=lambda g: (g.language or "", g.dialect or "")
                    ),
                )
                for name, unit in VOICE_METRICS
            ],
        )

    def usage(self, filters: UsageFilter) -> UsageCountsAnswer:
        self._check(filters, "usage", "model", "role", "language", "dialect")
        picked = self._picked(filters)
        runs = self._runs(picked)
        buckets = self._buckets(runs, filters.bucket)
        keys = buckets.keys
        bucket = picked.key()
        speech = (
            "CASE WHEN e.role = 'tts' THEN e.audio_out_seconds "
            "WHEN e.role = 'stt' THEN e.audio_in_seconds END"
        )
        params = dict(picked.params)
        attempts = {
            key: int(n)
            for key, n in self._rows(
                f"SELECT {bucket}, count(*) FROM runs r JOIN attempts a ON a.run_id = r.id "  # nosec B608
                f"AND a.project_id = r.project_id WHERE {picked.where} GROUP BY {bucket}",
                params,
            )
        }
        audio: dict[str, Measured] = {}
        for key, total, measured, of in self._rows(
            f"SELECT {bucket}, sum({speech}), count({speech}), count(*) "  # nosec B608
            "FROM runs r JOIN usage_events e ON e.run_id = r.id AND e.project_id = r.project_id "
            f"WHERE {picked.where} AND e.answered AND e.rescore IS NULL "
            f"AND e.role IN ('tts', 'stt') GROUP BY {bucket}",
            dict(picked.params),
        ):
            audio[key] = _minutes(total, measured, of)
        names = (
            "JOIN tasks t ON t.id = r.task_id JOIN projects p ON p.id = r.project_id "
            "JOIN organizations o ON o.id = p.organization_id"
        )
        task_runs = self._rows(
            f"SELECT o.slug || '/' || p.slug, t.slug, count(*) FROM runs r {names} "  # nosec B608
            f"WHERE {picked.where} GROUP BY o.slug, p.slug, t.slug",
            dict(picked.params),
        )
        task_attempts = {
            (project, task): (int(n), int(voice))
            for project, task, n, voice in self._rows(
                f"SELECT o.slug || '/' || p.slug, t.slug, count(*), "  # nosec B608
                "count(*) FILTER (WHERE a.voice) FROM runs r JOIN attempts a "
                f"ON a.run_id = r.id AND a.project_id = r.project_id {names} "
                f"WHERE {picked.where} GROUP BY o.slug, p.slug, t.slug",
                dict(picked.params),
            )
        }
        task_audio = {
            (project, task): _minutes(total, measured, of)
            for project, task, total, measured, of in self._rows(
                f"SELECT o.slug || '/' || p.slug, t.slug, sum({speech}), count({speech}), "  # nosec B608
                "count(*) FROM runs r JOIN usage_events e ON e.run_id = r.id "
                f"AND e.project_id = r.project_id {names} "
                f"WHERE {picked.where} AND e.answered AND e.rescore IS NULL "
                "AND e.role IN ('tts', 'stt') GROUP BY o.slug, p.slug, t.slug",
                dict(picked.params),
            )
        }
        by_task = [
            TaskUsage(
                project=project,
                task=task,
                runs=int(n_runs),
                attempts=task_attempts.get((project, task), (0, 0))[0],
                voice_attempts=task_attempts.get((project, task), (0, 0))[1],
                audio_minutes=task_audio.get((project, task), _minutes(None, 0, 0)),
            )
            for project, task, n_runs in task_runs
        ]
        return UsageCountsAnswer(
            filters=filters.echo(),
            options=self.dashboard_options(),
            scope=DashboardScope(runs=len(runs), attempts=sum(attempts.values())),
            buckets=buckets.list(),
            attempts=[attempts.get(k, 0) for k in keys],
            audio_minutes=[audio.get(k, _minutes(None, 0, 0)) for k in keys],
            by_task=sorted(by_task, key=lambda t: (t.project or "", t.task or "")),
        )

    # --- D1: cost and tokens, folded as the folders fold them ------------------------------

    def _usage_runs(self, filters: UsageFilter) -> tuple[FilterOptions, list[RunUsage]]:
        params: dict[str, Any] = {}
        scope = self._scope_sql(params)
        params["rescore_prefix"] = RESCORE_PREFIX
        latest = (
            "(SELECT max(substr(b.name, length(:rescore_prefix) + 1, "
            "length(b.name) - length(:rescore_prefix) - length('/result.json'))) "
            "FROM blobs b WHERE b.run_id = r.id AND b.project_id = r.project_id "
            "AND b.state = 'committed' AND b.kind = 'result' "
            "AND b.name LIKE :rescore_prefix || '%/result.json')"
        )
        suites = self._rows(f"SELECT DISTINCT r.suite FROM runs r WHERE {scope}", dict(params))  # nosec B608
        roles = self._rows(
            "SELECT DISTINCT e.role, e.model FROM runs r JOIN usage_events e ON e.run_id = r.id "  # nosec B608
            f"AND e.project_id = r.project_id WHERE {scope} "
            f"AND (e.rescore IS NULL OR e.rescore = {latest}) "
            "UNION SELECT DISTINCT u.role, u.model FROM runs r JOIN run_usage u "
            f"ON u.run_id = r.id AND u.project_id = r.project_id WHERE {scope}",
            dict(params),
        )
        options = FilterOptions(
            suites=sorted(r[0] for r in suites),
            roles=sorted({r[0] for r in roles}),
            models=sorted({r[1] for r in roles}),
            server_only=[],
        )
        picked = self._picked(filters)
        p = {**picked.params, "rescore_prefix": RESCORE_PREFIX}
        heads = self._rows(
            f"SELECT r.id, r.suite, r.started_at, r.layout, {latest} FROM runs r "  # nosec B608
            f"WHERE {picked.where}",
            p,
        )
        if not heads:
            return options, []
        ids = [h[0] for h in heads]
        p = {**self._id_params(ids)}
        verdicts: dict[str, list[TestHead]] = defaultdict(list)
        for run_id, test_id, status in self._rows(
            "SELECT v.run_id, v.test_id, v.status FROM verdicts v WHERE v.run_id = ANY(:ids) "  # nosec B608
            f"{self._project_clause('v')}",
            p,
        ):
            verdicts[run_id].append(TestHead(test_id, VerdictStatus(status)))
        attempts: dict[str, dict[tuple[str, int], uuid.UUID]] = defaultdict(dict)
        recorded: dict[uuid.UUID, bool] = {}
        for run_id, test_id, index, attempt_id, has_usage in self._rows(
            "SELECT a.run_id, a.test_id, a.attempt_index, a.id, a.usage_recorded FROM attempts a "  # nosec B608
            f"WHERE a.run_id = ANY(:ids) {self._project_clause('a')}",
            p,
        ):
            attempts[run_id][(test_id, index)] = attempt_id
            recorded[attempt_id] = bool(has_usage)
        events: dict[uuid.UUID | None, list[UsageEvent]] = defaultdict(list)
        outside: dict[str, list[UsageEvent]] = defaultdict(list)
        again: dict[str, list[UsageEvent]] = defaultdict(list)
        stamps = {h[0]: h[4] for h in heads}
        for row in self._rows(
            f"SELECT e.run_id, e.attempt_id, e.rescore, {EVENT_COLUMNS} FROM usage_events e "  # nosec B608
            f"WHERE e.run_id = ANY(:ids) {self._project_clause('e')} ORDER BY e.at, e.id",
            p,
        ):
            run_id, attempt_id, stamp = row[0], row[1], row[2]
            event = _event(row[3:])
            if stamp is not None:
                if stamp == stamps.get(run_id):
                    again[run_id].append(event)
            elif attempt_id is not None:
                events[attempt_id].append(event)
            else:
                outside[run_id].append(event)
        rows: dict[str, list[ModelUsage]] = defaultdict(list)
        for run_id, body in self._rows(
            "SELECT u.run_id, u.body FROM run_usage u WHERE u.run_id = ANY(:ids) "  # nosec B608
            f"{self._project_clause('u')} ORDER BY u.id",
            p,
        ):
            rows[run_id].append(ModelUsage.model_validate(body))
        out = []
        for run_id, suite, started, layout, stamp in heads:
            out.append(
                RunUsage(
                    result=RunHead(
                        run_id=run_id,
                        suite=suite,
                        started_at=_utc(started),
                        tests=tuple(sorted(verdicts[run_id], key=lambda t: t.test_id)),
                        usage=tuple(rows[run_id]),
                    ),
                    legacy=layout in PRE_USAGE_LAYOUTS,
                    unreadable=False,
                    attempts={
                        key: tuple(events[aid]) if recorded[aid] else None
                        for key, aid in sorted(attempts[run_id].items())
                    },
                    outside=tuple(outside[run_id]),
                    rescore=tuple(again[run_id]),
                    rescored=stamp is not None,
                )
            )
        out.sort(key=lambda r: (r.result.started_at, r.result.run_id))
        return options, out

    def _project_clause(self, alias: str) -> str:
        return f"AND {alias}.project_id = :scope_project" if self.store.project_id else ""

    def _id_params(self, ids: list[str]) -> dict[str, Any]:
        params: dict[str, Any] = {"ids": ids}
        if self.store.project_id is not None:
            params["scope_project"] = self.store.project_id
        return params

    def cost(self, filters: UsageFilter) -> CostAnswer:
        self._check(filters, "cost", "language", "dialect")
        options, runs = self._usage_runs(filters)
        return self.fold_cost(filters, options, runs, [])

    def tokens(self, filters: UsageFilter) -> TokensAnswer:
        self._check(filters, "tokens", "language", "dialect")
        options, runs = self._usage_runs(filters)
        return self.fold_tokens(filters, options, runs, [])


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _utc(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _minutes(total: Any, measured: Any, of: Any) -> Measured:
    measured, of = int(measured or 0), int(of or 0)
    return Measured(
        value=round(float(total) / 60, DIGITS) if measured else None,
        measured=measured,
        of=of,
        complete=measured == of,
    )


def _event(row: Sequence[Any]) -> UsageEvent:
    (
        at,
        role,
        provider,
        model,
        answered,
        input_tokens,
        output_tokens,
        reasoning_tokens,
        cached_input_tokens,
        audio_in_seconds,
        audio_out_seconds,
        characters,
        latency_ms,
        rate_limited,
        retries,
        cost_usd,
        cost_source,
        price_from,
    ) = row
    return UsageEvent(
        at=_utc(at),
        role=role,
        provider=provider,
        model=model,
        answered=answered,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_input_tokens=cached_input_tokens,
        audio_in_seconds=audio_in_seconds,
        audio_out_seconds=audio_out_seconds,
        characters=characters,
        latency_ms=latency_ms,
        rate_limited=rate_limited,
        retries=retries,
        cost_usd=cost_usd,
        cost_source=cost_source,
        price_from=price_from if isinstance(price_from, date) else None,
    )


__all__ = ["FilterOutsideScope", "PgAnalytics"]
