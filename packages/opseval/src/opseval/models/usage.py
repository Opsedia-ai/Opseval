# SPDX-License-Identifier: Apache-2.0
"""Usage events and model prices (ADR-0066).

One :class:`UsageEvent` per LLM, TTS, STT or telephony call. A count the provider did not
report is ``None``, never 0, and stays ``None`` through the meter, the record, the API and the
chart. Prices are rows with an ``effective_from`` date, so a cost is computed with the row in
force when the call was made and an old run keeps its price.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UsageRole = Literal["simulator", "judge", "attacker", "generator", "tts", "stt", "telephony"]
"""The one list of roles, used by the meter, the price table and (later) budgets."""

USAGE_ROLES: tuple[UsageRole, ...] = (
    "simulator",
    "judge",
    "attacker",
    "generator",
    "tts",
    "stt",
    "telephony",
)

TOKEN_ROLES: frozenset[UsageRole] = frozenset({"simulator", "judge", "attacker", "generator"})
"""Roles whose calls are counted in tokens. TTS, STT and telephony calls never have tokens."""


def has_tokens(role: str) -> bool:
    """Whether a call in ``role`` has tokens to count (ADR-0066). The one predicate every token
    aggregate uses: a TTS, STT or telephony call is left out of token totals and of their
    ``of``, so a voice attempt's speech calls never make its token total look partial."""
    return role in TOKEN_ROLES


CostSource = Literal["provider", "price_table", "unknown"]
"""Where a call's cost came from: the provider's own answer (it wins, and is labelled), the
price table, or nowhere ("no price", never $0)."""


class UsageEvent(BaseModel):
    """One call the harness paid for. Lives in the attempt's record in files mode (ADR-0066).

    Token counts follow the OpenAI usage object: ``cached_input_tokens`` is the part of
    ``input_tokens`` served from the provider's cache (``prompt_tokens_details.cached_tokens``)
    and ``reasoning_tokens`` the part of ``output_tokens`` spent reasoning
    (``completion_tokens_details.reasoning_tokens``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    """When the call started. The price in force at this moment prices it."""
    role: UsageRole
    provider: str | None = None
    """Who served it: the API host for an LLM, the speech provider's name for TTS/STT."""
    model: str
    """The model as sent (a speech plugin's model is prefixed with its provider)."""
    answered: bool = True
    """False when the call got no usable answer (it failed after its retries). A failed call
    produced nothing to count, so its counts and cost are ``None`` and the aggregates count it
    in ``failed_calls`` rather than as a call whose count is unknown."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    audio_in_seconds: float | None = None
    audio_out_seconds: float | None = None
    characters: int | None = None
    latency_ms: float | None = None
    """From the first send to the answer, retries and their back-off included."""
    rate_limited: int = 0
    """HTTP 429 answers within this call, one per response."""
    retries: int = 0
    """How many times the call was sent again after a retryable failure."""
    cost_usd: float | None = None
    """``None``: no price, or a priced count the provider did not report. Never 0 for that."""
    cost_source: CostSource = "unknown"
    price_from: date | None = None
    """The ``effective_from`` of the price row used, when ``cost_source`` is ``price_table``."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.cost_usd is None) != (self.cost_source == "unknown"):
            raise ValueError("cost_usd is None exactly when cost_source is 'unknown'")
        if self.price_from is not None and self.cost_source != "price_table":
            raise ValueError("price_from is set only for a price_table cost")
        return self


class AttemptUsage(BaseModel):
    """The calls made for one attempt outside its record: a re-score's judge calls
    (ADR-0066). A run's own attempt calls live in the attempt's record instead."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    test_id: str
    attempt_index: int
    events: tuple[UsageEvent, ...]


PriceUnit = Literal["tokens", "characters", "seconds"]


class ModelPrice(BaseModel):
    """One row of the price table. An edit adds a row; it never rewrites one.

    Token prices are USD per 1M tokens, character prices USD per 1M characters and audio
    prices USD per minute, as providers publish them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    match: str
    """A regular expression the whole model name must match (case-insensitive)."""
    unit: PriceUnit
    effective_from: date
    input: float | None = Field(default=None, ge=0)
    output: float | None = Field(default=None, ge=0)
    reasoning: float | None = Field(default=None, ge=0)
    """Reasoning tokens' price, when it differs from ``output``; ``None`` bills them as output."""
    cached_input: float | None = Field(default=None, ge=0)
    """Cached input's price; ``None`` bills cached tokens as ordinary input."""
    audio_in: float | None = Field(default=None, ge=0)
    audio_out: float | None = Field(default=None, ge=0)
    characters: float | None = Field(default=None, ge=0)
    source: str = ""
    """Where the price was read, for a reviewer to check it."""

    @field_validator("match")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"match is not a regular expression: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _priced_for_its_unit(self) -> Self:
        needs = {
            "tokens": (self.input, self.output),
            "characters": (self.characters,),
            "seconds": (self.audio_in, self.audio_out),
        }[self.unit]
        if self.unit == "tokens" and None in needs:
            raise ValueError("a tokens price needs both input and output")
        if all(v is None for v in needs):
            raise ValueError(f"a {self.unit} price needs a price for that unit")
        return self

    def matches(self, model: str) -> bool:
        return re.fullmatch(self.match, model, flags=re.IGNORECASE) is not None


class PriceTable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    prices: tuple[ModelPrice, ...] = ()


class UsageReported(BaseModel):
    """How many answered calls reported each count (ADR-0066). A total is ``None`` unless
    every answered call reported it; this says how many did."""

    model_config = ConfigDict(extra="forbid")
    answered: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0
    cost: int = 0


@dataclass(frozen=True)
class CostParts:
    """What a set of answered calls cost, split into the part that is known and the calls that
    had no price (ADR-0066, the D1 follow-up).

    The one arithmetic for a cost that covers several calls, used by the run's usage rows
    (``ModelUsage``), the run detail, the Usage page and every analytics store, so they can
    never disagree. ``known_usd`` is the sum over the priced calls (``None`` when none was
    priced, never $0); ``total_usd`` is that sum only when no call was left unpriced, so a
    figure that misses calls is never shown as the whole bill. Sums use :func:`math.fsum`,
    which is exact whatever the order, so a database and a folder add the same rows to the
    same number."""

    known_usd: float | None = None
    priced: int = 0
    unpriced: int = 0

    @property
    def calls(self) -> int:
        return self.priced + self.unpriced

    @property
    def complete(self) -> bool:
        """Every call was priced (and there was at least one)."""
        return self.priced > 0 and self.unpriced == 0

    @property
    def total_usd(self) -> float | None:
        return self.known_usd if self.complete else None

    def __add__(self, other: CostParts) -> CostParts:
        known = [v for v in (self.known_usd, other.known_usd) if v is not None]
        return CostParts(
            known_usd=math.fsum(known) if known else None,
            priced=self.priced + other.priced,
            unpriced=self.unpriced + other.unpriced,
        )


def cost_parts(costs: Iterable[float | None]) -> CostParts:
    """The known part of ``costs`` (one per answered call; ``None`` is a call with no price)
    and how many calls were priced and unpriced."""
    known: list[float] = []
    unpriced = 0
    for cost in costs:
        if cost is None:
            unpriced += 1
        else:
            known.append(cost)
    return CostParts(
        known_usd=math.fsum(known) if known else None, priced=len(known), unpriced=unpriced
    )
