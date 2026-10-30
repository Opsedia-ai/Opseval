# SPDX-License-Identifier: Apache-2.0
"""The price table over HTTP (ADR-0066 D2): read it, add a project row, ask which row prices a
model at a moment.

The built-in table ships with Opseval and is read-only. A project's own rows need server mode
(the database); in local mode a write is refused with 409 ``PRICES_READ_ONLY``, never quietly
dropped. A row is matched by name or by a ``*`` glob, never a regular expression, and is in
force from now or later: calls already made keep the cost they were stored with.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Protocol

from pydantic import ValidationError

from opseval.accounts.roles import Need, Principal, decide
from opseval.models.usage import ModelPrice, PriceTable
from opseval.pricing import builtin_prices, price_for
from opseval.server import schemas as s
from opseval.server.actions import ApiError
from opseval.server.views import price_out

READ_ONLY = "adding a price needs server mode (a database): the built-in table is read-only here"


class PriceStore(Protocol):
    def project_prices(self) -> list[ModelPrice]: ...

    def price_table(self) -> PriceTable: ...

    def add_price(
        self, price: ModelPrice, *, by: str | None, now: datetime | None = None
    ) -> ModelPrice: ...


def editable(store: object) -> PriceStore | None:
    """The store, when it keeps a project's prices (server mode)."""
    needed = ("project_prices", "price_table", "add_price")
    if all(callable(getattr(store, name, None)) for name in needed):
        return store  # type: ignore[return-value]
    return None


def list_prices(store: object, principal: Principal | None) -> s.PricesOut:
    found = editable(store)
    project = found.project_prices() if found is not None else []
    note: str | None = None
    if found is None:
        note = READ_ONLY
    elif decide(principal, Need.MANAGE) is not None:
        note = "adding a price needs an admin or owner of the project"
    return s.PricesOut(
        builtin=[price_out(r) for r in builtin_prices().prices],
        project=[price_out(r) for r in project],
        can_edit=note is None,
        edit_note=note,
    )


def _instant(raw: str | None, field: str) -> datetime | None:
    if raw is None:
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        raise ApiError(422, "INVALID_REQUEST", f"{field}: not an ISO date or instant") from None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def add_price(
    store: object,
    body: s.PriceIn,
    principal: Principal | None,
    audit: Callable[[str, dict[str, Any]], None] | None,
    *,
    now: datetime | None = None,
) -> s.PriceOut:
    found = editable(store)
    if found is None:
        raise ApiError(409, "PRICES_READ_ONLY", READ_ONLY)
    now = now or datetime.now(UTC)
    at = _instant(body.effective_at, "effectiveAt") or now
    if at < now:
        raise ApiError(
            422,
            "PRICE_BACKDATED",
            "a price is in force from now or later, never from the past: calls already made "
            "keep the cost they were stored with",
        )
    try:
        price = ModelPrice(
            match=body.pattern,
            match_kind=body.match_kind,
            unit=body.unit,
            effective_from=at.astimezone(UTC).date(),
            effective_at=at,
            project=True,
            input=body.input,
            output=body.output,
            reasoning=body.reasoning,
            cached_input=body.cached_input,
            audio_in=body.audio_in,
            audio_out=body.audio_out,
            characters=body.characters,
            source=body.source,
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        message = str(first.get("msg", "invalid price")).removeprefix("Value error, ")
        raise ApiError(422, "INVALID_PRICE", message) from None
    who = principal.id if principal is not None else None
    added = found.add_price(price, by=who, now=now)
    if audit is not None:
        audit(
            "price.create",
            {
                "id": added.id,
                "pattern": added.match,
                "match_kind": added.match_kind,
                "unit": added.unit,
                "effective_at": added.effective_at.isoformat() if added.effective_at else None,
            },
        )
    return price_out(added)


def resolve(store: object, model: str, at_raw: str | None) -> s.PriceResolveOut:
    at = _instant(at_raw, "at") or datetime.now(UTC)
    if at_raw is not None and len(at_raw) == 10:
        at = datetime.combine(date.fromisoformat(at_raw), datetime.min.time(), tzinfo=UTC)
    found = editable(store)
    table = found.price_table() if found is not None else None
    row = price_for(model, at, table)
    return s.PriceResolveOut(
        model=model,
        at=at.isoformat(),
        price=price_out(row) if row is not None else None,
        source=None if row is None else ("project" if row.project else "builtin"),
    )


__all__ = ["add_price", "editable", "list_prices", "resolve"]
