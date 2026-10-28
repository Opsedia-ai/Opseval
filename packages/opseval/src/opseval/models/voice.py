# SPDX-License-Identifier: Apache-2.0
"""What the simulated caller does to a voice call (ADR-0055): the scenario's ``voice:`` block.

Everything here is something the **harness** does to the **caller's** side of the call:
interrupt the agent, pause, speak over background noise, use a given voice. None of it ever
touches the agent's audio, and none of it is ever counted as the agent's time or fault.

A scenario without ``voice:`` runs exactly as before. ``duplex: true``, or any ``barge_in``,
runs it on the full-duplex driver; ``silences``, ``noise`` and ``caller_voice`` apply on both
the turn-based bridge and the duplex driver. Every field is part of the scenario, so of its
digest: two runs with different noise are never compared as the same test.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)


class BargeIn(_Strict):
    """The caller interrupts one of the agent's spoken turns."""

    agent_turn: Annotated[int, Field(ge=1, le=100)]
    """Which of the agent's spoken turns to interrupt (1 = its first)."""
    after_ms: Annotated[int, Field(ge=0, le=60_000)] = 800
    """How far into the agent's speech (from its first sample with energy) the caller starts
    talking."""
    say: str = Field(min_length=1, max_length=500)
    """What the caller says over the agent. It becomes a customer turn in the record."""


class Silence(_Strict):
    """The caller says nothing for a while, before speaking or in the middle of a turn."""

    caller_turn: Annotated[int, Field(ge=1, le=100)]
    """Which caller turn (1 = the opening message)."""
    ms: Annotated[int, Field(ge=50, le=30_000)]
    """How long the silence lasts."""
    at: Literal["before", "mid"] = "mid"
    """``before``: silence, then the whole turn. ``mid``: the turn is cut at its middle word
    and the silence goes between the two halves (a caller pausing mid-sentence)."""


NoiseKind = Literal["white", "pink", "babble"]


class Noise(_Strict):
    """Background noise mixed into the caller's audio at a set signal-to-noise ratio.

    The noise is generated, not recorded (no speech, no licence, no size): ``white`` and
    ``pink`` are what they say; ``babble`` is several amplitude-modulated, band-limited noise
    voices, a crowd-like murmur with no words in it. The same ``seed`` gives the same noise.
    It is mixed into what the agent hears only: the STT canary and every reference text use
    the caller's clean audio and words."""

    kind: NoiseKind = "pink"
    snr_db: Annotated[float, Field(ge=-10.0, le=60.0)] = 15.0
    """Signal-to-noise ratio in dB, measured against the caller's speech."""
    seed: Annotated[int, Field(ge=0, le=2**31 - 1)] = 0


class VoiceSpec(_Strict):
    """The scenario's ``voice:`` block (ADR-0055)."""

    duplex: bool = False
    """Run on the full-duplex driver: the caller's audio is paced in real time while the
    agent's is read, so overlap, barge-in and dead air are measured. Implied by
    ``barge_in``."""
    barge_in: list[BargeIn] = Field(default_factory=list)
    """Interruptions the caller makes (duplex only)."""
    silences: list[Silence] = Field(default_factory=list)
    noise: Noise | None = None
    caller_voice: str | None = Field(default=None, min_length=1, max_length=100)
    """The caller's voice: a persona voice name looked up in ``OPSEVAL_TTS_VOICE_MAP`` (then
    the language, then ``default``), or a provider voice as is. Unset: by language."""
    code_switching: list[str] = Field(default_factory=list, max_length=3)
    """Languages the caller mixes within a sentence (``[ar, en]``): an LLM persona is told to
    switch between them, and the TTS voice is looked up for the first one, so it must be a
    voice that speaks all of them (``caller_voice``)."""

    @field_validator("code_switching")
    @classmethod
    def _languages(cls, value: list[str]) -> list[str]:
        if value and len(value) < 2:
            raise ValueError("code_switching names at least two languages, or none")
        if len(set(value)) != len(value):
            raise ValueError("code_switching lists a language twice")
        return value

    @model_validator(mode="after")
    def _check(self) -> VoiceSpec:
        turns = [b.agent_turn for b in self.barge_in]
        if len(set(turns)) != len(turns):
            raise ValueError("barge_in interrupts each agent turn at most once")
        where = [(s.caller_turn, s.at) for s in self.silences]
        if len(set(where)) != len(where):
            raise ValueError("silences: one 'before' and one 'mid' per caller turn at most")
        if self.code_switching and self.caller_voice is None:
            raise ValueError(
                "code_switching needs a caller_voice that speaks every listed language; "
                "a per-language voice would switch voices mid-sentence"
            )
        return self

    @property
    def runs_duplex(self) -> bool:
        return self.duplex or bool(self.barge_in)


__all__ = ["BargeIn", "Noise", "NoiseKind", "Silence", "VoiceSpec"]
