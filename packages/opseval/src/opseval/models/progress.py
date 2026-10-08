# SPDX-License-Identifier: Apache-2.0
"""How far a run in flight has got, as the runner itself records it (progress.json).

Written by the runner at each step, never inferred from other files: only the runner knows
that a scenario was skipped, or that its attempts are recorded but still being scored.
It is not a result: verdicts exist only once ``result.json`` does.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ScenarioState = Literal["waiting", "running", "done", "skipped"]


class ScenarioProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    state: ScenarioState = "waiting"
    attempts: int = 0
    """Attempts recorded so far."""
    repeats: int | None = None
    """Attempts planned; known once the scenario has started."""


class RunProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: Literal["running", "finishing"] = "running"
    """``finishing``: every scenario is over; the gate, reports and sinks are being written."""
    current: str | None = None
    scenarios: list[ScenarioProgress]
