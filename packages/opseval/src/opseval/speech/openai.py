# SPDX-License-Identifier: Apache-2.0
"""The built-in speech clients: the OpenAI-compatible audio API over httpx (ADR-0054).

- TTS: ``POST {url}/v1/audio/speech`` with ``model``, ``input``, ``voice`` and
  ``response_format`` (``wav``, or ``pcm``: raw PCM16 at ``OPSEVAL_TTS_PCM_RATE``).
- STT: ``POST {url}/v1/audio/transcriptions``, multipart, the audio as a WAV file and
  ``response_format=verbose_json`` with word and segment timestamps where the server gives them.

That one shape covers OpenAI, Groq, speaches and a local Whisper server. No vendor SDK
(ADR-0003). Retries follow :mod:`opseval.llm`: 429, 5xx, connection errors and timeouts back
off and retry; any other 4xx is Opseval's configuration and fails at once. Every failure is a
``TtsError`` / ``SttError`` (harness errors), a 429 that outlasts its retries is also
``RateLimited``, and nothing here ever raises ``TransportTimeout``: the provider's slowness is
never the agent's.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from opseval.audio import AudioDecodeError, from_wav, to_wav
from opseval.errors import (
    SttError,
    SttRateLimited,
    SttRequestError,
    TtsError,
    TtsRateLimited,
    TtsRequestError,
)
from opseval.llm import BACKOFF, RETRY_STATUSES, _retry_after
from opseval.models.record import WordTiming
from opseval.speech.base import Audio, SpeechConfig, SpeechToText, TextToSpeech, Transcript

Sleep = Callable[[float], Awaitable[None]]


def new_http_client() -> httpx.AsyncClient:
    """A client per call when none was injected (tests replace this)."""
    return httpx.AsyncClient()


async def backoff_sleep(seconds: float) -> None:
    """The wait between retries when none was injected (tests replace this)."""
    await asyncio.sleep(seconds)


class _Client:
    """The retrying POST shared by both clients."""

    _error: type[TtsError] | type[SttError]
    _rate_limited: type[TtsError] | type[SttError]
    _rejected: type[TtsError] | type[SttError]
    _path: str

    def __init__(
        self,
        config: SpeechConfig,
        *,
        http: httpx.AsyncClient | None = None,
        sleep: Sleep | None = None,
        max_retries: int = 5,
    ) -> None:
        if not config.url:
            raise ValueError(f"the built-in {config.label or config.role} client needs a URL")
        self.config = config
        self._base = config.url.rstrip("/")
        self._http = http
        self._sleep = sleep
        self._max_retries = max_retries

    async def _post(self, **request: Any) -> httpx.Response:
        what = self.config.label or self.config.role
        key = self.config.key
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        client = self._http or new_http_client()
        try:
            for attempt in range(self._max_retries + 1):
                retry_after: float | None = None
                try:
                    resp = await client.post(
                        f"{self._base}{self._path}",
                        headers=headers,
                        timeout=self.config.timeout_seconds,
                        **request,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    error = type(exc).__name__
                else:
                    if resp.status_code == 200:
                        return resp
                    if resp.status_code not in RETRY_STATUSES:
                        raise self._rejected(
                            f"{what} provider rejected the request: HTTP {resp.status_code} "
                            f"{resp.text[:200]} (check its model, voice and key)"
                        )
                    error = f"HTTP {resp.status_code}"
                    retry_after = _retry_after(resp)
                if attempt == self._max_retries:
                    gave_up = self._rate_limited if error == "HTTP 429" else self._error
                    raise gave_up(
                        f"{what} provider unavailable after {attempt + 1} attempts ({error})"
                    )
                await (self._sleep or backoff_sleep)(
                    retry_after if retry_after is not None else BACKOFF[min(attempt, 4)]
                )
        finally:
            if self._http is None:
                await client.aclose()
        raise self._error("unreachable")  # pragma: no cover

    async def aclose(self) -> None:
        return None


class OpenAITextToSpeech(_Client, TextToSpeech):
    """``POST {url}/v1/audio/speech``."""

    name = "openai"
    _error = TtsError
    _rate_limited = TtsRateLimited
    _rejected = TtsRequestError
    _path = "/v1/audio/speech"

    def __init__(
        self,
        config: SpeechConfig,
        *,
        response_format: str = "wav",
        pcm_rate: int = 24_000,
        http: httpx.AsyncClient | None = None,
        sleep: Sleep | None = None,
        max_retries: int = 5,
    ) -> None:
        super().__init__(config, http=http, sleep=sleep, max_retries=max_retries)
        if response_format not in ("wav", "pcm"):
            raise ValueError(f"response_format must be wav or pcm, got {response_format!r}")
        self._format = response_format
        self._pcm_rate = pcm_rate

    async def synthesize(self, text: str, *, voice: str | None, language: str) -> Audio:
        if not voice:
            raise TtsRequestError(
                f"no TTS voice for language {language!r}: set OPSEVAL_TTS_VOICE_MAP "
                '(for instance {"default": "alloy"})'
            )
        body = {
            "model": self.config.model,
            "input": text,
            "voice": voice,
            "response_format": self._format,
        }
        resp = await self._post(json=body)
        return self._decode(resp)

    def _decode(self, resp: httpx.Response) -> Audio:
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype.startswith(("application/json", "text/")):
            raise TtsError(f"TTS provider answered {ctype}, not audio")
        data = resp.content
        if not data:
            raise TtsError("TTS provider returned no audio")
        try:
            if self._format == "wav":
                pcm16, rate = from_wav(data)
            else:
                pcm16, rate = data, self._pcm_rate
            audio = Audio(pcm16=pcm16, rate=rate)
        except (AudioDecodeError, ValueError) as exc:
            raise TtsError(f"TTS provider returned audio Opseval cannot decode: {exc}") from exc
        if not audio.pcm16:
            raise TtsError("TTS provider returned no audio samples")
        return audio


class OpenAISpeechToText(_Client, SpeechToText):
    """``POST {url}/v1/audio/transcriptions`` with ``verbose_json``."""

    name = "openai"
    _error = SttError
    _rate_limited = SttRateLimited
    _rejected = SttRequestError
    _path = "/v1/audio/transcriptions"

    async def transcribe(
        self, audio: Audio, *, language: str, hint: str | None = None
    ) -> Transcript:
        data: dict[str, Any] = {
            "model": self.config.model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"],
        }
        primary = language.split("-")[0].split("_")[0].lower()
        if primary:
            # The API takes ISO-639-1 ("ar"), not a regional tag ("ar-EG").
            data["language"] = primary
        if hint:
            data["prompt"] = hint
        files = {"file": ("audio.wav", to_wav(audio.pcm16, audio.rate), "audio/wav")}
        resp = await self._post(data=data, files=files)
        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> Transcript:
        try:
            body = resp.json()
        except ValueError as exc:
            raise SttError("STT provider returned a body that is not JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise SttError("STT provider returned no 'text' in its transcription")
        return Transcript(
            text=body["text"].strip(),
            provider=self.config.provider,
            model=self.config.model,
            confidence=_confidence(body.get("segments")),
            words=_words(body.get("words")),
        )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _words(raw: Any) -> tuple[WordTiming, ...]:
    """Word timings, all or nothing: one malformed entry and none are kept, because a partial
    list would read as an agent that said fewer words."""
    if not isinstance(raw, list):
        return ()
    words: list[WordTiming] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("word"), str):
            return ()
        start, end = _number(item.get("start")), _number(item.get("end"))
        if start is None or end is None or end < start or start < 0:
            return ()
        probability = _number(item.get("probability"))
        if probability is not None and not 0 <= probability <= 1:
            probability = None
        words.append(
            WordTiming(
                word=item["word"].strip(),
                start_ms=start * 1000,
                end_ms=end * 1000,
                confidence=probability
                if probability is not None and 0 <= probability <= 1
                else None,
            )
        )
    return tuple(words)


def _confidence(segments: Any) -> float | None:
    """The mean of ``exp(avg_logprob)`` over the segments, when every segment has one; else
    ``None``. A provider that reports no log-probabilities has no confidence, not 1.0."""
    if not isinstance(segments, list) or not segments:
        return None
    probabilities: list[float] = []
    for segment in segments:
        logprob = _number(segment.get("avg_logprob")) if isinstance(segment, dict) else None
        if logprob is None:
            return None
        probabilities.append(min(1.0, math.exp(min(logprob, 0.0))))
    return sum(probabilities) / len(probabilities)


__all__ = ["OpenAISpeechToText", "OpenAITextToSpeech"]
