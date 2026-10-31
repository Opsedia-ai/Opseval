# SPDX-License-Identifier: Apache-2.0
"""Twilio, the built-in telephony provider (ADR-0056): REST over httpx, no SDK (ADR-0003).

One attempt is one call:

1. The session opens its control channel to ``opseval voice-gateway`` and registers: the
   gateway mints a per-call token and the TwiML and status-callback URLs that carry it.
2. ``POST /Calls`` dials the agent's number (``profile.telephony.to``, which must be in
   ``OPSEVAL_TELEPHONY_ALLOWED_NUMBERS``) from the operator's number, pointing Twilio at the
   gateway's TwiML webhook. The returned ``CallSid`` is bound to the token.
3. When the agent answers, Twilio fetches the TwiML (``<Connect><Stream>`` back to the
   gateway), opens the Media Stream and the gateway relays it, μ-law 8 kHz both ways.
4. Closing hangs up (``Status=completed``) and reads the call record for its status, duration
   and price, which goes into the usage meter as role ``telephony`` (``None`` when Twilio has
   not priced the call yet, never 0).

The one rule on the phone (ADR-0056): what comes before Twilio says it placed the call is the
harness's (:class:`~opseval.errors.TelephonyError`); a busy, rejected or unanswered agent
number is the agent's (:class:`~opseval.errors.TransportTimeout`, naming the status); the
stream dropping is :class:`~opseval.errors.TransportClosed` (not judged); the agent hanging up
is a recorded :class:`~opseval.transport.audio.Hangup`. Decided from Twilio's call status and
SIP code, never from message text.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from pydantic import SecretStr

from opseval.audio import AudioDecodeError, AudioFormat, decode, encode
from opseval.config import Settings, check_url_allowed
from opseval.errors import (
    ConfigError,
    TelephonyError,
    TelephonyRateLimited,
    TransportClosed,
    TransportTimeout,
)
from opseval.models.preflight import CheckStatus, HealthCheck
from opseval.models.profile import SutProfile, TelephonyConfig
from opseval.models.record import CallInfo
from opseval.models.usage import UsageEvent
from opseval.telephony import dtmf
from opseval.telephony.base import (
    Observable,
    TelephonyContext,
    TelephonyProvider,
    check_allowed_number,
    last4,
    masked,
)
from opseval.telephony.link import GatewayLink, check_control_url
from opseval.transport.audio import (
    DTMF,
    DUPLEX,
    REALTIME,
    AgentAudio,
    AttemptClock,
    AudioSession,
    AudioTransport,
    Dtmf,
    Hangup,
    Mark,
    UndecodableAudio,
    VoiceEvent,
)

if False:  # pragma: no cover - typing only, without importing the meter at run time
    from opseval.llm import UsageMeter

API_VERSION = "2010-04-01"
CALL_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")
ERROR_CODE = re.compile(r"^[0-9]{1,6}$")
ANSWER_GRACE_SECONDS = 20.0
"""After the ring timeout, how long Twilio may take to fetch the TwiML and open the stream.
Longer is the gateway's problem (its public URL), never the agent's."""
POLL_SECONDS = 2.0
"""While the call rings, its status is also read from the REST API this often, in case a
status callback never reaches the gateway."""
CLOSE_SECONDS = 5.0
"""Hanging up and reading the call record at the end of an attempt, at most."""
REJECTED_SIP = frozenset({"603"})
"""SIP 603 (Decline): the agent's side refused the call."""
OBSERVES: tuple[Observable, ...] = ("dtmf", "hangup")
"""A Media Stream carries the far end's key presses and its hang-up. A transfer made by the
agent's own switch is not visible to Twilio: ``voice.transfer`` is not measured on it."""


# --- webhook signatures --------------------------------------------------------------------


def signature(auth_token: str, url: str, params: Iterable[tuple[str, str]] = ()) -> str:
    """Twilio's ``X-Twilio-Signature``: base64 HMAC-SHA1, keyed with the auth token, over the
    full URL followed by every POST parameter's name and value, sorted by name."""
    data = url + "".join(k + v for k, v in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def valid_signature(
    auth_token: str, url: str, params: Iterable[tuple[str, str]], header: str | None
) -> bool:
    """Whether ``header`` is the signature of ``url`` and ``params``, in constant time."""
    if not header:
        return False
    expected = signature(auth_token, url, params)
    return hmac.compare_digest(expected.encode("ascii"), header.strip().encode("ascii", "replace"))


# --- the REST client ---------------------------------------------------------------------


class TwilioApiError(TelephonyError):
    """Twilio's REST API refused or failed a request. ``status`` is its HTTP status (None when
    it never answered), ``code`` Twilio's numeric error code when it gave one. Never its
    message text, which can repeat a number."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class TwilioRateLimited(TwilioApiError, TelephonyRateLimited):
    pass


class TwilioClient:
    """The few REST calls Opseval makes, on one account."""

    def __init__(
        self,
        api_url: str,
        account_sid: str,
        auth_token: SecretStr,
        *,
        allowlist: set[str],
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        check_url_allowed(api_url, allowlist, secure_schemes=("https",))
        self.account_sid = account_sid
        self._http = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            auth=(account_sid, auth_token.get_secret_value()),
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def _path(self, tail: str = "") -> str:
        return f"/{API_VERSION}/Accounts/{self.account_sid}{tail}.json"

    async def _request(
        self, method: str, path: str, data: list[tuple[str, str]] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, data=data)
        except httpx.HTTPError as exc:
            raise TwilioApiError(
                f"Twilio's API did not answer ({type(exc).__name__})", status=None
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        code = None
        if isinstance(body, dict) and ERROR_CODE.fullmatch(str(body.get("code", ""))):
            code = str(body["code"])
        status = response.status_code
        if status == 429:
            raise TwilioRateLimited("Twilio's API kept answering HTTP 429", status=429, code=code)
        if status in (401, 403):
            raise TwilioApiError(
                f"Twilio refused Opseval's account SID and auth token (HTTP {status}): check "
                "telephony.account_sid and OPSEVAL_TWILIO_AUTH_TOKEN",
                status=status,
                code=code,
            )
        if status >= 400:
            raise TwilioApiError(
                f"Twilio's API answered HTTP {status} (error code {code or 'none'})",
                status=status,
                code=code,
            )
        if not isinstance(body, dict):
            raise TwilioApiError(f"Twilio's API sent an unreadable answer (HTTP {status})")
        return body

    async def fetch_account(self) -> dict[str, Any]:
        return await self._request("GET", self._path())

    async def create_call(
        self,
        *,
        to: str,
        from_: str,
        url: str,
        status_callback: str,
        ring_timeout: int,
        time_limit: int,
    ) -> dict[str, Any]:
        data = [
            ("To", to),
            ("From", from_),
            ("Url", url),
            ("Method", "POST"),
            ("StatusCallback", status_callback),
            ("StatusCallbackMethod", "POST"),
            *[("StatusCallbackEvent", e) for e in ("initiated", "ringing", "answered", "completed")],
            ("Timeout", str(ring_timeout)),
            ("TimeLimit", str(time_limit)),
        ]
        return await self._request("POST", self._path("/Calls"), data)

    async def fetch_call(self, sid: str) -> dict[str, Any]:
        return await self._request("GET", self._path(f"/Calls/{sid}"))

    async def hangup(self, sid: str) -> dict[str, Any]:
        return await self._request("POST", self._path(f"/Calls/{sid}"), [("Status", "completed")])

    async def aclose(self) -> None:
        await self._http.aclose()


def outcome(
    status: str | None, error_code: str | None, sip_code: str | None, ring_timeout: int
) -> Exception | None:
    """What a call status means before the agent answered: the agent's ``TransportTimeout``
    (busy, rejected, no answer), the harness's ``TelephonyError`` (Twilio or Opseval failed it),
    or ``None`` (still on its way)."""
    if status == "busy":
        return TransportTimeout("the agent's number was busy (Twilio call status busy)")
    if status == "no-answer":
        return TransportTimeout(
            f"the agent's number did not answer within {ring_timeout}s (Twilio call status "
            "no-answer)"
        )
    if status == "failed" and sip_code in REJECTED_SIP:
        return TransportTimeout(
            f"the agent's number rejected the call (Twilio call status failed, SIP {sip_code})"
        )
    if status == "failed":
        return TelephonyError(
            f"Twilio failed the call (error code {error_code or 'none'}): a provider, carrier or "
            "account problem, not the agent"
        )
    if status == "canceled":
        return TelephonyError("the call was canceled before the agent answered")
    if status == "completed":
        return TelephonyError(
            "the call ended before its media stream reached the voice gateway (Twilio error "
            f"code {error_code or 'none'}): check the gateway's public URL"
        )
    return None


def _code(value: Any) -> str | None:
    text = str(value) if value is not None else ""
    return text if ERROR_CODE.fullmatch(text) else None


def _price(value: Any) -> float | None:
    """Twilio reports a call's price as a negative decimal string, or null until it is set."""
    if value is None or value == "":
        return None
    try:
        price = abs(float(value))
    except (TypeError, ValueError):
        return None
    return price if price == price and price != float("inf") else None


def _seconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


# --- the session (one call) ------------------------------------------------------------------


class TwilioSession(AudioSession):
    """One attempt's phone call, as an :class:`AudioSession`: μ-law 8 kHz both ways."""

    capabilities: ClassVar[frozenset[str]] = frozenset({DTMF, DUPLEX, REALTIME})

    def __init__(self, transport: TwilioTransport) -> None:
        super().__init__(AudioFormat(codec="mulaw", rate=8_000), AttemptClock())
        self._t = transport
        self._config = transport.config
        self._client: TwilioClient | None = None
        self._link: GatewayLink | None = None
        self._sid: str | None = None
        self._status: str | None = None
        self._answered_ms: float | None = None
        self._ended = False
        """The call is over (the agent hung up, or the stream stopped)."""
        self._closed = False
        self._duration: float | None = None
        self._price: float | None = None
        self._price_unit: str | None = None

    # --- placing the call ---------------------------------------------------------------
    async def start(self) -> None:
        settings = self._t.settings
        assert settings.twilio_auth_token is not None  # nosec B101 - checked by the provider
        self._client = self._t.client()
        self._link = GatewayLink(
            settings.voice_gateway_url, settings.voice_gateway_token, clock=self.clock.now_ms
        )
        await self._link.open()
        ttl = self._config.ring_timeout_seconds + self._config.max_call_seconds + 120
        registration = await self._link.register(ttl)
        assert self._config.from_ is not None  # nosec B101 - the profile requires it for twilio
        call = await self._client.create_call(
            to=self._config.to,
            from_=self._config.from_,
            url=registration.twiml_url,
            status_callback=registration.status_url,
            ring_timeout=self._config.ring_timeout_seconds,
            time_limit=self._config.max_call_seconds,
        )
        sid = call.get("sid")
        if not isinstance(sid, str) or CALL_SID.fullmatch(sid) is None:
            raise TelephonyError("Twilio placed the call but sent no valid call SID")
        self._sid = sid
        self._status = str(call.get("status") or "queued")
        await self._link.bind(sid)
        await self._until_answered()

    async def _until_answered(self) -> None:
        """Wait for the media stream, or for a status that ends the call first."""
        link = self._link
        client = self._client
        assert link is not None and client is not None and self._sid is not None  # nosec B101
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.ring_timeout_seconds + ANSWER_GRACE_SECONDS
        ring = self._config.ring_timeout_seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TelephonyError(
                    "the call's media stream never reached the voice gateway: check that "
                    "Twilio can reach the gateway's public URL"
                )
            try:
                async with asyncio.timeout(min(remaining, POLL_SECONDS)):
                    item = await link.events.get()
            except TimeoutError:
                polled = await client.fetch_call(self._sid)
                self._status = str(polled.get("status") or self._status)
                found = outcome(self._status, _code(polled.get("error_code")), None, ring)
                if found is not None:
                    raise found from None
                continue
            if isinstance(item, TransportClosed):
                raise TelephonyError("the voice gateway closed the control channel mid-call")
            at, message = item
            event = message.get("ev")
            if event == "stream_start":
                self._answered_ms = at
                self._status = "in-progress"
                return
            if event == "status":
                self._status = str(message.get("status") or self._status)
                found = outcome(
                    self._status, _code(message.get("error_code")), _code(message.get("sip")), ring
                )
                if found is not None:
                    raise found

    # --- the call ---------------------------------------------------------------------
    def _require(self) -> GatewayLink:
        if self._link is None:
            raise TelephonyError("the call is not placed")
        return self._link

    async def send_audio(self, pcm16: bytes) -> None:
        if self._ended:
            return  # nobody is on the line; the bridge reads the hang-up next
        payload = base64.b64encode(encode(pcm16, "mulaw")).decode("ascii")
        await self._require().send({"op": "media", "payload": payload})

    async def send_dtmf(self, digits: str) -> None:
        """The keys as in-band tones on the line (:mod:`opseval.telephony.dtmf`), paced in real
        time like the caller's speech."""
        pcm = dtmf.tones(digits, self.format.rate)
        step = self.format.rate * dtmf.FRAME_MS // 1000 * 2
        began = self.clock.now_ms()
        for k, i in enumerate(range(0, len(pcm), step)):
            ahead = began + k * dtmf.FRAME_MS - self.clock.now_ms()
            if ahead > 0:
                await asyncio.sleep(ahead / 1000)
            await self.send_audio(pcm[i : i + step])

    async def end_of_caller_turn(self, *, timeout: float | None = None) -> None:
        return None  # a phone line has no turn marker: the agent hears the silence

    def _event(self, at: float, message: dict[str, Any]) -> VoiceEvent | None:
        event = message.get("ev")
        if event == "media":
            raw = message.get("payload")
            try:
                wire = base64.b64decode(raw if isinstance(raw, str) else "", validate=True)
            except (binascii.Error, ValueError) as exc:
                text = raw.encode("utf-8", "replace") if isinstance(raw, str) else b""
                return UndecodableAudio(at_ms=at, data=text, reason=f"not base64: {exc}")
            try:
                return AgentAudio(at_ms=at, pcm16=decode(wire, "mulaw"))
            except AudioDecodeError as exc:  # pragma: no cover - every byte is valid μ-law
                return UndecodableAudio(at_ms=at, data=wire, reason=str(exc))
        if event == "dtmf":
            digits = str(message.get("digits", ""))
            return Dtmf(at_ms=at, digits=digits) if digits else None
        if event == "mark":
            return Mark(at_ms=at, name=str(message.get("name", "")))
        if event == "stop" or (event == "status" and message.get("status") == "completed"):
            if self._ended:
                return None
            self._ended = True
            self._status = "completed"
            return Hangup(at_ms=at, by="agent")
        if event == "stream_closed":
            raise TransportClosed(
                "the call's media stream dropped before the call ended (not the agent's doing)"
            )
        if event == "status":
            self._status = str(message.get("status") or self._status)
        return None

    async def next_event(self, timeout: float) -> VoiceEvent:
        link = self._require()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            try:
                async with asyncio.timeout(max(remaining, 0.001)):
                    item = await link.events.get()
            except TimeoutError as exc:
                raise TransportTimeout(f"nothing from the agent within {timeout:.1f}s") from exc
            if isinstance(item, TransportClosed):
                link.events.put_nowait(item)  # closed stays closed for every later read
                raise item
            found = self._event(*item)
            if found is not None:
                return found

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._sid is not None and self._client is not None:
                with contextlib.suppress(TimeoutError, TelephonyError):
                    async with asyncio.timeout(CLOSE_SECONDS):
                        await self._finish(self._client, self._sid)
                self._meter()
        finally:
            if self._link is not None:
                await self._link.close()
            if self._client is not None:
                await self._client.aclose()

    async def _finish(self, client: TwilioClient, sid: str) -> None:
        if not self._ended and self._status not in ("busy", "no-answer", "failed", "canceled"):
            await client.hangup(sid)
        call = await client.fetch_call(sid)
        self._status = str(call.get("status") or self._status)
        self._duration = _seconds(call.get("duration"))
        self._price = _price(call.get("price"))
        unit = call.get("price_unit")
        self._price_unit = str(unit)[:8] if isinstance(unit, str) and unit else None

    def _meter(self) -> None:
        """The call's cost, as Twilio reported it (role ``telephony``). A price Twilio has not
        set, or in another currency, is ``None``: not measured, never 0."""
        meter = self._t.meter
        if meter is None:
            return
        usd = self._price if self._price_unit == "USD" else None
        meter.add(
            UsageEvent(at=datetime.now(UTC), role="telephony", provider="twilio", model="voice"),
            provider_cost=usd,
        )

    def call_info(self) -> CallInfo | None:
        return CallInfo(
            provider="twilio",
            call_id=self._sid,
            status=self._status,
            to_last4=last4(self._config.to),
            from_last4=last4(self._config.from_),
            answered_ms=self._answered_ms,
            duration_seconds=self._duration,
            price=self._price,
            price_unit=self._price_unit,
            observes=OBSERVES,
        )


class TwilioTransport(AudioTransport):
    name = "phone"
    capabilities = TwilioSession.capabilities

    def __init__(
        self,
        profile: SutProfile,
        settings: Settings,
        *,
        meter: UsageMeter | None = None,
        http: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if profile.telephony is None or profile.telephony.account_sid is None:
            raise ConfigError(f"profile {profile.name!r} has no twilio telephony: block")
        self.profile = profile
        self.config: TelephonyConfig = profile.telephony
        self.settings = settings
        self.meter = meter
        self._http = http

    def client(self) -> TwilioClient:
        token = self.settings.twilio_auth_token
        assert token is not None and self.config.account_sid is not None  # nosec B101
        return TwilioClient(
            self.settings.twilio_api_url,
            self.config.account_sid,
            token,
            allowlist=self.settings.allowlist(),
            timeout=self.settings.twilio_timeout_seconds,
            transport=self._http,
        )

    @property
    def target(self) -> str:
        return masked(self.config.to)

    async def open(self) -> TwilioSession:
        return TwilioSession(self)


def _check(context: TelephonyContext) -> TelephonyConfig:
    config = context.telephony
    settings = context.settings
    token = settings.twilio_auth_token
    if token is None or not token.get_secret_value():
        raise ConfigError("a Twilio call needs OPSEVAL_TWILIO_AUTH_TOKEN (the account's auth token)")
    check_url_allowed(settings.twilio_api_url, settings.allowlist(), secure_schemes=("https",))
    check_allowed_number(config, settings)
    check_control_url(settings.voice_gateway_url)
    if settings.voice_gateway_token is None:
        raise ConfigError(
            "a phone run needs OPSEVAL_VOICE_GATEWAY_TOKEN, the voice gateway's control token"
        )
    return config


class TwilioProvider(TelephonyProvider):
    name = "twilio"
    capabilities = TwilioSession.capabilities
    observes = OBSERVES

    def transport(self, context: TelephonyContext) -> TwilioTransport:
        _check(context)
        return TwilioTransport(context.profile, context.settings, meter=context.meter)

    async def preflight(self, context: TelephonyContext) -> list[HealthCheck]:
        _check(context)
        transport = TwilioTransport(context.profile, context.settings)
        return [await _check_account(transport), await check_gateway(context.settings)]


async def _check_account(transport: TwilioTransport) -> HealthCheck:
    client = transport.client()
    try:
        account = await client.fetch_account()
    except TwilioApiError as exc:
        if exc.status in (401, 403, 404):
            raise ConfigError(
                f"Twilio refused the account (HTTP {exc.status}): check telephony.account_sid "
                "and OPSEVAL_TWILIO_AUTH_TOKEN. Nothing was dialled."
            ) from exc
        return HealthCheck(name="telephony", status=CheckStatus.UNAVAILABLE, detail=str(exc))
    finally:
        await client.aclose()
    state = account.get("status")
    if state not in (None, "active"):
        shown = state if isinstance(state, str) and state.isalpha() else "not active"
        return HealthCheck(
            name="telephony",
            status=CheckStatus.UNAVAILABLE,
            detail=f"the Twilio account is {shown}",
        )
    return HealthCheck(
        name="telephony",
        status=CheckStatus.OK,
        detail=f"Twilio account ending {transport.config.account_sid[-4:] if transport.config.account_sid else '?'} "
        f"answered; calls go to {masked(transport.config.to)}",
    )


async def check_gateway(settings: Settings) -> HealthCheck:
    """The gateway's control channel answers, its public URL is allowlisted, and that URL
    reaches this same gateway from here (its ``/healthz`` names the instance the control
    channel did)."""
    link = GatewayLink(settings.voice_gateway_url, settings.voice_gateway_token)
    try:
        await link.open()
        hello = await link.hello()
    except TelephonyError as exc:
        return HealthCheck(name="voice-gateway", status=CheckStatus.UNAVAILABLE, detail=str(exc))
    finally:
        await link.close()
    public, instance = hello.get("public_url"), hello.get("instance")
    if not isinstance(public, str) or not isinstance(instance, str):
        return HealthCheck(
            name="voice-gateway",
            status=CheckStatus.UNAVAILABLE,
            detail="the voice gateway did not say its public URL",
        )
    check_url_allowed(public, settings.allowlist(), secure_schemes=("https",))
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as http:
            response = await http.get(public.rstrip("/") + "/healthz")
        seen = response.json().get("instance") if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return HealthCheck(
            name="voice-gateway",
            status=CheckStatus.UNAVAILABLE,
            detail=f"the gateway's public URL is not reachable ({type(exc).__name__})",
        )
    if not isinstance(seen, str) or not hmac.compare_digest(seen, instance):
        return HealthCheck(
            name="voice-gateway",
            status=CheckStatus.UNAVAILABLE,
            detail="the gateway's public URL does not reach this voice gateway",
        )
    return HealthCheck(
        name="voice-gateway",
        status=CheckStatus.OK,
        detail="control channel and public URL both reach the voice gateway",
    )


__all__ = [
    "OBSERVES",
    "TwilioApiError",
    "TwilioClient",
    "TwilioProvider",
    "TwilioSession",
    "TwilioTransport",
    "check_gateway",
    "outcome",
    "signature",
    "valid_signature",
]
