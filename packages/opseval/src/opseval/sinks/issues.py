# SPDX-License-Identifier: Apache-2.0
"""What every issue-tracker sink shares (ADR-0040, ADR-0045): which scenarios are reported,
the bounded publish loop, and how a partial failure is reported.

A tracker sink only says how to find a scenario's open issue, how to open one and how to
comment on one. The rules live here once, so the GitHub and Jira sinks cannot drift apart.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from typing import Any

import httpx

from opseval.models.results import SuiteResult, TestVerdict, VerdictStatus
from opseval.models.scenario import Criticality
from opseval.sinks.base import Sink

SERIOUS = (Criticality.CRITICAL, Criticality.BLOCKER)


def failed_serious(result: SuiteResult) -> list[TestVerdict]:
    """Failed ``critical`` or ``blocker`` scenarios. An inconclusive, skipped or blocked one is
    not the agent failing, and an issue saying it was would break the core rule elsewhere."""
    return [
        t for t in result.tests if t.status is VerdictStatus.FAILED and t.criticality in SERIOUS
    ]


class StopWriting(RuntimeError):
    """Writing more would risk duplicates (an issue the next run cannot find): stop the sink."""


class IssueSink(Sink):
    """A sink that opens one issue per failed serious scenario and comments on repeats."""

    service = "tracker"

    def __init__(
        self,
        where: str,
        headers: dict[str, str],
        *,
        max_issues: int,
        timeout: float,
        client_factory: Callable[[], httpx.AsyncClient] | None,
    ) -> None:
        self._where = where
        self._headers = headers
        self._max = max_issues
        self._timeout = timeout
        self._client_factory = client_factory

    @property
    @abstractmethod
    def _base(self) -> str: ...

    async def _call(
        self, client: httpx.AsyncClient, method: str, path: str, **kw: Any
    ) -> httpx.Response:
        response = await client.request(
            method, f"{self._base}{path}", headers=self._headers, timeout=self._timeout, **kw
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"{self.service} answered HTTP {response.status_code} to {method} {path}"
            )
        return response

    @abstractmethod
    def _tag(self, result: SuiteResult, t: TestVerdict) -> str:
        """What finds this scenario's open issue again (a marker, a label)."""

    @abstractmethod
    async def _open_issues(self, client: httpx.AsyncClient) -> dict[str, str]:
        """Every open issue this sink wrote, by tag -> reference. Raises rather than guess:
        a missed issue would be opened again on every run."""

    @abstractmethod
    async def _comment(
        self, client: httpx.AsyncClient, ref: str, result: SuiteResult, t: TestVerdict
    ) -> None: ...

    @abstractmethod
    async def _create(
        self, client: httpx.AsyncClient, result: SuiteResult, t: TestVerdict, tag: str
    ) -> tuple[str, StopWriting | None]:
        """Open an issue. Returns its reference and, when it cannot be found again by its tag,
        the reason to stop (raised once the issue is counted as opened). Raises
        :class:`StopWriting` when an issue may have been created but cannot be referred to."""

    def _show(self, ref: str) -> str:
        return ref

    async def publish(self, result: SuiteResult) -> str:
        failing = failed_serious(result)
        if not failing:
            return "no failed critical or blocker scenario: nothing to report"
        client = self._client_factory() if self._client_factory else httpx.AsyncClient()
        opened: list[str] = []
        commented: list[str] = []
        errors: list[str] = []
        try:
            existing = await self._open_issues(client)
            for t in failing[: self._max]:
                try:
                    tag = self._tag(result, t)
                    ref = existing.get(tag)
                    if ref is not None:
                        await self._comment(client, ref, result, t)
                        commented.append(ref)
                        continue
                    ref, lost = await self._create(client, result, t, tag)
                    opened.append(ref)
                    existing[tag] = ref  # a second failure in this run comments, not duplicates
                    if lost is not None:
                        raise lost
                except StopWriting as exc:
                    errors.append(str(exc))
                    break  # every further issue would be a future duplicate too
                except Exception as exc:  # noqa: BLE001 - one issue must not stop the others
                    errors.append(f"{t.test_id}: {type(exc).__name__}: {exc}")
        finally:
            await client.aclose()
        parts = []
        if opened:
            parts.append(f"opened {', '.join(self._show(r) for r in opened)}")
        if commented:
            parts.append(f"commented on {', '.join(self._show(r) for r in commented)}")
        if len(failing) > self._max:
            parts.append(f"{len(failing) - self._max} more not reported (limit {self._max})")
        summary = f"{self._where}: " + "; ".join(parts or ["nothing written"])
        if errors:
            # A sink that was asked for and partly failed says so, with what it did manage.
            raise RuntimeError(f"{summary}; failed for {'; '.join(errors)}")
        return summary


__all__ = ["SERIOUS", "IssueSink", "StopWriting", "failed_serious"]
