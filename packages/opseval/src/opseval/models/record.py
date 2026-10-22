# SPDX-License-Identifier: Apache-2.0
"""The immutable evidence of one attempt. Scorers read only this."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from opseval.models.profile import FrameKind

SCRIPT_EXHAUSTED = "script_exhausted"
"""Noted when a scripted customer ran out of turns (ADR-0043). The attempt ends
``goal_reached``, but that shows only that the conversation ran to its end, not that the goal
was met; scorers read the note, which lives here so they need not import the driver."""


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


class FrameRecord(Frozen):
    seq: int
    at: datetime
    direction: Literal["sent", "received"]
    kind: FrameKind
    payload: Any
    latency_ms: float | None = None
    phase: str | None = None
    text: str | None = None


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

    def received(self) -> list[FrameRecord]:
        return [f for f in self.frames if f.direction == "received"]

    def transcript(self) -> list[tuple[str, str]]:
        """(speaker, text) pairs; speaker is ``customer`` or ``agent``."""
        return [
            ("customer" if f.direction == "sent" else "agent", f.text)
            for f in self.frames
            if f.text
        ]
