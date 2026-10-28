# SPDX-License-Identifier: Apache-2.0
"""SUT profile: everything platform-specific about the system under test.

Adopting Opseval for a new agent means writing a profile, not forking the tool.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrameKind(StrEnum):
    USER_MESSAGE = "user_message"  # sent: customer turn
    RESUME = "resume"  # sent: answer to a checkpoint
    MESSAGE = "message"  # received: agent reply or phase preview
    NEEDS_HUMAN = "needs_human"  # received: checkpoint raised by the agent
    RESULT = "result"  # received: result frame (may be the committed outcome)
    ERROR = "error"  # received: error frame
    NOTIFICATION = "notification"  # received: out-of-band, logged only
    HANDSHAKE = "handshake"  # transport chatter
    UNKNOWN = "unknown"  # unclassifiable, recorded verbatim


class ArtifactSpec(BaseModel):
    """Something the agent creates in the system under test, and where its id appears
    (FR-REC-01). Recorded per attempt, so a test tenant's leftovers can be found and cleaned."""

    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=60)
    """What it is, e.g. ``refund`` or ``ticket``."""
    path: str = Field(min_length=1, max_length=200)
    """Dotted path to its id inside a frame's payload, e.g. ``data.refund_status.refund_id``."""
    frame_kinds: list[FrameKind] = Field(default_factory=lambda: [FrameKind.RESULT], min_length=1)
    """Which received frames to look in."""

    @model_validator(mode="after")
    def _received_kinds_only(self) -> ArtifactSpec:
        sent = {FrameKind.USER_MESSAGE, FrameKind.RESUME}
        mine = sorted(k.value for k in self.frame_kinds if k in sent)
        if mine:
            # Those are frames Opseval sends: an artifact can never be read from them.
            raise ValueError(f"frame_kinds {mine} are frames Opseval sends, not the agent")
        return self


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_key: str | None = None
    """A key in a frame's data that proves the tool ran (degraded, frames-only mode)."""
    trace_name: str | None = None
    """Observation name in traces, if it differs from the tool name."""
    commits: bool = False
    args_schema: dict[str, Any] | None = None
    """JSON Schema for this tool's arguments, checked by ``tools.args_valid``.
    Arguments are only visible in traces, so the scorer needs full trace coverage."""


class CheckpointSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str | None = None


class TraceConfig(BaseModel):
    """Where to read the agent's own traces (ADR-0011)."""

    model_config = ConfigDict(extra="forbid")
    source: Literal["langfuse", "otlp-file"] = "langfuse"
    """``otlp-file`` reads the OTLP-JSON the OpenTelemetry Collector's file exporter
    writes (ADR-0028); its path comes from ``OPSEVAL_OTLP_SPANS_PATH``."""
    session_id: str = "{session_id}"
    """Template for the trace session id. Values: session_id, run_id, test_id, attempt_index."""
    clock_skew_ms: int = Field(default=2000, ge=0, le=600_000)
    """Trace timestamps within this window of a decision are treated as ambiguous."""
    wait_seconds: float = Field(default=15.0, ge=0, le=600)
    """How long to wait for traces to be ingested after the conversation ends."""
    poll_interval_seconds: float = Field(default=2.0, gt=0, le=60)


class ReconnectConfig(BaseModel):
    """What to do when the connection drops mid-conversation (websocket transport)."""

    model_config = ConfigDict(extra="forbid")
    resume_session_frame: dict[str, Any] | None = None
    """Sent first on a new connection to resume the session (``{session_id}``, ``{token}``).
    Without it a drop cannot be resumed and stays a transport failure (not judged)."""
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_seconds: float = Field(default=0.5, ge=0, le=30)
    confirm_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    """How long to wait for a handshake frame carrying the resumed session id."""
    resend_unanswered: bool = False
    """Resend the last frame once after resuming if the agent had not answered it yet.
    Only safe if the agent handles duplicate messages."""


class TokenConfig(BaseModel):
    """Short-lived HS256 bearer tokens signed with ``OPSEVAL_TOKEN_SIGNING_KEY``."""

    model_config = ConfigDict(extra="forbid")
    ttl_seconds: int = Field(default=300, ge=10, le=86_400)
    refresh_before_seconds: int = Field(default=30, ge=1, le=3_600)
    claims: dict[str, Any] = Field(default_factory=dict)
    """Extra JWT claims (e.g. ``aud``, ``tenant``); ``iat``, ``exp`` and ``jti`` are set."""
    refresh_frame: dict[str, Any] | None = None
    """Sent on an open connection before the token expires (``{token}``). Without it a
    new token is only used when the connection is next opened."""


class ProbeExpect(BaseModel):
    """What a correct answer to a probe looks like.

    Every field given must hold. An empty expectation means "any answer at all", which
    is a real thing to assert about a protocol: that the agent replies rather than
    ignoring the frame.
    """

    model_config = ConfigDict(extra="forbid")
    kind: FrameKind | None = None
    """The canonical kind the reply must classify as, via the profile's own ``kinds``."""
    fields: dict[str, Any] = Field(default_factory=dict)
    """Keys the reply must carry, with these exact values. A subset match, so an agent
    may send more than this."""
    has_fields: list[str] = Field(default_factory=list)
    """Keys the reply must carry, with any value."""


class Probe(BaseModel):
    """One raw frame sent before the conversation, with the answer it must get (FR-RUN-07)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    send: dict[str, Any]
    """Sent verbatim, with ``"{session_id}"`` and ``"{token}"`` substituted."""
    expect: ProbeExpect = Field(default_factory=lambda: ProbeExpect())
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)


AUDIO_RATES_ALLOWED: tuple[int, ...] = (8_000, 16_000, 22_050, 24_000, 44_100, 48_000)
"""Mirrors ``opseval.audio.RATES`` (models import nothing else in opseval; a test keeps them
equal)."""


class AudioEnvelope(BaseModel):
    """How an ``audio-ws`` agent frames its stream (ADR-0053). Every event is a JSON object
    whose ``type_field`` names it; audio may also arrive as binary WebSocket frames."""

    model_config = ConfigDict(extra="forbid")
    type_field: str = "type"
    audio_event: str = "audio"
    """The event carrying a chunk of audio, base64 in ``audio_field``."""
    audio_field: str = "audio"
    binary: bool = False
    """Send the caller's audio as binary WebSocket frames instead of base64 JSON. Binary
    frames from the agent are always read as audio."""
    start_frame: dict[str, Any] | None = None
    """Sent once when the stream opens (``{codec}`` and ``{rate}`` are substituted)."""
    caller_turn_end_event: str | None = "end_of_turn"
    """Sent when the caller has finished speaking; ``null`` sends nothing (the agent detects
    the end of the caller's turn itself)."""
    speech_start_event: str | None = "speech_start"
    speech_end_event: str | None = "speech_end"
    """The agent's own end of turn. Without it the bridge detects the end from silence."""
    mark_event: str | None = "mark"
    mark_name_field: str = "name"
    transcript_event: str | None = "transcript"
    """The agent's own transcript of what it said, when it sends one (kept as evidence; the
    text scorers read Opseval's own STT)."""
    transcript_text_field: str = "text"
    dtmf_event: str | None = "dtmf"
    dtmf_digits_field: str = "digits"
    transfer_event: str | None = "transfer"
    transfer_to_field: str = "to"
    hangup_event: str | None = "hangup"


class AudioHttp(BaseModel):
    """How an ``audio-http`` agent takes and returns a turn (ADR-0053)."""

    model_config = ConfigDict(extra="forbid")
    request_format: Literal["wav", "raw"] = "wav"
    """The caller's turn as a WAV file, or raw bytes in the profile's codec and rate."""
    session_header: str = "X-Opseval-Session"
    """A header carrying one id per attempt, so the agent can keep the conversation."""


class AudioConfig(BaseModel):
    """A speaking agent (ADR-0053): what goes over the wire and when a turn ends."""

    model_config = ConfigDict(extra="forbid")
    codec: Literal["pcm16", "mulaw", "alaw"] = "pcm16"
    rate: int = 16_000
    end_of_turn_silence_ms: int = Field(default=800, ge=50, le=30_000)
    """Without the agent's own end-of-speech event, this much silence after it spoke ends its
    turn."""
    max_turn_seconds: float = Field(default=60.0, gt=0, le=600)
    """An agent that speaks longer than this in one turn never ended it: its fault
    (a ``TransportTimeout``)."""
    silence_dbfs: float = Field(default=-50.0, le=0)
    """Below this level a chunk is silence. Silence is not an empty transcript (ADR-0053)."""
    chunk_ms: int = Field(default=20, ge=5, le=1000)
    """How much caller audio goes in one chunk."""
    language: str | None = None
    """The language the STT hears; the persona's language when unset."""
    envelope: AudioEnvelope = Field(default_factory=AudioEnvelope)
    http: AudioHttp = Field(default_factory=AudioHttp)

    @model_validator(mode="after")
    def _rate(self) -> AudioConfig:
        if self.rate not in AUDIO_RATES_ALLOWED:
            raise ValueError(f"audio.rate {self.rate} is not one of {AUDIO_RATES_ALLOWED}")
        return self


def placeholder_path(template: Any, name: str, at: str = "") -> str | None:
    """The dotted path of the exact ``"{name}"`` value in a frame template, or ``None``.

    The voice bridge reads the customer's words back out of a rendered frame at that path
    (ADR-0053), so only a whole-value placeholder counts: ``"Say: {text}"`` would put words
    the customer never said into the agent's ear."""
    if template == "{" + name + "}":
        return at
    items: list[tuple[str, Any]] = []
    if isinstance(template, dict):
        items = [(str(k), v) for k, v in template.items()]
    elif isinstance(template, list):
        items = [(str(i), v) for i, v in enumerate(template)]
    for key, value in items:
        found = placeholder_path(value, name, f"{at}.{key}" if at else key)
        if found is not None:
            return found
    return None


class SutProfile(BaseModel):
    """Maps an agent's wire protocol onto Opseval's canonical model."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    sut_model: str | None = None
    """The model the agent runs on, when you know it (e.g. ``gpt-4o``). Opseval cannot see
    inside a black-box agent, so declaring it here is the only thing that lets the
    judge-independence check work (ADR-0016). Unset means *unknown*, not *independent*."""

    # --- inbound frame classification -------------------------------------------------
    kind_field: str = "type"
    kinds: dict[str, FrameKind] = Field(default_factory=dict)
    session_id_field: str | None = "session_id"
    text_field: str = "text"
    phase_field: str | None = "phase"
    checkpoint_field: str = "checkpoint"
    payload_field: str = "payload"
    outcome_field: str = "outcome"
    data_field: str | None = "data"
    turn_end_field: str | None = None
    """If set, a MESSAGE frame whose field is truthy ends the agent's turn."""
    turn_quiet_ms: int = Field(default=400, ge=0, le=60_000)
    """Otherwise the agent's turn ends after this much silence."""
    tool_calls_field: str | None = None
    """Key inside ``data_field`` holding a list of ``{name, arguments}`` tool calls."""
    tool_calls_complete: bool = False
    """Frames list *every* tool call the agent makes (a raw model endpoint, whose client runs
    the tools). Without it, frames can prove that a tool ran but never that it did not: an
    agent may run tools on its own server and report none of them, so a ``must_call`` tool
    missing from the frames is *not measured* rather than failed (ADR-0043)."""

    # --- outbound templates ("{name}" string values are substituted) ------------------
    auth_frame: dict[str, Any] | None = None
    user_message_frame: dict[str, Any] = Field(
        default_factory=lambda: {"type": "user_message", "text": "{text}"}
    )
    resume_frame: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "resume",
            "checkpoint": "{checkpoint}",
            "action": "{action}",
            "feedback": "{feedback}",
        }
    )
    handshake_required: bool = False
    reject_via_message_allowed: bool = True
    transport_options: dict[str, Any] = Field(default_factory=dict)
    """Transport-specific settings (e.g. ``model`` and ``system_prompt`` for openai-chat)."""
    traces: TraceConfig | None = None
    reconnect: ReconnectConfig | None = None
    token: TokenConfig | None = None
    """Mint HS256 tokens per attempt instead of using ``OPSEVAL_STATIC_TOKEN``."""
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    """What the agent creates and where to read its id, so each record lists it (FR-REC-01)."""
    probes: list[Probe] = Field(default_factory=list)
    """Raw protocol frames checked once per attempt, after connecting and before the
    conversation starts (FR-RUN-04's PROBES state). Graded by ``protocol.probes_passed``;
    they never abort the conversation, so a protocol fault and its consequences are both
    visible in one record."""
    audio: AudioConfig | None = None
    """A speaking agent, reached over ``audio-http`` or ``audio-ws`` through the voice bridge
    (ADR-0053). Its turns are spoken; the transcript becomes each reply's ``text``."""

    # --- conversation model -----------------------------------------------------------
    phases: list[str] = Field(default_factory=list)
    checkpoints: dict[str, CheckpointSpec] = Field(default_factory=dict)
    commit_gate: str | None = None
    """The checkpoint whose approval leads to the irreversible committed outcome."""
    committed_outcome: str | None = None
    tools: dict[str, ToolSpec] = Field(default_factory=dict)
    timezones: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> SutProfile:
        if self.commit_gate is not None and self.commit_gate not in self.checkpoints:
            raise ValueError(f"commit_gate {self.commit_gate!r} is not a declared checkpoint")
        committing = sorted(name for name, tool in self.tools.items() if tool.commits)
        if committing and self.commit_gate is None:
            # A committing tool with no gate in front of it: nothing for the commit guard
            # (ADR-0008) to stop at, so a run could perform the irreversible step unasked.
            raise ValueError(
                f"tools {committing} commit an irreversible outcome but the profile declares no "
                "commit_gate: name the checkpoint whose approval leads to it, so Opseval stops "
                "there unless the scenario has allow_commit: true and the run --allow-commit"
            )
        if self.committed_outcome is not None and self.commit_gate is None:
            # The commit guard (ADR-0008) stops at the gate *before* the outcome. Without a
            # gate there is nothing to stop at, and a run would reach the irreversible
            # outcome with no double opt-in at all.
            raise ValueError(
                f"committed_outcome {self.committed_outcome!r} needs a commit_gate: the "
                "checkpoint whose approval leads to it, where Opseval stops unless the scenario "
                "has allow_commit: true and the run has --allow-commit"
            )
        if (
            self.reconnect is not None
            and self.reconnect.resume_session_frame is not None
            and not self.session_id_field
        ):
            raise ValueError(
                "reconnect.resume_session_frame needs session_id_field: a session can only be "
                "resumed if the agent tells Opseval its id"
            )
        if self.audio is not None:
            self._check_voice()
        for name, spec in self.checkpoints.items():
            if spec.phase is not None and self.phases and spec.phase not in self.phases:
                raise ValueError(f"checkpoint {name!r} refers to unknown phase {spec.phase!r}")
        return self

    def _check_voice(self) -> None:
        """A voice profile's frames must say where the customer's words are, because the
        bridge speaks them (ADR-0053). Found at load time, so ``opseval validate`` fails
        instead of every attempt ending in a configuration error mid-run."""
        if self.probes:
            # A probe is a raw JSON frame; a speaking agent is reached through audio.
            raise ValueError("probes are JSON frames: a voice profile (audio:) cannot send them")
        if placeholder_path(self.user_message_frame, "text") is None:
            raise ValueError(
                'a voice profile (audio:) needs a value that is exactly "{text}" in '
                "user_message_frame: the voice bridge speaks the customer's words from there"
            )
        if (
            placeholder_path(self.resume_frame, "action") is None
            and placeholder_path(self.resume_frame, "feedback") is None
        ):
            raise ValueError(
                'a voice profile (audio:) needs a value that is exactly "{action}" or '
                '"{feedback}" in resume_frame: the voice bridge speaks a checkpoint decision '
                "from there"
            )

    def classify(self, payload: object) -> FrameKind:
        """Classify a decoded inbound frame. Never raises."""
        if not isinstance(payload, dict):
            return FrameKind.UNKNOWN
        synthetic = payload.get("_opseval_kind")
        if isinstance(synthetic, str) and synthetic in FrameKind.__members__.values():
            return FrameKind(synthetic)
        raw = payload.get(self.kind_field)
        if not isinstance(raw, str):
            return FrameKind.UNKNOWN
        return self.kinds.get(raw, FrameKind.UNKNOWN)

    def answers(self, kind: FrameKind, text: str | None, payload: object) -> bool:
        """Whether a received frame answers a customer turn (ADR-0043).

        Words, a checkpoint, a result, an error, or tool calls the agent asked the client to
        run. A handshake or a notification does not, even with text ("typing…"), and neither
        does a frame with no words. The driver keeps waiting for an answer (I12) and
        ``conversation.agent_replied`` grades on the same rule, so they cannot disagree.
        """
        if kind in (FrameKind.NEEDS_HUMAN, FrameKind.RESULT, FrameKind.ERROR):
            return True
        if kind in (FrameKind.HANDSHAKE, FrameKind.NOTIFICATION):
            return False
        if text is not None and text.strip():
            return True
        if self.tool_calls_field and self.data_field and isinstance(payload, dict):
            data = payload.get(self.data_field)
            calls = data.get(self.tool_calls_field) if isinstance(data, dict) else None
            return isinstance(calls, list) and bool(calls)
        return False

    def evidence_keys(self) -> dict[str, str]:
        """Map evidence key -> tool name, for tools that declare one."""
        return {spec.evidence_key: name for name, spec in self.tools.items() if spec.evidence_key}

    def trace_names(self) -> dict[str, str]:
        """Map trace observation name -> tool name."""
        return {(spec.trace_name or name): name for name, spec in self.tools.items()}

    def declared_model(self) -> str | None:
        """The agent's model, only if it was declared explicitly.

        ``transport_options["model"]`` is deliberately *not* used as a fallback: many
        agent gateways ignore the field (the scaffold ships ``model: default``), so
        reading it would infer a model the agent never ran.
        """
        return self.sut_model or None

    def frame_verifiable(self, tool: str) -> bool:
        """Can frames alone show whether ``tool`` ran?"""
        spec = self.tools.get(tool)
        return spec is not None and (spec.evidence_key is not None or bool(self.tool_calls_field))


def render_template(template: object, values: dict[str, Any]) -> object:
    """Substitute ``"{name}"`` string values in a nested template.

    A value that is exactly ``"{name}"`` is replaced by ``values[name]`` (keeping its
    type, including ``None``). Keys whose value renders to ``None`` are dropped.
    """
    if isinstance(template, dict):
        out: dict[str, object] = {}
        for key, sub in template.items():
            rendered = render_template(sub, values)
            if rendered is not None:
                out[key] = rendered
        return out
    if isinstance(template, list):
        return [render_template(item, values) for item in template]
    if isinstance(template, str) and template.startswith("{") and template.endswith("}"):
        name = template[1:-1]
        if name in values:
            return values[name]
    return template


def render_frame(template: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Render an outbound frame template (a mapping always renders to a mapping)."""
    return cast(dict[str, Any], render_template(template, values))
