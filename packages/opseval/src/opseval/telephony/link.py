# SPDX-License-Identifier: Apache-2.0
"""The run's side of the closed channel to ``opseval voice-gateway`` (ADR-0056).

The gateway is the only thing the telephony provider connects back to; a run never listens
on the internet. Each attempt opens its own control connection (loopback only, a bearer
token), registers, and gets a fresh per-call token and the webhook URLs to give the provider.
From then on the connection carries that call alone: the provider's status callbacks and the
media stream come to the run through it, and every message names the token, which the run
checks against the one it was given. A message for any other token is dropped and logged
(token prefix only), so a stray stream can never become this attempt's agent audio.

Faults are the harness's: a gateway that is down, refuses the control token or closes the
channel is a :class:`~opseval.errors.TelephonyError` (the channel closing mid-call is
:class:`~opseval.errors.TransportClosed`), never the agent's.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import websockets
from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI, WebSocketException

from opseval.errors import ConfigError, TelephonyError, TransportClosed

log = logging.getLogger("opseval.telephony")

PROTOCOL = 1
OPEN_TIMEOUT = 5.0
MAX_MESSAGE = 1024 * 1024
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback(host: str) -> bool:
    host = host.strip("[]").lower()
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_control_url(url: str) -> str:
    """The gateway's control channel is loopback only (``ws://127.0.0.1:port``): it carries
    every call's audio, so it never crosses a network (ADR-0056; mutually authenticated TLS
    between hosts is not built)."""
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise ConfigError("OPSEVAL_VOICE_GATEWAY_URL must be ws://127.0.0.1:<port>")
    if not is_loopback(parts.hostname):
        raise ConfigError(
            f"OPSEVAL_VOICE_GATEWAY_URL points at {parts.hostname!r}: the gateway's control "
            "channel is loopback only (run the gateway on this host)"
        )
    if parts.query or parts.username or parts.password:
        raise ConfigError("OPSEVAL_VOICE_GATEWAY_URL takes no credentials or query string")
    return url


def token_prefix(token: str) -> str:
    """What may be logged of a call token: enough to match two log lines, never to use it."""
    return token[:6] + "…" if token else "-"


@dataclass(frozen=True)
class Registration:
    token: str
    twiml_url: str
    status_url: str


class GatewayLink:
    """One attempt's control connection to the gateway."""

    def __init__(
        self, url: str, token: SecretStr | None, *, clock: Callable[[], float] | None = None
    ) -> None:
        self.url = check_control_url(url)
        if token is None or not token.get_secret_value():
            raise ConfigError(
                "a phone run needs OPSEVAL_VOICE_GATEWAY_TOKEN, the voice gateway's control "
                "token (the same value the gateway was started with)"
            )
        self._secret = token
        self._conn: ClientConnection | None = None
        self.token: str | None = None
        self.clock = clock or (lambda: 0.0)
        """Stamps each message on arrival (the attempt's clock), before anyone reads it."""
        self.events: asyncio.Queue[tuple[float, dict[str, Any]] | TransportClosed] = (
            asyncio.Queue()
        )
        self._reader: asyncio.Task[None] | None = None
        self._replies: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def open(self) -> None:
        headers = {"Authorization": f"Bearer {self._secret.get_secret_value()}"}
        try:
            self._conn = await websockets.connect(
                self.url,
                additional_headers=headers,
                open_timeout=OPEN_TIMEOUT,
                max_size=MAX_MESSAGE,
            )
        except InvalidStatus as exc:
            code = exc.response.status_code
            raise TelephonyError(
                f"the voice gateway refused the control connection (HTTP {code}): check "
                "OPSEVAL_VOICE_GATEWAY_TOKEN"
            ) from exc
        except (OSError, TimeoutError, InvalidURI, WebSocketException) as exc:
            raise TelephonyError(
                f"the voice gateway is not reachable at {self.url} ({type(exc).__name__}): "
                "start `opseval voice-gateway --public-url https://…` first"
            ) from exc
        self._reader = asyncio.create_task(self._read(self._conn))

    async def _read(self, conn: ClientConnection) -> None:
        try:
            async for raw in conn:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("ev") in ("hello", "registered", "error"):
                    self._replies.put_nowait(message)
                    continue
                if not self._ours(message):
                    continue
                self.events.put_nowait((self.clock(), message))
        except ConnectionClosed:
            pass
        self.events.put_nowait(TransportClosed("the voice gateway closed the control channel"))
        self._replies.put_nowait({"ev": "error", "error": "closed"})

    def _ours(self, message: dict[str, Any]) -> bool:
        """Accept a message only for the token minted for this attempt (ADR-0056)."""
        token = message.get("token")
        mine = self.token or ""
        if isinstance(token, str) and mine and hmac.compare_digest(token, mine):
            return True
        log.warning(
            "dropped a voice gateway message for another call (token %s)",
            token_prefix(token if isinstance(token, str) else ""),
        )
        return False

    def _require(self) -> ClientConnection:
        if self._conn is None:
            raise TelephonyError("the voice gateway control channel is not open")
        return self._conn

    async def send(self, message: dict[str, Any]) -> None:
        try:
            await self._require().send(json.dumps(message))
        except ConnectionClosed as exc:
            raise TransportClosed("the voice gateway closed the control channel") from exc

    async def _ask(self, message: dict[str, Any], want: str) -> dict[str, Any]:
        await self.send(message)
        try:
            async with asyncio.timeout(OPEN_TIMEOUT):
                reply = await self._replies.get()
        except TimeoutError as exc:
            raise TelephonyError(f"the voice gateway did not answer {message['op']!r}") from exc
        if reply.get("ev") != want:
            reason = reply.get("error")
            shown = reason if isinstance(reason, str) and reason.isidentifier() else "refused"
            raise TelephonyError(f"the voice gateway refused {message['op']!r} ({shown})")
        return reply

    async def hello(self) -> dict[str, Any]:
        """The gateway's public URL and its instance id (preflight checks both)."""
        return await self._ask({"op": "hello", "version": PROTOCOL}, "hello")

    async def register(self, ttl_seconds: int) -> Registration:
        reply = await self._ask({"op": "register", "ttl_seconds": ttl_seconds}, "registered")
        token, twiml, status = reply.get("token"), reply.get("twiml_url"), reply.get("status_url")
        if not all(isinstance(v, str) and v for v in (token, twiml, status)):
            raise TelephonyError("the voice gateway sent an incomplete registration")
        self.token = str(token)
        return Registration(token=str(token), twiml_url=str(twiml), status_url=str(status))

    async def bind(self, call_id: str) -> None:
        await self.send({"op": "bind", "call_id": call_id})

    async def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(WebSocketException, OSError):
                await self._conn.close()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader


__all__ = [
    "GatewayLink",
    "Registration",
    "check_control_url",
    "is_loopback",
    "token_prefix",
]
