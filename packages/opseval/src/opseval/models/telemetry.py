# SPDX-License-Identifier: Apache-2.0
"""The telemetry payload, as a type (ADR-0010).

The payload is the contract, so it is a model with ``extra="forbid"`` rather than a loose
dict: a field cannot be added without the schema version and the documentation moving
with it, which a snapshot test enforces.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TELEMETRY_SCHEMA = "opseval.telemetry/v1"

BUCKETS: tuple[tuple[int, str], ...] = (
    (0, "0"),
    (1, "1"),
    (5, "2-5"),
    (20, "6-20"),
    (100, "21-100"),
)
"""Upper bound → label. Anything larger is ``100+``."""


def bucket(count: int) -> str:
    """A count as a coarse bucket, so an exact number never leaves the machine."""
    for ceiling, label in BUCKETS:
        if count <= ceiling:
            return label
    return "100+"


class TelemetryEvent(StrEnum):
    RUN = "run_completed"
    DEMO = "demo_completed"
    RESCORE = "rescore_completed"


class TelemetryPayload(BaseModel):
    """Everything that is ever sent. Nothing outside these fields leaves the machine."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_: str = Field(default=TELEMETRY_SCHEMA, alias="schema")
    install_id: str
    event: TelemetryEvent
    opseval_version: str
    python: str
    os: str
    arch: str
    mode: Literal["local", "server"] = "local"
    ci: bool = False

    scenarios: str
    attempts: str
    judged_attempts: str

    exit_code: int
    gate: str

    transports: list[str] = Field(default_factory=list)
    scorers: list[str] = Field(default_factory=list)
    trace_source: str = "none"
    languages: list[str] = Field(default_factory=list)
    demo_target: bool = False


__all__ = [
    "BUCKETS",
    "TELEMETRY_SCHEMA",
    "TelemetryEvent",
    "TelemetryPayload",
    "bucket",
]
