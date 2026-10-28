# SPDX-License-Identifier: Apache-2.0
"""The analytics store protocol (ADR-0052): one question, one typed answer, every store.

``FileAnalytics`` answers from the run folders (local mode). ``PgAnalytics`` (server mode,
ADR-0066 phase D2) will answer the same questions from the database, and a golden test will
hold the two to identical answers, ``null``s included.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from opseval.analytics.models import Bucket, CostAnswer, TokensAnswer, UsageFilterEcho
from opseval.errors import ConfigError

SERVER_ONLY_FILTERS = ("org", "project", "task", "label")
"""Filters that need server mode: local run folders have no organisation, project, task or
catalogue label. Asked of a local store they are refused, never ignored: a dashboard that
silently dropped a filter would show every run as if it were the ones asked for."""


class UnsupportedFilter(ConfigError):
    """A filter this store cannot apply."""


@dataclass(frozen=True)
class UsageFilter:
    suite: str | None = None
    model: str | None = None
    role: str | None = None
    since: date | None = None
    until: date | None = None
    """Inclusive, on the day a run started (UTC)."""
    bucket: Bucket = "day"
    org: str | None = None
    project: str | None = None
    task: str | None = None
    label: str | None = None

    def echo(self) -> UsageFilterEcho:
        return UsageFilterEcho(
            suite=self.suite,
            model=self.model,
            role=self.role,
            since=self.since,
            until=self.until,
            bucket=self.bucket,
        )

    def server_only(self) -> list[str]:
        return [name for name in SERVER_ONLY_FILTERS if getattr(self, name) is not None]


class AnalyticsStore(Protocol):
    """Every aggregate the console's dashboards draw. A new question is added here and to
    every implementation at once (ADR-0052)."""

    def cost(self, filters: UsageFilter) -> CostAnswer: ...

    def tokens(self, filters: UsageFilter) -> TokensAnswer: ...
