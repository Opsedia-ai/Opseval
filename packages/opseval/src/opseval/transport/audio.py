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
from typing import Any, ClassVar, Literal

from opseval.audio import AudioFormat, warm
from opseval.errors import UnsupportedCapability
from opseval.models.record import CallInfo, TransportEvent

Speaker = Literal["caller", "agent"]
DTMF = "dtmf"
"""The capability a transport declares in :attr:`AudioSession.capabilities` when it can
press keys."""
DUPLEX = "duplex"
"""The capability of a streaming transport that reads the agent while the caller speaks, so
the full-duplex driver (ADR-0055) can run on it. A request/response transport (``audio-http``)
hears the caller only once the turn is over, so it cannot."""
REALTIME = "realtime"
"""The line plays the caller's audio in real time, as a phone call does (ADR-0056). Such a
session always runs on the full-duplex driver, which paces the caller on the attempt's clock:
on the turn-based bridge the seconds the agent spends *hearing* the caller would be counted as
its own latency. A ``realtime`` session also declares ``duplex``."""


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
class AgentFrame:
    """A data message the agent sent beside its speech (a checkpoint, a result, an error):
    handed to the driver as a frame, classified by the profile like any other."""

    at_ms: float
    payload: Any


@dataclass(frozen=True)
class UndecodableAudio:
    """Audio from the agent that is not what its format says (ADR-0053): the agent's fault.
    The bytes are kept as they came, for the record."""

    at_ms: float
    data: bytes = field(repr=False)
    reason: str


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
    | AgentFrame
    | UndecodableAudio
)


class AudioSession(ABC):
    """One voice attempt with the agent. A transport creates a fresh one per attempt."""

    session_id: str | None = None
    capabilities: ClassVar[frozenset[str]] = frozenset()
    """What this transport can do beyond speaking and listening (``{DTMF}``). Declared on
    the class so a loader can refuse, at load time, a scenario that needs a capability the
    profile's transport lacks (ADR-0053)."""

    def __init__(self, fmt: AudioFormat, clock: AttemptClock | None = None) -> None:
        self.format = fmt
        """What goes over the wire. Events and ``send_audio`` always use PCM16 at
        ``format.rate``; the transport encodes and decodes the wire codec itself."""
        self.clock = clock or AttemptClock()
        # The codec tables are built here, when the session is made, so the first turn of
        # the attempt does not pay for them on the agent's clock.
        warm(fmt.codec)

    def transport_events(self) -> tuple[TransportEvent, ...]:
        """Drops, resumes and token refreshes seen by this session (recorded as evidence)."""
        return ()

    def call_info(self) -> CallInfo | None:
        """The phone call behind this session (ADR-0056), read once the session is closed.
        None for every transport that is not a phone call."""
        return None

    @abstractmethod
    async def start(self) -> None:
        """Open the call or stream. A connection that never opens is a harness error."""

    @abstractmethod
    async def send_audio(self, pcm16: bytes) -> None:
        """Stream one chunk of the caller's speech (PCM16 at ``format.rate``)."""

    async def send_dtmf(self, digits: str) -> None:
        """Press keys. A transport without DTMF says so rather than dropping them: a
        configuration problem (exit 3), never the agent's fault. A transport that overrides
        this declares ``DTMF`` in :attr:`capabilities`."""
        raise UnsupportedCapability(
            f"{type(self).__name__} cannot send DTMF; the scenario presses keys, so use a "
            "transport that declares the 'dtmf' capability"
        )

    @abstractmethod
    async def end_of_caller_turn(self, *, timeout: float | None = None) -> None:
        """The caller has finished speaking: flush, and for request/response transports, send.
        The agent's turn clock starts here (ADR-0042: harness time is not the agent's).
        ``timeout`` is how long the agent may take to answer, where the transport needs it to
        bound a request (``audio-http``)."""

    @abstractmethod
    async def next_event(self, timeout: float) -> VoiceEvent:
        """The agent's next event. Raises ``TransportTimeout`` when nothing comes in time and
        ``TransportClosed`` when the connection is gone."""

    @abstractmethod
    async def close(self) -> None: ...


class AudioTransport(ABC):
    """Factory for voice sessions, one per attempt (the voice counterpart of ``Transport``)."""

    name: str = "audio"
    capabilities: ClassVar[frozenset[str]] = frozenset()
    """What its sessions can do (:attr:`AudioSession.capabilities`), known before a call, so
    the runner can refuse a scenario the transport cannot run (exit 3) before anything is
    sent."""

    @abstractmethod
    async def open(self) -> AudioSession:
        """A new, not yet started, session."""

    @property
    @abstractmethod
    def target(self) -> str: ...


__all__ = [
    "AgentAudio",
    "AgentFrame",
    "AudioTransport",
    "UndecodableAudio",
    "AgentSpeechEnd",
    "AgentSpeechStart",
    "AgentTranscript",
    "DTMF",
    "DUPLEX",
    "REALTIME",
    "AttemptClock",
    "AudioSession",
    "Dtmf",
    "Hangup",
    "Mark",
    "Speaker",
    "Transfer",
    "VoiceEvent",
]
