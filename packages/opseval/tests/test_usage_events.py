# SPDX-License-Identifier: Apache-2.0
"""Usage events and the versioned price table (ADR-0066).

A count the provider did not report is ``None`` from the meter to the result, never 0; a
model with no price costs ``None``, never $0; and a price added later never changes what an
earlier call cost.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from opseval.config import Settings
from opseval.demo.refund_agent import run_agent
from opseval.errors import LLMInfraError
from opseval.llm import ChatClient, UsageMeter, token_counts
from opseval.models.record import RunRecord
from opseval.models.results import ModelUsage, SuiteResult
from opseval.models.usage import ModelPrice, PriceTable, UsageEvent
from opseval.pricing import builtin_prices, price_for, priced, repriced
from opseval.runner import RunOptions, launch
from opseval.scoring import ScoringEngine
from opseval.storage import FileStore

WHEN = datetime(2026, 3, 1, 12, tzinfo=UTC)


def table(*rows: ModelPrice) -> PriceTable:
    return PriceTable(prices=rows)


def tokens_price(day: date, inp: float, out: float, **kw: Any) -> ModelPrice:
    return ModelPrice(match="m", unit="tokens", effective_from=day, input=inp, output=out, **kw)


def event(**kw: Any) -> UsageEvent:
    base: dict[str, Any] = {"at": WHEN, "role": "judge", "model": "m"}
    return UsageEvent(**(base | kw))


# --- reading the provider's counts ----------------------------------------------------


class TestTokenCounts:
    def test_reasoning_and_cached_come_from_the_details(self) -> None:
        counts = token_counts(
            {
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "prompt_tokens_details": {"cached_tokens": 60},
                "completion_tokens_details": {"reasoning_tokens": 25},
            }
        )
        assert counts == {
            "input_tokens": 100,
            "output_tokens": 40,
            "cached_input_tokens": 60,
            "reasoning_tokens": 25,
        }

    def test_a_missing_count_is_none_and_zero_stays_zero(self) -> None:
        counts = token_counts({"prompt_tokens": 0})
        assert counts["input_tokens"] == 0
        assert counts["output_tokens"] is None
        assert counts["reasoning_tokens"] is None and counts["cached_input_tokens"] is None

    @pytest.mark.parametrize("bad", [None, "12", -1, True, 1.5, {"n": 1}])
    def test_anything_but_a_count_is_not_a_count(self, bad: Any) -> None:
        assert token_counts({"prompt_tokens": bad})["input_tokens"] is None

    def test_no_usage_object_reports_nothing(self) -> None:
        assert set(token_counts(None).values()) == {None}


# --- the meter ------------------------------------------------------------------------


class TestMeter:
    def test_a_total_is_none_when_one_answered_call_did_not_report_it(self) -> None:
        meter = UsageMeter()
        meter.record("judge", "m", {"prompt_tokens": 10, "completion_tokens": 2}, 0)
        meter.record("judge", "m", {"prompt_tokens": 7}, 0)
        (row,) = meter.snapshot()
        assert row.input_tokens == 17
        assert row.output_tokens is None  # one call did not say: no total, never 9 or 2
        assert row.reported is not None
        assert (row.reported.answered, row.reported.output_tokens) == (2, 1)
        assert row.reasoning_tokens is None and row.reported.reasoning_tokens == 0

    def test_a_failed_call_is_counted_apart_and_has_nothing_to_count(self) -> None:
        meter = UsageMeter()
        meter.record("judge", "m", {"prompt_tokens": 3, "completion_tokens": 1}, 0)
        meter.record("judge", "m", None, 2)  # gave up after its retries
        (row,) = meter.snapshot()
        assert (row.calls, row.failed_calls, row.rate_limited_calls) == (2, 1, 2)
        assert (row.input_tokens, row.output_tokens) == (3, 1)
        (_, failed) = meter.events
        assert failed[1].answered is False and failed[1].cost_source == "unknown"

    def test_an_unpriced_model_costs_none_never_zero(self) -> None:
        meter = UsageMeter(prices=table())
        event = meter.record("judge", "m", {"prompt_tokens": 3, "completion_tokens": 1}, 0)
        assert event.cost_usd is None and event.cost_source == "unknown"
        assert meter.snapshot()[0].cost_usd is None

    def test_a_provider_reported_cost_wins_and_says_so(self) -> None:
        meter = UsageMeter(prices=table(tokens_price(date(2020, 1, 1), 1.0, 1.0)))
        event = meter.record(
            "judge", "m", {"prompt_tokens": 3, "completion_tokens": 1, "cost": 0.5}, 0
        )
        assert (event.cost_usd, event.cost_source, event.price_from) == (0.5, "provider", None)

    def test_calls_in_an_attempt_block_belong_to_it_across_tasks(self) -> None:
        meter = UsageMeter()

        async def judge(n: int) -> None:
            await asyncio.sleep(0)
            meter.record("judge", "m", {"prompt_tokens": n, "completion_tokens": 0}, 0)

        async def attempt(test_id: str, index: int) -> None:
            with meter.attempt(test_id, index):
                meter.record("simulator", "s", {"prompt_tokens": 1, "completion_tokens": 1}, 0)
                await asyncio.gather(judge(10), judge(20))

        async def run() -> None:
            await asyncio.gather(attempt("a", 0), attempt("b", 0))
            meter.record("judge", "m", {"prompt_tokens": 5, "completion_tokens": 0}, 0)

        asyncio.run(run())
        assert sorted(e.input_tokens or 0 for e in meter.events_for("a", 0)) == [1, 10, 20]
        assert len(meter.events_for("b", 0)) == 3
        assert [e.input_tokens for e in meter.outside_attempts()] == [5]

    def test_an_unknown_role_is_a_bug_not_a_new_row(self) -> None:
        with pytest.raises(ValueError, match="unknown usage role"):
            UsageMeter().record("critic", "m", {}, 0)


class TestChatClient:
    async def test_the_event_carries_counts_latency_provider_and_retries(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "hi"}}],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 9,
                        "completion_tokens_details": {"reasoning_tokens": 4},
                    },
                },
            )

        async def no_sleep(_: float) -> None:
            return None

        meter = UsageMeter(prices=table())
        client = ChatClient(
            base_url="https://llm.example.com/v1",
            model="m",
            api_key=None,
            role="judge",
            meter=meter,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            sleep=no_sleep,
        )
        assert await client.complete([{"role": "user", "content": "x"}]) == "hi"
        ((_, e),) = meter.events
        assert (e.input_tokens, e.output_tokens, e.reasoning_tokens) == (12, 9, 4)
        assert e.cached_input_tokens is None
        assert (e.provider, e.retries, e.rate_limited) == ("llm.example.com", 1, 1)
        assert e.latency_ms is not None and e.latency_ms >= 0

    async def test_an_answer_without_usage_is_answered_with_no_counts(self) -> None:
        reply = {"choices": [{"message": {"content": "hi"}}]}
        meter = UsageMeter()
        client = ChatClient(
            base_url="http://127.0.0.1:1/v1",
            model="m",
            api_key=None,
            role="simulator",
            meter=meter,
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply))
            ),
        )
        await client.complete([])
        (row,) = meter.snapshot()
        assert row.calls == 1 and row.failed_calls == 0
        assert row.input_tokens is None and row.output_tokens is None  # never 0

    async def test_a_call_that_gave_up_is_a_failed_event(self) -> None:
        async def no_sleep(_: float) -> None:
            return None

        meter = UsageMeter()
        client = ChatClient(
            base_url="http://127.0.0.1:1/v1",
            model="m",
            api_key=None,
            role="judge",
            meter=meter,
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
            sleep=no_sleep,
            max_retries=1,
        )
        with pytest.raises(LLMInfraError):
            await client.complete([])
        ((_, e),) = meter.events
        assert e.answered is False and e.retries == 1 and e.input_tokens is None


# --- prices ---------------------------------------------------------------------------


class TestPrices:
    def test_the_built_in_table_loads_and_every_row_is_valid(self) -> None:
        rows = builtin_prices().prices
        assert rows, "the built-in table ships with Opseval"
        assert all(row.source for row in rows), "every built-in price names where it was read"

    def test_the_row_in_force_on_the_call_day_prices_it(self) -> None:
        prices = table(
            tokens_price(date(2026, 1, 1), 2.0, 8.0),
            tokens_price(date(2026, 6, 1), 1.0, 4.0),
        )
        march = price_for("m", datetime(2026, 3, 1, tzinfo=UTC), prices)
        july = price_for("m", date(2026, 7, 1), prices)
        assert march is not None and march.input == 2.0
        assert july is not None and july.input == 1.0
        assert price_for("m", date(2025, 12, 31), prices) is None  # before any row: no price
        assert price_for("other", date(2026, 7, 1), prices) is None

    def test_a_price_added_later_never_changes_an_earlier_call(self) -> None:
        before = table(tokens_price(date(2026, 1, 1), 2.0, 8.0))
        e = priced(event(input_tokens=1_000_000, output_tokens=0), before)
        assert e.cost_usd == pytest.approx(2.0) and e.price_from == date(2026, 1, 1)
        after = table(*before.prices, tokens_price(date(2026, 6, 1), 1.0, 4.0))
        assert repriced(e, after).cost_usd == pytest.approx(2.0)
        later = event(at=datetime(2026, 7, 1, tzinfo=UTC), input_tokens=1_000_000, output_tokens=0)
        assert priced(later, after).cost_usd == pytest.approx(1.0)

    def test_the_same_day_a_later_row_wins(self) -> None:
        day = date(2026, 1, 1)
        prices = table(tokens_price(day, 2.0, 8.0), tokens_price(day, 3.0, 8.0))
        row = price_for("m", day, prices)
        assert row is not None and row.input == 3.0

    def test_cached_and_reasoning_tokens_are_priced_as_their_part(self) -> None:
        prices = table(tokens_price(date(2026, 1, 1), 2.0, 10.0, cached_input=0.5, reasoning=20.0))
        e = priced(
            event(
                input_tokens=1_000_000,
                cached_input_tokens=400_000,
                output_tokens=1_000_000,
                reasoning_tokens=250_000,
            ),
            prices,
        )
        # 600k at 2 + 400k at 0.5 + 750k at 10 + 250k at 20
        assert e.cost_usd == pytest.approx(1.2 + 0.2 + 7.5 + 5.0)

    def test_an_unreported_split_is_billed_at_the_base_rate(self) -> None:
        prices = table(tokens_price(date(2026, 1, 1), 2.0, 10.0, cached_input=0.5))
        e = priced(event(input_tokens=1_000_000, output_tokens=0), prices)
        assert e.cost_usd == pytest.approx(2.0)

    def test_a_priced_count_not_reported_is_unknown_never_zero(self) -> None:
        prices = table(tokens_price(date(2026, 1, 1), 2.0, 10.0))
        e = priced(event(input_tokens=5), prices)
        assert (e.cost_usd, e.cost_source) == (None, "unknown")

    def test_speech_is_priced_per_character_or_per_minute(self) -> None:
        prices = table(
            ModelPrice(
                match="tts", unit="characters", effective_from=date(2020, 1, 1), characters=15
            ),
            ModelPrice(
                match="stt", unit="seconds", effective_from=date(2020, 1, 1), audio_in=0.006
            ),
        )
        tts = priced(event(role="tts", model="tts", characters=1000), prices)
        stt = priced(event(role="stt", model="stt", audio_in_seconds=120.0), prices)
        silent = priced(event(role="stt", model="stt"), prices)
        assert tts.cost_usd == pytest.approx(0.015)
        assert stt.cost_usd == pytest.approx(0.012)
        assert silent.cost_usd is None

    def test_a_failed_call_is_not_priced(self) -> None:
        prices = table(tokens_price(date(2026, 1, 1), 2.0, 10.0))
        assert priced(event(answered=False), prices).cost_source == "unknown"

    @pytest.mark.parametrize(
        "row",
        [
            {"match": "m", "unit": "tokens", "effective_from": "2026-01-01", "input": 1},
            {"match": "m", "unit": "characters", "effective_from": "2026-01-01", "input": 1},
            {
                "match": "(",
                "unit": "tokens",
                "effective_from": "2026-01-01",
                "input": 1,
                "output": 1,
            },
            {
                "match": "m",
                "unit": "tokens",
                "effective_from": "2026-01-01",
                "input": -1,
                "output": 1,
            },
        ],
    )
    def test_a_broken_price_row_fails_to_load(self, row: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            ModelPrice.model_validate(row)

    def test_an_event_cost_and_its_source_agree(self) -> None:
        with pytest.raises(ValueError):
            event(cost_usd=0.0)  # a cost with source "unknown"
        with pytest.raises(ValueError):
            event(cost_source="price_table")  # a priced source with no cost


# --- old files ------------------------------------------------------------------------


class TestOldFiles:
    def test_an_old_usage_row_reads_with_the_new_fields_not_measured(self) -> None:
        row = ModelUsage.model_validate(
            {"role": "judge", "model": "m", "calls": 2, "input_tokens": 0, "output_tokens": 0}
        )
        assert row.reported is None  # written before per-call usage
        assert row.reasoning_tokens is None and row.cached_input_tokens is None

    def test_an_old_record_has_no_usage_events_not_an_empty_list(self) -> None:
        record = RunRecord.model_validate(
            {
                "run_id": "r",
                "test_id": "t",
                "attempt_index": 0,
                "seed": 1,
                "target": "ws://x",
                "started_at": WHEN.isoformat(),
                "ended_at": WHEN.isoformat(),
                "terminal_state": "goal_reached",
            }
        )
        assert record.usage_events is None

    def test_an_old_result_has_no_events_outside_attempts(self) -> None:
        old = {
            "opseval_version": "0.4",
            "run_id": "r",
            "suite": "s",
            "target": "ws://x",
            "profile": "p",
            "started_at": WHEN.isoformat(),
            "ended_at": WHEN.isoformat(),
            "tests": [],
            "gate": {"status": "pass"},
            "exit_code": 0,
            "usage": [{"role": "judge", "model": "m", "calls": 1, "input_tokens": 0}],
        }
        result = SuiteResult.model_validate(old)
        assert result.usage_events == [] and result.usage[0].reported is None


# --- a run ----------------------------------------------------------------------------


async def test_a_run_writes_each_attempts_calls_into_its_record(
    cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calls made while scoring an attempt (the judges) land in that attempt's record, and
    the result folds every call per role and model (ADR-0066)."""
    meters: list[UsageMeter] = []

    class Meter(UsageMeter):
        def __init__(self, **kw: Any) -> None:
            super().__init__(**kw)
            meters.append(self)

    real = ScoringEngine.score

    async def score(self: ScoringEngine, record: Any, scenario: Any, profile: Any) -> Any:
        meters[0].record("judge", "j", {"prompt_tokens": record.attempt_index + 1}, 0)
        return await real(self, record, scenario, profile)

    monkeypatch.setattr("opseval.runner.UsageMeter", Meter)
    monkeypatch.setattr(ScoringEngine, "score", score)
    async with run_agent() as url:
        result = await launch(
            cat_dir / "suites" / "smoke.yaml",
            RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=2),
            Settings(reports_dir=str(tmp_path), allowed_hosts="127.0.0.1"),
        )
    records = FileStore(tmp_path).load_records(result.run_id)
    assert records
    for r in records:
        assert r.usage_events is not None
        assert [e.input_tokens for e in r.usage_events] == [r.attempt_index + 1]
    (row,) = result.usage
    assert (row.role, row.calls, row.input_tokens, row.output_tokens) == (
        "judge",
        len(records),
        sum(r.attempt_index + 1 for r in records),
        None,  # no call reported its output: not measured, never 0
    )
    assert result.usage_events == []


def test_the_report_shows_a_total_not_every_call_reported_as_partial_coverage() -> None:
    from opseval.models.results import Gate
    from opseval.reporting import render_html

    meter = UsageMeter(prices=table())
    meter.record("judge", "m", {"prompt_tokens": 4, "completion_tokens": 1}, 0)
    meter.record("judge", "m", {"prompt_tokens": 4}, 0)
    result = SuiteResult(
        opseval_version="0",
        run_id="r",
        suite="s",
        target="ws://x",
        profile="p",
        started_at=WHEN,
        ended_at=WHEN,
        tests=[],
        gate=Gate(status="pass"),
        exit_code=0,
        usage=meter.snapshot(),
    )
    row = render_html(result, [])
    row = row[row.index("<td>judge</td>") :].split("</tr>")[0]
    assert "<td>8</td>" in row  # input: both calls reported it
    assert "1 of 2 calls reported" in row  # output: no total, never 1
    assert "no price" in row
