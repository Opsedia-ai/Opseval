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
from typing import Any

SYNTHETIC_KIND = "_opseval_kind"
"""Key used for frames synthesised by a transport (e.g. an HTTP 500 from the agent)."""


def decode_frame(data: str | bytes) -> Any:
    """Decode one inbound message. Never raises."""
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_raw": data if isinstance(data, str) else data.decode("utf-8", "replace")}


class Session(ABC):
    session_id: str | None = None

    @abstractmethod
    async def send(self, frame: dict[str, Any]) -> None: ...

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
