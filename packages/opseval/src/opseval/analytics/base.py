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

from opseval.analytics.models import (
    Bucket,
    CostAnswer,
    LatencyAnswer,
    QualityAnswer,
    TokensAnswer,
    UsageCountsAnswer,
    UsageFilterEcho,
    VoiceAnswer,
)
from opseval.errors import ConfigError

SERVER_ONLY_FILTERS = ("org", "project", "task", "label")
"""Filters that need server mode: local run folders have no organisation, project, task or
catalogue label. Asked of a local store they are refused, never ignored: a dashboard that
silently dropped a filter would show every run as if it were the ones asked for."""


NOT_RECORDED_FILTERS = ("label",)
"""Filters no store can apply yet: a run does not record which catalogue label it ran
(ADR-0050). Refused in every mode, never ignored."""


class UnsupportedFilter(ConfigError):
    """A filter this store cannot apply."""

    code = "FILTER_NEEDS_SERVER_MODE"


class FilterNotApplicable(UnsupportedFilter):
    """A filter that means nothing for this question (a model on the Quality dashboard)."""

    code = "FILTER_NOT_APPLICABLE"


class FilterNotRecorded(UnsupportedFilter):
    """A filter on something runs do not record yet."""

    code = "FILTER_NOT_RECORDED"


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
    language: str | None = None
    """The persona's language as declared (``ar``), on an attempt (D2 dashboards)."""
    dialect: str | None = None

    def echo(self) -> UsageFilterEcho:
        return UsageFilterEcho(
            suite=self.suite,
            model=self.model,
            role=self.role,
            since=self.since,
            until=self.until,
            bucket=self.bucket,
            task=self.task,
            language=self.language,
            dialect=self.dialect,
        )

    def server_only(self) -> list[str]:
        return [name for name in SERVER_ONLY_FILTERS if getattr(self, name) is not None]

    def refuse_not_recorded(self) -> None:
        """Refuse filters no store can apply, whatever the mode: checked first, so local and
        server mode give the same reason (``FILTER_NOT_RECORDED``) for a label."""
        recorded = [n for n in NOT_RECORDED_FILTERS if getattr(self, n) is not None]
        if recorded:
            raise FilterNotRecorded(
                f"filter by {', '.join(recorded)}: runs do not record the catalogue label they "
                "ran yet, so no run can be picked by it"
            )

    def refuse(self, question: str, *names: str) -> None:
        """Refuse filters set that do not apply to ``question``, and those no store records:
        a filter is applied or refused, never ignored (ADR-0052)."""
        self.refuse_not_recorded()
        given = [n for n in names if getattr(self, n) is not None]
        if given:
            raise FilterNotApplicable(
                f"filter by {', '.join(given)} does not apply to the {question} dashboard"
            )


class AnalyticsStore(Protocol):
    """Every aggregate the console's dashboards draw. A new question is added here and to
    every implementation at once (ADR-0052)."""

    def cost(self, filters: UsageFilter) -> CostAnswer: ...

    def tokens(self, filters: UsageFilter) -> TokensAnswer: ...

    def latency(self, filters: UsageFilter) -> LatencyAnswer: ...

    def quality(self, filters: UsageFilter, *, limit: int = 20) -> QualityAnswer: ...

    def voice(self, filters: UsageFilter) -> VoiceAnswer: ...

    def usage(self, filters: UsageFilter) -> UsageCountsAnswer: ...
