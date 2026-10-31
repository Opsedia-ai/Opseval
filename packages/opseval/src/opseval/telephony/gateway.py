# SPDX-License-Identifier: Apache-2.0
"""``opseval voice-gateway``: the one internet-facing listener (ADR-0056).

Twilio connects back to it for a call's TwiML, its status callbacks and its Media Stream. It
serves nothing else: no catalogue, no runs, no console (``opseval serve`` stays as it was).

- **TLS only.** ``--public-url`` is ``https`` (streams ``wss``) and allowlisted. The gateway
  serves TLS itself (``--tls-cert``/``--tls-key``) or sits behind a declared
  ``--trusted-proxy`` (then only the proxy may connect); anything else is refused at start.
- **Every request is signed.** Each webhook and each stream handshake must carry a valid
  ``X-Twilio-Signature`` over the configured public URL (never the ``Host`` header) and the
  POST parameters, checked in constant time with ``OPSEVAL_TWILIO_AUTH_TOKEN``. A missing or
  wrong one is 403, logged without its body. There is no switch to skip it.
- **Only calls it placed.** A run registers each call over the control channel and gets an
  unguessable per-call token, which every URL Twilio is given carries; the call SID Twilio
  reports must be the one the run bound to it. An unknown, expired or finished call is
  refused, and a call has one media stream at most, so a replayed handshake is refused too.
- **A closed channel to the run.** The control channel is loopback only, behind its own
  bearer token (``OPSEVAL_VOICE_GATEWAY_TOKEN``); a connection with an ``Origin`` header (a
  browser) is refused. Each message to the run carries the call's token, so the run accepts it
  only for the attempt it was minted for.

Needs the ``serve`` extra (Starlette and uvicorn) for the public side; the control channel is
``websockets``, a core dependency.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from xml.sax.saxutils import quoteattr

from pydantic import SecretStr
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response as WsResponse

from opseval.config import Settings, check_url_allowed
from opseval.errors import ConfigError
from opseval.telephony.link import MAX_MESSAGE, PROTOCOL, is_loopback, token_prefix
from opseval.telephony.twilio import valid_signature

log = logging.getLogger("opseval.voice_gateway")

MAX_BODY = 64 * 1024
"""A Twilio webhook body is a few hundred bytes; anything larger is refused unread."""
MAX_CALLS = 256
"""Calls registered at once, at most."""
MAX_TTL_SECONDS = 6 * 3600
BIND_WAIT_SECONDS = 5.0
"""How long a webhook may wait for the run to bind the call SID it just got from Twilio."""
GRACE_SECONDS = 120.0
"""After a call ends, its late status callbacks are still answered (and dropped) this long,
so Twilio does not log errors; its token opens nothing any more."""
STATUSES = re.compile(r"^[a-z-]{1,20}$")
CODE = re.compile(r"^[0-9]{1,6}$")
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _proxies(items: list[str]) -> list[Network]:
    out: list[Network] = []
    for raw in (i.strip() for i in items):
        if not raw:
            continue
        try:
            out.append(ipaddress.ip_network(raw, strict=False))
        except ValueError as exc:
            raise ConfigError(f"trusted proxy {raw!r} is not an address or CIDR") from exc
    return out


@dataclass
class GatewayConfig:
    public_url: str
    """The https address Twilio reaches the gateway at (scheme, host and port only)."""
    auth_token: SecretStr
    control_token: SecretStr
    host: str = "127.0.0.1"
    port: int = 8766
    control_url: str = "ws://127.0.0.1:8767"
    proxies: list[Network] = field(default_factory=list)
    tls_cert: Path | None = None
    tls_key: Path | None = None
    insecure_loopback: bool = False
    """Plain http on loopback with no TLS: only the in-process demo and the tests, never the
    CLI (the fake Twilio of ``opseval demo --voice --phone`` runs on this machine)."""

    @property
    def tls(self) -> bool:
        return self.tls_cert is not None and self.tls_key is not None

    @property
    def public(self) -> str:
        return self.public_url.rstrip("/")

    @property
    def public_ws(self) -> str:
        scheme, rest = self.public.split("://", 1)
        return ("wss" if scheme == "https" else "ws") + "://" + rest


def check_config(config: GatewayConfig, settings: Settings) -> None:
    """Refuse (exit 3) a gateway that would accept unauthenticated or unencrypted traffic."""
    parts = urlsplit(config.public_url.strip())
    host = (parts.hostname or "").lower()
    if not host or parts.scheme not in ("https", "http"):
        raise ConfigError("--public-url must be https://host[:port]")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise ConfigError("--public-url takes only scheme, host and port (no path or query)")
    plain_ok = config.insecure_loopback and is_loopback(host) and is_loopback(config.host)
    if parts.scheme != "https" and not plain_ok:
        raise ConfigError(
            "--public-url must be https: Twilio sends the call's audio and its auth-token "
            "signatures there (ADR-0056)"
        )
    check_url_allowed(config.public_url, settings.allowlist(), secure_schemes=("https",))
    if (config.tls_cert is None) != (config.tls_key is None):
        raise ConfigError("give both --tls-cert and --tls-key, or neither")
    if not (config.tls or config.proxies or plain_ok):
        raise ConfigError(
            "the voice gateway serves https: give --tls-cert and --tls-key, or --trusted-proxy "
            "for the proxy (or tunnel) that terminates TLS in front of it"
        )
    if not is_loopback(config.host) and not (config.tls or config.proxies):
        raise ConfigError(f"binding {config.host!r} needs TLS or a trusted proxy")
    control = urlsplit(config.control_url)
    if control.scheme != "ws" or not control.hostname or not is_loopback(control.hostname):
        raise ConfigError(
            "OPSEVAL_VOICE_GATEWAY_URL must be ws://127.0.0.1:<port>: the control channel "
            "is loopback only"
        )
    for name, secret in (
        ("OPSEVAL_TWILIO_AUTH_TOKEN", config.auth_token),
        ("OPSEVAL_VOICE_GATEWAY_TOKEN", config.control_token),
    ):
        if len(secret.get_secret_value()) < 16:
            raise ConfigError(f"{name} must be set (at least 16 characters)")


def config_from(
    settings: Settings,
    *,
    public_url: str,
    host: str = "127.0.0.1",
    port: int = 8766,
    proxies: list[str] | None = None,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
) -> GatewayConfig:
    if settings.twilio_auth_token is None or settings.voice_gateway_token is None:
        raise ConfigError(
            "the voice gateway needs OPSEVAL_TWILIO_AUTH_TOKEN (to verify Twilio's signatures) "
            "and OPSEVAL_VOICE_GATEWAY_TOKEN (the control channel's token)"
        )
    config = GatewayConfig(
        public_url=public_url,
        auth_token=settings.twilio_auth_token,
        control_token=settings.voice_gateway_token,
        host=host,
        port=port,
        control_url=settings.voice_gateway_url,
        proxies=_proxies(proxies or []),
        tls_cert=tls_cert,
        tls_key=tls_key,
    )
    check_config(config, settings)
    return config


# --- calls -----------------------------------------------------------------------------------


def _key(token: str) -> str:
    """Calls are looked up by the token's hash, so the lookup never compares the secret."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Call:
    token: str
    control: ServerConnection
    expires: float
    sid: str | None = None
    bound: asyncio.Event = field(default_factory=asyncio.Event)
    stream: Any = None
    stream_sid: str | None = None
    streamed: bool = False
    ended: bool = False

    async def tell(self, message: dict[str, Any]) -> None:
        """To the run, always naming this call's token."""
        with contextlib.suppress(ConnectionClosed, WebSocketException, OSError):
            await self.control.send(json.dumps({**message, "token": self.token}))

    async def to_twilio(self, message: dict[str, Any]) -> None:
        if self.stream is None or self.stream_sid is None:
            return
        with contextlib.suppress(Exception):
            await self.stream.send_text(json.dumps({**message, "streamSid": self.stream_sid}))


class Registry:
    def __init__(self) -> None:
        self._calls: dict[str, Call] = {}

    def mint(self, control: ServerConnection, ttl: float) -> Call | None:
        self.sweep()
        if len(self._calls) >= MAX_CALLS:
            return None
        token = secrets.token_urlsafe(32)
        call = Call(token=token, control=control, expires=time.monotonic() + ttl)
        self._calls[_key(token)] = call
        return call

    def get(self, token: str) -> Call | None:
        if TOKEN.fullmatch(token) is None:
            return None
        call = self._calls.get(_key(token))
        if call is None or time.monotonic() > call.expires:
            return None
        return call

    def end(self, call: Call) -> None:
        call.ended = True
        call.expires = min(call.expires, time.monotonic() + GRACE_SECONDS)

    def sweep(self) -> None:
        now = time.monotonic()
        for key in [k for k, c in self._calls.items() if now > c.expires]:
            del self._calls[key]

    def __len__(self) -> int:
        return len(self._calls)


# --- the gateway ---------------------------------------------------------------------------


class Gateway:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.calls = Registry()
        self.instance = secrets.token_hex(8)
        """Named by ``/healthz`` and by the control channel's hello, so preflight can tell
        that the public URL reaches this gateway and not another server."""

    # --- helpers --------------------------------------------------------------------------
    def _shown(self, path: str) -> str:
        parts = path.split("/")
        return "/".join(token_prefix(p) if TOKEN.fullmatch(p) else p for p in parts)

    def _refuse(self, what: str, path: str, why: str) -> None:
        log.warning("refused %s %s: %s", what, self._shown(path), why)

    def _peer_ok(self, client: Any) -> bool:
        if not self.config.proxies:
            return True
        try:
            address = ipaddress.ip_address(client.host if client else "")
        except ValueError:
            return False
        return any(address in network for network in self.config.proxies)

    def _signed(self, base: str, scope: dict[str, Any], header: str | None, params: Any) -> bool:
        raw = scope.get("raw_path") or scope.get("path", "").encode("utf-8")
        path = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
        query = scope.get("query_string", b"").decode("latin-1")
        url = base + path + (f"?{query}" if query else "")
        return valid_signature(self.config.auth_token.get_secret_value(), url, params, header)

    # --- the public side (Starlette) ----------------------------------------------------
    def app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route, WebSocketRoute
        from starlette.websockets import WebSocket, WebSocketDisconnect

        gateway = self

        async def form(request: Request) -> list[tuple[str, str]] | None:
            length = request.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
                return None
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > MAX_BODY:
                    return None
            ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if body and ctype != "application/x-www-form-urlencoded":
                return None
            try:
                return parse_qsl(body.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                return None

        async def webhook(request: Request) -> tuple[Call | None, dict[str, str], Response | None]:
            path = request.url.path
            if not gateway._peer_ok(request.client):
                gateway._refuse("webhook", path, "not from a trusted proxy")
                return None, {}, Response(status_code=403)
            params = await form(request)
            if params is None:
                gateway._refuse("webhook", path, "body too large or not a form")
                return None, {}, Response(status_code=413)
            header = request.headers.get("x-twilio-signature")
            if not gateway._signed(gateway.config.public, request.scope, header, params):
                gateway._refuse("webhook", path, "bad or missing X-Twilio-Signature")
                return None, {}, Response(status_code=403)
            call = gateway.calls.get(request.path_params["token"])
            if call is None:
                gateway._refuse("webhook", path, "unknown or expired call")
                return None, {}, Response(status_code=404)
            values = dict(params)
            if not call.ended:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(BIND_WAIT_SECONDS):
                        await call.bound.wait()
            sid = values.get("CallSid", "")
            if call.sid is None or not hmac.compare_digest(sid.encode(), call.sid.encode()):
                gateway._refuse("webhook", path, "call SID is not the one this token was bound to")
                return None, {}, Response(status_code=403)
            return call, values, None

        async def twiml(request: Request) -> Response:
            call, _values, refused = await webhook(request)
            if refused is not None or call is None:
                return refused or Response(status_code=403)
            if call.ended or call.streamed:
                gateway._refuse("webhook", request.url.path, "the call is over")
                return Response(status_code=404)
            stream = f"{gateway.config.public_ws}/twilio/stream/{call.token}"
            body = (
                '<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
                f"<Stream url={quoteattr(stream)} /></Connect></Response>"
            )
            return Response(body, media_type="text/xml", headers={"Cache-Control": "no-store"})

        async def status(request: Request) -> Response:
            call, values, refused = await webhook(request)
            if refused is not None or call is None:
                return refused or Response(status_code=403)
            if not call.ended:
                state = values.get("CallStatus", "")
                await call.tell(
                    {
                        "ev": "status",
                        "status": state if STATUSES.fullmatch(state) else None,
                        "error_code": _code(values.get("ErrorCode")),
                        "sip": _code(values.get("SipResponseCode")),
                    }
                )
            return Response(status_code=204)

        async def health(request: Request) -> Response:
            return JSONResponse(
                {"service": "opseval-voice-gateway", "instance": gateway.instance},
                headers={"Cache-Control": "no-store"},
            )

        async def stream(websocket: WebSocket) -> None:
            path = websocket.url.path
            if not gateway._peer_ok(websocket.client):
                gateway._refuse("stream", path, "not from a trusted proxy")
                await websocket.close(code=1008)
                return
            header = websocket.headers.get("x-twilio-signature")
            if not gateway._signed(gateway.config.public_ws, websocket.scope, header, ()):
                gateway._refuse("stream", path, "bad or missing X-Twilio-Signature")
                await websocket.close(code=1008)
                return
            call = gateway.calls.get(websocket.path_params["token"])
            if call is None or call.ended or call.streamed or call.sid is None:
                gateway._refuse("stream", path, "unknown, finished or already streamed call")
                await websocket.close(code=1008)
                return
            call.streamed = True
            await websocket.accept()
            started = stopped = False
            try:
                while True:
                    raw = await websocket.receive_text()
                    if len(raw) > MAX_MESSAGE:
                        break
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    event = message.get("event")
                    if event == "start" and not started:
                        start = message.get("start") or {}
                        sid = str(start.get("callSid", "")) if isinstance(start, dict) else ""
                        if not hmac.compare_digest(sid.encode(), call.sid.encode()):
                            gateway._refuse("stream", path, "stream for another call SID")
                            break
                        stream_sid = message.get("streamSid") or start.get("streamSid")
                        call.stream_sid = str(stream_sid) if stream_sid else None
                        call.stream = websocket
                        started = True
                        await call.tell({"ev": "stream_start"})
                    elif not started:
                        continue  # "connected" comes first
                    elif event == "media":
                        media = message.get("media") or {}
                        if isinstance(media, dict) and media.get("track", "inbound") == "inbound":
                            await call.tell({"ev": "media", "payload": media.get("payload")})
                    elif event == "dtmf":
                        found = message.get("dtmf") or {}
                        digit = str(found.get("digit", "")) if isinstance(found, dict) else ""
                        if re.fullmatch(r"[0-9*#A-D]", digit):
                            await call.tell({"ev": "dtmf", "digits": digit})
                    elif event == "mark":
                        mark = message.get("mark") or {}
                        name = str(mark.get("name", "")) if isinstance(mark, dict) else ""
                        await call.tell({"ev": "mark", "name": name[:64]})
                    elif event == "stop":
                        stopped = True
                        await call.tell({"ev": "stop"})
                        break
            except WebSocketDisconnect:
                pass
            finally:
                call.stream = None
                if started and not stopped and not call.ended:
                    await call.tell({"ev": "stream_closed"})
                with contextlib.suppress(Exception):
                    await websocket.close()

        return Starlette(
            routes=[
                Route("/twilio/twiml/{token}", twiml, methods=["POST"]),
                Route("/twilio/status/{token}", status, methods=["POST"]),
                Route("/healthz", health, methods=["GET"]),
                WebSocketRoute("/twilio/stream/{token}", stream),
            ]
        )

    # --- the control channel (websockets, loopback) --------------------------------------------
    def _authorize(self, connection: ServerConnection, request: WsRequest) -> WsResponse | None:
        if "Origin" in request.headers:
            return connection.respond(HTTPStatus.FORBIDDEN, "no browsers\n")
        given = request.headers.get("Authorization", "")
        want = f"Bearer {self.config.control_token.get_secret_value()}"
        if not hmac.compare_digest(given.encode(), want.encode()):
            log.warning("refused a control connection: bad or missing token")
            return connection.respond(HTTPStatus.UNAUTHORIZED, "control token required\n")
        return None

    async def control(self, conn: ServerConnection) -> None:
        call: Call | None = None

        async def reply(message: dict[str, Any]) -> None:
            with contextlib.suppress(ConnectionClosed):
                await conn.send(json.dumps(message))

        try:
            async for raw in conn:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(message, dict):
                    continue
                op = message.get("op")
                if op == "hello":
                    await reply(
                        {
                            "ev": "hello",
                            "version": PROTOCOL,
                            "public_url": self.config.public,
                            "instance": self.instance,
                        }
                    )
                elif op == "register" and call is None:
                    ttl = message.get("ttl_seconds")
                    seconds = ttl if isinstance(ttl, int) and 0 < ttl <= MAX_TTL_SECONDS else 900
                    call = self.calls.mint(conn, seconds)
                    if call is None:
                        await reply({"ev": "error", "error": "too_many_calls"})
                        continue
                    base = self.config.public
                    await reply(
                        {
                            "ev": "registered",
                            "token": call.token,
                            "twiml_url": f"{base}/twilio/twiml/{call.token}",
                            "status_url": f"{base}/twilio/status/{call.token}",
                        }
                    )
                elif op == "bind" and call is not None and call.sid is None:
                    sid = message.get("call_id")
                    if isinstance(sid, str) and ID.fullmatch(sid):
                        call.sid = sid
                        call.bound.set()
                elif op == "media" and call is not None:
                    payload = message.get("payload")
                    if isinstance(payload, str):
                        await call.to_twilio({"event": "media", "media": {"payload": payload}})
                elif op == "mark" and call is not None:
                    name = str(message.get("name", ""))[:64]
                    await call.to_twilio({"event": "mark", "mark": {"name": name}})
                elif op == "clear" and call is not None:
                    await call.to_twilio({"event": "clear"})
        except ConnectionClosed:
            pass
        finally:
            if call is not None:
                self.calls.end(call)
                if call.stream is not None:
                    with contextlib.suppress(Exception):
                        await call.stream.close()

    # --- running ----------------------------------------------------------------------------
    @asynccontextmanager
    async def running(self, *, embedded: bool = True) -> AsyncIterator[Gateway]:
        """Serve both sides until the block exits. With ``port`` 0 the public side binds a free
        port; :attr:`bound_port` says which."""
        import uvicorn

        control = urlsplit(self.config.control_url)
        control_server: Server = await serve(
            self.control,
            control.hostname or "127.0.0.1",
            control.port or 8767,
            process_request=self._authorize,
            max_size=MAX_MESSAGE,
            server_header=None,
        )
        sock = socket.socket(
            socket.AF_INET6 if ":" in self.config.host else socket.AF_INET, socket.SOCK_STREAM
        )
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.config.host, self.config.port))
        self.bound_port: int = sock.getsockname()[1]
        self.control_port: int = next(iter(control_server.sockets)).getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                self.app(),
                log_level="warning",
                access_log=False,  # an access line would carry the call token in its path
                proxy_headers=False,
                server_header=False,
                date_header=False,
                lifespan="off",
                ws_max_size=MAX_MESSAGE,
                ssl_certfile=str(self.config.tls_cert) if self.config.tls_cert else None,
                ssl_keyfile=str(self.config.tls_key) if self.config.tls_key else None,
            )
        )
        # Embedded (the demo, the tests): no signal handlers of its own, so Ctrl-C still ends
        # the run with 130. The CLI serves with uvicorn's own handling.
        task = asyncio.create_task(server._serve([sock]) if embedded else server.serve([sock]))
        try:
            while not server.started:
                if task.done():
                    await task
                    raise ConfigError("the voice gateway could not start")
                await asyncio.sleep(0.01)
            yield self
        finally:
            server.should_exit = True
            with contextlib.suppress(BaseException):
                await task
            control_server.close()
            with contextlib.suppress(BaseException):
                await control_server.wait_closed()
            sock.close()

    async def serve_forever(self) -> None:
        async with self.running(embedded=False):
            await asyncio.Event().wait()


def _code(value: str | None) -> str | None:
    return value if value is not None and CODE.fullmatch(value) else None


def headers_for(signature: str) -> Headers:
    return Headers({"X-Twilio-Signature": signature})


__all__ = [
    "Call",
    "Gateway",
    "GatewayConfig",
    "Registry",
    "check_config",
    "config_from",
]
