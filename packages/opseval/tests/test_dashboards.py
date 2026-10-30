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


# --- the API ------------------------------------------------------------------------------


@pytest.fixture
def api(files: FileStore) -> Any:
    from fastapi.testclient import TestClient

    from opseval.config import Settings
    from opseval.server import create_app

    app = create_app(settings=Settings(_env_file=None, reports_dir=str(files.root)), token="t")
    return TestClient(app, base_url="http://127.0.0.1:8000", headers={"Authorization": "Bearer t"})


@pytest.fixture
def empty_api(tmp_path: Path) -> Any:
    from fastapi.testclient import TestClient

    from opseval.config import Settings
    from opseval.server import create_app

    app = create_app(
        settings=Settings(_env_file=None, reports_dir=str(tmp_path / "none")), token="t"
    )
    return TestClient(app, base_url="http://127.0.0.1:8000", headers={"Authorization": "Bearer t"})


class TestEndpoints:
    def test_every_dashboard_answers_null_not_0(self, api: Any) -> None:
        latency = api.get("/api/v1/analytics/latency", params={"bucket": "week"}).json()
        assert latency["rule"] == "nearest-rank"
        gap = latency["voiceTurnLatency"]["points"][1]
        assert gap == {"n": 0, "unmeasured": 0, "p50": None, "p95": None, "p99": None, "mean": None}
        quality = api.get("/api/v1/analytics/quality").json()
        # Mar 9: only an inconclusive verdict. Not judged is a gap, never a 0% day.
        assert quality["passRate"][2]["value"] is None and quality["passRate"][2]["notJudged"] == 1
        assert quality["judgeAgreement"] is None
        assert quality["gateHistory"][2]["gate"] == "not_judged"
        voice = api.get("/api/v1/analytics/voice").json()
        dead = next(m for m in voice["metrics"] if m["name"] == "voice.dead_air_ms")
        assert dead["total"]["p50"] is None and dead["total"]["unmeasured"] == 1
        usage = api.get("/api/v1/analytics/usage", params={"bucket": "run"}).json()
        assert usage["audioMinutes"][1]["value"] is None and usage["audioMinutes"][1]["of"] == 0
        assert usage["byTask"][0]["project"] is None

    def test_an_empty_store_measures_nothing(self, empty_api: Any) -> None:
        for name in ("latency", "quality", "voice", "usage"):
            body = empty_api.get(f"/api/v1/analytics/{name}").json()
            assert body["scope"]["runs"] == 0, name
        quality = empty_api.get("/api/v1/analytics/quality").json()
        assert quality["passRateTotal"] == {
            "value": None,
            "passed": 0,
            "judged": 0,
            "notJudged": 0,
        }
        latency = empty_api.get("/api/v1/analytics/latency").json()
        assert latency["turnLatency"]["total"]["p50"] is None and latency["buckets"] == []

    def test_filters_are_applied_or_refused(self, api: Any) -> None:
        r = api.get("/api/v1/analytics/quality", params={"model": "m-sim"})
        assert r.status_code == 400 and r.json()["code"] == "FILTER_NOT_APPLICABLE"
        r = api.get("/api/v1/analytics/voice", params={"task": "default"})
        assert r.status_code == 400 and r.json()["code"] == "FILTER_NEEDS_SERVER_MODE"
        r = api.get("/api/v1/analytics/cost", params={"language": "ar"})
        assert r.status_code == 400 and r.json()["code"] == "FILTER_NOT_APPLICABLE"
        body = api.get("/api/v1/analytics/quality", params={"language": "ar"}).json()
        assert body["filters"]["language"] == "ar" and body["passRateTotal"]["judged"] == 1

    def test_run_detail_and_usage_page_carry_the_split(self, api: Any) -> None:
        detail = api.get("/api/v1/runs/A").json()
        assert detail["cost"]["unpricedCalls"] == 3 and detail["cost"]["totalUsd"] is None
        dash = {r["run"]: r for r in api.get("/api/v1/analytics/cost").json()["runs"]}
        assert detail["cost"]["knownUsd"] == dash["A"]["costUsd"]["value"]
        page = {r["run"]: r for r in api.get("/api/v1/usage").json()}
        assert page["A"]["costUsd"] == detail["cost"]["knownUsd"]
        assert page["A"]["unpricedCalls"] == 3 and page["A"]["costComplete"] is False
        judge = next(u for u in detail["usage"] if u["model"] == "x-unpriced")
        assert judge["costUsd"] is None and judge["unpricedCalls"] == 1


class TestPricesInLocalMode:
    def test_the_built_in_table_is_read_only(self, api: Any) -> None:
        body = api.get("/api/v1/prices").json()
        assert body["canEdit"] is False and "server mode" in body["editNote"]
        assert body["project"] == [] and any(p["match"].endswith("gpt-4o") for p in body["builtin"])
        r = api.post(
            "/api/v1/prices",
            json={"pattern": "m", "matchKind": "exact", "unit": "tokens", "input": 1, "output": 1},
        )
        assert r.status_code == 409 and r.json()["code"] == "PRICES_READ_ONLY"

    def test_resolve_names_the_row_or_no_price(self, api: Any) -> None:
        found = api.get(
            "/api/v1/prices/resolve", params={"model": "gpt-4o-mini", "at": "2024-09-01"}
        ).json()
        assert found["source"] == "builtin" and found["price"]["cachedInput"] is None
        later = api.get(
            "/api/v1/prices/resolve", params={"model": "GPT-4o-mini", "at": "2025-01-01"}
        ).json()
        assert later["price"]["cachedInput"] == 0.075
        none = api.get("/api/v1/prices/resolve", params={"model": "my-own-model"}).json()
        assert none["price"] is None and none["source"] is None


class TestPricePatterns:
    @pytest.mark.parametrize(
        "pattern",
        ["(a+)+$", "gpt-4o?", "a|b", "[a-z]+", "*", "**", "a*b*c*d*e*f", "x" * 201, "two words"],
    )
    def test_a_regex_or_anything_but_a_name_or_glob_is_refused(self, pattern: str) -> None:
        with pytest.raises(ValueError):
            ModelPrice(
                match=pattern,
                match_kind="glob",
                unit="tokens",
                effective_from=date(2030, 1, 1),
                effective_at=datetime(2030, 1, 1, tzinfo=UTC),
                project=True,
                input=1,
                output=1,
            )

    def test_a_project_row_cannot_be_a_regex(self) -> None:
        with pytest.raises(ValueError, match="never a regex"):
            ModelPrice(
                match="gpt-4o",
                unit="tokens",
                effective_from=date(2030, 1, 1),
                effective_at=datetime(2030, 1, 1, tzinfo=UTC),
                project=True,
                input=1,
                output=1,
            )

    def test_globs_match_in_linear_time(self) -> None:
        from opseval.models.usage import glob_match

        assert glob_match("gpt-4o*", "GPT-4o-mini-2024-07-18")
        assert glob_match("openai/*-mini", "openai/gpt-4o-mini")
        assert not glob_match("gpt-4o*", "my-gpt-4o")
        # The classic catastrophic-backtracking shape, as a glob: answered at once.
        assert not glob_match("a*a*a*a*b", "a" * 5000)


# --- prices in server mode -----------------------------------------------------------------


@pytest.fixture
async def world(pg_url: str, tmp_path: Path) -> Any:
    from tests.test_accounts_db import world as make_world

    gen = make_world.__wrapped__(pg_url, tmp_path)  # type: ignore[attr-defined]
    value = await gen.__anext__()
    try:
        yield value
    finally:
        await gen.aclose()


NEW_PRICE = {
    "pattern": "m-sim",
    "matchKind": "exact",
    "unit": "tokens",
    "input": 100,
    "output": 200,
    "source": "our contract",
}


class TestPricesInServerMode:
    def test_only_an_admin_adds_a_row_and_it_is_audited(self, world: Any) -> None:
        viewer = world.login("viewer@acme.test")
        listed = viewer.get("/api/v1/prices").json()
        assert listed["canEdit"] is False and "admin" in listed["editNote"]
        assert viewer.post("/api/v1/prices", json=NEW_PRICE).status_code == 403
        member = world.login("member@acme.test")
        assert member.post("/api/v1/prices", json=NEW_PRICE).status_code == 403
        admin = world.login("admin@acme.test")
        assert admin.get("/api/v1/prices").json()["canEdit"] is True
        r = admin.post("/api/v1/prices", json=NEW_PRICE)
        assert r.status_code == 201, r.text
        row = r.json()
        assert row["project"] is True and row["matchKind"] == "exact" and row["createdBy"]
        assert [p["id"] for p in admin.get("/api/v1/prices").json()["project"]] == [row["id"]]
        (event,) = world.events("price.create")
        assert event.detail["pattern"] == "m-sim" and event.actor_kind == "user"
        assert not world.events("POST /prices")  # one specific event, not the generic one

    def test_a_key_never_edits_prices(self, world: Any) -> None:
        from fastapi.testclient import TestClient

        from tests.test_accounts_db import BASE

        header = world.key(
            "owner@acme.test", ["runs:read", "runs:start", "catalogue:write", "catalogue:label"]
        )
        c = TestClient(world.app, base_url=BASE, headers={"Authorization": header})
        assert c.post("/api/v1/prices", json=NEW_PRICE).status_code == 403

    def test_a_row_is_new_never_backdated_and_validated(self, world: Any) -> None:
        admin = world.login("admin@acme.test")
        r = admin.post("/api/v1/prices", json={**NEW_PRICE, "effectiveAt": "2020-01-01T00:00:00Z"})
        assert r.status_code == 422 and r.json()["code"] == "PRICE_BACKDATED"
        r = admin.post(
            "/api/v1/prices", json={**NEW_PRICE, "pattern": "(a+)+$", "matchKind": "glob"}
        )
        assert r.status_code == 422 and r.json()["code"] == "INVALID_PRICE"
        r = admin.post("/api/v1/prices", json={**NEW_PRICE, "matchKind": "regex"})
        assert r.status_code == 422
        r = admin.post("/api/v1/prices", json={**NEW_PRICE, "output": None})
        assert r.status_code == 422 and "input and output" in r.json()["message"]
        first = admin.post("/api/v1/prices", json=NEW_PRICE).json()
        second = admin.post("/api/v1/prices", json={**NEW_PRICE, "input": 300}).json()
        rows = admin.get("/api/v1/prices").json()["project"]
        assert [p["id"] for p in rows] == [first["id"], second["id"]]  # added, never rewritten
        assert rows[0]["input"] == 100

    def test_a_price_change_does_not_alter_earlier_runs_cost(self, world: Any) -> None:
        from opseval.llm import UsageMeter

        store = world.store
        populate(
            store,
            lambda st, run_id, m: st.import_manifest(run_id, {**m, "layout": "opseval-runs/5"}),
        )
        admin = world.login("admin@acme.test")
        before = admin.get("/api/v1/analytics/cost").json()
        detail = admin.get("/api/v1/runs/A").json()["cost"]
        assert admin.post("/api/v1/prices", json=NEW_PRICE).status_code == 201
        after = admin.get("/api/v1/analytics/cost").json()
        assert after["totalUsd"] == before["totalUsd"] and after["runs"] == before["runs"]
        assert admin.get("/api/v1/runs/A").json()["cost"] == detail
        # A call made from now on is priced with the project's row, which wins over the
        # built-in table; resolving says which row prices it.
        meter = UsageMeter(prices=store.price_table())
        event = meter.record(
            "simulator", "m-sim", {"prompt_tokens": 1000, "completion_tokens": 0}, 0
        )
        assert event.cost_usd == pytest.approx(0.1) and event.cost_source == "price_table"
        resolved = admin.get("/api/v1/prices/resolve", params={"model": "M-SIM"}).json()
        assert resolved["source"] == "project" and resolved["price"]["input"] == 100
        earlier = admin.get(
            "/api/v1/prices/resolve", params={"model": "m-sim", "at": "2026-03-02"}
        ).json()
        assert earlier["price"] is None  # no row was in force then: "no price", never $0

    def test_the_server_answers_dashboards_from_the_database(self, world: Any) -> None:
        populate(
            world.store,
            lambda st, run_id, m: st.import_manifest(run_id, {**m, "layout": "opseval-runs/5"}),
        )
        admin = world.login("viewer@acme.test")
        body = admin.get("/api/v1/analytics/usage").json()
        assert body["byTask"][0]["project"] == "acme/bot" and body["byTask"][0]["task"] == "default"
        assert body["options"]["tasks"] == ["default"]
        r = admin.get("/api/v1/analytics/latency", params={"project": "rival/x"})
        assert r.status_code == 400 and r.json()["code"] == "FILTER_OUTSIDE_SCOPE"
        r = admin.get("/api/v1/analytics/latency", params={"label": "production"})
        assert r.status_code == 400 and r.json()["code"] == "FILTER_NOT_RECORDED"
