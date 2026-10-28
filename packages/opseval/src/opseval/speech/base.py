# SPDX-License-Identifier: Apache-2.0
"""The speech provider contracts (ADR-0054).

A provider is a plain class with one async method: :class:`TextToSpeech` turns the simulated
customer's text into audio, :class:`SpeechToText` turns the agent's audio into a
:class:`Transcript`. The built-in OpenAI-compatible clients (:mod:`opseval.speech.openai`)
implement them, and so does any ``opseval.tts`` / ``opseval.stt`` plugin.

Audio is always **PCM16 little-endian mono** with its sample rate (:class:`Audio`), the
internal format of :mod:`opseval.audio`. A field the provider does not give (confidence, word
timings) is ``None`` or empty, never 0, so a check that needs it reports ``NOT_MEASURABLE``.

A provider may raise anything; the runner never calls it directly but through
:mod:`opseval.speech.guard`, which turns every failure into a ``TtsError`` or ``SttError``
(harness errors) and bounds every call with its own timeout.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from opseval.audio import duration_ms
from opseval.models.record import TranscriptInfo, WordTiming

SpeechRole = Literal["tts", "stt"]


@dataclass(frozen=True)
class Audio:
    """PCM16 little-endian mono at ``rate`` Hz."""

    pcm16: bytes = field(repr=False)
    rate: int

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError(f"a sample rate must be positive, got {self.rate}")
        if len(self.pcm16) % 2:
            raise ValueError(f"PCM16 needs an even number of bytes, got {len(self.pcm16)}")

    @property
    def seconds(self) -> float:
        return duration_ms(self.pcm16, self.rate) / 1000


@dataclass(frozen=True)
class Transcript:
    """What a speech-to-text provider heard. ``text`` may be empty: whether that is the
    agent's fault or Opseval's is for the STT canary to decide (ADR-0053), never this type."""

    text: str
    provider: str
    model: str | None
    confidence: float | None = None
    """0 to 1, or ``None`` when the provider gives none (not measured, never 1.0)."""
    words: tuple[WordTiming, ...] = ()
    """Word timings in milliseconds from the start of the audio; empty when not given."""

    def info(self) -> TranscriptInfo:
        """The evidence for ``FrameRecord.transcript``."""
        return TranscriptInfo(
            source="opseval",
            provider=self.provider,
            model=self.model,
            confidence=self.confidence,
            words=self.words,
        )


@dataclass(frozen=True)
class SpeechConfig:
    """What a provider is built from: the settings for its role, already allowlist-checked.

    A plugin receives this in its constructor and must send conversation content only to
    ``url``, the one host Opseval checked. ``secret`` reads any other secret by name the way
    a tenant's ``token_env`` is read (environment first, then ``.env``), because no module
    but :mod:`opseval.config` reads the environment.
    """

    role: SpeechRole
    provider: str
    url: str | None
    model: str | None
    timeout_seconds: float
    key: str | None = field(default=None, repr=False)
    voice_map: Mapping[str, str] = field(default_factory=dict)
    secret: Callable[[str], str | None] = field(default=lambda _name: None, repr=False)
    label: str = ""
    """Which configured provider this is (``tts``, ``stt``, ``stt second``), for messages."""

    @property
    def model_label(self) -> str:
        """The model as the usage meter names it: the provider is part of it unless it is the
        built-in, so two providers serving the same model id are counted apart."""
        model = self.model or "-"
        return model if self.provider == "openai" else f"{self.provider}:{model}"


class TextToSpeech(ABC):
    """Turns text into audio. Subclass it for an ``opseval.tts`` plugin."""

    name: str = "tts"
    """The provider's own name (a plugin must set its own)."""

    def __init__(self, config: SpeechConfig) -> None:
        self.config = config

    @abstractmethod
    async def synthesize(self, text: str, *, voice: str | None, language: str) -> Audio:
        """Speak ``text`` in ``language`` (a BCP-47 tag) with the provider's ``voice``, or
        the provider's default when ``None``."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release any connection the provider holds."""


class SpeechToText(ABC):
    """Turns audio into a :class:`Transcript`. Subclass it for an ``opseval.stt`` plugin."""

    name: str = "stt"

    def __init__(self, config: SpeechConfig) -> None:
        self.config = config

    @abstractmethod
    async def transcribe(
        self, audio: Audio, *, language: str, hint: str | None = None
    ) -> Transcript:
        """What ``audio`` says, in ``language``. ``hint`` is optional vocabulary (names,
        order ids) a provider may use as a prompt; it never changes what counts as heard."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release any connection the provider holds."""


__all__ = [
    "Audio",
    "SpeechConfig",
    "SpeechRole",
    "SpeechToText",
    "TextToSpeech",
    "Transcript",
]
