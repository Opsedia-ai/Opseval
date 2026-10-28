# SPDX-License-Identifier: Apache-2.0
"""The built-in speech clients: the OpenAI-compatible audio API over httpx (ADR-0054).

- TTS: ``POST {url}/v1/audio/speech`` with ``model``, ``input``, ``voice`` and
  ``response_format`` (``wav``, or ``pcm``: raw PCM16 at ``OPSEVAL_TTS_PCM_RATE``).
- STT: ``POST {url}/v1/audio/transcriptions``, multipart, the audio as a WAV file and
  ``response_format=verbose_json`` with word and segment timestamps where the server gives them.

That one shape covers OpenAI, Groq, speaches and a local Whisper server. No vendor SDK
(ADR-0003). Retries follow :mod:`opseval.llm`: 429, 5xx, connection errors and timeouts back
off and retry, all inside the call's own time limit; any other 4xx is Opseval's configuration
and fails at once. Every failure is a ``TtsError`` / ``SttError`` (harness errors), a 429 that
outlasts its retries, or a 429/503 still coming when the limit is reached, is also
``RateLimited``, and nothing here ever raises ``TransportTimeout``: the provider's slowness is
never the agent's. An error names the HTTP status and, when the provider declares one, its
short error code; never the response body, which may echo the text to speak or the key.
"""

from __future__ import annotations

import asyncio
import math
import re
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
from opseval.llm import BACKOFF, RETRY_STATUSES, UsageMeter, _retry_after
from opseval.models.record import WordTiming
from opseval.speech.base import Audio, SpeechConfig, SpeechToText, TextToSpeech, Transcript

Sleep = Callable[[float], Awaitable[None]]


def new_http_client() -> httpx.AsyncClient:
    """A client per call when none was injected (tests replace this)."""
    return httpx.AsyncClient()


async def backoff_sleep(seconds: float) -> None:
    """The wait between retries when none was injected (tests replace this)."""
    await asyncio.sleep(seconds)


REQUEST_SHARE = 0.5
"""The most of a call's time limit one HTTP request may take, so a request that hangs leaves
time for at least one retry instead of spending the whole budget."""

DEADLINE_MARGIN = 0.05
"""The client stops this share of the limit (at most half a second) before the guard's own
timeout would, so a provider that keeps answering 429 is reported as rate limited, not as a
plain timeout."""

BUSY_STATUSES = frozenset({429, 503})
"""Answers that mean "busy, try later": a deadline reached after one is ``RateLimited``."""

SAFE_CODE = re.compile(r"^[a-z0-9_.-]{1,64}$")
"""A provider's declared error code or type is quoted in an error only when it matches this;
the body itself never is, because it may echo the text to speak or the key."""

SAFE_CONTENT_TYPE = re.compile(r"^[a-z0-9.+-]{1,40}/[a-z0-9.+-]{1,60}$")


def _error_code(resp: httpx.Response) -> str | None:
    """The provider's declared error code or type (``{"error": {"code"|"type": ...}}``, or a
    top-level ``code``/``type``), when it is a short safe token. Never free text."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    candidates: list[Any] = []
    if isinstance(error, dict):
        candidates += [error.get("code"), error.get("type")]
    candidates += [body.get("code"), body.get("type")]
    for value in candidates:
        if isinstance(value, str) and SAFE_CODE.match(value):
            return value
    return None


def _status(resp: httpx.Response) -> str:
    code = _error_code(resp)
    return f"HTTP {resp.status_code}" + (f" ({code})" if code else "")


class _Client:
    """The retrying POST shared by both clients.

    Every retry fits inside the call's own limit (``timeout_seconds``): a deadline is set when
    the call starts, each request may take the smaller of what is left and its share of the
    limit (:data:`REQUEST_SHARE`), and no wait between retries, a ``Retry-After`` included,
    runs past the deadline. When the deadline comes after the provider answered 429 or 503,
    the call is ``RateLimited``: the provider was busy, which a plain timeout would hide.

    Each 429 answer is counted in the usage meter, one per response, as :class:`ChatClient`
    counts them.
    """

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
        meter: UsageMeter | None = None,
    ) -> None:
        if not config.url:
            raise ValueError(f"the built-in {config.label or config.role} client needs a URL")
        self.config = config
        self._base = config.url.rstrip("/")
        self._http = http
        self._sleep = sleep
        self._max_retries = max_retries
        self._meter = meter

    async def _post(self, **request: Any) -> httpx.Response:
        what = self.config.label or self.config.role
        key = self.config.key
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        limit = self.config.timeout_seconds
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit - min(limit * DEADLINE_MARGIN, 0.5)
        cap = limit * REQUEST_SHARE
        client = self._http or new_http_client()
        too_many = 0  # 429 answers, one per response
        busy: str | None = None  # the last 429/503 answer, for the message
        attempts = 0
        error = "no attempt fitted in the time limit"
        try:
            for attempt in range(self._max_retries + 1):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                attempts += 1
                retry_after: float | None = None
                try:
                    async with asyncio.timeout(min(remaining, cap)):
                        resp = await client.post(
                            f"{self._base}{self._path}",
                            headers=headers,
                            timeout=min(remaining, cap),
                            **request,
                        )
                except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
                    error = "timed out" if isinstance(exc, TimeoutError) else type(exc).__name__
                else:
                    if resp.status_code == 200:
                        return resp
                    if resp.status_code not in RETRY_STATUSES:
                        raise self._rejected(
                            f"{what} provider rejected the request: {_status(resp)} "
                            "(check its model, voice and key)"
                        )
                    error = _status(resp)
                    if resp.status_code == 429:
                        too_many += 1
                    if resp.status_code in BUSY_STATUSES:
                        busy = error
                    retry_after = _retry_after(resp)
                if attempt == self._max_retries:
                    gave_up = self._rate_limited if _was_429(error) else self._error
                    raise gave_up(
                        f"{what} provider unavailable after {attempts} attempts ({error})"
                    )
                wait = retry_after if retry_after is not None else BACKOFF[min(attempt, 4)]
                if wait >= deadline - loop.time():
                    break  # the wait alone would outlast the limit: never sleep past it
                await (self._sleep or backoff_sleep)(wait)
            if busy is not None:
                raise self._rate_limited(
                    f"{what} provider rate limited or unavailable until its time limit "
                    f"({limit:g} s, OPSEVAL_{self.config.role.upper()}_TIMEOUT_SECONDS) after "
                    f"{attempts} attempts (last {busy})"
                )
            raise self._error(
                f"{what} provider did not answer within its time limit ({limit:g} s, "
                f"OPSEVAL_{self.config.role.upper()}_TIMEOUT_SECONDS) after {attempts} "
                f"attempts ({error})"
            )
        finally:
            if self._meter is not None:
                self._meter.record_speech_rate_limited(
                    self.config.role, self.config.model_label, too_many
                )
            if self._http is None:
                await client.aclose()

    async def aclose(self) -> None:
        return None


def _was_429(error: str) -> bool:
    return error.startswith("HTTP 429")


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
        meter: UsageMeter | None = None,
    ) -> None:
        super().__init__(config, http=http, sleep=sleep, max_retries=max_retries, meter=meter)
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
            shown = ctype if SAFE_CONTENT_TYPE.match(ctype) else "a non-audio type"
            raise TtsError(f"TTS provider answered {shown}, not audio")
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
