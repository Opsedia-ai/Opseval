# SPDX-License-Identifier: Apache-2.0
"""``audio-ws``: a speaking agent over a WebSocket audio stream (ADR-0053).

Caller audio goes out as the profile's codec at its rate, either as base64 inside a JSON
envelope (``{"type": "audio", "audio": "<base64>"}``, names configurable in
``audio.envelope``) or as binary frames. The agent's audio comes back the same way (binary
frames are always audio); its other events are JSON objects named by ``envelope.type_field``:
speech start and end, marks, its own transcript, DTMF, transfer and hang-up. Any other JSON
object is a data message (a checkpoint, a result) handed to the driver as a frame.

Faults follow the one rule: a connection that never opens is a ``TransportError`` and one
that drops is ``TransportClosed`` (not judged); nothing within the wait is
``TransportTimeout`` (the agent's fault); audio that is not what the codec says (bad base64,
an odd number of PCM16 bytes) is :class:`UndecodableAudio`, the agent's fault.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
from typing import Any, ClassVar

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

from opseval.audio import AudioDecodeError, AudioFormat, decode, encode
from opseval.auth import TokenSource, as_token_source
from opseval.errors import ConfigError, TransportClosed, TransportError, TransportTimeout
from opseval.models.profile import AudioConfig, SutProfile, render_frame
from opseval.transport.audio import (
    DTMF,
    AgentAudio,
    AgentFrame,
    AgentSpeechEnd,
    AgentSpeechStart,
    AgentTranscript,
    AttemptClock,
    AudioSession,
    AudioTransport,
    Dtmf,
    Hangup,
    Mark,
    Transfer,
    UndecodableAudio,
    VoiceEvent,
)


class AudioWsSession(AudioSession):
    capabilities: ClassVar[frozenset[str]] = frozenset({DTMF})

    def __init__(self, transport: AudioWsTransport, cfg: AudioConfig) -> None:
        super().__init__(AudioFormat(codec=cfg.codec, rate=cfg.rate), AttemptClock())
        self._transport = transport
        self._cfg = cfg
        self._conn: ClientConnection | None = None
        # Messages stamped when they arrive, by a reader that runs for the whole call: the
        # bridge reads them later (after STT, say), and latency must not count that wait.
        self._inbox: asyncio.Queue[tuple[float, str | bytes] | TransportClosed] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None

    def _require(self) -> ClientConnection:
        if self._conn is None:
            raise TransportError("the audio stream is not open")
        return self._conn

    async def _send_json(self, frame: dict[str, Any]) -> None:
        try:
            await self._require().send(json.dumps(frame, ensure_ascii=False))
        except ConnectionClosed as exc:
            raise TransportClosed(
                f"the audio stream closed: {exc}", exc.rcvd.code if exc.rcvd else None
            ) from exc

    async def start(self) -> None:
        self._conn = await self._transport.connect_ws()
        profile, env = self._transport.profile, self._cfg.envelope
        tokens = self._transport.tokens
        if profile.auth_frame is not None:
            if tokens is None:
                raise TransportError(
                    "the SUT profile requires auth but no token is configured "
                    "(set OPSEVAL_STATIC_TOKEN, or OPSEVAL_TOKEN_SIGNING_KEY with a profile "
                    "'token')"
                )
            await self._send_json(render_frame(profile.auth_frame, {"token": tokens.current()}))
        if env.start_frame is not None:
            values = {"codec": self._cfg.codec, "rate": self._cfg.rate}
            await self._send_json(render_frame(env.start_frame, values))
        self._reader = asyncio.create_task(self._read(self._conn))

    async def _read(self, conn: ClientConnection) -> None:
        """Stamp every message on the attempt's clock the moment it arrives (ADR-0053)."""
        try:
            async for data in conn:
                self._inbox.put_nowait((self.clock.now_ms(), data))
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            self._inbox.put_nowait(TransportClosed(f"the audio stream closed: {exc}", code))
            return
        self._inbox.put_nowait(TransportClosed("the audio stream closed", 1000))

    async def send_audio(self, pcm16: bytes) -> None:
        wire = encode(pcm16, self._cfg.codec)
        env = self._cfg.envelope
        if env.binary:
            try:
                await self._require().send(wire)
            except ConnectionClosed as exc:
                raise TransportClosed(f"the audio stream closed: {exc}") from exc
            return
        payload = base64.b64encode(wire).decode("ascii")
        await self._send_json({env.type_field: env.audio_event, env.audio_field: payload})

    async def send_dtmf(self, digits: str) -> None:
        env = self._cfg.envelope
        if env.dtmf_event is None:
            await super().send_dtmf(digits)
            return
        await self._send_json({env.type_field: env.dtmf_event, env.dtmf_digits_field: digits})

    async def end_of_caller_turn(self, *, timeout: float | None = None) -> None:
        env = self._cfg.envelope
        if env.caller_turn_end_event is not None:
            await self._send_json({env.type_field: env.caller_turn_end_event})

    def _audio(self, at: float, wire: bytes) -> VoiceEvent:
        try:
            return AgentAudio(at_ms=at, pcm16=decode(wire, self._cfg.codec))
        except AudioDecodeError as exc:
            return UndecodableAudio(at_ms=at, data=wire, reason=str(exc))

    def _event(self, at: float, data: str | bytes) -> VoiceEvent:
        if isinstance(data, bytes):
            return self._audio(at, data)
        try:
            frame = json.loads(data)
        except json.JSONDecodeError:
            return AgentFrame(at_ms=at, payload={"_raw": data})
        env = self._cfg.envelope
        kind = frame.get(env.type_field) if isinstance(frame, dict) else None
        if not isinstance(frame, dict) or not isinstance(kind, str):
            return AgentFrame(at_ms=at, payload=frame)
        if kind == env.audio_event:
            raw = frame.get(env.audio_field)
            try:
                wire = base64.b64decode(raw if isinstance(raw, str) else "", validate=True)
            except (binascii.Error, ValueError) as exc:
                text = raw.encode("utf-8", "replace") if isinstance(raw, str) else b""
                return UndecodableAudio(at_ms=at, data=text, reason=f"not base64: {exc}")
            return self._audio(at, wire)
        if kind == env.speech_start_event:
            return AgentSpeechStart(at_ms=at)
        if kind == env.speech_end_event:
            return AgentSpeechEnd(at_ms=at)
        if kind == env.mark_event:
            return Mark(at_ms=at, name=str(frame.get(env.mark_name_field, "")))
        if kind == env.transcript_event:
            return AgentTranscript(at_ms=at, text=str(frame.get(env.transcript_text_field, "")))
        if kind == env.dtmf_event:
            return Dtmf(at_ms=at, digits=str(frame.get(env.dtmf_digits_field, "")))
        if kind == env.transfer_event:
            to = frame.get(env.transfer_to_field)
            return Transfer(at_ms=at, to=to if isinstance(to, str) else None)
        if kind == env.hangup_event:
            return Hangup(at_ms=at, by="agent")
        return AgentFrame(at_ms=at, payload=frame)

    async def next_event(self, timeout: float) -> VoiceEvent:
        self._require()
        try:
            item = await asyncio.wait_for(self._inbox.get(), timeout=timeout)
        except TimeoutError as exc:
            raise TransportTimeout(f"nothing from the agent within {timeout:.1f}s") from exc
        if isinstance(item, TransportClosed):
            self._inbox.put_nowait(item)  # closed stays closed for every later read
            raise item
        at, data = item
        return self._event(at, data)

    async def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(WebSocketException):
                await self._conn.close()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader


class AudioWsTransport(AudioTransport):
    name = "audio-ws"

    def __init__(
        self,
        url: str,
        profile: SutProfile,
        *,
        token: str | TokenSource | None = None,
        open_timeout: float = 15.0,
    ) -> None:
        if profile.audio is None:
            raise ConfigError(f"profile {profile.name!r} has no audio: block, which audio-ws needs")
        self._url = url
        self.profile = profile
        self.cfg: AudioConfig = profile.audio
        self.tokens = as_token_source(token)
        self._open_timeout = open_timeout

    @property
    def target(self) -> str:
        return self._url

    async def connect_ws(self) -> ClientConnection:
        try:
            return await websockets.connect(
                self._url, open_timeout=self._open_timeout, max_size=16 * 1024 * 1024
            )
        except (OSError, TimeoutError, InvalidURI, WebSocketException) as exc:
            raise TransportError(f"cannot connect to {self._url}: {exc}") from exc

    async def open(self) -> AudioWsSession:
        return AudioWsSession(self, self.cfg)


__all__ = ["AudioWsSession", "AudioWsTransport"]
