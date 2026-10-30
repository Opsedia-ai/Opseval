# SPDX-License-Identifier: Apache-2.0
"""A project's own model prices (ADR-0066 D2), over the built-in table.

An edit adds a row; nothing here updates or deletes one. A row is in force from the instant it
was added or later, never before: a cost is stored with each call when it is made (D1), so a
row that reached into the past could only make the price table disagree with the costs the
runs already hold.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opseval.db.schema import ModelPriceRow
from opseval.models.usage import ModelPrice


class PriceBackdated(ValueError):
    """A row whose ``effective_at`` is in the past: it would reprice calls already made."""


def to_model(row: ModelPriceRow) -> ModelPrice:
    at = row.effective_from if row.effective_from.tzinfo else row.effective_from.replace(tzinfo=UTC)
    return ModelPrice(
        id=str(row.id),
        match=row.pattern,
        match_kind=row.match_kind,
        unit=row.unit,
        effective_from=at.astimezone(UTC).date(),
        effective_at=at,
        project=True,
        input=row.input,
        output=row.output,
        reasoning=row.reasoning,
        cached_input=row.cached_input,
        audio_in=row.audio_in,
        audio_out=row.audio_out,
        characters=row.characters,
        source=row.source,
        created_by=row.created_by,
    )


async def project_prices(session: AsyncSession, project_id: uuid.UUID) -> list[ModelPrice]:
    """The project's rows, oldest first (the order :func:`opseval.pricing.price_for` reads)."""
    rows = await session.scalars(
        select(ModelPriceRow)
        .where(ModelPriceRow.project_id == project_id)
        .order_by(ModelPriceRow.effective_from, ModelPriceRow.created_at, ModelPriceRow.id)
    )
    return [to_model(r) for r in rows]


async def add_price(
    session: AsyncSession,
    project_id: uuid.UUID,
    price: ModelPrice,
    *,
    by: str | None,
    now: datetime | None = None,
) -> ModelPrice:
    """Add ``price`` (validated as a project row) from its ``effective_at``, which may not be
    before ``now``."""
    now = now or datetime.now(UTC)
    at = price.effective_at or now
    if at < now:
        raise PriceBackdated(
            "a price is in force from now or later, never from the past: calls already made "
            "keep the cost they were stored with"
        )
    row = ModelPriceRow(
        id=uuid.uuid4(),
        project_id=project_id,
        pattern=price.match,
        match_kind=price.match_kind,
        unit=price.unit,
        effective_from=at,
        input=price.input,
        output=price.output,
        reasoning=price.reasoning,
        cached_input=price.cached_input,
        audio_in=price.audio_in,
        audio_out=price.audio_out,
        characters=price.characters,
        source=price.source,
        created_by=by[:320] if by else None,
    )
    session.add(row)
    await session.flush()
    return to_model(row)


__all__ = ["PriceBackdated", "add_price", "project_prices", "to_model"]
