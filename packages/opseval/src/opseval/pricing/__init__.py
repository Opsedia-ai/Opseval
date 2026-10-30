# SPDX-License-Identifier: Apache-2.0
"""The versioned model price table (ADR-0066, D1: the built-in list).

``builtin.yaml`` ships with Opseval and replaces the old hard-coded ``llm.PRICES``. Every row
has an ``effective_from`` date; a call is priced with the newest matching row in force on the
day it was made, and the cost is stored with the call: it is never recomputed, so a price
added later (whatever its ``effective_from``) never changes what an earlier run cost. A model
with no row has no price: ``None``, never $0. A cost the provider reported itself wins and is
labelled ``provider``. In server mode a project adds its own rows (``model_prices``, ADR-0066
D2): they win over the built-in ones, are matched by name or glob (never a regex), and are in
force from the instant they were added, never before, so adding one reprices nothing.
"""

from __future__ import annotations

from datetime import date, datetime
from functools import cache
from importlib.resources import files

import yaml

from opseval.models.usage import CostSource, ModelPrice, PriceTable, UsageEvent

PER_MILLION = 1_000_000
SECONDS_PER_MINUTE = 60


@cache
def builtin_prices() -> PriceTable:
    """The table shipped with this version. Validated on first use; a broken row fails loudly
    (a test loads it, so it never reaches a release)."""
    text = (files("opseval.pricing") / "builtin.yaml").read_text("utf-8")
    return PriceTable.model_validate(yaml.safe_load(text))


def price_for(
    model: str, at: datetime | date, table: PriceTable | None = None
) -> ModelPrice | None:
    """The row in force for ``model`` at ``at``, or ``None`` (no price).

    A project's own row in force wins over the built-in table (ADR-0066 D2); within each, the
    newest in force wins, and between rows from the same moment the later one in the table
    (an addition, never a rewrite)."""
    best: ModelPrice | None = None
    for row in (table or builtin_prices()).prices:
        if not (row.in_force(at) and row.matches(model)):
            continue
        if best is None or _later(row, best):
            best = row
    return best


def _later(row: ModelPrice, best: ModelPrice) -> bool:
    if row.project != best.project:
        return row.project
    if row.effective_at is not None and best.effective_at is not None:
        return row.effective_at >= best.effective_at
    return row.effective_from >= best.effective_from


def with_project(rows: list[ModelPrice] | tuple[ModelPrice, ...]) -> PriceTable:
    """The built-in table with a project's own rows (server mode) after it."""
    return PriceTable(prices=(*builtin_prices().prices, *rows))


def _tokens_cost(price: ModelPrice, event: UsageEvent) -> float | None:
    if event.input_tokens is None or event.output_tokens is None:
        return None
    if price.input is None or price.output is None:  # refused at load; kept for the types
        return None
    # A cache or reasoning split the provider did not report is billed at the base rate: the
    # provider reported no discount, so none is assumed.
    cached = 0
    if price.cached_input is not None and event.cached_input_tokens is not None:
        cached = min(event.cached_input_tokens, event.input_tokens)
    reasoning = 0
    if price.reasoning is not None and event.reasoning_tokens is not None:
        reasoning = min(event.reasoning_tokens, event.output_tokens)
    total = (event.input_tokens - cached) * price.input + (
        event.output_tokens - reasoning
    ) * price.output
    total += cached * (price.cached_input or 0.0) + reasoning * (price.reasoning or 0.0)
    return total / PER_MILLION


def _units_cost(price: ModelPrice, event: UsageEvent) -> float | None:
    if price.unit == "characters":
        if event.characters is None or price.characters is None:
            return None
        return event.characters * price.characters / PER_MILLION
    priced = [
        (rate, seconds)
        for rate, seconds in (
            (price.audio_in, event.audio_in_seconds),
            (price.audio_out, event.audio_out_seconds),
        )
        if rate is not None
    ]
    if any(seconds is None for _, seconds in priced):
        return None  # a priced quantity the call did not report: unknown, never 0
    return sum(rate * (seconds or 0.0) for rate, seconds in priced) / SECONDS_PER_MINUTE


def priced(
    event: UsageEvent, table: PriceTable | None = None, *, provider_cost: float | None = None
) -> UsageEvent:
    """``event`` with its cost: the provider's own figure if it gave one, else the price in
    force when the call was made, else ``unknown`` (``None``, never $0). A failed call
    produced nothing to price."""
    source: CostSource = "unknown"
    cost: float | None = None
    price_from: date | None = None
    if not event.answered:
        pass
    elif provider_cost is not None:
        cost, source = provider_cost, "provider"
    else:
        row = price_for(event.model, event.at, table)
        if row is not None:
            cost = _tokens_cost(row, event) if row.unit == "tokens" else _units_cost(row, event)
            if cost is not None:
                source, price_from = "price_table", row.effective_from
    return event.model_copy(
        update={"cost_usd": cost, "cost_source": source, "price_from": price_from}
    )


__all__ = ["builtin_prices", "price_for", "priced", "with_project"]
