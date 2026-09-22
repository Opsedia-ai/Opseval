# SPDX-License-Identifier: Apache-2.0
"""WebSocket transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

from opseval.errors import TransportClosed, TransportError, TransportTimeout
from opseval.models.profile import FrameKind, SutProfile, render_template
from opseval.transport.base import Session, Transport, decode_frame


class WebSocketSession(Session):
    def __init__(self, conn: ClientConnection, profile: SutProfile) -> None:
        self._conn = conn
        self._profile = profile
        self._pending: list[Any] = []

    async def send(self, frame: dict[str, Any]) -> None:
        if self.session_id and self._profile.session_id_field:
            frame = {**frame, self._profile.session_id_field: self.session_id}
        try:
            await self._conn.send(json.dumps(frame, ensure_ascii=False))
        except ConnectionClosed as exc:
            raise TransportClosed(
                "connection closed while sending", exc.rcvd.code if exc.rcvd else None
            ) from exc

    async def receive(self, timeout: float) -> Any:
        if self._pending:
            return self._pending.pop(0)
        try:
            data = await asyncio.wait_for(self._conn.recv(), timeout=timeout)
        except TimeoutError as exc:
            raise TransportTimeout(f"no frame within {timeout:.1f}s") from exc
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            raise TransportClosed(f"connection closed (code {code})", code) from exc
        return decode_frame(data)

    async def close(self) -> None:
        with contextlib.suppress(WebSocketException):  # already closed
            await self._conn.close()


class WebSocketTransport(Transport):
    name = "websocket"

    def __init__(
        self,
        url: str,
        profile: SutProfile,
        *,
        token: str | None = None,
        open_timeout: float = 15.0,
        handshake_timeout: float = 30.0,
    ) -> None:
        self._url = url
        self._profile = profile
        self._token = token
        self._open_timeout = open_timeout
        self._handshake_timeout = handshake_timeout

    @property
    def target(self) -> str:
        return self._url

    async def connect(self) -> Session:
        try:
            conn = await websockets.connect(
                self._url, open_timeout=self._open_timeout, max_size=8 * 1024 * 1024
            )
        except (OSError, TimeoutError, InvalidURI, WebSocketException) as exc:
            raise TransportError(f"cannot connect to {self._url}: {exc}") from exc
        session = WebSocketSession(conn, self._profile)
        if self._profile.auth_frame is not None:
            if self._token is None:
                await session.close()
                raise TransportError("the SUT profile requires auth but no token is configured")
            await session.send(
                render_template(self._profile.auth_frame, {"token": self._token})  # type: ignore[arg-type]
            )
        if self._profile.handshake_required or self._profile.session_id_field:
            await self._await_handshake(session)
        return session

    async def _await_handshake(self, session: WebSocketSession) -> None:
        """Read handshake frames; keep any conversation frame for the driver."""
        deadline = asyncio.get_running_loop().time() + self._handshake_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                if self._profile.handshake_required:
                    await session.close()
                    raise TransportError("no handshake frame from the SUT")
                return
            try:
                frame = await session.receive(
                    min(remaining, 0.5) if not self._profile.handshake_required else remaining
                )
            except TransportTimeout:
                if self._profile.handshake_required:
                    continue
                return
            except TransportClosed as exc:
                raise TransportError(f"connection closed during handshake: {exc}") from exc
            if self._profile.classify(frame) is FrameKind.HANDSHAKE:
                sid_field = self._profile.session_id_field
                if sid_field and isinstance(frame, dict) and isinstance(frame.get(sid_field), str):
                    session.session_id = frame[sid_field]
                return
            session._pending.append(frame)
            return
