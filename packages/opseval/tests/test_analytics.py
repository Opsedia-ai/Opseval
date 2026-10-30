# SPDX-License-Identifier: Apache-2.0
"""The Cost and Tokens dashboards' aggregates, on a golden fixture (ADR-0052, ADR-0066).

Every value is checked by hand below. Nothing measured is ``None`` (never 0), a partial sum
says how much it covers, a failed call is counted apart, a run written before per-call usage
is named, and a price added later never changes an earlier run's cost.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from opseval import pricing, rescore
from opseval.analytics import FileAnalytics, UnsupportedFilter, UsageFilter
from opseval.config import Settings
from opseval.demo.refund_agent import run_agent
from opseval.llm import UsageMeter, fold_usage
from opseval.models.record import RunRecord, TerminalState
from opseval.models.results import Gate, ModelUsage, SuiteResult, TestVerdict, VerdictStatus
from opseval.models.scenario import Criticality
from opseval.models.usage import AttemptUsage, ModelPrice, PriceTable, UsageEvent
from opseval.pricing import priced
from opseval.rescore import _Rescorer
from opseval.runner import RunOptions, launch
from opseval.server import create_app
from opseval.server.views import usage as run_usage
from opseval.storage import FileStore

PRICES = PriceTable(
    prices=(
        ModelPrice(
            match="m-sim", unit="tokens", effective_from=date(2026, 1, 1), input=1, output=2
        ),
        ModelPrice(
            match="m-judge",
            unit="tokens",
            effective_from=date(2026, 1, 1),
            input=10,
            output=20,
            cached_input=5,
        ),
    )
)
A_DAY = datetime(2026, 3, 1, 9, tzinfo=UTC)
B_DAY = datetime(2026, 3, 2, 9, tzinfo=UTC)
C_DAY = datetime(2026, 3, 9, 9, tzinfo=UTC)


def ev(role: str, model: str, **kw: Any) -> UsageEvent:
    """A call priced as the meter prices it, with PRICES: the cost is stored with it."""
    return priced(UsageEvent(at=A_DAY, role=role, model=model, **kw), PRICES)


def result(
    run_id: str, when: datetime, suite: str, verdicts: dict[str, VerdictStatus], **kw: Any
) -> SuiteResult:
    return SuiteResult(
        opseval_version="0",
        run_id=run_id,
        suite=suite,
        target="ws://x",
        profile="p",
        started_at=when,
        ended_at=when,
        tests=[
            TestVerdict(test_id=t, name=t, criticality=Criticality.NORMAL, status=st)
            for t, st in verdicts.items()
        ],
        gate=Gate(status="pass"),
        exit_code=0,
        **kw,
    )


def record(
    run_id: str, test_id: str, index: int, events: tuple[UsageEvent, ...] | None
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        test_id=test_id,
        attempt_index=index,
        seed=0,
        target="ws://x",
        started_at=A_DAY,
        ended_at=A_DAY,
        terminal_state=TerminalState.GOAL_REACHED,
        usage_events=events,
    )


@pytest.fixture
def store(tmp_path: Path) -> FileStore:
    store = FileStore(tmp_path)
    # Run A: per-call usage. Costs at PRICES: e1 140e-6, e2 1950e-6, e6 120e-6; e3 has no
    # output count and e5 an unpriced model (cost unknown); e4 failed.
    store.write_manifest("A", {"run_id": "A"})
    store.save_record(
        record(
            "A",
            "s1",
            0,
            (
                ev("simulator", "m-sim", input_tokens=100, output_tokens=20),
                ev(
                    "judge",
                    "m-judge",
                    input_tokens=200,
                    cached_input_tokens=50,
                    output_tokens=10,
                    reasoning_tokens=4,
                ),
            ),
        )
    )
    store.save_record(
        record(
            "A",
            "s1",
            1,
            (
                ev("simulator", "m-sim", input_tokens=100),
                ev("judge", "m-judge", answered=False),
            ),
        )
    )
    store.save_record(record("A", "s2", 0, (ev("judge", "x", input_tokens=300, output_tokens=30),)))
    store.save_result(
        result(
            "A",
            A_DAY,
            "smoke",
            {"s1": VerdictStatus.PASSED, "s2": VerdictStatus.FAILED},
            usage_events=[ev("judge", "m-judge", input_tokens=10, output_tokens=1)],
        )
    )
    # Run B: written before per-call usage (layout 5): its row's totals, as written.
    store.write_manifest("B", {"run_id": "B"})
    manifest = store.run_dir("B") / "manifest.json"
    manifest.write_text(json.dumps({"layout": "opseval-runs/5", "run_id": "B"}), "utf-8")
    store.save_result(
        result(
            "B",
            B_DAY,
            "smoke",
            {"s1": VerdictStatus.PASSED},
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
    # Run C: another suite; a scripted attempt that made no call.
    store.write_manifest("C", {"run_id": "C"})
    store.save_record(record("C", "s3", 0, ()))
    store.save_result(result("C", C_DAY, "other", {"s3": VerdictStatus.PASSED}))
    return store


def m(answer: Any) -> tuple[Any, int, int, bool]:
    value = None if answer.value is None else round(answer.value, 8)
    return (value, answer.measured, answer.of, answer.complete)


# --- cost -----------------------------------------------------------------------------


class TestCost:
    def test_totals_say_what_they_cover(self, store: FileStore) -> None:
        cost = FileAnalytics(store).cost(UsageFilter())
        assert m(cost.total_usd) == (0.50221, 5, 7, False)  # a floor: two calls unpriced
        assert cost.passed_tests == 3
        per_passed = cost.cost_per_passed_test
        assert per_passed.value == pytest.approx(0.50221 / 3)
        assert (per_passed.measured, per_passed.of, per_passed.complete) == (3, 3, False)
        assert cost.sources == {"price_table": 3, "unknown": 2}

    def test_per_run_and_a_run_with_no_calls_is_null_never_zero(self, store: FileStore) -> None:
        runs = {r.run: r for r in FileAnalytics(store).cost(UsageFilter()).runs}
        assert list(runs) == ["C", "B", "A"]  # newest first
        assert m(runs["A"].cost_usd) == (0.00221, 3, 5, False)
        assert m(runs["B"].cost_usd) == (0.5, 2, 2, True) and runs["B"].per_call is False
        assert m(runs["C"].cost_usd) == (None, 0, 0, True)
        assert runs["C"].cost_per_passed_test.value is None

    def test_by_role_over_day_buckets_draws_gaps(self, store: FileStore) -> None:
        cost = FileAnalytics(store).cost(UsageFilter())
        assert [b.key for b in cost.buckets] == ["2026-03-01", "2026-03-02", "2026-03-09"]
        roles = {s.group: [m(p) for p in s.points] for s in cost.by_role}
        assert roles["simulator"] == [
            (0.00014, 1, 2, False),
            (0.5, 2, 2, True),
            (None, 0, 0, True),
        ]
        assert roles["judge"] == [(0.00207, 2, 3, False), (None, 0, 0, True), (None, 0, 0, True)]
        models = {s.group for s in cost.by_model}
        assert models == {"m-sim", "m-judge", "x"}

    def test_per_scenario(self, store: FileStore) -> None:
        rows = {r.scenario: r for r in FileAnalytics(store).cost(UsageFilter()).scenarios}
        s1 = rows["s1"]
        assert (s1.attempts, s1.attempts_measured, s1.passed_runs) == (2, 2, 1)
        assert m(s1.cost_usd) == (0.00209, 2, 3, False)  # e1 + e2; e3 reported no output
        assert s1.cost_per_attempt.value == pytest.approx(0.00209 / 2)
        assert s1.cost_per_attempt.complete is False  # its numerator is a floor
        assert m(rows["s2"].cost_usd) == (None, 0, 1, False)  # unpriced: not $0
        assert rows["s2"].cost_per_passed_test.value is None

    def test_the_scope_names_old_runs_and_failed_calls(self, store: FileStore) -> None:
        scope = FileAnalytics(store).cost(UsageFilter()).scope
        assert scope.legacy_runs == ["B"]
        assert (scope.runs, scope.calls, scope.failed_calls) == (3, 8, 1)
        assert (scope.attempts, scope.attempts_without_usage) == (4, 0)

    def test_a_price_added_later_keeps_earlier_runs_cost_in_every_view(
        self, store: FileStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cost stored with a call is the cost (ADR-0066): a row added later, even one
        backdated to before the run, changes neither the dashboard nor the Usage page."""
        calls = (
            ev("simulator", "m-sim", input_tokens=100, output_tokens=20),
            ev("judge", "m-judge", input_tokens=200, cached_input_tokens=50, output_tokens=10),
        )
        store.write_manifest("D", {"run_id": "D"})
        store.save_record(record("D", "s1", 0, calls))
        rows = [fold_usage(e.role, e.model, [e]) for e in calls]
        store.save_result(result("D", C_DAY, "smoke", {"s1": VerdictStatus.PASSED}, usage=rows))
        before = FileAnalytics(store).cost(UsageFilter())
        page = {r.run: r for r in run_usage(store)}
        backdated = PriceTable(
            prices=(
                *PRICES.prices,
                ModelPrice(
                    match=".*", unit="tokens", effective_from=date(2020, 1, 1), input=99, output=99
                ),
                ModelPrice(
                    match=".*", unit="tokens", effective_from=date(2026, 2, 1), input=7, output=7
                ),
            )
        )
        monkeypatch.setattr(pricing, "builtin_prices", lambda: backdated)
        after = FileAnalytics(store).cost(UsageFilter())
        assert m(after.total_usd) == m(before.total_usd) == (0.5043, 7, 9, False)
        assert {r.run: m(r.cost_usd) for r in after.runs} == {
            r.run: m(r.cost_usd) for r in before.runs
        }
        assert after.sources == before.sources == {"price_table": 5, "unknown": 2}
        assert {r.run: r for r in run_usage(store)} == page
        # Fully priced, the run's own rows and the dashboard give the same figure.
        runs = {r.run: r for r in after.runs}
        assert page["D"].cost_usd == runs["D"].cost_usd.value == 0.00209
        assert page["D"].cost_complete is runs["D"].cost_usd.complete is True

    def test_filters(self, store: FileStore) -> None:
        analytics = FileAnalytics(store)
        judge = analytics.cost(UsageFilter(role="judge"))
        assert m(judge.total_usd) == (0.00207, 2, 3, False)
        assert [r.run for r in analytics.cost(UsageFilter(suite="other")).runs] == ["C"]
        since = analytics.cost(UsageFilter(since=date(2026, 3, 2), until=date(2026, 3, 2)))
        assert [r.run for r in since.runs] == ["B"]
        sim = analytics.cost(UsageFilter(model="m-sim"))
        assert m(sim.total_usd) == (0.50014, 3, 4, False)
        weeks = analytics.cost(UsageFilter(bucket="week")).buckets
        assert [b.key for b in weeks] == ["2026-02-23", "2026-03-02", "2026-03-09"]
        runs = analytics.cost(UsageFilter(bucket="run")).buckets
        assert [b.key for b in runs] == ["A", "B", "C"]
        assert analytics.cost(UsageFilter()).options.suites == ["other", "smoke"]

    @pytest.mark.parametrize("name", ["org", "project", "task"])
    def test_a_server_only_filter_is_refused_never_ignored(
        self, store: FileStore, name: str
    ) -> None:
        with pytest.raises(UnsupportedFilter, match="server mode"):
            FileAnalytics(store).cost(UsageFilter(**{name: "x"}))  # type: ignore[arg-type]

    def test_an_empty_folder_measures_nothing(self, tmp_path: Path) -> None:
        cost = FileAnalytics(FileStore(tmp_path / "none")).cost(UsageFilter())
        assert cost.total_usd.value is None and cost.buckets == [] and cost.runs == []


# --- tokens ---------------------------------------------------------------------------


class TestTokens:
    def test_stack_segments_add_up_and_null_stays_null(self, store: FileStore) -> None:
        tokens = FileAnalytics(store).tokens(UsageFilter())
        kinds = {s.group: s.total for s in tokens.by_kind}
        assert list(kinds) == ["input", "cached_input", "output", "reasoning"]
        assert m(kinds["input"]) == (710, 7, 7, True)  # input not reported as cached
        assert m(kinds["cached_input"]) == (50, 1, 7, False)
        assert m(kinds["output"]) == (67, 6, 7, False)  # e3 reported no output
        assert m(kinds["reasoning"]) == (4, 1, 7, False)
        assert m(tokens.totals.input) == (760, 7, 7, True)
        assert m(tokens.totals.total) == (731, 6, 7, False)

    def test_reasoning_share_and_tokens_per_attempt(self, store: FileStore) -> None:
        tokens = FileAnalytics(store).tokens(UsageFilter())
        assert tokens.reasoning_share.value == pytest.approx(0.4)
        assert (tokens.reasoning_share.measured, tokens.reasoning_share.of) == (1, 7)
        # s1#0: 330, s2#0: 330, s3#0 (no call): 0; s1#1 left out (a count not reported).
        per = tokens.tokens_per_attempt
        assert per.value == pytest.approx(220)
        assert (per.measured, per.of, per.complete) == (3, 4, True)

    def test_per_model_over_buckets(self, store: FileStore) -> None:
        tokens = FileAnalytics(store).tokens(UsageFilter())
        models = {s.group: [m(p) for p in s.points] for s in tokens.by_model}
        assert models["m-sim"] == [(120, 1, 2, False), (60, 2, 2, True), (None, 0, 0, True)]
        assert models["m-judge"][0] == (221, 2, 2, True)

    def test_legacy_runs_have_no_reasoning_or_cache_counts(self, store: FileStore) -> None:
        tokens = FileAnalytics(store).tokens(UsageFilter(suite="smoke", since=B_DAY.date()))
        assert tokens.scope.legacy_runs == ["B"]
        assert m(tokens.totals.reasoning) == (None, 0, 2, False)
        assert tokens.tokens_per_attempt.value is None  # no per-call attempts

    def test_per_scenario(self, store: FileStore) -> None:
        rows = {r.scenario: r for r in FileAnalytics(store).tokens(UsageFilter()).scenarios}
        assert m(rows["s1"].tokens) == (330, 2, 3, False)
        assert rows["s1"].tokens_per_attempt.value == pytest.approx(165)
        assert rows["s1"].tokens_per_attempt.complete is False  # s1#1 reported no output
        assert rows["s3"].tokens.value is None and rows["s3"].tokens.of == 0


# --- review of #86 --------------------------------------------------------------------


def fresh(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path / "fresh")


class TestReview:
    def test_a_voice_attempt_speech_calls_never_make_tokens_partial(self, tmp_path: Path) -> None:
        store = fresh(tmp_path)
        store.write_manifest("V", {"run_id": "V"})
        calls = (
            ev("judge", "m-judge", input_tokens=200, output_tokens=10),
            ev("tts", "tts-1", characters=40, audio_out_seconds=2.0),
        )
        store.save_record(record("V", "v1", 0, calls))
        rows = [fold_usage(e.role, e.model, [e]) for e in calls]
        store.save_result(result("V", A_DAY, "voice", {"v1": VerdictStatus.PASSED}, usage=rows))
        tokens = FileAnalytics(store).tokens(UsageFilter())
        assert m(tokens.totals.total) == (210, 1, 1, True)
        assert m(tokens.totals.input) == (200, 1, 1, True)
        kinds = {s.group: s.total for s in tokens.by_kind}
        assert m(kinds["input"]) == (200, 1, 1, True) and m(kinds["output"]) == (10, 1, 1, True)
        assert {s.group for s in tokens.by_model} == {"m-judge"}
        per = tokens.tokens_per_attempt
        assert (per.value, per.measured, per.of, per.complete) == (210, 1, 1, True)
        assert (tokens.reasoning_share.measured, tokens.reasoning_share.of) == (0, 1)
        (row,) = tokens.scenarios
        assert m(row.tokens) == (210, 1, 1, True) and row.tokens_per_attempt.value == 210
        # The speech call still costs money: it is in the cost, just not in the tokens.
        assert FileAnalytics(store).cost(UsageFilter()).scope.calls == 2
        # And the run's own usage page agrees.
        (page,) = run_usage(store)
        assert page.judge_tokens == 210 and page.judge_tokens_complete is True

    def test_ratios_divide_by_measured_units_and_say_when_they_are_a_floor(
        self, tmp_path: Path
    ) -> None:
        store = fresh(tmp_path)
        # Run P: s1 passed with per-call usage; s2 passed but its record has none (it was
        # saved before scoring and never again); s3 failed with an unpriced call.
        store.write_manifest("P", {"run_id": "P"})
        store.save_record(
            record("P", "s1", 0, (ev("simulator", "m-sim", input_tokens=1_000, output_tokens=0),))
        )
        store.save_record(record("P", "s2", 0, None))
        store.save_record(
            record("P", "s3", 0, (ev("judge", "x", input_tokens=5, output_tokens=5),))
        )
        verdicts = {"s1": VerdictStatus.PASSED, "s2": VerdictStatus.PASSED}
        store.save_result(result("P", A_DAY, "smoke", verdicts | {"s3": VerdictStatus.FAILED}))
        cost = FileAnalytics(store).cost(UsageFilter())
        assert m(cost.total_usd) == (0.001, 1, 2, False)
        r = cost.cost_per_passed_test
        # Over s1 only (s2's cost is unknown, so it does not dilute): a floor (s3 unpriced).
        assert (r.value, r.measured, r.of, r.complete) == (0.001, 1, 2, False)
        (run,) = cost.runs
        assert m(run.cost_per_passed_test) == (0.001, 1, 2, False)
        rows = {row.scenario: row for row in cost.scenarios}
        assert rows["s1"].passed_runs == 1 and rows["s2"].passed_runs == 0
        assert m(rows["s2"].cost_per_passed_test) == (None, 0, 1, True)
        assert m(rows["s2"].cost_per_attempt) == (None, 0, 1, True)
        assert m(rows["s1"].cost_per_attempt) == (0.001, 1, 1, True)
        assert rows["s3"].cost_per_attempt.value is None  # unpriced: not measured, never $0

    def test_a_run_with_unreadable_records_contributes_nothing(self, store: FileStore) -> None:
        healthy = FileAnalytics(store).cost(UsageFilter())
        broken = store.run_dir("C") / "records" / "s9__attempt0.json"
        broken.write_text("{not json", "utf-8")
        store.save_result(
            result(
                "C",
                C_DAY,
                "other",
                {"s3": VerdictStatus.PASSED},
                usage_events=[ev("judge", "m-judge", input_tokens=10, output_tokens=1)],
            )
        )
        for answer in (
            FileAnalytics(store).cost(UsageFilter()),
            FileAnalytics(store).tokens(UsageFilter()),
        ):
            assert answer.scope.unreadable_runs == ["C"]
            assert answer.scope.runs == 2
            assert "2026-03-09" not in [b.key for b in answer.buckets]
        cost = FileAnalytics(store).cost(UsageFilter())
        assert "C" not in [r.run for r in cost.runs]
        assert cost.passed_tests == healthy.passed_tests - 1
        assert m(cost.total_usd) == m(healthy.total_usd)  # its outside call is not counted
        assert "s3" not in [row.scenario for row in cost.scenarios]

    def test_the_latest_rescore_is_counted_with_its_run_and_shown_apart(
        self, store: FileStore
    ) -> None:
        healthy = FileAnalytics(store).cost(UsageFilter())
        again = ev("judge", "m-judge", input_tokens=1_000, output_tokens=0)  # 0.01
        older = ev("judge", "m-judge", input_tokens=100_000, output_tokens=0)
        base = store.load_result("A")
        for stamp, call in (("20260302T000000000000Z", older), ("20260303T000000000000Z", again)):
            store.save_rescore(
                "A",
                stamp,
                base.model_copy(
                    update={
                        "usage_events": [],
                        "attempt_usage": [
                            AttemptUsage(test_id="s1", attempt_index=0, events=(call,))
                        ],
                    }
                ),
                "",
                "",
            )
        cost = FileAnalytics(store).cost(UsageFilter())
        assert cost.scope.rescored_runs == ["A"] and cost.scope.rescore_calls == 1
        assert cost.scope.runs == healthy.scope.runs  # never a run of its own
        roles = {s.group: s.total for s in cost.by_role}
        assert m(roles["judge (rescore)"]) == (0.01, 1, 1, True)
        assert m(roles["judge"]) == m({s.group: s.total for s in healthy.by_role}["judge"])
        assert cost.total_usd.value == pytest.approx((healthy.total_usd.value or 0) + 0.01)
        runs = {r.run: r for r in cost.runs}
        assert runs["A"].cost_usd.value == pytest.approx(0.00221 + 0.01)
        # Per-scenario figures are the attempts as they ran, not their re-grading.
        s1 = {r.scenario: r for r in cost.scenarios}["s1"]
        assert m(s1.cost_usd) == (0.00209, 2, 3, False)
        tokens = FileAnalytics(store).tokens(UsageFilter())
        assert tokens.scope.rescore_calls == 1
        assert tokens.totals.input.value == 760 + 1_000

    def test_one_request_parses_each_result_once(
        self, store: FileStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        load = FileStore.load_result

        def counting(self: FileStore, run_id: str) -> SuiteResult:
            seen.append(run_id)
            return load(self, run_id)

        monkeypatch.setattr(FileStore, "load_result", counting)
        analytics = FileAnalytics(store)
        analytics.cost(UsageFilter())
        assert sorted(seen) == ["A", "B", "C"]  # once each, options and answer together
        analytics.tokens(UsageFilter())
        analytics.cost(UsageFilter(role="judge"))
        assert sorted(seen) == ["A", "B", "C"]  # unchanged files: parsed from the cache
        store.save_result(store.load_result("C").model_copy(update={"exit_code": 1}))
        seen.clear()
        analytics.cost(UsageFilter())
        assert seen == ["C"]  # only the one changed run is read again

    async def test_rescore_attributes_its_calls_to_attempts(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "reports"))
        async with run_agent() as url:
            run = await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1),
                settings,
            )
        meters: list[UsageMeter] = []

        class Captured(UsageMeter):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                meters.append(self)

        score = _Rescorer.score

        async def judged(self: _Rescorer, *args: Any) -> Any:
            meters[-1].record("judge", "m-judge", {"prompt_tokens": 3, "completion_tokens": 1}, 0)
            return await score(self, *args)

        monkeypatch.setattr(rescore, "UsageMeter", Captured)
        monkeypatch.setattr(_Rescorer, "score", judged)
        outcome = await rescore.rescore_run(Path(settings.reports_dir) / run.run_id, settings)
        attempts = {(a.test_id, a.attempt_index) for a in outcome.result.attempt_usage}
        judged_attempts = {(t.test_id, a.attempt_index) for t in run.tests for a in t.attempts}
        assert attempts == judged_attempts and attempts
        assert outcome.result.usage_events == []
        cost = FileAnalytics(FileStore(settings.reports_dir)).cost(UsageFilter())
        assert cost.scope.rescored_runs == [run.run_id]
        assert cost.scope.rescore_calls == len(attempts)


# --- the API --------------------------------------------------------------------------


@pytest.fixture
def api(store: FileStore) -> TestClient:
    app = create_app(settings=Settings(reports_dir=str(store.root)), token="t")
    return TestClient(app, base_url="http://127.0.0.1:8000", headers={"Authorization": "Bearer t"})


class TestEndpoints:
    def test_cost_is_camel_case_and_null_is_null(self, api: TestClient) -> None:
        body = api.get("/api/v1/analytics/cost").json()
        # The cost stored with each call, never repriced with the built-in table.
        assert body["totalUsd"]["value"] == 0.50221
        assert body["totalUsd"]["measured"] == 5 and body["totalUsd"]["complete"] is False
        assert body["costPerPassedTest"]["complete"] is False
        runs = {r["run"]: r for r in body["runs"]}
        assert runs["C"]["costUsd"]["value"] is None
        assert body["scope"]["legacyRuns"] == ["B"]
        assert body["options"]["serverOnly"] == ["org", "project", "task", "label"]

    def test_tokens_with_filters(self, api: TestClient) -> None:
        body = api.get(
            "/api/v1/analytics/tokens", params={"role": "judge", "bucket": "week"}
        ).json()
        assert body["filters"] == {
            "suite": None,
            "model": None,
            "role": "judge",
            "since": None,
            "until": None,
            "bucket": "week",
            "task": None,
            "language": None,
            "dialect": None,
        }
        assert {s["group"] for s in body["byModel"]} == {"m-judge", "x"}

    def test_a_server_only_filter_is_a_named_400(self, api: TestClient) -> None:
        res = api.get("/api/v1/analytics/cost", params={"project": "p"})
        assert res.status_code == 400 and res.json()["code"] == "FILTER_NEEDS_SERVER_MODE"

    def test_a_backwards_range_and_a_bad_bucket_are_refused(self, api: TestClient) -> None:
        res = api.get(
            "/api/v1/analytics/cost", params={"since": "2026-03-09", "until": "2026-03-01"}
        )
        assert res.status_code == 422 and res.json()["code"] == "BAD_RANGE"
        assert api.get("/api/v1/analytics/tokens", params={"bucket": "year"}).status_code == 422

    def test_the_usage_page_names_runs_before_per_call_usage(self, store: FileStore) -> None:
        rows = {r.run: r for r in run_usage(store)}
        assert rows["B"].per_call is False and rows["A"].per_call is True

    def test_the_usage_page_says_when_a_token_total_is_a_floor(self, tmp_path: Path) -> None:
        store = fresh(tmp_path)
        store.write_manifest("U", {"run_id": "U"})
        rows = [
            ModelUsage(role="simulator", model="a", calls=1, input_tokens=10, output_tokens=2),
            ModelUsage(role="simulator", model="b", calls=1, input_tokens=5, output_tokens=None),
            ModelUsage(role="judge", model="j", calls=1, input_tokens=7, output_tokens=1),
        ]
        store.save_result(result("U", A_DAY, "smoke", {}, usage=rows))
        (page,) = run_usage(store)
        assert (page.simulator_tokens, page.simulator_tokens_complete) == (12, False)
        assert (page.judge_tokens, page.judge_tokens_complete) == (8, True)
