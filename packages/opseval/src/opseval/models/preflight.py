# SPDX-License-Identifier: Apache-2.0
"""Preflight report models (ADR-0020).

Kept here, away from the checking logic, so a :class:`~opseval.models.results.SuiteResult`
can carry the report without ``models`` depending on anything else in opseval.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CheckStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    """Reachable-but-broken or not reachable at all: scenarios needing it are skipped."""
    NOT_CONFIGURED = "not_configured"
    """Needed by the suite but not set up. Also a skip, with a different fix."""
    NOT_NEEDED = "not_needed"
    UNVERIFIED = "unverified"
    """Configured and reachable, but validity was not proven (see ``verify_llm``)."""


class HealthCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: CheckStatus
    detail: str
    needed_by: int = 0
    """How many scenarios in this suite depend on it."""

    @property
    def blocking(self) -> bool:
        return self.status in (CheckStatus.UNAVAILABLE, CheckStatus.NOT_CONFIGURED)


class PreflightReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks: list[HealthCheck] = []

    def get(self, name: str) -> HealthCheck | None:
        return next((c for c in self.checks if c.name == name), None)

    def blocking(self) -> list[HealthCheck]:
        return [c for c in self.checks if c.blocking]

    def reason(self, name: str) -> str | None:
        """Why ``name`` would stop a scenario running, if it would."""
        check = self.get(name)
        return f"{name} unavailable: {check.detail}" if check and check.blocking else None

    def summary(self) -> str:
        icon = {
            CheckStatus.OK: "OK          ",
            CheckStatus.UNAVAILABLE: "UNAVAILABLE ",
            CheckStatus.NOT_CONFIGURED: "NOT SET UP  ",
            CheckStatus.NOT_NEEDED: "not needed  ",
            CheckStatus.UNVERIFIED: "UNVERIFIED  ",
        }
        lines = ["Preflight", ""]
        for check in self.checks:
            lines.append(f"  {icon[check.status]} {check.name:<12} {check.detail}")
        blocking = self.blocking()
        lines.append("")
        lines.append(
            "Scenarios depending on "
            + ", ".join(c.name for c in blocking)
            + " will be skipped (skipped_precondition), not failed."
            if blocking
            else "Everything this suite needs is available."
        )
        return "\n".join(lines)


__all__ = ["CheckStatus", "HealthCheck", "PreflightReport"]
