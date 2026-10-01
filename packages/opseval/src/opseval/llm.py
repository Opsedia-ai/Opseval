# SPDX-License-Identifier: Apache-2.0
"""OpenAI-compatible chat client (no vendor SDKs, ADR-0003).

Retries 429 / 5xx / connection errors / timeouts with backoff; other 4xx fail at once.
Every call is counted in :class:`UsageMeter`. Unknown prices give ``None``, never 0.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from opseval.config import Settings, check_url_allowed
from opseval.errors import LLMInfraError, LLMRequestError
from opseval.models.results import ModelUsage

# USD per 1M tokens (input, output). Deliberately small; unknown models are "no rate".
PRICES: dict[str, tuple[float, float]] = {}

RETRY_STATUSES = {429, 500, 502, 503, 504}
BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0)


@dataclass
class UsageMeter:
    by_key: dict[tuple[str, str], ModelUsage] = field(default_factory=dict)

    def record(
        self, role: str, model: str, usage: dict[str, Any] | None, rate_limited: int
    ) -> None:
        entry = self.by_key.setdefault((role, model), ModelUsage(role=role, model=model))
        entry.calls += 1
        entry.rate_limited_calls += rate_limited
        if usage:
            entry.input_tokens += int(usage.get("prompt_tokens", 0) or 0)
            entry.output_tokens += int(usage.get("completion_tokens", 0) or 0)
        price = PRICES.get(model)
        entry.cost_usd = (
            None
            if price is None
            else (entry.input_tokens * price[0] + entry.output_tokens * price[1]) / 1_000_000
        )

    def snapshot(self) -> list[ModelUsage]:
        return [u.model_copy() for u in self.by_key.values()]


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
                        return self._parse(resp, rate_limited)
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
                    self._meter.record(self.role, self.model, None, rate_limited)
                    raise LLMInfraError(
                        f"{self.role} LLM unavailable after {attempt + 1} attempts ({error})"
                    )
                await self._sleep(
                    retry_after if retry_after is not None else BACKOFF[min(attempt, 4)]
                )
        finally:
            if self._http is None:
                await client.aclose()
        raise LLMInfraError("unreachable")  # pragma: no cover

    def _parse(self, resp: httpx.Response, rate_limited: int) -> str:
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMInfraError(f"{self.role} LLM returned a malformed body") from exc
        if not isinstance(content, str):
            raise LLMInfraError(f"{self.role} LLM returned no text content")
        self._meter.record(self.role, self.model, data.get("usage"), rate_limited)
        return content


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
    if key is not None:
        check_url_allowed(base, settings.allowlist(), secure_schemes=("https",))
    return ChatClient(
        base_url=base,
        model=model,
        api_key=key.get_secret_value() if key else None,
        role=role,
        meter=meter,
    )


# --- model families (judge independence, ADR-0016) ------------------------------------

# Specific vendors first; ``openai`` last, because "openai" is also the name of the
# OpenAI-*compatible* API dialect ("openai/deepseek-chat") and must never outrank the
# vendor named in the model itself.
_FAMILIES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # family        substrings in the model id            whole tokens
    ("anthropic", ("claude", "fable"), ()),
    ("deepseek", ("deepseek",), ()),
    ("qwen", ("qwen",), ()),
    ("google", ("gemini", "gemma", "palm", "bison"), ()),
    ("mistral", ("mistral", "mixtral", "magistral", "codestral", "ministral"), ()),
    ("meta", ("llama",), ()),
    ("cohere", ("cohere",), ("command",)),
    ("xai", ("grok",), ()),
    ("microsoft", ("phi",), ()),
    ("amazon", ("titan", "nova"), ()),
    ("openai", ("gpt", "chatgpt", "davinci", "babbage"), ("o1", "o3", "o4")),
)

# Vendor names that identify a vendor when they appear as a deployment prefix
# ("us.anthropic.<id>", "mistral/<id>"). ``openai`` is absent on purpose: as a prefix it
# denotes the API dialect, not the vendor.
_PREFIX_VENDORS = frozenset(f for f, _, _ in _FAMILIES if f != "openai")


def model_family(model: str | None) -> str | None:
    """The vendor family behind a model id, or ``None`` when it cannot be told.

    The model id itself decides, so ``openai/deepseek-chat`` is ``deepseek`` (the
    ``openai/`` prefix only says the endpoint speaks the OpenAI dialect). A vendor named
    in a deployment prefix is used only as a fallback, as in ``us.anthropic.<new-id>``.

    ``None`` means *unknown*, never *independent*: callers must not read it as a
    guarantee that two models come from different vendors.
    """
    if not model:
        return None
    normalised = model.strip().lower().replace("_", "-")
    segment = normalised.rsplit("/", 1)[-1]
    tokens = set(re.split(r"[-.:@]+", segment))
    for family, substrings, whole in _FAMILIES:
        if any(s in segment for s in substrings) or tokens & set(whole):
            return family
    # Fall back to a vendor named in a deployment prefix: every "/"- or "."-separated
    # component except the model id itself ("us.anthropic.<new-id>" -> anthropic).
    prefix = set(re.split(r"[/.]+", normalised)[:-1])
    for family in _PREFIX_VENDORS:
        if family in prefix:
            return family
    return None
