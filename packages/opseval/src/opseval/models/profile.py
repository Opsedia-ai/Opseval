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
    probes: list[Probe] = Field(default_factory=list)
    """Raw protocol frames checked once per attempt, after connecting and before the
    conversation starts (FR-RUN-04's PROBES state). Graded by ``protocol.probes_passed``;
    they never abort the conversation, so a protocol fault and its consequences are both
    visible in one record."""

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
        for name, spec in self.checkpoints.items():
            if spec.phase is not None and self.phases and spec.phase not in self.phases:
                raise ValueError(f"checkpoint {name!r} refers to unknown phase {spec.phase!r}")
        return self

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
