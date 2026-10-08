# SPDX-License-Identifier: Apache-2.0
"""Server-Sent Events transport (ADR-0019).

The agent answers a POST with ``text/event-stream`` and streams its turn as events. One
``send`` consumes the stream for that turn and queues the decoded frames; ``receive``
hands them out and raises :class:`TransportTimeout` once they are exhausted, exactly like
the request/response transports.

Which errors belong to whom is the whole point here:

- a stream that stalls, or a turn that never ends, is :class:`TransportTimeout` — the
  agent did not answer in time, and that counts against it;
- a connection that fails or dies mid-stream is :class:`TransportClosed` (a
  ``HarnessError``) — Opseval could not keep the session, which is never the agent's
  fault, so the attempt is *not judged*;
- an HTTP 5xx is the agent answering with an error, so it becomes an ``error`` frame and
  is scored;
- a malformed event is recorded verbatim rather than raising, so bad bytes can never look
  like a clean turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from opseval.auth import BearerAuth, TokenSource, as_token_source
from opseval.errors import TransportClosed, TransportError, TransportTimeout
from opseval.models.profile import SutProfile
from opseval.transport.base import (
    NOT_SENT_TIMEOUTS,
    SYNTHETIC_KIND,
    Session,
    Transport,
    decode_frame,
)

SSE_MEDIA_TYPE = "text/event-stream"


def _request_timeout(turn: float) -> httpx.Timeout:
    """httpx limits for one turn, with connecting held to a share of the turn budget.

    ``send`` bounds the whole turn with ``asyncio.timeout(turn)``. If httpx's connect and
    pool limits were the same length, that outer deadline would fire first on a host that
    never accepts the connection, and the resulting ``TimeoutError`` would blame the agent.
    Keeping them shorter lets ``ConnectTimeout``/``PoolTimeout`` surface as what they are:
    a request that was never sent (ADR-0029).
    """
    setup = min(10.0, turn / 2)
    return httpx.Timeout(turn, connect=setup, pool=setup)


class SseEvent:
    """One dispatched Server-Sent Event."""

    __slots__ = ("data", "event", "last_event_id")

    def __init__(self, data: str, event: str | None, last_event_id: str | None) -> None:
        self.data = data
        self.event = event
        self.last_event_id = last_event_id


def parse_sse_lines(lines: list[str]) -> list[SseEvent]:
    """Parse a complete event stream (per the WHATWG field grammar). Never raises."""
    events: list[SseEvent] = []
    data: list[str] = []
    event_name: str | None = None
    last_id: str | None = None

    def dispatch() -> None:
        nonlocal data, event_name
        # A blank line with no data is a heartbeat, not an event.
        if data:
            events.append(SseEvent("\n".join(data), event_name, last_id))
        data, event_name = [], None

    for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")
        if not line:
            dispatch()
            continue
        if line.startswith(":"):
            continue  # comment / keep-alive
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "data":
            data.append(value)
        elif field == "event":
            event_name = value or None
        elif field == "id" and "\x00" not in value:
            last_id = value
        # "retry" is a reconnection hint; Opseval reconnects per attempt, not per stream.
    dispatch()  # a stream may end without a trailing blank line
    return events


def frame_from_event(event: SseEvent, profile: SutProfile) -> Any:
    """Decode one event into a frame, taking the kind from the event name if needed.

    Many SSE agents put the message type in the event name and the payload in ``data``.
    The event name is used only when the payload does not already declare a kind, so a
    profile-driven ``kind_field`` always wins. An undecodable payload is never stamped:
    giving ``_raw`` bytes a kind would let the profile classify them, and garbage must
    not be able to look like a clean turn.
    """
    frame = decode_frame(event.data)
    if (
        event.event
        and isinstance(frame, dict)
        and "_raw" not in frame
        and not isinstance(frame.get(profile.kind_field), str)
    ):
        return {**frame, profile.kind_field: event.event}
    return frame


class SseSession(Session):
    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        profile: SutProfile,
        *,
        request_timeout: float,
        max_turn_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self._client = client
        self._url = url
        self._profile = profile
        self._timeout = request_timeout
        self._max_turn_bytes = max_turn_bytes
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

    async def _read_stream(self, response: httpx.Response) -> list[str]:
        lines: list[str] = []
        size = 0
        async for line in response.aiter_lines():
            size += len(line) + 1
            if size > self._max_turn_bytes:
                raise TransportTimeout(
                    f"agent streamed more than {self._max_turn_bytes} bytes in one turn "
                    "without ending it"
                )
            lines.append(line)
        return lines

    async def send(self, frame: dict[str, Any]) -> None:
        payload = self._stamp(frame)
        try:
            # httpx's `timeout` bounds each read, not the stream, so an agent that
            # trickles keep-alive comments could hold the turn open indefinitely. This
            # bounds the turn itself, which is what the docstring promises.
            async with (
                asyncio.timeout(self._timeout),
                self._client.stream(
                    "POST",
                    self._url,
                    json=payload,
                    timeout=_request_timeout(self._timeout),
                    headers={"Accept": SSE_MEDIA_TYPE},
                ) as response,
            ):
                if response.status_code >= 500:
                    # The agent answered, with an error: that is agent behaviour.
                    await response.aread()
                    self._queue.append(
                        {
                            SYNTHETIC_KIND: "error",
                            "status": response.status_code,
                            "body": response.text[:500],
                        }
                    )
                    return
                if response.status_code >= 400:
                    await response.aread()
                    raise TransportError(
                        f"agent rejected the request with HTTP {response.status_code}"
                    )
                media = (response.headers.get("content-type") or "").split(";")[0].strip()
                if media and media != SSE_MEDIA_TYPE:
                    # A JSON body, a proxy's HTML login page or a redirect would parse to
                    # zero events, and an empty turn is scored against the agent. Refusing
                    # here keeps a harness or configuration problem out of the agent's
                    # record. An absent header is still allowed: some gateways omit it.
                    await response.aread()
                    hint = (
                        " use --transport http-json for a plain JSON endpoint;"
                        if media.endswith("json")
                        else ""
                    )
                    raise TransportError(
                        f"{self._url} answered {media!r}, not {SSE_MEDIA_TYPE};{hint}"
                        " check the URL and that it is an event-stream endpoint"
                    )
                lines = await self._read_stream(response)
        except NOT_SENT_TIMEOUTS as exc:
            # The stream never opened, so the agent was never asked: same as a refusal.
            raise TransportClosed(f"could not connect to {self._url}: {exc!r}") from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            # The agent held the stream open without finishing its turn.
            raise TransportTimeout(
                f"agent did not finish its turn within {self._timeout:.1f}s"
            ) from exc
        except httpx.HTTPError as exc:
            # Opseval could not keep the session: never the agent's fault.
            raise TransportClosed(f"event stream from {self._url} failed: {exc}") from exc
        self._absorb([frame_from_event(e, self._profile) for e in parse_sse_lines(lines)])

    async def receive(self, timeout: float) -> Any:
        if not self._queue:
            raise TransportTimeout("the agent has no further frames for this turn")
        return self._queue.pop(0)

    async def close(self) -> None:
        self._queue.clear()
        await self._client.aclose()


class SseTransport(Transport):
    """Drive an agent that streams its turns as Server-Sent Events."""

    name = "sse"

    def __init__(
        self,
        url: str,
        profile: SutProfile,
        *,
        token: str | TokenSource | None = None,
        request_timeout: float = 120.0,
        max_turn_bytes: int = 32 * 1024 * 1024,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._url = url
        self._profile = profile
        self._tokens = as_token_source(token)
        self._timeout = request_timeout
        self._max_turn_bytes = max_turn_bytes
        self._client_factory = client_factory

    @property
    def target(self) -> str:
        return self._url

    async def connect(self) -> Session:
        client = self._client_factory() if self._client_factory else httpx.AsyncClient()
        if self._tokens is not None:
            client.auth = BearerAuth(self._tokens)
        return SseSession(
            client,
            self._url,
            self._profile,
            request_timeout=self._timeout,
            max_turn_bytes=self._max_turn_bytes,
        )


__all__ = [
    "SSE_MEDIA_TYPE",
    "SseEvent",
    "SseSession",
    "SseTransport",
    "frame_from_event",
    "parse_sse_lines",
]
