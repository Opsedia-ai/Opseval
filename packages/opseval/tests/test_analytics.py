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

from opseval.analytics import FileAnalytics, UnsupportedFilter, UsageFilter
from opseval.config import Settings
from opseval.models.record import RunRecord, TerminalState
from opseval.models.results import Gate, ModelUsage, SuiteResult, TestVerdict, VerdictStatus
from opseval.models.scenario import Criticality
from opseval.models.usage import ModelPrice, PriceTable, UsageEvent
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
    return UsageEvent(at=A_DAY, role=role, model=model, **kw)  # type: ignore[arg-type]


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
        cost = FileAnalytics(store, PRICES).cost(UsageFilter())
        assert m(cost.total_usd) == (0.50221, 5, 7, False)  # a floor: two calls unpriced
        assert cost.passed_tests == 3
        assert cost.cost_per_passed_test == pytest.approx(0.50221 / 3)
        assert cost.sources == {"price_table": 3, "unknown": 2}

    def test_per_run_and_a_run_with_no_calls_is_null_never_zero(self, store: FileStore) -> None:
        runs = {r.run: r for r in FileAnalytics(store, PRICES).cost(UsageFilter()).runs}
        assert list(runs) == ["C", "B", "A"]  # newest first
        assert m(runs["A"].cost_usd) == (0.00221, 3, 5, False)
        assert m(runs["B"].cost_usd) == (0.5, 2, 2, True) and runs["B"].per_call is False
        assert m(runs["C"].cost_usd) == (None, 0, 0, True)
        assert runs["C"].cost_per_passed_test is None

    def test_by_role_over_day_buckets_draws_gaps(self, store: FileStore) -> None:
        cost = FileAnalytics(store, PRICES).cost(UsageFilter())
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
        rows = {r.scenario: r for r in FileAnalytics(store, PRICES).cost(UsageFilter()).scenarios}
        s1 = rows["s1"]
        assert (s1.attempts, s1.attempts_measured, s1.passed_runs) == (2, 2, 1)
        assert m(s1.cost_usd) == (0.00209, 2, 3, False)  # e1 + e2; e3 reported no output
        assert s1.cost_per_attempt == pytest.approx(0.00209 / 2)
        assert m(rows["s2"].cost_usd) == (None, 0, 1, False)  # unpriced: not $0
        assert rows["s2"].cost_per_passed_test is None

    def test_the_scope_names_old_runs_and_failed_calls(self, store: FileStore) -> None:
        scope = FileAnalytics(store, PRICES).cost(UsageFilter()).scope
        assert scope.legacy_runs == ["B"]
        assert (scope.runs, scope.calls, scope.failed_calls) == (3, 8, 1)
        assert (scope.attempts, scope.attempts_without_usage) == (4, 0)

    def test_a_price_added_later_keeps_earlier_runs_cost(self, store: FileStore) -> None:
        later = PriceTable(
            prices=(
                *PRICES.prices,
                ModelPrice(
                    match="m-judge",
                    unit="tokens",
                    effective_from=date(2026, 4, 1),
                    input=1,
                    output=1,
                ),
                ModelPrice(
                    match="x", unit="tokens", effective_from=date(2026, 4, 1), input=1, output=1
                ),
            )
        )
        assert m(FileAnalytics(store, later).cost(UsageFilter()).total_usd) == (
            0.50221,
            5,
            7,
            False,
        )

    def test_a_price_in_force_on_the_day_is_used_even_if_shipped_later(
        self, store: FileStore
    ) -> None:
        backdated = PriceTable(
            prices=(
                *PRICES.prices,
                ModelPrice(
                    match="x", unit="tokens", effective_from=date(2026, 1, 1), input=1, output=1
                ),
            )
        )
        runs = {r.run: r for r in FileAnalytics(store, backdated).cost(UsageFilter()).runs}
        assert m(runs["A"].cost_usd) == (0.00254, 4, 5, False)

    def test_filters(self, store: FileStore) -> None:
        analytics = FileAnalytics(store, PRICES)
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

    @pytest.mark.parametrize("name", ["org", "project", "task", "label"])
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
        tokens = FileAnalytics(store, PRICES).tokens(UsageFilter())
        kinds = {s.group: s.total for s in tokens.by_kind}
        assert list(kinds) == ["input", "cached_input", "output", "reasoning"]
        assert m(kinds["input"]) == (710, 7, 7, True)  # input not reported as cached
        assert m(kinds["cached_input"]) == (50, 1, 7, False)
        assert m(kinds["output"]) == (67, 6, 7, False)  # e3 reported no output
        assert m(kinds["reasoning"]) == (4, 1, 7, False)
        assert m(tokens.totals.input) == (760, 7, 7, True)
        assert m(tokens.totals.total) == (731, 6, 7, False)

    def test_reasoning_share_and_tokens_per_attempt(self, store: FileStore) -> None:
        tokens = FileAnalytics(store, PRICES).tokens(UsageFilter())
        assert tokens.reasoning_share.value == pytest.approx(0.4)
        assert (tokens.reasoning_share.measured, tokens.reasoning_share.of) == (1, 7)
        # s1#0: 330, s2#0: 330, s3#0 (no call): 0; s1#1 left out (a count not reported).
        assert tokens.tokens_per_attempt.value == pytest.approx(220)
        assert (tokens.tokens_per_attempt.measured, tokens.tokens_per_attempt.of) == (3, 4)

    def test_per_model_over_buckets(self, store: FileStore) -> None:
        tokens = FileAnalytics(store, PRICES).tokens(UsageFilter())
        models = {s.group: [m(p) for p in s.points] for s in tokens.by_model}
        assert models["m-sim"] == [(120, 1, 2, False), (60, 2, 2, True), (None, 0, 0, True)]
        assert models["m-judge"][0] == (221, 2, 2, True)

    def test_legacy_runs_have_no_reasoning_or_cache_counts(self, store: FileStore) -> None:
        tokens = FileAnalytics(store, PRICES).tokens(UsageFilter(suite="smoke", since=B_DAY.date()))
        assert tokens.scope.legacy_runs == ["B"]
        assert m(tokens.totals.reasoning) == (None, 0, 2, False)
        assert tokens.tokens_per_attempt.value is None  # no per-call attempts

    def test_per_scenario(self, store: FileStore) -> None:
        rows = {r.scenario: r for r in FileAnalytics(store, PRICES).tokens(UsageFilter()).scenarios}
        assert m(rows["s1"].tokens) == (330, 2, 3, False)
        assert rows["s1"].tokens_per_attempt == pytest.approx(165)
        assert rows["s3"].tokens.value is None and rows["s3"].tokens.of == 0


# --- the API --------------------------------------------------------------------------


@pytest.fixture
def api(store: FileStore) -> TestClient:
    app = create_app(settings=Settings(reports_dir=str(store.root)), token="t")
    return TestClient(app, base_url="http://127.0.0.1:8000", headers={"Authorization": "Bearer t"})


class TestEndpoints:
    def test_cost_is_camel_case_and_null_is_null(self, api: TestClient) -> None:
        body = api.get("/api/v1/analytics/cost").json()
        # The API prices with the built-in table, where these fixture models have no row: only
        # the old run's cost, as written, is known.
        assert body["totalUsd"]["value"] == 0.5
        assert body["totalUsd"]["measured"] == 2 and body["totalUsd"]["complete"] is False
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
