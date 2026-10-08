# SPDX-License-Identifier: Apache-2.0
"""OpenAI-compatible Chat Completions transport.

Many agents are exposed as ``POST {base}/chat/completions``. The session keeps the
conversation history, sends it on every customer turn, and turns each reply into a
canonical ``message`` frame (``expects_reply: true``). Tool calls returned by the
endpoint are kept as evidence under ``data.tool_calls``.

Error mapping follows the core promise:

- a timeout counts against the agent (it did not answer);
- HTTP 5xx is the agent answering with an error (an error frame);
- HTTP 429 is retried, and if the endpoint stays rate-limited the attempt is not
  judged (the harness may be the cause);
- other 4xx and connection errors are harness/configuration problems.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from opseval.auth import BearerAuth, TokenSource, as_token_source
from opseval.errors import TransportError, TransportRateLimited, TransportTimeout
from opseval.models.profile import SutProfile
from opseval.transport.base import NOT_SENT_TIMEOUTS, SYNTHETIC_KIND, Session, Transport

RATE_LIMIT_RETRIES = 3
BACKOFF = (1.0, 2.0, 4.0)


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            continue
        args: Any = fn.get("arguments")
        if isinstance(args, str):
            with contextlib.suppress(json.JSONDecodeError):  # else keep the raw string
                args = json.loads(args)
        out.append({"name": fn["name"], "arguments": args})
    return out


class OpenAIChatSession(Session):
    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        profile: SutProfile,
        timeout: float,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._client = client
        self._url = url
        self._profile = profile
        self._timeout = timeout
        self._sleep = sleep
        self._queue: list[Any] = []
        opts = profile.transport_options
        self._model = str(opts.get("model", "default"))
        extra = opts.get("extra_body")
        self._extra: dict[str, Any] = extra if isinstance(extra, dict) else {}
        self._temperature = opts.get("temperature")
        self.messages: list[dict[str, Any]] = []
        if isinstance(opts.get("system_prompt"), str) and opts["system_prompt"]:
            self.messages.append({"role": "system", "content": opts["system_prompt"]})

    async def send(self, frame: dict[str, Any]) -> None:
        text = frame.get(self._profile.text_field)
        if not isinstance(text, str):
            raise TransportError(
                "openai-chat can only send customer messages; this profile has no checkpoints"
            )
        self.messages.append({"role": "user", "content": text})
        body: dict[str, Any] = {**self._extra, "model": self._model, "messages": self.messages}
        if self._temperature is not None:
            body["temperature"] = self._temperature
        resp = await self._post(body)
        if resp.status_code >= 500:
            self._queue.append(
                {SYNTHETIC_KIND: "error", "status": resp.status_code, "body": resp.text[:500]}
            )
            return
        if resp.status_code >= 400:
            raise TransportError(
                f"the endpoint rejected the request with HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            message = resp.json()["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError("message is not an object")
        except (ValueError, KeyError, IndexError, TypeError):
            self._queue.append({SYNTHETIC_KIND: "error", "body": resp.text[:500]})
            return
        content = message.get("content")
        content = content if isinstance(content, str) else ""
        calls = _tool_calls(message)
        # Keep only the text in the history: returned tool calls cannot be executed by
        # a black-box tester, and an unanswered tool call would make the next request invalid.
        self.messages.append({"role": "assistant", "content": content})
        frame_out: dict[str, Any] = {"type": "message", "text": content, "expects_reply": True}
        if calls:
            frame_out["data"] = {"tool_calls": calls}
        self._queue.append(frame_out)

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                resp = await self._client.post(self._url, json=body, timeout=self._timeout)
            except NOT_SENT_TIMEOUTS as exc:
                raise TransportError(f"could not connect to {self._url}: {exc!r}") from exc
            except httpx.TimeoutException as exc:
                raise TransportTimeout(f"agent did not answer within {self._timeout:.1f}s") from exc
            except httpx.HTTPError as exc:
                raise TransportError(f"request to {self._url} failed: {exc}") from exc
            if resp.status_code != 429:
                return resp
            if attempt == RATE_LIMIT_RETRIES:
                break
            await self._sleep(_retry_after(resp) or BACKOFF[min(attempt, len(BACKOFF) - 1)])
        raise TransportRateLimited(
            f"the endpoint kept answering HTTP 429 after {RATE_LIMIT_RETRIES} retries; "
            "lower the concurrency or raise its rate limit"
        )

    async def receive(self, timeout: float) -> Any:
        if not self._queue:
            raise TransportTimeout("the agent has no further frames for this turn")
        return self._queue.pop(0)

    async def close(self) -> None:
        self._queue.clear()
        await self._client.aclose()


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return min(float(resp.headers["retry-after"]), 30.0)
    except (KeyError, ValueError):
        return None


class OpenAIChatTransport(Transport):
    name = "openai-chat"

    def __init__(
        self,
        base_url: str,
        profile: SutProfile,
        *,
        token: str | TokenSource | None = None,
        request_timeout: float = 120.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        base = base_url.rstrip("/")
        self._url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        self._profile = profile
        self._tokens = as_token_source(token)
        self._timeout = request_timeout
        self._client_factory = client_factory
        self._sleep = sleep

    @property
    def target(self) -> str:
        return self._url

    async def connect(self) -> Session:
        client = self._client_factory() if self._client_factory else httpx.AsyncClient()
        if self._tokens is not None:
            client.auth = BearerAuth(self._tokens)
        return OpenAIChatSession(client, self._url, self._profile, self._timeout, self._sleep)
