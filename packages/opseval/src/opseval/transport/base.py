# SPDX-License-Identifier: Apache-2.0
"""Transport contracts (ADR-0007).

A transport opens a :class:`Session` with the system under test. Sessions exchange
decoded JSON frames. ``receive`` raises :class:`~opseval.errors.TransportTimeout` when
nothing arrives in time and :class:`~opseval.errors.TransportClosed` when the
connection is gone. Decoding never raises: unparseable input becomes an ``_raw`` frame.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx

from opseval.models.record import TransportEvent

SYNTHETIC_KIND = "_opseval_kind"
"""Key used for frames synthesised by a transport (e.g. an HTTP 500 from the agent)."""

NOT_SENT_TIMEOUTS: tuple[type[httpx.TimeoutException], ...] = (
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)
"""httpx timeouts raised before the agent had the request: the connection never opened,
or Opseval's own connection pool was exhausted. They are subclasses of
``httpx.TimeoutException``, so a transport must catch them *first* and treat them like a
refused connection (a harness error), never as the agent failing to answer (ADR-0030)."""


def turn_timeout(turn: float) -> httpx.Timeout:
    """httpx limits for one request/response turn, with connecting held to a share of it.

    httpx's ``timeout`` bounds each phase (connect, write, every read) separately, not the
    request, so a transport also bounds the whole turn with ``asyncio.timeout(turn)``. If
    connecting were allowed as long as that, the outer deadline would fire first on a host
    that never accepts the connection, and the ``TimeoutError`` would blame the agent. Keeping
    connect and pool shorter lets them surface as what they are: a request that was never
    sent (ADR-0030).
    """
    setup = min(10.0, turn / 2)
    return httpx.Timeout(turn, connect=setup, pool=setup)


def decode_frame(data: str | bytes) -> Any:
    """Decode one inbound message. Never raises."""
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_raw": data if isinstance(data, str) else data.decode("utf-8", "replace")}


class Session(ABC):
    session_id: str | None = None
    replies_in_send: ClassVar[bool] = False
    """True where ``send`` returns only once the agent has answered (request/response
    transports). The driver then stamps a sent frame when sending began, not after the reply:
    otherwise everything the agent did in reply would be placed before it (ADR-0042)."""

    def transport_events(self) -> tuple[TransportEvent, ...]:
        """Drops, resumes and token refreshes seen by this session (recorded as evidence)."""
        return ()

    @abstractmethod
    async def send(self, frame: dict[str, Any], *, timeout: float | None = None) -> None:
        """Send one frame.

        ``timeout`` is how long the agent may take to answer it: the scenario's turn timeout,
        or a probe's own. It matters where sending waits for the answer (http-json,
        openai-chat, sse, python), and running past it raises :class:`TransportTimeout`,
        which counts against the agent. ``None`` keeps the transport's default. Streaming
        transports (websocket) ignore it, because ``receive`` bounds their wait.
        """

    @abstractmethod
    async def receive(self, timeout: float) -> Any: ...

    @abstractmethod
    async def close(self) -> None: ...


class Transport(ABC):
    """Factory for sessions. One fresh session per attempt."""

    name: str = "transport"

    @abstractmethod
    async def connect(self) -> Session: ...

    @property
    @abstractmethod
    def target(self) -> str:
        """Human-readable target (URL or callable name), recorded in run records."""
