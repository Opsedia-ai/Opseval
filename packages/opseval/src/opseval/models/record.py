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
    kind: Literal[
        "speech_start",
        "speech_end",
        "barge_in",
        "dtmf",
        "transfer",
        "hangup",
        "mark",
        "agent_heard",
    ]
    """``agent_heard`` (ADR-0055): the agent's own transcript of a caller turn, when its
    protocol sends one (``detail``, redacted like any transcript); ``voice.agent_asr_wer``
    grades it against what the caller said."""
    speaker: Literal["caller", "agent"] | None = None
    detail: str | None = None


class VoiceSpan(Frozen):
    """One stretch of the call on the attempt's monotonic clock (ADR-0055).

    ``speech``: someone made sound. A caller span is one whole caller turn, from its first
    sample to its last (a planted mid-turn silence included: the caller has not finished);
    ``barge_in`` marks the caller's planted interruptions. An agent span is one voiced stretch
    of an agent turn, from its first chunk with energy to the end of its last one as it would
    play out (a chunk cannot play before it arrives, nor over the one before it), so a turn
    with pauses is several spans with the same ``turn``. ``harness``: Opseval's own TTS or STT
    was working; such a span is never the agent's time, and a gap it overlaps cannot be
    placed (left out, degraded)."""

    kind: Literal["speech", "harness"]
    speaker: Literal["caller", "agent"] | None = None
    start_ms: float = Field(ge=0)
    end_ms: float
    turn: int | None = Field(default=None, ge=1)
    """The speaker's own turn number (1 = its first): caller turns and agent turns are
    counted separately. None for a harness span."""
    barge_in: bool = False
    """A caller turn the scenario planted to interrupt the agent (``voice.barge_in``)."""
    detail: str | None = None
    """For a harness span: what ran (``tts``, ``stt``, ``canary``, ``noise``)."""
    holds_line: bool = True
    """For a harness span: whether the agent's audio could not be heard (placed on the clock)
    while it ran. ``False`` on the duplex driver, whose session stamps every agent event on
    arrival while Opseval works (the ``duplex`` capability): such a span is beside the agent's
    path, and a gap it overlaps is measured. Only a span that holds the line leaves a gap
    out (``UNPLACEABLE``, review of #91)."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError("a span cannot end before it starts (end_ms < start_ms)")
        if (self.kind == "speech") != (self.speaker is not None):
            raise ValueError("a speech span names its speaker; a harness span names none")
        if self.barge_in and self.speaker != "caller":
            raise ValueError("only a caller turn is a planted barge-in")
        return self


VoiceMetricName = Literal[
    "voice.time_to_first_audio_ms",
    "voice.turn_latency_ms",
    "voice.barge_in_yield_ms",
    "voice.talk_over_ratio",
    "voice.dead_air_ms",
    "voice.agent_asr_wer",
    "voice.asr_confidence",
]


class VoiceMetric(Frozen):
    """One raw voice measurement (ADR-0055), the files-mode twin of an ``attempt_metrics`` row
    (ADR-0051): one row per turn for a per-turn metric (``turn`` set), one per attempt for an
    attempt-level one (``turn`` None). Percentiles are computed from the rows by whoever reads
    them, never stored folded.

    ``value`` None is **not measured**, and ``reason`` says why; it is never 0. ``degraded``
    marks a value measured on partial evidence (a gap the clocks could not place was left
    out), and ``reason`` names what was left out."""

    name: VoiceMetricName
    turn: int | None = Field(default=None, ge=1)
    """The agent turn (or, for ``voice.agent_asr_wer``, the caller turn) it measures."""
    value: float | None
    unit: Literal["ms", "ratio", "wer", "confidence"]
    degraded: bool = False
    reason: str | None = None

    @model_validator(mode="after")
    def _explained(self) -> Self:
        if (self.value is None or self.degraded) and not self.reason:
            raise ValueError("a metric that is not measured, or degraded, says why (reason)")
        return self


class NoiseInfo(Frozen):
    """The background noise mixed into the caller's audio (ADR-0055): enough to make it
    again, bit for bit (generator, kind, seed) and to read a result against it (SNR)."""

    generator: str
    """``opseval-noise/<version>``: the in-house procedural generator."""
    kind: Literal["white", "pink", "babble"]
    snr_db: float
    seed: int


class VoiceCallerTurn(Frozen):
    """One caller turn, as the metrics read it (ADR-0055)."""

    turn: int = Field(ge=1)
    """The caller's own turn number in the call (planted interruptions included), as on its
    speech span."""
    text: str
    """What the caller said, before any noise (redacted): the agent WER's reference."""
    start_ms: float = Field(ge=0)
    end_ms: float = Field(ge=0)
    barge_in: bool = False


class VoiceAgentTurn(Frozen):
    """One spoken agent turn, as the metrics read it."""

    turn: int = Field(ge=1)
    heard: str | None = None
    """How Opseval heard it (``stt``, ``second_stt``, ``unintelligible``, ``asr_failed``,
    ``cut_short``); None when it was never transcribed."""
    confidence: float | None = None
    burst: bool = False
    """Its audio arrived faster than it plays (turn-based bridge): pauses inside it are not on
    the clock, so its dead air is degraded."""


class VoiceAgentHeard(Frozen):
    """One final transcript the agent sent of the caller (``AgentTranscript(of=caller)``),
    stamped on arrival; attributed to a caller turn by time when the metrics are computed."""

    at_ms: float = Field(ge=0)
    text: str


class VoiceBargeIn(Frozen):
    """A planted interruption and what became of it."""

    agent_turn: int = Field(ge=1)
    after_ms: int = Field(ge=0)
    fired_at_ms: float | None = None
    ended_at_ms: float | None = None
    """When the caller finished the interruption."""
    reason: str | None = None


class VoiceMetricInputs(Frozen):
    """Everything besides the spans that the voice metrics are computed from (layout 8)."""

    version: str
    """The metric rules' version (``voice.metrics.METRICS_VERSION``) the stored rows were
    computed with."""
    call_start_ms: float = Field(default=0.0, ge=0)
    callers: tuple[VoiceCallerTurn, ...] = ()
    agents: tuple[VoiceAgentTurn, ...] = ()
    agent_heard: tuple[VoiceAgentHeard, ...] | None = None
    """None: the agent sent no transcript of the caller at all (its WER is not measurable)."""
    barge_ins: tuple[VoiceBargeIn, ...] = ()


class VoiceTimeline(Frozen):
    """A voice attempt's timeline on its monotonic clock (ADR-0053, ADR-0055)."""

    codec: Literal["pcm16", "mulaw", "alaw"]
    rate: int
    events: tuple[VoiceEventRecord, ...] = ()
    mode: Literal["turns", "duplex"] = "turns"
    """``turns``: the voice bridge (one side speaks, then the other). ``duplex``: the
    full-duplex driver, which plays the caller in real time while it listens (ADR-0055)."""
    spans: tuple[VoiceSpan, ...] | None = None
    """Who spoke when, and when the harness worked. None on records written before ADR-0055
    (layouts 3 to 6): not recorded, so every voice metric reads as not measured."""
    metrics: tuple[VoiceMetric, ...] | None = None
    """The raw voice metrics, computed from ``spans`` when the attempt ended. None on older
    records: not measured, never 0."""
    noise: NoiseInfo | None = None
    inputs: VoiceMetricInputs | None = None
    """What the metrics were computed from besides ``spans`` (layout 8): caller texts and
    times, the agent's own transcripts with their arrival, agent turns, planted barge-ins.
    ``opseval rescore`` recomputes the metrics from them when the metric rules changed
    (``inputs.version``). None on older records: their stored metrics are used as written."""
    stt_min_confidence: float | None = Field(default=None, ge=0, le=1)
    """``OPSEVAL_STT_MIN_CONFIDENCE`` when the call ran (layout 8): the bound under which a
    word or turn Opseval heard counts as unsure (``judge.pronunciation_dialect_fit``)."""

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


TURN_KINDS = frozenset({FrameKind.USER_MESSAGE, FrameKind.RESUME})
"""Sent frames that are conversation turns (a customer message, a checkpoint answer)."""
NOT_REPLIES = frozenset({FrameKind.HANDSHAKE, FrameKind.NOTIFICATION})
"""Received frames that never answer a turn: transport chatter and out-of-band notices."""
NO_REPLY = "NO_REPLY"
NOT_TIMED = "NOT_TIMED"


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
    """The persona as run, plus ``pack``: the tag of the language pack it resolved to (``null``
    when none was used, ADR-0059). Judges grade against it; a record without ``pack`` predates
    it and is graded with the resolution of its time."""
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

    def turn_latencies(self) -> list[tuple[float | None, str | None]]:
        """The agent's reply time per conversation turn (ADR-0066, the Latency dashboard), as
        ``(latency_ms, reason)``.

        A turn is a customer message or a checkpoint answer the driver sent; protocol probes,
        their answers, handshake and auth frames and out-of-band notifications are not turns
        (the same frames ``protocol.replies_read`` leaves out). Its latency is the
        ``latency_ms`` of the first frame back. ``(None, NOT_TIMED)``: a reply the clocks could
        not time (a voice barge-in). ``(None, NO_REPLY)``: the agent never answered before the
        next turn or the end of the attempt (a timeout on the agent): counted, never dropped
        and never a number."""
        probe_sent = {p.sent_seq for p in self.probes if p.sent_seq is not None}
        probe_replies = {p.received_seq for p in self.probes if p.received_seq is not None}
        out: list[tuple[float | None, str | None]] = []
        waiting = False
        for f in self.frames:
            if f.direction == "sent":
                if f.kind not in TURN_KINDS or f.seq in probe_sent:
                    continue
                if waiting:
                    out.append((None, NO_REPLY))
                waiting = True
            elif waiting and f.kind not in NOT_REPLIES and f.seq not in probe_replies:
                out.append((f.latency_ms, None if f.latency_ms is not None else NOT_TIMED))
                waiting = False
        if waiting:
            out.append((None, NO_REPLY))
        return out

    def language(self) -> tuple[str | None, str | None]:
        """The persona's language and dialect as the scenario declared them."""
        language = self.persona.get("language")
        dialect = self.persona.get("dialect")
        return (
            language if isinstance(language, str) else None,
            dialect if isinstance(dialect, str) and dialect else None,
        )

    def transcript(self) -> list[tuple[str, str]]:
        """(speaker, text) pairs; speaker is ``customer`` or ``agent``."""
        return [
            ("customer" if f.direction == "sent" else "agent", f.text)
            for f in self.frames
            if f.text
        ]
