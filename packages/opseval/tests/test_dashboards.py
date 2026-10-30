# SPDX-License-Identifier: Apache-2.0
"""The D2 dashboards (ADR-0066) and ``PgAnalytics`` (ADR-0052).

The golden test writes the same fixture runs to a folder and to the database and requires
``FileAnalytics`` and ``PgAnalytics`` to give identical answers to every question, ``null``s
included. The other tests check the answers by hand: a percentile is nearest-rank, a pass
rate counts judged results only, an unmeasured value is counted apart and never as 0.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opseval.analytics import FileAnalytics, UsageFilter
from opseval.analytics.base import FilterNotApplicable, FilterNotRecorded, UnsupportedFilter
from opseval.analytics.dashboards import nearest_rank, percentiles
from opseval.llm import fold_usage
from opseval.models.profile import FrameKind
from opseval.models.record import FrameRecord, RunRecord, TerminalState, VoiceMetric, VoiceTimeline
from opseval.models.results import (
    AttemptVerdict,
    Axis,
    Gate,
    ModelUsage,
    ScoreResult,
    ScoreStatus,
    SuiteResult,
    TestVerdict,
    VerdictStatus,
)
from opseval.models.scenario import Criticality
from opseval.models.usage import AttemptUsage, ModelPrice, PriceTable, UsageEvent
from opseval.pricing import priced
from opseval.storage import FileStore, RunStore

PRICES = PriceTable(
    prices=(
        ModelPrice(
            match="m-sim", unit="tokens", effective_from=date(2026, 1, 1), input=1, output=2
        ),
        ModelPrice(
            match="m-judge", unit="tokens", effective_from=date(2026, 1, 1), input=10, output=20
        ),
        ModelPrice(
            match="tts-1", unit="characters", effective_from=date(2026, 1, 1), characters=15
        ),
    )
)
DAY = datetime(2026, 3, 2, 9, tzinfo=UTC)  # a Monday


def ev(role: str, model: str, at: datetime = DAY, **kw: Any) -> UsageEvent:
    return priced(UsageEvent(at=at, role=role, model=model, **kw), PRICES)  # type: ignore[arg-type]


def frames(*latencies: float | None) -> tuple[FrameRecord, ...]:
    """One message sent and one reply per latency (``None``: a reply the clocks could not time)."""
    out: list[FrameRecord] = []
    for latency in latencies:
        for direction, value in (("sent", None), ("received", latency)):
            out.append(
                FrameRecord(
                    seq=len(out),
                    at=DAY,
                    direction=direction,  # type: ignore[arg-type]
                    kind=FrameKind.MESSAGE,
                    payload={},
                    latency_ms=value,
                    text="hi",
                )
            )
    # A second frame after a reply is not another turn.
    if out:
        out.append(
            FrameRecord(
                seq=len(out),
                at=DAY,
                direction="received",
                kind=FrameKind.MESSAGE,
                payload={},
                latency_ms=999.0,
                text="more",
            )
        )
    return tuple(out)


def voice(*metrics: tuple[str, int | None, float | None, bool]) -> VoiceTimeline:
    units = {"ratio": "ratio", "wer": "wer", "confidence": "confidence"}
    return VoiceTimeline(
        codec="pcm16",
        rate=16000,
        mode="duplex",
        metrics=tuple(
            VoiceMetric(
                name=name,  # type: ignore[arg-type]
                turn=turn,
                value=value,
                unit=next((u for k, u in units.items() if k in name), "ms"),  # type: ignore[arg-type]
                degraded=degraded,
                reason=None if value is not None and not degraded else "PARTIAL: test",
            )
            for name, turn, value, degraded in metrics
        ),
    )


def rec(
    run: str,
    test: str,
    index: int,
    *,
    language: str = "en",
    dialect: str | None = None,
    latencies: tuple[float | None, ...] = (),
    events: tuple[UsageEvent, ...] | None = (),
    timeline: VoiceTimeline | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run,
        test_id=test,
        attempt_index=index,
        seed=0,
        target="ws://x",
        started_at=DAY,
        ended_at=DAY,
        terminal_state=TerminalState.GOAL_REACHED,
        frames=frames(*latencies),
        persona={"role": "customer", "language": language, "dialect": dialect},
        usage_events=events,
        voice=timeline,
    )


def score(scorer: str, status: ScoreStatus, value: float | None) -> ScoreResult:
    return ScoreResult(
        scorer_id=scorer,
        axis=Axis.OUTPUT,
        status=status,
        score=value,
        threshold=0.5,
        reason=None if status is not ScoreStatus.INFRA_ERROR else "TIMEOUT: judge",
    )


def attempt(index: int, *scores: ScoreResult) -> AttemptVerdict:
    return AttemptVerdict(
        attempt_index=index,
        passed=True,
        terminal_state=TerminalState.GOAL_REACHED,
        scores=list(scores),
        record_path="x",
    )


def verdict(
    test: str, status: VerdictStatus, *attempts: AttemptVerdict, flaky: bool = False
) -> TestVerdict:
    return TestVerdict(
        test_id=test,
        name=f"Scenario {test}",
        criticality=Criticality.NORMAL,
        status=status,
        flaky=flaky,
        pass_rate=1.0 if status is VerdictStatus.PASSED else 0.0,
        attempts=list(attempts),
    )


def result(
    run: str,
    when: datetime,
    suite: str,
    tests: list[TestVerdict],
    *,
    usage: list[ModelUsage] | None = None,
    outside: list[UsageEvent] | None = None,
    exit_code: int = 0,
) -> SuiteResult:
    return SuiteResult(
        opseval_version="0",
        run_id=run,
        suite=suite,
        target="ws://x",
        profile="p",
        started_at=when,
        ended_at=when,
        tests=tests,
        gate=Gate(
            status="pass" if exit_code == 0 else "block",
            blocking=["s: failed (normal)"] if exit_code == 1 else [],
        ),
        exit_code=exit_code,
        usage=usage or [],
        usage_events=outside or [],
    )


def rows_of(*events: UsageEvent) -> list[ModelUsage]:
    by: dict[tuple[str, str], list[UsageEvent]] = {}
    for e in events:
        by.setdefault((e.role, e.model), []).append(e)
    return [fold_usage(r, m, list(es)) for (r, m), es in by.items()]


def populate(store: RunStore, legacy: Any) -> None:
    """The fixture runs, written through the store's own contract."""
    # Run A (Mon): text and voice; latencies, LLM and speech calls, scores, a flaky pass.
    a1 = (
        ev("simulator", "m-sim", input_tokens=100, output_tokens=20, latency_ms=400.0),
        ev("judge", "m-judge", input_tokens=50, output_tokens=5, latency_ms=900.0),
    )
    a2 = (
        ev("simulator", "m-sim", input_tokens=80, output_tokens=10, latency_ms=600.0),
        ev("judge", "x-unpriced", input_tokens=10, output_tokens=1),  # no latency, no price
        ev("judge", "m-judge", answered=False, latency_ms=5000.0),
    )
    a3 = (
        ev("tts", "tts-1", characters=1000, audio_out_seconds=30.0, latency_ms=200.0),
        ev("stt", "whisper-x", audio_in_seconds=90.0, latency_ms=300.0),
        ev("stt", "whisper-x", latency_ms=310.0),  # no audio reported
    )
    store.write_manifest("A", {"run_id": "A", "suite": "smoke"})
    store.save_record(rec("A", "s1", 0, latencies=(100.0, 250.0), events=a1))
    store.save_record(rec("A", "s1", 1, latencies=(300.0, None), events=a2))
    store.save_record(
        rec(
            "A",
            "s2",
            0,
            language="ar",
            dialect="egyptian",
            latencies=(800.0,),
            events=a3,
            timeline=voice(
                ("voice.time_to_first_audio_ms", None, 650.0, False),
                ("voice.turn_latency_ms", 1, 900.0, False),
                ("voice.barge_in_yield_ms", 1, 320.0, False),
                ("voice.talk_over_ratio", None, 0.12, True),
                ("voice.dead_air_ms", 1, None, False),
                ("voice.asr_confidence", 1, 0.91, False),
                ("voice.agent_asr_wer", 1, 0.2, False),
            ),
        )
    )
    outside = [ev("judge", "m-judge", input_tokens=7, output_tokens=1, latency_ms=100.0)]
    store.save_result(
        result(
            "A",
            DAY,
            "smoke",
            [
                verdict(
                    "s1",
                    VerdictStatus.PASSED,
                    attempt(0, score("goal", ScoreStatus.PASSED, 1.0)),
                    attempt(1, score("goal", ScoreStatus.INFRA_ERROR, None)),
                    flaky=True,
                ),
                verdict(
                    "s2",
                    VerdictStatus.FAILED,
                    attempt(0, score("goal", ScoreStatus.FAILED, 0.25)),
                ),
            ],
            usage=rows_of(*a1, *a2, *a3, *outside),
            outside=outside,
            exit_code=1,
        )
    )
    # Run B (Tue): written before per-call usage (layout 5).
    legacy(store, "B", {"run_id": "B", "suite": "smoke"})
    store.save_record(rec("B", "s1", 0, latencies=(50.0,), events=None))
    store.save_result(
        result(
            "B",
            DAY + timedelta(days=1),
            "smoke",
            [verdict("s1", VerdictStatus.PASSED, attempt(0, score("goal", ScoreStatus.PASSED, 1)))],
            usage=[
                ModelUsage(
                    role="simulator",
                    model="m-sim",
                    calls=2,
                    input_tokens=50,
                    output_tokens=10,
                    cost_usd=0.5,
                )
            ],
        )
    )
    # Run C (next Mon): another suite; a scripted attempt, no call, nothing judged.
    store.write_manifest("C", {"run_id": "C", "suite": "other"})
    store.save_record(rec("C", "s3", 0))
    store.save_result(
        result(
            "C",
            DAY + timedelta(days=7),
            "other",
            [verdict("s3", VerdictStatus.INCONCLUSIVE, attempt(0))],
            exit_code=2,
        )
    )
    # Run D (next Tue): s1 flips to failed; re-scored later.
    d1 = (ev("judge", "m-judge", input_tokens=20, output_tokens=2, latency_ms=700.0),)
    store.write_manifest("D", {"run_id": "D", "suite": "smoke"})
    store.save_record(rec("D", "s1", 0, latencies=(150.0,), events=d1))
    store.save_result(
        result(
            "D",
            DAY + timedelta(days=8),
            "smoke",
            [verdict("s1", VerdictStatus.FAILED, attempt(0, score("goal", ScoreStatus.FAILED, 0)))],
            usage=rows_of(*d1),
            exit_code=1,
        )
    )
    again = result("D", DAY + timedelta(days=8), "smoke", [])
    again.attempt_usage = [
        AttemptUsage(
            test_id="s1",
            attempt_index=0,
            events=(ev("judge", "m-judge", input_tokens=30, output_tokens=3, latency_ms=50.0),),
        )
    ]
    store.save_rescore("D", "20260311T000000Z", again, "<html/>", "<x/>")


def file_legacy(store: FileStore, run_id: str, manifest: dict[str, Any]) -> None:
    store.write_manifest(run_id, manifest)
    path = store.run_dir(run_id) / "manifest.json"
    path.write_text(json.dumps({**manifest, "layout": "opseval-runs/5"}), "utf-8")


@pytest.fixture
def files(tmp_path: Path) -> FileStore:
    store = FileStore(tmp_path / "runs")
    populate(store, file_legacy)
    return store


# --- the definitions ------------------------------------------------------------------------


class TestDefinitions:
    def test_percentiles_are_nearest_rank(self) -> None:
        values = [float(v) for v in range(1, 21)]
        assert nearest_rank(values, 0.5) == 10.0
        assert nearest_rank(values, 0.95) == 19.0
        assert nearest_rank(values, 0.99) == 20.0
        assert nearest_rank([7.0], 0.99) == 7.0
        assert nearest_rank([], 0.5) is None

    def test_an_unmeasured_value_is_counted_apart_never_as_0(self) -> None:
        p = percentiles([None, 10.0, None, 30.0])
        assert (p.n, p.unmeasured, p.p50, p.p99, p.mean) == (2, 2, 10.0, 30.0, 20.0)
        nothing = percentiles([None])
        assert (nothing.n, nothing.unmeasured, nothing.p50, nothing.mean) == (0, 1, None, None)


# --- the folders' answers, by hand ----------------------------------------------------------


class TestLatency:
    def test_turn_latency_counts_first_replies_and_names_the_untimed(
        self, files: FileStore
    ) -> None:
        answer = FileAnalytics(files).latency(UsageFilter(bucket="week"))
        turn = answer.turn_latency
        # A: 100, 250, 300, (None), 800; B: 50; D: 150. The 999 ms second frames are not turns.
        assert (turn.total.n, turn.total.unmeasured) == (6, 1)
        assert (turn.total.p50, turn.total.p95, turn.total.p99) == (150.0, 800.0, 800.0)
        assert [b.key for b in answer.buckets] == ["2026-03-02", "2026-03-09"]
        assert [p.n for p in turn.points] == [5, 1]
        assert answer.time_to_first_audio.total.p50 == 650.0
        assert answer.voice_turn_latency.points[1].n == 0  # a week with none: a gap
        assert answer.voice_turn_latency.points[1].p50 is None

    def test_llm_latency_per_role_answered_calls_only(self, files: FileStore) -> None:
        answer = FileAnalytics(files).latency(UsageFilter())
        roles = {s.group: s.total for s in answer.llm_latency}
        assert set(roles) == {"judge", "simulator", "stt", "tts"}
        # judge: 900 (A), unpriced call with no latency, 100 (outside), 700 (D); the failed
        # call's 5000 ms and the re-score's 50 ms are not the run's answered calls.
        assert (roles["judge"].n, roles["judge"].unmeasured, roles["judge"].p99) == (3, 1, 900.0)

    def test_language_filter(self, files: FileStore) -> None:
        answer = FileAnalytics(files).latency(UsageFilter(language="ar", dialect="egyptian"))
        assert answer.turn_latency.total.n == 1 and answer.scope.attempts == 1

    def test_model_does_not_apply(self, files: FileStore) -> None:
        with pytest.raises(FilterNotApplicable):
            FileAnalytics(files).latency(UsageFilter(model="m-sim"))


class TestQuality:
    def test_pass_rate_counts_judged_results_only(self, files: FileStore) -> None:
        answer = FileAnalytics(files).quality(UsageFilter())
        total = answer.pass_rate_total
        assert (total.passed, total.judged, total.not_judged) == (2, 4, 1)
        assert total.value == 0.5
        by_day = {b.key: p for b, p in zip(answer.buckets, answer.pass_rate, strict=True)}
        assert by_day["2026-03-09"].value is None  # only an inconclusive verdict: not 0%
        assert by_day["2026-03-09"].not_judged == 1

    def test_gates_flaky_checks_and_matrix(self, files: FileStore) -> None:
        answer = FileAnalytics(files).quality(UsageFilter())
        assert [(g.run, g.gate) for g in answer.gate_history] == [
            ("A", "blocked"),
            ("B", "safe"),
            ("C", "not_judged"),
            ("D", "blocked"),
        ]
        flaky = {f.scenario: f for f in answer.flaky}
        assert (flaky["s1"].flips, flaky["s1"].flaky_runs, flaky["s1"].judged_runs) == (1, 1, 3)
        goal = next(c for c in answer.checks if c.scorer_id == "goal")
        assert (goal.pass_rate.passed, goal.pass_rate.judged, goal.pass_rate.not_judged) == (
            2,
            4,
            1,
        )
        assert goal.mean_score == round((1 + 0.25 + 1 + 0) / 4, 6)  # infra_error has none
        assert [r.run for r in answer.matrix.runs] == ["A", "B", "C", "D"]
        s3 = next(r for r in answer.matrix.scenarios if r.id == "s3")
        assert [c.status for c in s3.cells] == [None, None, "inconclusive", None]
        assert answer.judge_agreement is None and "not measured" in answer.judge_agreement_note

    def test_language_filter_picks_verdicts_by_their_first_attempt(self, files: FileStore) -> None:
        answer = FileAnalytics(files).quality(UsageFilter(language="ar"))
        assert (answer.pass_rate_total.judged, answer.pass_rate_total.passed) == (1, 0)


class TestVoice:
    def test_metrics_by_language_with_unmeasured_and_degraded(self, files: FileStore) -> None:
        answer = FileAnalytics(files).voice(UsageFilter())
        metrics = {m.name: m for m in answer.metrics}
        assert answer.voice_attempts == 1
        assert [m.name for m in answer.metrics][0] == "voice.barge_in_yield_ms"
        dead = metrics["voice.dead_air_ms"]
        assert (dead.total.n, dead.total.unmeasured, dead.total.p50) == (0, 1, None)
        talk = metrics["voice.talk_over_ratio"]
        assert (talk.total.p50, talk.degraded) == (0.12, 1)
        assert [(g.language, g.dialect) for g in talk.by_language] == [("ar", "egyptian")]
        assert metrics["voice.agent_asr_wer"].total.mean == 0.2

    def test_no_voice_is_not_measured(self, files: FileStore) -> None:
        answer = FileAnalytics(files).voice(UsageFilter(language="en"))
        assert answer.voice_attempts == 0
        assert all(m.total.n == 0 and m.total.p50 is None for m in answer.metrics)


class TestUsage:
    def test_attempts_and_audio_minutes(self, files: FileStore) -> None:
        answer = FileAnalytics(files).usage(UsageFilter())
        (row,) = answer.by_task
        assert (row.project, row.task, row.runs, row.attempts, row.voice_attempts) == (
            None,
            None,
            4,
            6,
            1,
        )
        audio = row.audio_minutes
        assert (audio.value, audio.measured, audio.of, audio.complete) == (2.0, 2, 3, False)
        assert answer.attempts == [3, 1, 1, 1]
        assert [m.value for m in answer.audio_minutes] == [2.0, None, None, None]

    def test_language_does_not_apply(self, files: FileStore) -> None:
        with pytest.raises(FilterNotApplicable):
            FileAnalytics(files).usage(UsageFilter(language="en"))

    def test_label_is_not_recorded_and_task_needs_server_mode(self, files: FileStore) -> None:
        with pytest.raises(UnsupportedFilter, match="server mode"):
            FileAnalytics(files).usage(UsageFilter(task="default"))
        with pytest.raises(UnsupportedFilter):
            FileAnalytics(files).quality(UsageFilter(label="production"))


class TestKnownPartCost:
    def test_run_rows_and_dashboard_agree_on_the_known_part(self, files: FileStore) -> None:
        from opseval.server.views import run_cost

        a = files.load_result("A")
        detail = run_cost(files, a)
        assert detail is not None
        cost = FileAnalytics(files).cost(UsageFilter())
        dash = {r.run: r.cost_usd for r in cost.runs}["A"]
        # a1 + a2 priced simulator/judge calls, the TTS call, the outside judge call; the
        # unpriced judge and both STT calls have no price; the failed call is not counted.
        assert (detail.priced_calls, detail.unpriced_calls) == (5, 3)
        assert detail.total_usd is None and detail.known_usd == dash.value
        assert (dash.measured, dash.of, dash.complete) == (5, 8, False)

    def test_a_row_says_its_known_part_and_unpriced_calls(self) -> None:
        events = [
            ev("judge", "m-judge", input_tokens=10, output_tokens=1),
            ev("judge", "m-judge", input_tokens=10, output_tokens=1),
        ]
        events.append(ev("judge", "m-judge", input_tokens=10))  # no output: not priced
        row = fold_usage("judge", "m-judge", events)
        assert row.cost_usd is None  # never a total that pretends to be complete
        assert row.unpriced_calls == 1
        assert row.cost_known_usd == pytest.approx(2 * (10 * 10 + 20) / 1e6)

    def test_rows_written_before_layout_9_read_with_the_split_they_tell(self) -> None:
        old = ModelUsage.model_validate(
            {"role": "judge", "model": "m", "calls": 3, "input_tokens": 3, "cost_usd": 0.3}
        )
        assert (old.cost_known_usd, old.unpriced_calls) == (0.3, 0)
        unpriced = ModelUsage.model_validate({"role": "judge", "model": "m", "calls": 2})
        assert (unpriced.cost_known_usd, unpriced.unpriced_calls) == (None, 2)
        partial = ModelUsage.model_validate(
            {
                "role": "judge",
                "model": "m",
                "calls": 2,
                "reported": {"answered": 2, "cost": 1},
            }
        )
        assert partial.unpriced_calls is None  # the split was not recorded: not guessed


# --- the golden test: the folder and the database agree ------------------------------------


QUERIES: list[tuple[str, UsageFilter]] = [
    ("cost", UsageFilter()),
    ("cost", UsageFilter(bucket="week")),
    ("cost", UsageFilter(bucket="run", role="judge")),
    ("cost", UsageFilter(suite="smoke", since=date(2026, 3, 3))),
    ("tokens", UsageFilter()),
    ("tokens", UsageFilter(model="m-judge", bucket="week")),
    ("latency", UsageFilter()),
    ("latency", UsageFilter(bucket="week", language="en")),
    ("latency", UsageFilter(bucket="run", suite="other")),
    ("quality", UsageFilter()),
    ("quality", UsageFilter(bucket="week", language="ar")),
    ("quality", UsageFilter(until=date(2026, 3, 2))),
    ("voice", UsageFilter()),
    ("voice", UsageFilter(language="ar", dialect="egyptian")),
    ("voice", UsageFilter(language="fr")),
    ("usage", UsageFilter()),
    ("usage", UsageFilter(bucket="run")),
    ("usage", UsageFilter(since=date(2030, 1, 1))),
]


def normalised(answer: Any) -> dict[str, Any]:
    """An answer as JSON, without what only one mode has: the tasks a database serves and
    which filters need server mode."""
    data = answer.model_dump(mode="json")
    options = data.get("options", {})
    options.pop("tasks", None)
    options.pop("server_only", None)
    for row in data.get("by_task", []):
        row["project"] = row["task"] = None
    return data


async def test_file_and_database_give_identical_answers(pg_url: str, tmp_path: Path) -> None:
    from opseval.analytics.pg import PgAnalytics
    from opseval.blobs import FileBlobStore
    from opseval.db.run_store import DbRunStore
    from tests.test_db_runs import PROJECT, prepare, settings_for

    files = FileStore(tmp_path / "runs")
    populate(files, file_legacy)
    await prepare(pg_url, tmp_path)
    db = DbRunStore.connect(
        settings_for(pg_url, tmp_path),
        FileBlobStore(tmp_path / "blobs"),
        project=PROJECT,
        task="default",
        task_reads=True,
    )
    try:
        populate(
            db,
            lambda store, run_id, m: store.import_manifest(
                run_id, {**m, "layout": "opseval-runs/5"}
            ),
        )
        folder, database = FileAnalytics(files), PgAnalytics(db)
        for question, filters in QUERIES:
            want = normalised(getattr(folder, question)(filters))
            got = normalised(getattr(database, question)(filters))
            assert got == want, f"{question} {filters}"
        # Not two empty answers agreeing: the database counted what the folder did.
        assert database.latency(UsageFilter()).turn_latency.total.n == 6
        assert database.cost(UsageFilter()).scope.rescore_calls == 1
        assert database.cost(UsageFilter()).scope.legacy_runs == ["B"]
        assert database.voice(UsageFilter()).metrics[0].total.p50 == 320.0
        assert database.quality(UsageFilter()).pass_rate_total.judged == 4
        # The task filter is applied in the database, never ignored.
        assert database.usage(UsageFilter(task="default")).scope.runs == 4
        assert database.usage(UsageFilter(task="nope")).scope.runs == 0
        (row,) = database.usage(UsageFilter()).by_task
        assert (row.project, row.task) == (PROJECT, "default")
        with pytest.raises(FilterNotRecorded):
            database.quality(UsageFilter(label="production"))
        with pytest.raises(UnsupportedFilter, match="serves project"):
            database.cost(UsageFilter(project="other/project"))
    finally:
        db.close()
