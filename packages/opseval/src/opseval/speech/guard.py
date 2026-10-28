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
- **Usage is counted** per role and model: characters and audio seconds, rate-limited calls.

The guard also resolves the persona's voice through ``OPSEVAL_TTS_VOICE_MAP``.
"""

from __future__ import annotations

import asyncio
import math

from opseval.errors import (
    HarnessError,
    RateLimited,
    SttError,
    SttRateLimited,
    TtsError,
    TtsRateLimited,
)
from opseval.llm import UsageMeter
from opseval.speech.base import Audio, SpeechConfig, SpeechToText, TextToSpeech, Transcript


def _harness_error(
    exc: BaseException,
    config: SpeechConfig,
    *,
    trusted: bool,
    plain: type[TtsError] | type[SttError],
    limited: type[TtsError] | type[SttError],
) -> HarnessError:
    """What a provider's failure becomes. A built-in's own ``TtsError``/``SttError`` keeps
    its message (Opseval wrote it); anything from a plugin is renamed to its type only."""
    what = config.label or config.role
    if trusted and isinstance(exc, plain):
        return exc
    if isinstance(exc, TimeoutError):
        return plain(
            f"{what} provider {config.provider!r} timed out (limit "
            f"{config.timeout_seconds:g} s, OPSEVAL_{config.role.upper()}_TIMEOUT_SECONDS)"
        )
    cls = limited if isinstance(exc, RateLimited) else plain
    return cls(f"{what} provider {config.provider!r} failed: {type(exc).__name__}")


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
        resolved = resolve_voice(dict(self.config.voice_map), voice, language)
        audio: Audio | None = None
        rate_limited = 0
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                audio = await self.provider.synthesize(text, voice=resolved, language=language)
        except Exception as exc:
            error = _harness_error(
                exc, self.config, trusted=self.trusted, plain=TtsError, limited=TtsRateLimited
            )
            rate_limited = int(isinstance(error, RateLimited))
            if error is exc:
                raise
            raise error from exc
        finally:
            self._meter.record_speech(
                "tts",
                self.config.model_label,
                characters=len(text),
                audio_out_seconds=audio.seconds if isinstance(audio, Audio) else None,
                rate_limited=rate_limited,
            )
        if not isinstance(audio, Audio):
            raise TtsError(
                f"TTS provider {self.config.provider!r} returned {type(audio).__name__}, not Audio"
            )
        if not audio.pcm16:
            raise TtsError(f"TTS provider {self.config.provider!r} returned no audio")
        return audio

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
        rate_limited = 0
        what = self.config.label or "stt"
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                transcript = await self.provider.transcribe(audio, language=language, hint=hint)
        except Exception as exc:
            error = _harness_error(
                exc, self.config, trusted=self.trusted, plain=SttError, limited=SttRateLimited
            )
            rate_limited = int(isinstance(error, RateLimited))
            if error is exc:
                raise
            raise error from exc
        finally:
            self._meter.record_speech(
                "stt",
                self.config.model_label,
                audio_in_seconds=audio.seconds,
                rate_limited=rate_limited,
            )
        if not isinstance(transcript, Transcript) or not isinstance(transcript.text, str):
            raise SttError(
                f"{what} provider {self.config.provider!r} returned "
                f"{type(transcript).__name__}, not a Transcript"
            )
        confidence = transcript.confidence
        if confidence is not None and not (
            isinstance(confidence, int | float)
            and math.isfinite(confidence)
            and 0 <= confidence <= 1
        ):
            raise SttError(
                f"{what} provider {self.config.provider!r} reported a confidence outside 0 to 1"
            )
        return transcript

    async def aclose(self) -> None:
        try:
            await self.provider.aclose()
        except Exception:  # noqa: BLE001 - closing must never change a result
            return


__all__ = ["GuardedSpeechToText", "GuardedTextToSpeech", "resolve_voice"]
