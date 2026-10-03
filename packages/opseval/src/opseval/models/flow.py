# SPDX-License-Identifier: Apache-2.0
"""Flow map models: expected versus observed phase transitions (FR-RPT-05, ADR-0023).

Kept away from the computation so a report, and later the console, can carry a flow map
without ``models`` depending on anything else in opseval.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Step(StrEnum):
    """What one observed phase transition was, relative to the profile's order."""

    EXPECTED = "expected"
    """Straight to the next declared phase."""
    SKIPPED = "skipped"
    """Forward, but over one or more declared phases."""
    BACKWARD = "backward"
    """Back to a phase the conversation had already passed (which covers revisiting it:
    consecutive frames in one phase are a single visit, so a transition's two sides are
    never the same phase)."""
    UNKNOWN = "unknown"
    """One side is not a phase the profile declares, so order says nothing about it."""

    @property
    def unexpected(self) -> bool:
        return self is not Step.EXPECTED


class Transition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    frm: str | None = Field(default=None, alias="from")
    """``None`` for the first phase observed."""
    to: str
    kind: Step
    at_seq: int
    """Sequence number of the frame that first showed ``to``."""
    over: list[str] = Field(default_factory=list)
    """Declared phases jumped over, for a ``skipped`` step."""


class FlowMap(BaseModel):
    """One attempt's path through the profile's phases.

    ``measured`` is false when the profile declares no phases, or when the conversation
    produced no phase markers at all. An unmeasured flow map renders as "not measured" —
    never as an empty diagram, which would read as "the agent did nothing".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    measured: bool = True
    reason: str | None = None
    expected: list[str] = Field(default_factory=list)
    observed: list[str] = Field(default_factory=list)
    """Declared phases in the order they were first seen, plus any unknown ones."""
    transitions: list[Transition] = Field(default_factory=list)
    never_reached: list[str] = Field(default_factory=list)
    unknown_phases: list[str] = Field(default_factory=list)
    """Phases the agent reported that the profile does not declare."""
    ended_at: str | None = None
    """The last phase observed, where the conversation came to rest."""

    @property
    def unexpected(self) -> list[Transition]:
        return [t for t in self.transitions if t.kind.unexpected]

    @property
    def clean(self) -> bool:
        """Every transition expected, nothing skipped, nothing left unreached."""
        return self.measured and not self.unexpected and not self.never_reached


__all__ = ["FlowMap", "Step", "Transition"]
