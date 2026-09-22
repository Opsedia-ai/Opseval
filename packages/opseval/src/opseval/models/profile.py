# SPDX-License-Identifier: Apache-2.0
"""SUT profile: everything platform-specific about the system under test.

Adopting Opseval for a new agent means writing a profile, not forking the tool.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

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
    commits: bool = False


class CheckpointSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str | None = None


class SutProfile(BaseModel):
    """Maps an agent's wire protocol onto Opseval's canonical model."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""

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
