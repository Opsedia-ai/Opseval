# SPDX-License-Identifier: Apache-2.0
"""When a run stops early (FR-CTL, ADR-0035).

A stop policy decides only whether the *next* scenario starts. It never changes a verdict:
every scenario it leaves out is recorded as ``skipped_precondition`` with the policy and
the reason, so nothing vanishes from the result and a stopped run is never "safe to
release". Policies are checked between scenarios, never inside one, so a scenario's
attempts always fold into a verdict from all of its repeats.

Set per suite in YAML (``stop:``); any ``opseval run`` flag replaces that one field.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StopPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fail_fast: bool = False
    """Stop after the first failed scenario the gate blocks on."""
    max_consecutive_failures: int | None = Field(default=None, ge=1)
    """Stop after this many failed scenarios in a row. Only a pass resets the count: a skip
    or an inconclusive scenario says nothing about whether the agent recovered."""
    stop_on_critical_failure: bool = False
    """Stop after a ``critical`` or ``blocker`` scenario fails."""
    stop_on_rate_limit: bool = False
    """Stop when a rate limit outlasted its retries (the agent's endpoint or an LLM) and
    invalidated an attempt: what follows would only be more of the same."""
    max_wall_clock_seconds: float | None = Field(default=None, gt=0)
    """Start no scenario once the run has taken this long."""

    def merged(self, override: StopPolicy | None) -> StopPolicy:
        """This policy with every field ``override`` sets explicitly replacing it."""
        if override is None:
            return self
        return self.model_copy(update=override.model_dump(exclude_unset=True))

    @property
    def active(self) -> bool:
        return self != StopPolicy()


__all__ = ["StopPolicy"]
