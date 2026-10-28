# SPDX-License-Identifier: Apache-2.0
"""``audio-http``: a speaking agent that takes a caller turn as an HTTP request and answers
with audio (ADR-0053).

Each caller turn is one ``POST`` of the whole turn, as a WAV file (``audio/wav``) or raw
bytes in the profile's codec and rate (``audio.http.request_format``), with one id per
attempt in ``audio.http.session_header`` so the agent can keep the conversation. The answer
is audio (``audio/wav``, or raw bytes in the profile's codec for any other audio type) or a
JSON object ``{"audio": "<base64 wav or raw>", "transcript": "..."}``.

The request is sent when the caller's turn ends (``end_of_caller_turn``: the agent's clock
starts there, ADR-0042), and ``next_event`` waits for the answer. Faults follow the one rule
as ``http-json``'s do: a request that never left (``NOT_SENT_TIMEOUTS``, refused) is a
``TransportError`` (not judged); an answer that does not come in time is ``TransportTimeout``;
a 5xx is the agent answering with an error (a synthetic ``error`` frame); another 4xx is
Opseval's request being refused (``TransportError``); audio that cannot be decoded is
:class:`UndecodableAudio`, the agent's fault.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

from opseval.audio import (
    AudioDecodeError,
    AudioFormat,
    Resampler,
    decode,
    encode,
    from_wav,
    to_wav,
)
from opseval.auth import BearerAuth, TokenSource, as_token_source
from opseval.errors import ConfigError, TransportError, TransportTimeout
from opseval.models.profile import AudioConfig, SutProfile
from opseval.transport.audio import (
    AgentAudio,
    AgentFrame,
    AgentSpeechEnd,
    AgentTranscript,
    AttemptClock,
    AudioSession,
    AudioTransport,
    UndecodableAudio,
    VoiceEvent,
)
from opseval.transport.base import NOT_SENT_TIMEOUTS, SYNTHETIC_KIND, turn_timeout

CHUNK_MS = 20


class AudioHttpSession(AudioSession):
    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        cfg: AudioConfig,
        default_timeout: float,
    ) -> None:
        super().__init__(AudioFormat(codec=cfg.codec, rate=cfg.rate), AttemptClock())
        self._client = client
        self._url = url
        self._cfg = cfg
        self._default_timeout = default_timeout
        self._buffer: list[bytes] = []
        self._pending: asyncio.Task[httpx.Response] | None = None
        self._events: deque[VoiceEvent] = deque()
        self.session_id = uuid.uuid4().hex

    async def start(self) -> None:
        return None

    async def send_audio(self, pcm16: bytes) -> None:
        self._buffer.append(pcm16)

    def _body(self) -> tuple[bytes, str]:
        pcm = b"".join(self._buffer)
        self._buffer.clear()
        if self._cfg.http.request_format == "wav":
            return to_wav(pcm, self._cfg.rate), "audio/wav"
        return encode(pcm, self._cfg.codec), "application/octet-stream"

    async def end_of_caller_turn(self, *, timeout: float | None = None) -> None:
        limit = self._default_timeout if timeout is None else timeout
        body, ctype = self._body()
        headers = {"content-type": ctype, self._cfg.http.session_header: self.session_id or ""}

        async def post() -> httpx.Response:
            return await self._client.post(
                self._url, content=body, headers=headers, timeout=turn_timeout(limit)
            )

        self._events.clear()
        self._pending = asyncio.create_task(post())
        await asyncio.sleep(0)  # let the request start: the agent's clock runs from here

    def _audio(self, at: float, data: bytes, wav: bool) -> list[VoiceEvent]:
        try:
            if wav:
                pcm, rate = from_wav(data)
                if rate != self._cfg.rate:
                    resampler = Resampler(rate, self._cfg.rate)
                    pcm = resampler.feed(pcm) + resampler.flush()
            else:
                pcm = decode(data, self._cfg.codec)
        except AudioDecodeError as exc:
            return [UndecodableAudio(at_ms=at, data=data, reason=str(exc))]
        step = max(2, self._cfg.rate * CHUNK_MS // 1000 * 2)
        events: list[VoiceEvent] = [
            AgentAudio(at_ms=at, pcm16=pcm[i : i + step]) for i in range(0, len(pcm), step)
        ]
        return [*events, AgentSpeechEnd(at_ms=at)]

    def _answer(self, resp: httpx.Response) -> list[VoiceEvent]:
        at = self.clock.now_ms()
        if resp.status_code >= 500:
            # The agent answered, with an error: agent behaviour, not harness noise.
            return [
                AgentFrame(at_ms=at, payload={SYNTHETIC_KIND: "error", "status": resp.status_code})
            ]
        if resp.status_code >= 400:
            raise TransportError(f"agent rejected the request with HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype == "application/json":
            try:
                body: Any = resp.json()
            except ValueError:
                return [
                    UndecodableAudio(at_ms=at, data=resp.content, reason="JSON that does not parse")
                ]
            if not isinstance(body, dict):
                return [AgentFrame(at_ms=at, payload=body)]
            events: list[VoiceEvent] = []
            transcript = body.get("transcript")
            if isinstance(transcript, str):
                events.append(AgentTranscript(at_ms=at, text=transcript))
            raw = body.get("audio")
            if not isinstance(raw, str):
                # A data message (a checkpoint, a result) with no speech.
                return [*events, AgentFrame(at_ms=at, payload=body)]
            try:
                data = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as exc:
                return [
                    UndecodableAudio(
                        at_ms=at, data=raw.encode("utf-8", "replace"), reason=f"not base64: {exc}"
                    )
                ]
            return [*events, *self._audio(at, data, data[:4] == b"RIFF")]
        if not resp.content:
            return [AgentSpeechEnd(at_ms=at)]  # an empty answer: no speech, so no turn
        return self._audio(at, resp.content, ctype in ("audio/wav", "audio/x-wav", "audio/wave"))

    async def next_event(self, timeout: float) -> VoiceEvent:
        if self._events:
            return self._events.popleft()
        task = self._pending
        if task is None:
            raise TransportTimeout("the agent has nothing further for this turn")
        try:
            resp = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError as exc:
            raise TransportTimeout(f"the agent did not answer within {timeout:.1f}s") from exc
        except NOT_SENT_TIMEOUTS as exc:
            self._pending = None
            raise TransportError(f"could not connect to {self._url}: {exc!r}") from exc
        except httpx.TimeoutException as exc:
            self._pending = None
            raise TransportTimeout("the agent did not answer in time") from exc
        except httpx.HTTPError as exc:
            self._pending = None
            raise TransportError(f"HTTP request to {self._url} failed: {exc}") from exc
        self._pending = None
        self._events.extend(self._answer(resp))
        if not self._events:
            raise TransportTimeout("the agent sent nothing")
        return self._events.popleft()

    async def close(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            with contextlib.suppress(BaseException):
                await self._pending
        await self._client.aclose()


class AudioHttpTransport(AudioTransport):
    name = "audio-http"

    def __init__(
        self,
        url: str,
        profile: SutProfile,
        *,
        token: str | TokenSource | None = None,
        request_timeout: float = 120.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        if profile.audio is None:
            raise ConfigError(
                f"profile {profile.name!r} has no audio: block, which audio-http needs"
            )
        self._url = url
        self.profile = profile
        self.cfg: AudioConfig = profile.audio
        self._tokens = as_token_source(token)
        self._timeout = request_timeout
        self._client_factory = client_factory

    @property
    def target(self) -> str:
        return self._url

    async def open(self) -> AudioHttpSession:
        client = self._client_factory() if self._client_factory else httpx.AsyncClient()
        if self._tokens is not None:
            client.auth = BearerAuth(self._tokens)
        return AudioHttpSession(client, self._url, self.cfg, self._timeout)


__all__ = ["AudioHttpSession", "AudioHttpTransport"]
