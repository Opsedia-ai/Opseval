# SPDX-License-Identifier: Apache-2.0
"""A run's cost, split into its known part and its unpriced calls (ADR-0066, D2).

The run detail, the Usage page and the dashboards all say what a run cost through
:func:`~opseval.models.usage.cost_parts`, so they agree: when some calls had a price and others
did not, each shows the cost of the known part and how many calls had no price, never one
total that pretends to be complete.
"""

from __future__ import annotations

from collections.abc import Iterable

from opseval.models.results import ModelUsage
from opseval.models.usage import CostParts, UsageEvent, cost_parts


def rows_cost(rows: Iterable[ModelUsage]) -> CostParts | None:
    """A run's cost from its usage rows; ``None`` when a row's split was not recorded (a
    layout 6 to 8 row with some calls priced and some not): ask :func:`events_cost`."""
    total = CostParts()
    for row in rows:
        parts = row.cost_parts()
        if parts is None:
            return None
        total = total + parts
    return total


def events_cost(events: Iterable[UsageEvent]) -> CostParts:
    """The cost of the answered calls among ``events``; a failed call produced nothing to
    price and is not an unpriced call."""
    return cost_parts(e.cost_usd for e in events if e.answered)


__all__ = ["events_cost", "rows_cost"]
