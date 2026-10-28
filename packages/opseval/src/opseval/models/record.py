# SPDX-License-Identifier: Apache-2.0
"""The immutable evidence of one attempt. Scorers read only this."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opseval.models.profile import FrameKind
from opseval.models.usage import UsageEvent

SCRIPT_EXHAUSTED = "script_exhausted"
"""Noted when a scripted customer ran out of turns (ADR-0043). The attempt ends
``goal_reached``, but that shows only that the conversation ran to its end, not that the goal
was met; scorers read the note, which lives here so they need not import the driver."""


AUDIO_RATES: tuple[int, ...] = (8_000, 16_000, 22_050, 24_000, 44_100, 48_000)
"""The sample rates a voice record may claim. Mirrors ``opseval.audio.RATES``: models are a
leaf and import nothing else in opseval, so the tuple is repeated and a test keeps the two
equal."""


VOICE_KEY = "_opseval_voice"
"""Where the voice bridge puts a spoken reply's evidence in the reply's payload (ADR-0053):
its level and length, every transcript tried, the decision taken on an empty one and the STT
canary behind it. In the payload, so ``rescore`` reads exactly what the run decided on."""


class TerminalState(StrEnum):
    OUTCOME_COMMITTED = "outcome_committed"
    STOPPED_AT_CHECKPOINT = "stopped_at_checkpoint"
    GOAL_REACHED = "goal_reached"
    MAX_TURNS_EXHAUSTED = "max_turns_exhausted"
    ERROR_FRAME = "error_frame"
    TIMEOUT = "timeout"
    TRANSPORT_FAILURE = "transport_failure"  # infra: never the agent's fault
    HARNESS_FAILURE = "harness_failure"  # infra: never the agent's fault

    @property
    def is_infra(self) -> bool:
        return self in (TerminalState.TRANSPORT_FAILURE, TerminalState.HARNESS_FAILURE)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AudioRef(Frozen):
    """One turn's audio, kept beside the record (ADR-0053). Caller and agent audio are
    separate files, so either can be withheld or deleted on its own.

    Three states, told apart by ``path`` and ``pruned``: a file (``path`` set), hashes only
    (no path, not pruned: ``OPSEVAL_RECORD_AUDIO=hashes``, or audio that could not be
    decoded), and pruned (no path, ``pruned``: the file existed and retention removed it).
    """

    path: str | None
    """Relative to the run folder, under ``audio/``. None when only the hash is kept
    (``OPSEVAL_RECORD_AUDIO=hashes``) or the file has been pruned (``pruned``)."""
    speaker: Literal["caller", "agent"]
    start_ms: float = Field(ge=0)
    """On the attempt's monotonic clock (``transport.audio.AttemptClock``)."""
    end_ms: float
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    codec: Literal["pcm16", "mulaw", "alaw"]
    rate: int
    bytes: int = Field(ge=0)
    pruned: bool = False
    """Retention removed the file (ADR-0053): the audio was there, so this is never read as
    missing evidence; text scorers still rescore from the transcript."""
    encryption: Literal["none", "aes-256-gcm"] = "none"
    """How the file is (or was, before pruning) encrypted at rest. ADR-0053 V0a records the
    field; V0d seals every file with AES-256-GCM under ``OPSEVAL_AUDIO_ENCRYPTION_KEY``."""
    key_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16,64}$")
    """A fingerprint of the key the file was encrypted with, never the key: it says which key
    opens the file after a rotation. Set exactly when ``encryption`` is not ``none``."""

    @field_validator("path")
    @classmethod
    def _inside_audio(cls, value: str | None) -> str | None:
        # A record is evidence other tools open: a path must never leave the run folder.
        if value is None:
            return value
        parts = value.split("/")
        if not value.startswith("audio/") or any(p in ("", ".", "..") for p in parts):
            raise ValueError("an audio path is relative to the run folder, under audio/")
        if "\\" in value or ":" in value:
            raise ValueError("an audio path uses '/' and no drive or scheme")
        return value

    @field_validator("rate")
    @classmethod
    def _known_rate(cls, value: int) -> int:
        if value not in AUDIO_RATES:
            raise ValueError(f"unsupported audio rate {value}; one of {AUDIO_RATES}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError("an audio turn cannot end before it starts (end_ms < start_ms)")
        if self.pruned and self.path is not None:
            raise ValueError("pruned audio has no file: path must be None")
        if (self.encryption == "none") != (self.key_id is None):
            raise ValueError("key_id is set exactly when the audio is encrypted")
        if self.encryption != "none" and self.hashes_only:
            raise ValueError("hashes-only audio has no file, so nothing is encrypted")
        return self

    @property
    def hashes_only(self) -> bool:
        """Only the hash and metadata were ever kept: no file, and none was pruned."""
        return self.path is None and not self.pruned


class WordTiming(Frozen):
    word: str
    start_ms: float
    end_ms: float
    confidence: float | None = None


class TranscriptInfo(Frozen):
    """Where a turn's ``text`` came from when it was spoken (ADR-0053). ``text`` stays the
    field every scorer reads; this is the evidence behind it."""

    source: Literal["opseval", "agent"]
    """``opseval``: Opseval's own speech-to-text heard the audio. ``agent``: the agent's
    protocol reported its own transcript."""
    provider: str | None = None
    model: str | None = None
    confidence: float | None = None
    """None when the provider gives none: not measured, never 1.0."""
    words: tuple[WordTiming, ...] = ()


class VoiceEventRecord(Frozen):
    at_ms: float
    kind: Literal["speech_start", "speech_end", "barge_in", "dtmf", "transfer", "hangup", "mark"]
    speaker: Literal["caller", "agent"] | None = None
    detail: str | None = None


class VoiceTimeline(Frozen):
    """A voice attempt's timeline on its monotonic clock (ADR-0053, ADR-0055)."""

    codec: Literal["pcm16", "mulaw", "alaw"]
    rate: int
    events: tuple[VoiceEventRecord, ...] = ()

    @field_validator("rate")
    @classmethod
    def _known_rate(cls, value: int) -> int:
        if value not in AUDIO_RATES:
            raise ValueError(f"unsupported audio rate {value}; one of {AUDIO_RATES}")
        return value


class FrameRecord(Frozen):
    seq: int
    at: datetime
    direction: Literal["sent", "received"]
    kind: FrameKind
    payload: Any
    latency_ms: float | None = None
    phase: str | None = None
    text: str | None = None
    audio: AudioRef | None = None
    """The spoken turn behind this frame, for voice attempts (ADR-0053)."""
    transcript: TranscriptInfo | None = None


class InterruptRecord(Frozen):
    seq: int
    checkpoint: str
    occurrence: int
    payload: Any = None
    action: str | None = None  # approve | reject | None when stopped before deciding
    feedback: str | None = None
    via: str | None = None
    expected_target_phase: str | None = None
    decision_seq: int | None = None


class ProbeResult(Frozen):
    """What one protocol probe got back (FR-RUN-07)."""

    name: str
    passed: bool
    reason: str | None = None
    """Why it did not pass. ``None`` when it did."""
    answered: bool = True
    """False when the agent said nothing within the probe's timeout."""
    sent_seq: int | None = None
    received_seq: int | None = None
    """Where the probe's frames sit in the record, so the evidence is findable."""


class CreatedArtifact(Frozen):
    """Something the agent created during the attempt (FR-REC-01): its kind, its id as the
    agent reported it, and the frame that reported it."""

    kind: str
    id: str
    seq: int
    id_redacted: bool = False
    """The id itself was a credential-shaped value and was redacted: the artifact exists, its
    id is not kept."""
    id_truncated: bool = False


class ToolCall(Frozen):
    """One tool call observed in the agent's own trace."""

    name: str
    started_at: datetime
    ended_at: datetime | None = None
    ok: bool = True
    arguments: Any = None
    observation_id: str | None = None


class TransportEvent(Frozen):
    """Connection-level events: drops, resumes and token refreshes (never token values)."""

    at: datetime
    kind: Literal["dropped", "resumed", "resume_failed", "token_refreshed"]
    close_code: int | None = None
    attempt: int | None = None
    detail: str | None = None


class RedactionInfo(Frozen):
    """What credential values were taken out of this record (FR-REC-03).

    ``applied`` exists so that an empty ``found`` cannot be misread. On a record written
    before value redaction shipped it is ``False``, which means *this was never checked*
    — not "nothing was there". That is the same distinction the scores make between
    ``failed`` and ``not measured``, applied to the record itself.
    """

    applied: bool = False
    found: tuple[str, ...] = ()
    """The distinct kinds found, sorted. Values are never kept.

    Distinct rather than counted: ``text`` is extracted from a frame's payload, so a
    single leaked key appears on both and a tally would say two."""


class RunRecord(Frozen):
    run_id: str
    test_id: str
    attempt_index: int
    seed: int
    session_id: str | None = None
    target: str
    started_at: datetime
    ended_at: datetime
    terminal_state: TerminalState
    stop_reason: str | None = None
    frames: tuple[FrameRecord, ...] = ()
    interrupts: tuple[InterruptRecord, ...] = ()
    user_turns: int = 0
    persona: dict[str, Any] = Field(default_factory=dict)
    trace_coverage: Literal["full", "frames_only"] = "frames_only"
    probes: tuple[ProbeResult, ...] = ()
    """Protocol probe outcomes, graded by ``protocol.probes_passed``."""
    tool_calls: tuple[ToolCall, ...] = ()
    """Filled from the trace source when ``trace_coverage`` is ``full``."""
    infra_errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    planted_injections: tuple[str, ...] = ()
    transport_events: tuple[TransportEvent, ...] = ()
    redaction: RedactionInfo = Field(default_factory=RedactionInfo)
    created_artifacts: tuple[CreatedArtifact, ...] = ()
    """What the agent created, read from frames as the profile declares (``artifacts``)."""
    artifacts_checked: bool = False
    """Whether they were looked for. Empty and unchecked is "not looked for", never "nothing
    created"; empty and checked is "nothing created"."""
    voice: VoiceTimeline | None = None
    """The attempt's spoken timeline; None for a text attempt (ADR-0053)."""
    usage_events: tuple[UsageEvent, ...] | None = None
    """Every LLM, TTS and STT call this attempt paid for: the simulator's turns, the speech
    and the judges' ballots (ADR-0066). ``None`` on records written before per-call usage
    (not measured); ``()`` when the attempt made no such call."""

    def received(self) -> list[FrameRecord]:
        return [f for f in self.frames if f.direction == "received"]

    def transcript(self) -> list[tuple[str, str]]:
        """(speaker, text) pairs; speaker is ``customer`` or ``agent``."""
        return [
            ("customer" if f.direction == "sent" else "agent", f.text)
            for f in self.frames
            if f.text
        ]
