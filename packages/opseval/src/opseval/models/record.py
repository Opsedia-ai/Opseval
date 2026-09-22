# SPDX-License-Identifier: Apache-2.0
"""The immutable evidence of one attempt. Scorers read only this."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from opseval.models.profile import FrameKind


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
    infra_errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    planted_injections: tuple[str, ...] = ()

    def received(self) -> list[FrameRecord]:
        return [f for f in self.frames if f.direction == "received"]

    def transcript(self) -> list[tuple[str, str]]:
        """(speaker, text) pairs; speaker is ``customer`` or ``agent``."""
        return [
            ("customer" if f.direction == "sent" else "agent", f.text)
            for f in self.frames
            if f.text
        ]
