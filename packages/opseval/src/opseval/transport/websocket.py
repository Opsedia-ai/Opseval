# SPDX-License-Identifier: Apache-2.0
"""WebSocket transport with session resume and token refresh.

- A dropped connection is resumed only when the SUT profile declares
  ``reconnect.resume_session_frame`` and the agent confirms the *same* session id.
  Otherwise the drop stays a transport failure: the attempt is not judged.
- Every drop, resume and token refresh is kept as a :class:`TransportEvent` so a drop
  the agent caused is visible in the run record even when the session survived it.
- With ``token`` in the profile, short-lived HS256 tokens are minted per attempt and
  refreshed before they expire, including while waiting for the agent's reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

from opseval.auth import TokenSource, as_token_source
from opseval.errors import TransportClosed, TransportError, TransportTimeout
from opseval.models.profile import FrameKind, SutProfile, render_frame
from opseval.models.record import TransportEvent
from opseval.transport.base import Session, Transport, decode_frame

EventKind = Literal["dropped", "resumed", "resume_failed", "token_refreshed"]


def _close_code(exc: ConnectionClosed) -> int | None:
    return exc.rcvd.code if exc.rcvd else None


class WebSocketSession(Session):
    def __init__(self, transport: WebSocketTransport, conn: ClientConnection) -> None:
        self._transport = transport
        self._profile = transport.profile
        self._conn = conn
        self._pending: list[Any] = []
        self._events: list[TransportEvent] = []
        self._last_sent: dict[str, Any] | None = None
        self._answered = True
        self._maybe_lost = False
        """A resume happened while a sent frame was unanswered and it was not resent."""

    def transport_events(self) -> tuple[TransportEvent, ...]:
        return tuple(self._events)

    def _event(self, kind: EventKind, **fields: Any) -> None:
        self._events.append(TransportEvent(at=datetime.now(UTC), kind=kind, **fields))

    # --- raw I/O ------------------------------------------------------------------
    def _with_session(self, frame: dict[str, Any]) -> dict[str, Any]:
        if self.session_id and self._profile.session_id_field:
            return {**frame, self._profile.session_id_field: self.session_id}
        return frame

    async def _send_raw(self, frame: dict[str, Any]) -> None:
        await self._conn.send(json.dumps(frame, ensure_ascii=False))

    async def _recv_raw(self, timeout: float) -> Any:
        try:
            data = await asyncio.wait_for(self._conn.recv(), timeout=timeout)
        except TimeoutError as exc:
            raise TransportTimeout(f"no frame within {timeout:.1f}s") from exc
        return decode_frame(data)

    # --- conversation I/O (used by the driver) ------------------------------------
    async def send(self, frame: dict[str, Any], *, timeout: float | None = None) -> None:
        # Sending never waits for the answer here: `receive` bounds that, so `timeout` is unused.
        await self._refresh_token_if_due()
        try:
            await self._send_raw(self._with_session(frame))
        except ConnectionClosed as exc:
            await self._recover(exc)
            await self._send_raw(self._with_session(frame))
        self._last_sent, self._answered, self._maybe_lost = frame, False, False

    async def receive(self, timeout: float) -> Any:
        if self._pending:
            return self._pending.pop(0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            await self._refresh_token_if_due()
            remaining = deadline - loop.time()
            if remaining <= 0:
                if self._maybe_lost:
                    raise TransportError(
                        "no reply after resuming the session: the last message may have been "
                        "lost in the drop (set reconnect.resend_unanswered if the agent "
                        "tolerates a duplicate)"
                    )
                raise TransportTimeout(f"no frame within {timeout:.1f}s")
            refresh_in = self._transport.seconds_until_refresh()
            wait = remaining if refresh_in is None else max(0.0, min(remaining, refresh_in))
            try:
                data = await asyncio.wait_for(self._conn.recv(), timeout=wait)
            except TimeoutError:
                continue  # either the deadline passed (checked above) or a refresh is due
            except ConnectionClosed as exc:
                unanswered = not self._answered and self._last_sent is not None
                await self._recover(exc)
                resent = (
                    self._profile.reconnect is not None
                    and self._profile.reconnect.resend_unanswered
                )
                self._maybe_lost = unanswered and not resent
                if self._pending:
                    return self._answer(self._pending.pop(0))
                continue
            return self._answer(decode_frame(data))

    def _answer(self, frame: Any) -> Any:
        """Note that the agent answered (handshake chatter does not count)."""
        if self._profile.classify(frame) is not FrameKind.HANDSHAKE:
            self._answered, self._maybe_lost = True, False
        return frame

    async def close(self) -> None:
        with contextlib.suppress(WebSocketException):  # already closed
            await self._conn.close()

    # --- token refresh --------------------------------------------------------------
    async def _refresh_token_if_due(self) -> None:
        cfg = self._profile.token
        tokens = self._transport.tokens
        if cfg is None or cfg.refresh_frame is None or tokens is None or not tokens.refresh_due():
            return
        token = tokens.current()
        try:
            await self._send_raw(
                self._with_session(render_frame(cfg.refresh_frame, {"token": token}))
            )
        except ConnectionClosed as exc:
            await self._recover(exc)  # the new connection authenticates with the fresh token
            return
        self._event("token_refreshed", detail="refresh frame sent")

    # --- resume ------------------------------------------------------------------------
    async def _recover(self, exc: ConnectionClosed) -> None:
        """Resume the same session on a new connection, or raise (transport failure)."""
        code = _close_code(exc)
        self._event("dropped", close_code=code, detail=exc.rcvd.reason if exc.rcvd else None)
        cfg = self._profile.reconnect
        if cfg is None or cfg.resume_session_frame is None:
            raise TransportClosed(
                f"connection closed (code {code}); the SUT profile declares no "
                "reconnect.resume_session_frame, so the session cannot be resumed",
                code,
            ) from exc
        if not self.session_id:
            raise TransportClosed(
                f"connection closed (code {code}) before the agent reported a session id", code
            ) from exc
        last_error = "not attempted"
        for attempt in range(1, cfg.max_attempts + 1):
            await self._transport.sleep(cfg.backoff_seconds * attempt)
            try:
                conn = await self._transport.open()
            except TransportError as err:
                last_error = str(err)
                self._event("resume_failed", attempt=attempt, detail=last_error)
                continue
            self._conn = conn
            try:
                await self._transport.authenticate(self)
                values = {"session_id": self.session_id, "token": self._transport.token_or_none()}
                await self._send_raw(render_frame(cfg.resume_session_frame, values))
                confirmed = await self._transport.confirm_resume(
                    self, self.session_id, cfg.confirm_timeout_seconds
                )
            except (TransportError, TransportTimeout, ConnectionClosed) as err:
                last_error = f"{type(err).__name__}: {err}"
                self._event("resume_failed", attempt=attempt, detail=last_error)
                await self.close()
                continue
            if confirmed != self.session_id:
                await self.close()
                self._event(
                    "resume_failed", attempt=attempt, detail="the agent started a new session"
                )
                raise TransportError(
                    f"resume refused: the agent answered with session {confirmed!r} instead of "
                    f"{self.session_id!r}"
                ) from exc
            self._event("resumed", attempt=attempt)
            if cfg.resend_unanswered and self._last_sent is not None and not self._answered:
                await self._send_raw(self._with_session(self._last_sent))
            return
        raise TransportError(
            f"connection closed (code {code}) and could not be resumed after "
            f"{cfg.max_attempts} attempt(s): {last_error}"
        ) from exc


class WebSocketTransport(Transport):
    name = "websocket"

    def __init__(
        self,
        url: str,
        profile: SutProfile,
        *,
        token: str | TokenSource | None = None,
        open_timeout: float = 15.0,
        handshake_timeout: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = url
        self.profile = profile
        self.tokens = as_token_source(token)
        self._open_timeout = open_timeout
        self._handshake_timeout = handshake_timeout
        self.sleep = sleep

    @property
    def target(self) -> str:
        return self._url

    def token_or_none(self) -> str | None:
        return self.tokens.current() if self.tokens else None

    def seconds_until_refresh(self) -> float | None:
        if self.profile.token is None or self.profile.token.refresh_frame is None:
            return None
        return self.tokens.seconds_until_refresh() if self.tokens else None

    async def open(self) -> ClientConnection:
        try:
            return await websockets.connect(
                self._url, open_timeout=self._open_timeout, max_size=8 * 1024 * 1024
            )
        except (OSError, TimeoutError, InvalidURI, WebSocketException) as exc:
            raise TransportError(f"cannot connect to {self._url}: {exc}") from exc

    async def authenticate(self, session: WebSocketSession) -> None:
        if self.profile.auth_frame is None:
            return
        if self.tokens is None:
            raise TransportError(
                "the SUT profile requires auth but no token is configured "
                "(set OPSEVAL_STATIC_TOKEN, or OPSEVAL_TOKEN_SIGNING_KEY with a profile 'token')"
            )
        await session._send_raw(
            render_frame(self.profile.auth_frame, {"token": self.tokens.current()})
        )

    async def connect(self) -> Session:
        session = WebSocketSession(self, await self.open())
        try:
            await self.authenticate(session)
            if self.profile.handshake_required or self.profile.session_id_field:
                sid = await self.read_handshake(session, required=self.profile.handshake_required)
                if sid is not None:
                    session.session_id = sid
        except ConnectionClosed as exc:
            await session.close()
            raise TransportError(f"connection closed during handshake: {exc}") from exc
        except TransportError:
            await session.close()
            raise
        return session

    async def confirm_resume(
        self, session: WebSocketSession, expected: str, timeout: float
    ) -> str | None:
        """Wait for a handshake frame carrying ``expected``.

        Returns ``expected`` when confirmed, the other session id if the agent only
        announced a different one (a fresh session: resume refused), and raises
        :class:`TransportError` if nothing identifies the session in time. Conversation
        frames that arrive meanwhile are kept for the driver.
        """
        sid_field = self.profile.session_id_field
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        other: str | None = None
        while (remaining := deadline - loop.time()) > 0:
            try:
                frame = await session._recv_raw(remaining)
            except TransportTimeout:
                break
            if self.profile.classify(frame) is not FrameKind.HANDSHAKE:
                session._pending.append(frame)
                continue
            sid = frame.get(sid_field) if sid_field and isinstance(frame, dict) else None
            if sid == expected:
                return expected
            if isinstance(sid, str):
                other = sid
        if other is not None:
            return other
        raise TransportError(f"the agent did not confirm session {expected!r} within {timeout}s")

    async def read_handshake(self, session: WebSocketSession, *, required: bool) -> str | None:
        """Read the handshake frame and return the session id it carries.

        A conversation frame that arrives first is kept for the driver. Without a
        required handshake, Opseval waits briefly and carries on.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._handshake_timeout
        sid_field = self.profile.session_id_field
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                if required:
                    raise TransportError("no handshake frame from the SUT")
                return None
            try:
                frame = await session._recv_raw(remaining if required else min(remaining, 0.5))
            except TransportTimeout:
                if required:
                    continue
                return None
            if self.profile.classify(frame) is FrameKind.HANDSHAKE:
                if sid_field and isinstance(frame, dict) and isinstance(frame.get(sid_field), str):
                    return str(frame[sid_field])
                return None
            session._pending.append(frame)
            return None
