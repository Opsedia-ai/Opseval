# SPDX-License-Identifier: Apache-2.0
"""Every speech call goes through a guard (ADR-0054), built-in or plugin.

As :class:`~opseval.auth.GuardedTokens` wraps a credential provider, these wrap a speech
provider so that the one rule holds whatever the provider does:

- **Anything it raises becomes** ``TtsError`` / ``SttError`` (harness errors), naming the
  provider and the exception type only, never the message: a plugin's message may carry a key
  or the text it was asked to speak. A plugin's ``TimeoutError``, or even a
  ``TransportTimeout``, must never reach the voice bridge as the agent's timeout.
- **Each call has its own limit** (``OPSEVAL_TTS_TIMEOUT_SECONDS``,
  ``OPSEVAL_STT_TIMEOUT_SECONDS``), retries included, off the agent's clock.
- **The answer is checked.** TTS that returns no audio, or STT that returns something that is
  not a :class:`Transcript` or a confidence outside 0 to 1, is the provider's failure.
- **Usage is counted** per role and model: every call, the characters and audio seconds of
  the calls that succeeded (a failed call produced nothing to count), and each 429.

The guard also resolves the persona's voice through ``OPSEVAL_TTS_VOICE_MAP``.
"""

from __future__ import annotations

import asyncio
import math
import time

from opseval.errors import (
    HarnessError,
    RateLimited,
    SttError,
    SttRateLimited,
    SttRequestError,
    TtsError,
    TtsRateLimited,
    TtsRequestError,
)
from opseval.langpacks import provider_language
from opseval.llm import UsageMeter
from opseval.speech.base import Audio, SpeechConfig, SpeechToText, TextToSpeech, Transcript


def _harness_error(
    exc: BaseException,
    config: SpeechConfig,
    *,
    trusted: bool,
    plain: type[TtsError] | type[SttError],
    limited: type[TtsError] | type[SttError],
    rejected: type[TtsError] | type[SttError],
) -> HarnessError:
    """What a provider's failure becomes. A built-in's own ``TtsError``/``SttError`` keeps
    its message (Opseval wrote it); anything from a plugin is renamed to its type only. A
    plugin's rate limit stays ``RateLimited`` and its rejected request (``*RequestError``:
    the configuration is wrong) stays a request error, so preflight can tell an outage from a
    configuration Opseval must refuse."""
    what = config.label or config.role
    if trusted and isinstance(exc, plain):
        return exc
    if isinstance(exc, TimeoutError):
        return plain(
            f"{what} provider {config.provider!r} timed out (limit "
            f"{config.timeout_seconds:g} s, OPSEVAL_{config.role.upper()}_TIMEOUT_SECONDS)"
        )
    if isinstance(exc, RateLimited):
        cls = limited
    elif isinstance(exc, rejected):
        cls = rejected
    else:
        cls = plain
    return cls(f"{what} provider {config.provider!r} failed: {type(exc).__name__}")


def _plugin_429(error: HarnessError | None, trusted: bool) -> int:
    """A plugin's rate limit counts as one 429: the guard cannot see its responses. The
    built-in client counts each 429 response itself, so the guard adds none for it."""
    return int(not trusted and isinstance(error, RateLimited))


def resolve_voice(voice_map: dict[str, str], voice: str | None, language: str) -> str | None:
    """The provider voice for a persona voice and a language: the persona voice's mapping,
    then the language's (``ar-EG``, then ``ar``), then ``default``, then the persona voice as
    given (it may already be a provider voice). ``None`` when nothing names one."""
    if voice and voice in voice_map:
        return voice_map[voice]
    primary = language.split("-")[0]
    for key in (language, primary, "default"):
        if key and key in voice_map:
            return voice_map[key]
    return voice or None


class GuardedTextToSpeech:
    def __init__(
        self,
        provider: TextToSpeech,
        config: SpeechConfig,
        meter: UsageMeter | None = None,
        *,
        trusted: bool = False,
    ) -> None:
        self.provider = provider
        self.trusted = trusted  # a built-in: its own error messages are kept
        self.config = config
        self._meter = meter or UsageMeter()

    @property
    def describe(self) -> str:
        return f"{self.config.provider} ({self.config.model or 'default model'})"

    async def synthesize(self, text: str, *, voice: str | None, language: str) -> Audio:
        # The voice map is keyed by the pack's tag (ar-x-gulf); the provider is only ever
        # given a BCP-47 tag it can know (ar), never a private-use one (ADR-0059).
        resolved = resolve_voice(dict(self.config.voice_map), voice, language)
        began = time.monotonic()
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                audio = await self.provider.synthesize(
                    text, voice=resolved, language=provider_language(language)
                )
        except Exception as exc:
            error = _harness_error(
                exc,
                self.config,
                trusted=self.trusted,
                plain=TtsError,
                limited=TtsRateLimited,
                rejected=TtsRequestError,
            )
            self._failed(error, began)
            if error is exc:
                raise
            raise error from exc
        if not isinstance(audio, Audio):
            self._failed(None, began)
            raise TtsError(
                f"TTS provider {self.config.provider!r} returned {type(audio).__name__}, not Audio"
            )
        if not audio.pcm16:
            self._failed(None, began)
            raise TtsError(f"TTS provider {self.config.provider!r} returned no audio")
        # Only a call that produced audio counts its characters and seconds.
        self._meter.record_speech(
            "tts",
            self.config.model_label,
            characters=len(text),
            audio_out_seconds=audio.seconds,
            provider=self.config.provider,
            latency_ms=_since(began),
        )
        return audio

    def _failed(self, error: HarnessError | None, began: float) -> None:
        self._meter.record_speech(
            "tts",
            self.config.model_label,
            rate_limited=_plugin_429(error, self.trusted),
            answered=False,
            provider=self.config.provider,
            latency_ms=_since(began),
        )

    async def aclose(self) -> None:
        try:
            await self.provider.aclose()
        except Exception:  # noqa: BLE001 - closing must never change a result
            return


class GuardedSpeechToText:
    def __init__(
        self,
        provider: SpeechToText,
        config: SpeechConfig,
        meter: UsageMeter | None = None,
        *,
        trusted: bool = False,
    ) -> None:
        self.provider = provider
        self.trusted = trusted
        self.config = config
        self._meter = meter or UsageMeter()

    @property
    def identity(self) -> tuple[str, str | None]:
        """Provider and model: two STT checks with the same identity prove nothing."""
        return self.config.provider, self.config.model

    @property
    def describe(self) -> str:
        return f"{self.config.provider} ({self.config.model or 'default model'})"

    async def transcribe(
        self, audio: Audio, *, language: str, hint: str | None = None
    ) -> Transcript:
        what = self.config.label or "stt"
        began = time.monotonic()
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                transcript = await self.provider.transcribe(
                    audio, language=provider_language(language), hint=hint
                )
        except Exception as exc:
            error = _harness_error(
                exc,
                self.config,
                trusted=self.trusted,
                plain=SttError,
                limited=SttRateLimited,
                rejected=SttRequestError,
            )
            self._failed(error, began)
            if error is exc:
                raise
            raise error from exc
        if not isinstance(transcript, Transcript) or not isinstance(transcript.text, str):
            self._failed(None, began)
            raise SttError(
                f"{what} provider {self.config.provider!r} returned "
                f"{type(transcript).__name__}, not a Transcript"
            )
        confidence = transcript.confidence
        if confidence is not None and (
            # bool is an int to Python, but True is not a confidence of 1.0
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            self._failed(None, began)
            raise SttError(
                f"{what} provider {self.config.provider!r} reported a confidence that is not a "
                "number from 0 to 1"
            )
        # Only a call that was heard counts its audio seconds.
        self._meter.record_speech(
            "stt",
            self.config.model_label,
            audio_in_seconds=audio.seconds,
            provider=self.config.provider,
            latency_ms=_since(began),
        )
        return transcript

    def _failed(self, error: HarnessError | None, began: float) -> None:
        self._meter.record_speech(
            "stt",
            self.config.model_label,
            rate_limited=_plugin_429(error, self.trusted),
            answered=False,
            provider=self.config.provider,
            latency_ms=_since(began),
        )

    async def aclose(self) -> None:
        try:
            await self.provider.aclose()
        except Exception:  # noqa: BLE001 - closing must never change a result
            return


def _since(began: float) -> float:
    return round((time.monotonic() - began) * 1000, 1)


__all__ = ["GuardedSpeechToText", "GuardedTextToSpeech", "resolve_voice"]
