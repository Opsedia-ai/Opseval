# SPDX-License-Identifier: Apache-2.0
"""The D2 dashboards' definitions (ADR-0066): latency, quality, voice and usage.

What each number means is written once, here: a percentile is nearest-rank, a pass rate is
over judged results only, an unmeasured value is counted apart and never as 0. ``FileAnalytics``
folds the run folders' facts with these functions; ``PgAnalytics`` answers the same questions
with SQL and a golden test holds the two to identical answers.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from opseval.analytics.models import (
    VOICE_METRICS,
    BucketOut,
    CheckStats,
    FlakyScenario,
    MatrixCell,
    MatrixRow,
    MatrixRun,
    Measured,
    Percentiles,
    PercentileSeries,
    Rate,
    TaskUsage,
    VerdictMatrix,
    VoiceGroup,
    VoiceMetricOut,
)

PERCENTILES = (0.5, 0.95, 0.99)
DIGITS = 6
"""Means and minutes are rounded to this many digits in every store, so a database's and a
folder's float sums never differ in the last bit."""
TURN_LATENCY = "agent.turn_latency_ms"
"""The ``attempt_metrics`` name the database stores an agent's reply time under (one row per
turn, :meth:`~opseval.models.record.RunRecord.turn_latencies`)."""
JUDGED = ("passed", "failed")


def nearest_rank(values: list[float], p: float) -> float | None:
    """The value at rank ``ceil(p * n)`` of the sorted values (Postgres ``percentile_disc``),
    or ``None`` when there is none."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(p * len(ordered)), 1) - 1]


def percentiles(values: Iterable[float | None]) -> Percentiles:
    """``n`` measured values and the ``unmeasured`` ones (``None``), never folded together."""
    measured: list[float] = []
    unmeasured = 0
    for v in values:
        if v is None:
            unmeasured += 1
        else:
            measured.append(float(v))
    n = len(measured)
    p50, p95, p99 = (nearest_rank(measured, p) for p in PERCENTILES)
    return Percentiles(
        n=n,
        unmeasured=unmeasured,
        p50=p50,
        p95=p95,
        p99=p99,
        mean=round(math.fsum(measured) / n, DIGITS) if n else None,
    )


def rate(passed: int, failed: int, other: int) -> Rate:
    judged = passed + failed
    return Rate(
        value=passed / judged if judged else None,
        passed=passed,
        judged=judged,
        not_judged=other,
    )


def bucket_of(run_id: str, started_at: datetime, bucket: str) -> tuple[str, str, datetime]:
    """A run's bucket: (key, label, start), for every dashboard (Cost and Tokens included)."""
    started = started_at.astimezone(UTC)
    if bucket == "run":
        return run_id, started.strftime("%b %-d %H:%M"), started
    day = started.date()
    if bucket == "week":
        day = day - timedelta(days=day.weekday())
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return day.isoformat(), f"wk of {start:%b %-d}", start
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return day.isoformat(), f"{start:%b %-d}", start


def label_of(started_at: datetime) -> str:
    return started_at.astimezone(UTC).strftime("%b %-d %H:%M")


@dataclass(frozen=True)
class AttemptFacts:
    """What the D2 dashboards read from one attempt."""

    test_id: str
    index: int
    language: str | None
    dialect: str | None
    voice: bool
    latencies: tuple[float | None, ...] = ()
    metrics: tuple[tuple[str, float | None, bool], ...] = ()
    """Voice metric rows: (name, value, degraded)."""
    scores: tuple[tuple[str, str, str, float | None], ...] = ()
    """(scorer_id, axis, status, score)."""


@dataclass(frozen=True)
class VerdictFacts:
    test_id: str
    name: str
    status: str
    pass_rate: float | None
    flaky: bool


@dataclass(frozen=True)
class CallFacts:
    """One call the run paid for, as the latency and usage dashboards read it."""

    role: str
    answered: bool
    latency_ms: float | None
    audio_in_seconds: float | None
    audio_out_seconds: float | None


@dataclass(frozen=True)
class RunFacts:
    run_id: str
    suite: str
    started_at: datetime
    gate: str
    verdicts: tuple[VerdictFacts, ...]
    attempts: tuple[AttemptFacts, ...]
    calls: tuple[CallFacts, ...]
    """The run's own calls (its attempts' and those outside any): a re-score's are not the
    run as it ran."""
    project: str | None = None
    task: str | None = None


@dataclass
class Buckets:
    """Runs placed in buckets, oldest first, as every dashboard places them."""

    kind: str
    keys: list[str] = field(default_factory=list)
    out: dict[str, BucketOut] = field(default_factory=dict)

    def add(self, run: RunFacts) -> str:
        key, label, start = bucket_of(run.run_id, run.started_at, self.kind)
        if key not in self.out:
            self.keys.append(key)
            self.out[key] = BucketOut(key=key, label=label, start=start, runs=0)
        self.out[key] = self.out[key].model_copy(update={"runs": self.out[key].runs + 1})
        return key

    def list(self) -> list[BucketOut]:
        return [self.out[k] for k in self.keys]


def wanted(a: AttemptFacts, language: str | None, dialect: str | None) -> bool:
    return (language is None or a.language == language) and (
        dialect is None or a.dialect == dialect
    )


def scenario_language(run: RunFacts) -> dict[str, tuple[str | None, str | None]]:
    """Each scenario's language and dialect in a run: its first attempt's."""
    out: dict[str, tuple[str | None, str | None]] = {}
    for a in sorted(run.attempts, key=lambda a: (a.test_id, a.index)):
        out.setdefault(a.test_id, (a.language, a.dialect))
    return out


def verdict_wanted(
    run: RunFacts,
    languages: dict[str, tuple[str | None, str | None]],
    v: VerdictFacts,
    language: str | None,
    dialect: str | None,
) -> bool:
    """A verdict counts under a language filter when its scenario's first attempt matches;
    a scenario with no attempt has no known language and is left out of a filtered answer."""
    if language is None and dialect is None:
        return True
    found = languages.get(v.test_id)
    if found is None:
        return False
    return (language is None or found[0] == language) and (dialect is None or found[1] == dialect)


def checks(attempts: Iterable[AttemptFacts]) -> list[CheckStats]:
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    scores: dict[tuple[str, str], list[float]] = defaultdict(list)
    for a in attempts:
        for scorer, axis, status, score in a.scores:
            key = (scorer, axis)
            slot = {"passed": 0, "failed": 1}.get(status, 2)
            tally[key][slot] += 1
            if score is not None:
                scores[key].append(score)
            else:
                scores.setdefault(key, [])
    out = []
    for (scorer, axis), (passed, failed, other) in sorted(tally.items()):
        values = scores[(scorer, axis)]
        out.append(
            CheckStats(
                scorer_id=scorer,
                axis=axis,
                pass_rate=rate(passed, failed, other),
                mean_score=round(math.fsum(values) / len(values), DIGITS) if values else None,
            )
        )
    return out


def flaky_and_matrix(
    runs: list[tuple[RunFacts, list[VerdictFacts]]], limit: int
) -> tuple[list[FlakyScenario], VerdictMatrix]:
    """Flaky scenarios across ``runs`` (oldest first) and the verdict matrix of the newest
    ``limit``, flips counted within each suite only (as ``views.scenario_trend`` counts)."""
    names: dict[str, str] = {}
    history: dict[str, list[tuple[str, str]]] = defaultdict(list)
    flaky_runs: dict[str, int] = defaultdict(int)
    for run, verdicts in runs:
        for v in verdicts:
            names[v.test_id] = v.name  # the newest name wins
            history[v.test_id].append((run.suite, v.status))
            flaky_runs[v.test_id] += v.flaky
    flaky = []
    for sid in sorted(names):
        judged = [(suite, st) for suite, st in history[sid] if st in JUDGED]
        changed = flips(judged)
        if changed or flaky_runs[sid]:
            flaky.append(
                FlakyScenario(
                    scenario=sid,
                    name=names[sid],
                    judged_runs=len(judged),
                    flaky_runs=flaky_runs[sid],
                    flips=changed,
                )
            )
    flaky.sort(key=lambda f: (-(f.flips + f.flaky_runs), f.scenario))
    chosen = runs[-limit:] if limit else []
    by_run = [{v.test_id: v for v in verdicts} for _, verdicts in chosen]
    shown = {tid: v.name for tests in by_run for tid, v in tests.items()}
    rows = []
    for sid in sorted(shown):
        cells = []
        judged = []
        for (run, _), tests in zip(chosen, by_run, strict=True):
            found = tests.get(sid)
            cells.append(
                MatrixCell(
                    run=run.run_id,
                    status=found.status if found else None,
                    pass_rate=found.pass_rate if found else None,
                    flaky=found.flaky if found else False,
                )
            )
            if found is not None and found.status in JUDGED:
                judged.append((run.suite, found.status))
        rows.append(MatrixRow(id=sid, name=shown[sid], cells=cells, flips=flips(judged)))
    matrix = VerdictMatrix(
        runs=[
            MatrixRun(run=r.run_id, label=label_of(r.started_at), suite=r.suite) for r, _ in chosen
        ],
        scenarios=rows,
    )
    return flaky, matrix


def flips(judged: list[tuple[str, str]]) -> int:
    """Changes between consecutive judged verdicts, ``(suite, status)`` oldest first, counted
    within each suite only: two suites can run a scenario differently (repeats, thresholds),
    so alternating between them is not the scenario changing. The one rule the Quality
    dashboard and ``views.scenario_trend`` both count by."""
    per_suite: dict[str, list[str]] = defaultdict(list)
    for suite, status in judged:
        per_suite[suite].append(status)
    return sum(
        sum(1 for a, b in zip(seq, seq[1:], strict=False) if a != b) for seq in per_suite.values()
    )


def audio_seconds(call: CallFacts) -> float | None:
    """The speech one call produced or heard: TTS audio out, STT audio in."""
    if call.role == "tts":
        return call.audio_out_seconds
    if call.role == "stt":
        return call.audio_in_seconds
    return None


def is_speech(call: CallFacts) -> bool:
    return call.answered and call.role in ("tts", "stt")


def minutes(calls: Iterable[CallFacts]) -> Measured:
    """Audio minutes over the answered speech calls; ``None`` with none, never 0."""
    speech = [c for c in calls if is_speech(c)]
    known = [s for s in (audio_seconds(c) for c in speech) if s is not None]
    return Measured(
        value=round(math.fsum(known) / 60, DIGITS) if known else None,
        measured=len(known),
        of=len(speech),
        complete=len(known) == len(speech),
    )


# --- the answers, folded from facts (the files store; the database answers in SQL) --------


def fold_latency(
    runs: list[RunFacts], bucket: str, language: str | None, dialect: str | None
) -> tuple[list[BucketOut], dict[str, PercentileSeries], list[PercentileSeries], int]:
    """(buckets, the three agent series by name, LLM latency per role, attempts counted)."""
    buckets = Buckets(bucket)
    cells: dict[tuple[str, str], list[float | None]] = defaultdict(list)
    roles: dict[tuple[str, str], list[float | None]] = defaultdict(list)
    attempts = 0
    for run in runs:
        key = buckets.add(run)
        for a in run.attempts:
            if not wanted(a, language, dialect):
                continue
            attempts += 1
            cells[("turn", key)].extend(a.latencies)
            for name, value, _ in a.metrics:
                if name == "voice.turn_latency_ms":
                    cells[("voice_turn", key)].append(value)
                elif name == "voice.time_to_first_audio_ms":
                    cells[("ttfa", key)].append(value)
        for c in run.calls:
            if c.answered:
                roles[(c.role, key)].append(c.latency_ms)
    keys = buckets.keys

    def series(
        group: str, source: dict[tuple[str, str], list[float | None]], name: str
    ) -> PercentileSeries:
        return PercentileSeries(
            group=group,
            points=[percentiles(source.get((name, k), [])) for k in keys],
            total=percentiles(v for k in keys for v in source.get((name, k), [])),
        )

    agent = {
        "turn": series("agent", cells, "turn"),
        "voice_turn": series("agent", cells, "voice_turn"),
        "ttfa": series("agent", cells, "ttfa"),
    }
    llm = [series(role, roles, role) for role in sorted({r for r, _ in roles})]
    return buckets.list(), agent, llm, attempts


def fold_quality(
    runs: list[RunFacts], bucket: str, language: str | None, dialect: str | None, limit: int
) -> tuple[
    list[BucketOut], list[Rate], Rate, list[CheckStats], list[FlakyScenario], VerdictMatrix, int
]:
    buckets = Buckets(bucket)
    tallies: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    picked: list[tuple[RunFacts, list[VerdictFacts]]] = []
    in_scope: list[AttemptFacts] = []
    for run in runs:
        key = buckets.add(run)
        languages = scenario_language(run)
        verdicts = [v for v in run.verdicts if verdict_wanted(run, languages, v, language, dialect)]
        picked.append((run, verdicts))
        for v in verdicts:
            tallies[key][{"passed": 0, "failed": 1}.get(v.status, 2)] += 1
        in_scope.extend(a for a in run.attempts if wanted(a, language, dialect))
    keys = buckets.keys
    total = [sum(tallies[k][i] for k in keys) for i in range(3)]
    flaky, matrix = flaky_and_matrix(picked, limit)
    return (
        buckets.list(),
        [rate(*tallies[k]) if k in tallies else rate(0, 0, 0) for k in keys],
        rate(*total),
        checks(in_scope),
        flaky,
        matrix,
        len(in_scope),
    )


def fold_voice(
    runs: list[RunFacts], language: str | None, dialect: str | None
) -> tuple[list[VoiceMetricOut], int, int]:
    """(the metrics, voice attempts, attempts counted)."""
    rows: dict[str, list[tuple[str | None, str | None, float | None, bool]]] = defaultdict(list)
    voice = attempts = 0
    for run in runs:
        for a in run.attempts:
            if not wanted(a, language, dialect):
                continue
            attempts += 1
            voice += a.voice
            for name, value, degraded in a.metrics:
                rows[name].append((a.language, a.dialect, value, degraded))
    out = []
    for name, unit in VOICE_METRICS:
        found = rows.get(name, [])
        groups: dict[tuple[str | None, str | None], list[tuple[float | None, bool]]] = defaultdict(
            list
        )
        for lang, dial, value, degraded in found:
            groups[(lang, dial)].append((value, degraded))
        out.append(
            VoiceMetricOut(
                name=name,
                unit=unit,
                total=percentiles(v for _, _, v, _ in found),
                degraded=sum(1 for _, _, v, d in found if d and v is not None),
                by_language=[
                    VoiceGroup(
                        language=lang,
                        dialect=dial,
                        stats=percentiles(v for v, _ in values),
                        degraded=sum(1 for v, d in values if d and v is not None),
                    )
                    for (lang, dial), values in sorted(
                        groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")
                    )
                ],
            )
        )
    return out, voice, attempts


def fold_usage(
    runs: list[RunFacts], bucket: str
) -> tuple[list[BucketOut], list[int], list[Measured], list[TaskUsage], int]:
    buckets = Buckets(bucket)
    attempts: dict[str, int] = defaultdict(int)
    calls: dict[str, list[CallFacts]] = defaultdict(list)
    tasks: dict[tuple[str | None, str | None], list[RunFacts]] = defaultdict(list)
    for run in runs:
        key = buckets.add(run)
        attempts[key] += len(run.attempts)
        calls[key].extend(run.calls)
        tasks[(run.project, run.task)].append(run)
    keys = buckets.keys
    by_task = [
        TaskUsage(
            project=project,
            task=task,
            runs=len(group),
            attempts=sum(len(r.attempts) for r in group),
            voice_attempts=sum(a.voice for r in group for a in r.attempts),
            audio_minutes=minutes(c for r in group for c in r.calls),
        )
        for (project, task), group in sorted(
            tasks.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")
        )
    ]
    return (
        buckets.list(),
        [attempts[k] for k in keys],
        [minutes(calls[k]) for k in keys],
        by_task,
        sum(attempts.values()),
    )


NOT_CALIBRATED = (
    "not measured: `opseval calibrate` writes its agreement beside a run folder, and no store "
    "reads calibrations yet"
)


__all__ = [
    "NOT_CALIBRATED",
    "TURN_LATENCY",
    "AttemptFacts",
    "Buckets",
    "CallFacts",
    "RunFacts",
    "VerdictFacts",
    "bucket_of",
    "checks",
    "flaky_and_matrix",
    "flips",
    "minutes",
    "nearest_rank",
    "percentiles",
    "rate",
]
