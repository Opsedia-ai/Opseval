# SPDX-License-Identifier: Apache-2.0
"""Request/response transports: HTTP JSON and in-process Python callables.

Each ``send`` produces the agent's reply frames immediately; ``receive`` hands them out
one by one and raises :class:`TransportTimeout` once the reply is exhausted (the agent
has nothing more to say this turn).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from opseval.errors import TransportError, TransportTimeout
from opseval.models.profile import SutProfile
from opseval.transport.base import SYNTHETIC_KIND, Session, Transport

Reply = dict[str, Any] | list[dict[str, Any]] | None
AgentCallable = Callable[[dict[str, Any]], Reply | Awaitable[Reply]]


def _as_frames(reply: object) -> list[Any]:
    if reply is None:
        return []
    if isinstance(reply, list):
        return list(reply)
    return [reply]


class _QueuedSession(Session):
    def __init__(self, profile: SutProfile) -> None:
        self._profile = profile
        self._queue: list[Any] = []

    def _stamp(self, frame: dict[str, Any]) -> dict[str, Any]:
        if self.session_id and self._profile.session_id_field:
            return {**frame, self._profile.session_id_field: self.session_id}
        return frame

    def _absorb(self, frames: list[Any]) -> None:
        sid_field = self._profile.session_id_field
        for frame in frames:
            if sid_field and isinstance(frame, dict) and isinstance(frame.get(sid_field), str):
                self.session_id = frame[sid_field]
        self._queue.extend(frames)

    async def receive(self, timeout: float) -> Any:
        if not self._queue:
            raise TransportTimeout("the agent has no further frames for this turn")
        return self._queue.pop(0)

    async def close(self) -> None:
        self._queue.clear()


class HttpJsonSession(_QueuedSession):
    def __init__(self, client: httpx.AsyncClient, url: str, profile: SutProfile, timeout: float):
        super().__init__(profile)
        self._client = client
        self._url = url
        self._timeout = timeout

    async def send(self, frame: dict[str, Any]) -> None:
        try:
            resp = await self._client.post(
                self._url, json=self._stamp(frame), timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise TransportTimeout(f"agent did not answer within {self._timeout:.1f}s") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"HTTP request to {self._url} failed: {exc}") from exc
        if resp.status_code >= 500:
            # The agent answered, with an error: that is agent behaviour, not harness noise.
            self._queue.append(
                {SYNTHETIC_KIND: "error", "status": resp.status_code, "body": resp.text[:500]}
            )
            return
        if resp.status_code >= 400:
            raise TransportError(f"agent rejected the request with HTTP {resp.status_code}")
        try:
            body = resp.json() if resp.content else None
        except ValueError:
            body = {"_raw": resp.text}
        self._absorb(_as_frames(body))

    async def close(self) -> None:
        await super().close()
        await self._client.aclose()


class HttpJsonTransport(Transport):
    name = "http-json"

    def __init__(
        self,
        url: str,
        profile: SutProfile,
        *,
        token: str | None = None,
        request_timeout: float = 120.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._url = url
        self._profile = profile
        self._token = token
        self._timeout = request_timeout
        self._client_factory = client_factory

    @property
    def target(self) -> str:
        return self._url

    async def connect(self) -> Session:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        client = self._client_factory() if self._client_factory else httpx.AsyncClient()
        client.headers.update(headers)
        return HttpJsonSession(client, self._url, self._profile, self._timeout)


class CallableSession(_QueuedSession):
    def __init__(self, fn: AgentCallable, profile: SutProfile) -> None:
        super().__init__(profile)
        self._fn = fn

    async def send(self, frame: dict[str, Any]) -> None:
        result = self._fn(self._stamp(frame))
        if inspect.isawaitable(result):
            result = await result
        self._absorb(_as_frames(result))


class PythonTransport(Transport):
    """Drive an in-process agent: handy in tests and notebooks."""

    name = "python"

    def __init__(self, factory: Callable[[], AgentCallable], profile: SutProfile) -> None:
        self._factory = factory
        self._profile = profile

    @property
    def target(self) -> str:
        return f"python:{getattr(self._factory, '__qualname__', repr(self._factory))}"

    async def connect(self) -> Session:
        return CallableSession(self._factory(), self._profile)
