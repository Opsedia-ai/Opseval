# SPDX-License-Identifier: Apache-2.0
"""The contract every voice transport implements (ADR-0053).

A text :class:`~opseval.transport.base.Session` exchanges JSON frames. A voice agent speaks:
an :class:`AudioSession` streams the caller's audio to it and yields what it does back as typed
:class:`VoiceEvent` values. HTTP audio files, WebSocket audio streams, phone calls and WebRTC
rooms each implement this one contract; the voice bridge (ADR-0053) and the full-duplex driver
(ADR-0055) build on it.

Every event carries ``at_ms``, milliseconds since the attempt started on **one monotonic
clock** (:class:`AttemptClock`). Latency, overlap and silence are measured on that clock only,
never on wall-clock time, which can jump.

Faults follow the one rule exactly as text sessions do: ``next_event`` raises
:class:`~opseval.errors.TransportTimeout` when the agent sends nothing in time (its fault) and
:class:`~opseval.errors.TransportClosed` when the connection is gone (not judged).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from opseval.audio import AudioFormat
from opseval.models.record import TransportEvent

Speaker = Literal["caller", "agent"]


class AttemptClock:
    """Milliseconds since the attempt started, from ``time.monotonic_ns``."""

    def __init__(self) -> None:
        self._start = time.monotonic_ns()

    def now_ms(self) -> float:
        return (time.monotonic_ns() - self._start) / 1_000_000


@dataclass(frozen=True)
class AgentAudio:
    """A chunk of the agent's speech, as PCM16 at the session's rate."""

    at_ms: float
    pcm16: bytes = field(repr=False)


@dataclass(frozen=True)
class AgentSpeechStart:
    at_ms: float


@dataclass(frozen=True)
class AgentSpeechEnd:
    """The agent says its turn is over (or its protocol marks it). Without one, the bridge
    detects the end of a turn from silence."""

    at_ms: float


@dataclass(frozen=True)
class AgentTranscript:
    """The agent's own transcript of what it said or heard, when its protocol exposes one."""

    at_ms: float
    text: str
    final: bool = True
    of: Speaker = "agent"


@dataclass(frozen=True)
class Dtmf:
    at_ms: float
    digits: str


@dataclass(frozen=True)
class Transfer:
    at_ms: float
    to: str | None = None


@dataclass(frozen=True)
class Hangup:
    at_ms: float
    by: Speaker


@dataclass(frozen=True)
class Mark:
    """A named point the agent's protocol reports (for example, playback finished)."""

    at_ms: float
    name: str


VoiceEvent = (
    AgentAudio
    | AgentSpeechStart
    | AgentSpeechEnd
    | AgentTranscript
    | Dtmf
    | Transfer
    | Hangup
    | Mark
)


class AudioSession(ABC):
    """One voice attempt with the agent. A transport creates a fresh one per attempt."""

    session_id: str | None = None

    def __init__(self, fmt: AudioFormat, clock: AttemptClock | None = None) -> None:
        self.format = fmt
        """What goes over the wire. Events and ``send_audio`` always use PCM16 at
        ``format.rate``; the transport encodes and decodes the wire codec itself."""
        self.clock = clock or AttemptClock()

    def transport_events(self) -> tuple[TransportEvent, ...]:
        """Drops, resumes and token refreshes seen by this session (recorded as evidence)."""
        return ()

    @abstractmethod
    async def start(self) -> None:
        """Open the call or stream. A connection that never opens is a harness error."""

    @abstractmethod
    async def send_audio(self, pcm16: bytes) -> None:
        """Stream one chunk of the caller's speech (PCM16 at ``format.rate``)."""

    async def send_dtmf(self, digits: str) -> None:
        """Press keys. A transport without DTMF says so rather than dropping them."""
        raise NotImplementedError(f"{type(self).__name__} cannot send DTMF")

    @abstractmethod
    async def end_of_caller_turn(self) -> None:
        """The caller has finished speaking: flush, and for request/response transports, send.
        The agent's turn clock starts here (ADR-0042: harness time is not the agent's)."""

    @abstractmethod
    async def next_event(self, timeout: float) -> VoiceEvent:
        """The agent's next event. Raises ``TransportTimeout`` when nothing comes in time and
        ``TransportClosed`` when the connection is gone."""

    @abstractmethod
    async def close(self) -> None: ...


__all__ = [
    "AgentAudio",
    "AgentSpeechEnd",
    "AgentSpeechStart",
    "AgentTranscript",
    "AttemptClock",
    "AudioSession",
    "Dtmf",
    "Hangup",
    "Mark",
    "Speaker",
    "Transfer",
    "VoiceEvent",
]
