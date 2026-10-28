# SPDX-License-Identifier: Apache-2.0
"""OpenAI-compatible chat client (no vendor SDKs, ADR-0003).

Retries 429 / 5xx / connection errors / timeouts with backoff; other 4xx fail at once.
Every call is one usage event in :class:`UsageMeter` (ADR-0066): a count the provider does
not report is ``None``, and an unknown price gives ``None``, never 0.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx

from opseval.config import Settings, check_url_allowed
from opseval.errors import LLMInfraError, LLMRateLimited, LLMRequestError
from opseval.models.results import ModelUsage
from opseval.models.usage import USAGE_ROLES, PriceTable, UsageEvent, UsageReported, UsageRole
from opseval.pricing import priced

RETRY_STATUSES = {429, 500, 502, 503, 504}
BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0)

AttemptKey = tuple[str, int]
"""(scenario id, attempt index): the attempt a call is made for."""

_ATTEMPT: ContextVar[AttemptKey | None] = ContextVar("opseval_attempt", default=None)


def _count(value: Any) -> int | None:
    """A token count as the provider reported it, or ``None`` when it did not (or reported
    something that is not a count). 0 is a count; a missing field is not."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _cost(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def token_counts(usage: dict[str, Any] | None) -> dict[str, int | None]:
    """The four token counts of an OpenAI-style ``usage`` object; ``None`` for each one the
    provider did not report, never 0 (ADR-0066)."""
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get("prompt_tokens_details")
    completion = usage.get("completion_tokens_details")
    return {
        "input_tokens": _count(usage.get("prompt_tokens")),
        "output_tokens": _count(usage.get("completion_tokens")),
        "cached_input_tokens": _count(prompt.get("cached_tokens"))
        if isinstance(prompt, dict)
        else None,
        "reasoning_tokens": _count(completion.get("reasoning_tokens"))
        if isinstance(completion, dict)
        else None,
    }


N = TypeVar("N", int, float)


def _sum_if_all(values: list[N | None]) -> N | None:
    """The total when every value is known, else ``None`` (ADR-0066). No values: ``None``."""
    known = [v for v in values if v is not None]
    if not values or len(known) < len(values):
        return None
    return sum(known[1:], known[0])


def _sum_known(values: list[N | None]) -> N | None:
    known = [v for v in values if v is not None]
    return sum(known[1:], known[0]) if known else None


def fold_usage(role: str, model: str, events: list[UsageEvent], rate_limited: int = 0) -> ModelUsage:
    """One ``(role, model)``'s events folded into a :class:`ModelUsage`.

    Token totals and the cost cover the answered calls and are ``None`` unless every one of
    them reported the count (``reported`` says how many did). Audio seconds and characters
    count the calls that produced them, as before (ADR-0054): a speech role that never sent
    audio shows "not measured", not 0 s."""
    answered = [e for e in events if e.answered]
    speech = role in ("tts", "stt")

    def tokens(name: str) -> int | None:
        if speech:
            return None
        counts: list[int | None] = [getattr(e, name) for e in answered]
        return _sum_if_all(counts)

    def reported(name: str) -> int:
        return sum(1 for e in answered if getattr(e, name) is not None)

    costs = [e.cost_usd for e in answered]
    return ModelUsage(
        role=role,
        model=model,
        calls=len(events),
        failed_calls=len(events) - len(answered),
        rate_limited_calls=sum(e.rate_limited for e in events) + rate_limited,
        input_tokens=tokens("input_tokens"),
        output_tokens=tokens("output_tokens"),
        reasoning_tokens=tokens("reasoning_tokens"),
        cached_input_tokens=tokens("cached_input_tokens"),
        cost_usd=_sum_if_all(costs),
        audio_in_seconds=_sum_known([e.audio_in_seconds for e in answered]),
        audio_out_seconds=_sum_known([e.audio_out_seconds for e in answered]),
        characters=_sum_known([e.characters for e in answered]),
        reported=UsageReported(
            answered=len(answered),
            input_tokens=reported("input_tokens"),
            output_tokens=reported("output_tokens"),
            reasoning_tokens=reported("reasoning_tokens"),
            cached_input_tokens=reported("cached_input_tokens"),
            cost=reported("cost_usd"),
        ),
    )


@dataclass
class UsageMeter:
    """Every call the harness pays for, one :class:`UsageEvent` each (ADR-0066).

    A call made inside :meth:`attempt` belongs to that attempt; its events go into the
    attempt's record. ``snapshot`` folds them per ``(role, model)`` for the result."""

    prices: PriceTable | None = None
    """The price table; ``None`` is the built-in one."""
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    events: list[tuple[AttemptKey | None, UsageEvent]] = field(default_factory=list)
    _order: list[tuple[str, str]] = field(default_factory=list)
    _extra_rate_limited: dict[tuple[str, str], int] = field(default_factory=dict)

    @contextmanager
    def attempt(self, test_id: str, index: int) -> Iterator[None]:
        """Attribute every call made in this block (and the tasks it starts) to one attempt."""
        token = _ATTEMPT.set((test_id, index))
        try:
            yield
        finally:
            _ATTEMPT.reset(token)

    def add(self, event: UsageEvent, *, provider_cost: float | None = None) -> UsageEvent:
        """Price ``event`` with the row in force when it was made and keep it."""
        event = priced(event, self.prices, provider_cost=provider_cost)
        self.events.append((_ATTEMPT.get(), event))
        key = (event.role, event.model)
        if key not in self._order:
            self._order.append(key)
        return event

    def record(
        self,
        role: str,
        model: str,
        usage: dict[str, Any] | None,
        rate_limited: int,
        *,
        answered: bool | None = None,
        provider: str | None = None,
        at: datetime | None = None,
        latency_ms: float | None = None,
        retries: int = 0,
    ) -> UsageEvent:
        """Count one LLM call. ``usage`` is the provider's usage object (``None``: none was
        sent, or the call failed). A count it does not carry stays ``None``, never 0."""
        counts = token_counts(usage)
        provider_cost = _cost(usage.get("cost")) if isinstance(usage, dict) else None
        return self.add(
            UsageEvent(
                at=at or self.clock(),
                role=_role(role),
                provider=provider,
                model=model,
                answered=usage is not None if answered is None else answered,
                rate_limited=rate_limited,
                retries=retries,
                latency_ms=latency_ms,
                **counts,
            ),
            provider_cost=provider_cost,
        )

    def record_speech(
        self,
        role: str,
        model: str,
        *,
        audio_in_seconds: float | None = None,
        audio_out_seconds: float | None = None,
        characters: int | None = None,
        rate_limited: int = 0,
        answered: bool = True,
        provider: str | None = None,
        at: datetime | None = None,
        latency_ms: float | None = None,
    ) -> UsageEvent:
        """Count one TTS or STT call (ADR-0054). A count the call did not produce (a failed
        call has no audio out) is ``None``; a role's totals count only the calls that produced
        them, so a role that never sent audio shows "not measured", not 0 s. A speech call
        has no tokens: they stay ``None``, never 0."""
        return self.add(
            UsageEvent(
                at=at or self.clock(),
                role=_role(role),
                provider=provider,
                model=model,
                answered=answered,
                audio_in_seconds=audio_in_seconds,
                audio_out_seconds=audio_out_seconds,
                characters=characters,
                rate_limited=rate_limited,
                latency_ms=latency_ms,
            )
        )

    def record_speech_rate_limited(self, role: str, model: str, responses: int) -> None:
        """Count HTTP 429 answers a speech client received within one call, one per response
        (as :class:`ChatClient` does), without counting another call."""
        if responses:
            key = (role, model)
            self._extra_rate_limited[key] = self._extra_rate_limited.get(key, 0) + responses
            if key not in self._order:
                self._order.append(key)

    def events_for(self, test_id: str, index: int) -> tuple[UsageEvent, ...]:
        return tuple(e for k, e in self.events if k == (test_id, index))

    def outside_attempts(self) -> list[UsageEvent]:
        return [e for k, e in self.events if k is None]

    def snapshot(self) -> list[ModelUsage]:
        return [
            fold_usage(
                role,
                model,
                [e for _, e in self.events if (e.role, e.model) == (role, model)],
                self._extra_rate_limited.get((role, model), 0),
            )
            for role, model in self._order
        ]


def _role(role: str) -> UsageRole:
    if role in USAGE_ROLES:
        return role
    raise ValueError(f"unknown usage role {role!r}; one of {', '.join(USAGE_ROLES)}")


class ChatClient:
    """Minimal async client for ``POST {base_url}/chat/completions``."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
        role: str,
        meter: UsageMeter | None = None,
        timeout: float = 60.0,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.role = role
        self._api_key = api_key
        self._meter = meter or UsageMeter()
        self._timeout = timeout
        self._http = http
        self._sleep = sleep
        self._max_retries = max_retries

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        seed: int | None = None,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> dict[str, Any]:
        """Return the assistant message parsed as a JSON object."""
        content = await self.complete(
            messages, seed=seed, temperature=temperature, max_tokens=max_tokens, json_mode=True
        )
        try:
            parsed = json.loads(_strip_fences(content))
        except json.JSONDecodeError as exc:
            raise LLMInfraError(
                f"{self.role} model returned invalid JSON: {content[:200]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise LLMInfraError(f"{self.role} model returned JSON that is not an object")
        return parsed

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        seed: int | None = None,
        temperature: float = 0.0,
        max_tokens: int = 800,
        json_mode: bool = False,
    ) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if seed is not None:
            body["seed"] = seed
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        client = self._http or httpx.AsyncClient()
        rate_limited = 0
        at = self._meter.clock()
        began = time.monotonic()
        try:
            for attempt in range(self._max_retries + 1):
                retry_after: float | None = None
                try:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=body,
                        headers=headers,
                        timeout=self._timeout,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    error: str = f"{type(exc).__name__}: {exc}"
                else:
                    if resp.status_code == 200:
                        return self._parse(resp, rate_limited, at, began, retries=attempt)
                    if resp.status_code not in RETRY_STATUSES:
                        raise LLMRequestError(
                            f"{self.role} LLM rejected the request: HTTP {resp.status_code} "
                            f"{resp.text[:200]}"
                        )
                    if resp.status_code == 429:
                        rate_limited += 1
                    error = f"HTTP {resp.status_code}"
                    retry_after = _retry_after(resp)
                if attempt == self._max_retries:
                    self._meter.record(
                        self.role,
                        self.model,
                        None,
                        rate_limited,
                        answered=False,
                        provider=self.provider,
                        at=at,
                        latency_ms=_since(began),
                        retries=attempt,
                    )
                    gave_up = LLMRateLimited if error == "HTTP 429" else LLMInfraError
                    raise gave_up(
                        f"{self.role} LLM unavailable after {attempt + 1} attempts ({error})"
                    )
                await self._sleep(
                    retry_after if retry_after is not None else BACKOFF[min(attempt, 4)]
                )
        finally:
            if self._http is None:
                await client.aclose()
        raise LLMInfraError("unreachable")  # pragma: no cover

    @property
    def provider(self) -> str | None:
        """Who serves the calls: the API's host, as sent."""
        return urlsplit(self.base_url).hostname

    def _parse(
        self, resp: httpx.Response, rate_limited: int, at: datetime, began: float, *, retries: int
    ) -> str:
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMInfraError(f"{self.role} LLM returned a malformed body") from exc
        if not isinstance(content, str):
            raise LLMInfraError(f"{self.role} LLM returned no text content")
        usage = data.get("usage")
        self._meter.record(
            self.role,
            self.model,
            # An answer without a usage object reported no counts: each stays None.
            usage if isinstance(usage, dict) else {},
            rate_limited,
            provider=self.provider,
            at=at,
            latency_ms=_since(began),
            retries=retries,
        )
        return content


def _since(began: float) -> float:
    return round((time.monotonic() - began) * 1000, 1)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is None:
        return None
    try:
        return min(float(value), 60.0)
    except ValueError:
        return None


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def client_for_role(settings: Settings, role: str, meter: UsageMeter) -> ChatClient | None:
    """The configured client for ``simulator`` or ``judge``, or ``None`` if not configured."""
    base = getattr(settings, f"{role}_base_url")
    model = getattr(settings, f"{role}_model")
    key = getattr(settings, f"{role}_api_key")
    if not base or not model:
        return None
    # Whoever receives the conversation must be allowlisted, key or no key (loopback is
    # always allowed): a keyless local proxy on another host still gets every transcript.
    check_url_allowed(base, settings.allowlist(), secure_schemes=("https",))
    return ChatClient(
        base_url=base,
        model=model,
        api_key=key.get_secret_value() if key else None,
        role=role,
        meter=meter,
    )


# --- model families (judge independence, ADR-0016) ------------------------------------

# (family, substrings anywhere in the model id, tokens that must START it)
# Short names that occur inside unrelated words ("phi" in delphi/seraphim, "nova" in
# casanova, the bare reasoning ids) are matched only as the first token, never as a
# substring. Specific vendors come before ``openai``, because "openai" also names the
# OpenAI-*compatible* API dialect and must not outrank the vendor in the id itself.
_FAMILIES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("anthropic", ("claude", "fable"), ()),
    ("deepseek", ("deepseek",), ()),
    ("qwen", ("qwen",), ()),
    ("google", ("gemini", "gemma", "palm", "bison"), ()),
    ("mistral", ("mistral", "mixtral", "magistral", "codestral", "ministral"), ()),
    ("meta", ("llama",), ()),
    ("cohere", ("cohere",), ("command",)),
    ("xai", ("grok",), ()),
    ("microsoft", (), ("phi",)),
    ("amazon", ("titan",), ("nova",)),
    ("openai", ("gpt", "chatgpt", "davinci", "babbage"), ("o1", "o3", "o4")),
)

_VENDORS: tuple[str, ...] = tuple(f for f, _, _ in _FAMILIES)
# Deployment/region components that may precede the model id ("us.anthropic.<id>").
# A component is only treated as a prefix if it is one of these or a vendor name, so a
# dotted *version* ("gemini-2.5-pro") is never mistaken for a prefix.
_PREFIX_WORDS = frozenset(
    {"us", "eu", "uk", "apac", "global", "azure", "bedrock", "vertex", "gateway", "models"}
)


def _split_prefix(normalised: str) -> tuple[str, set[str]]:
    """Separate the model id from any deployment prefix, returning (id, prefix vendors)."""
    head, _, tail = normalised.rpartition("/")
    vendors = {c for c in head.split("/") if c in _VENDORS} if head else set()
    parts = tail.split(".")
    while len(parts) > 1 and (parts[0] in _VENDORS or parts[0] in _PREFIX_WORDS):
        if parts[0] in _VENDORS:
            vendors.add(parts[0])
        parts = parts[1:]
    return ".".join(parts), vendors


def model_family(model: str | None) -> str | None:
    """The vendor family behind a model id, or ``None`` when it cannot be told.

    The model id decides; a deployment prefix is only a fallback, so
    ``openai/deepseek-chat`` is ``deepseek`` (the ``openai/`` prefix only says the
    endpoint speaks the OpenAI dialect) while ``us.anthropic.<new-id>`` still resolves.

    ``None`` means *unknown*, never *independent*: callers must not read it as a
    guarantee that two models come from different vendors.
    """
    if not model:
        return None
    normalised = model.strip().lower().replace("_", "-")
    segment, prefix_vendors = _split_prefix(normalised)
    first = re.split(r"[-:@.]", segment, maxsplit=1)[0]
    for family, substrings, starts in _FAMILIES:
        if any(s in segment for s in substrings) or first in starts:
            return family
    # "openai" as a prefix denotes the API dialect, not the vendor, so it never counts.
    for family in _VENDORS:
        if family != "openai" and family in prefix_vendors:
            return family
    return None
