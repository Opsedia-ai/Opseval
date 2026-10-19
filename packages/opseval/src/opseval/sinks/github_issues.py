# SPDX-License-Identifier: Apache-2.0
"""GitHub Issues sink: an issue for each failed critical scenario (FR-INT-05, ADR-0040).

Rules, beyond those every sink follows (ADR-0027):

- **Only failed ``critical`` or ``blocker`` scenarios.** An inconclusive, skipped or blocked
  scenario is not the agent failing; an issue saying it was would be the core rule broken in
  someone else's tracker.
- **One issue per scenario, not per run.** A hidden marker (suite and scenario id) finds the
  open issue a scenario already has; a repeat failure adds a comment to it.
- **No conversation content.** An issue tracker is another audience, often a wider one, and a
  transcript can hold personal data. The issue says which scenario failed, which checks, the
  pass rate, the run and the target; the conversation stays in the run's report.
- **Bounded.** At most ``max_issues`` issues are opened or commented on per run; the rest are
  counted in the outcome, so a broken agent cannot flood a tracker.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx

from opseval.models.results import SuiteResult, TestVerdict, VerdictStatus
from opseval.models.scenario import Criticality
from opseval.sinks.base import Sink

LABEL = "opseval"
SERIOUS = (Criticality.CRITICAL, Criticality.BLOCKER)


def marker(suite: str, test_id: str) -> str:
    """The hidden tag that finds a scenario's open issue. Only safe characters, so no name
    can end the HTML comment it sits in."""
    safe = re.compile(r"[^A-Za-z0-9_.-]")
    return f"<!-- opseval:{safe.sub('_', suite)}:{safe.sub('_', test_id)} -->"


def _md(text: str, limit: int = 300) -> str:
    """Text from a scenario or a result, made inert in an issue: one line, no HTML or code
    fences, and no ``@`` that GitHub would turn into a notification to a real person."""
    flat = " ".join(text.split())
    for ch in "`<>":
        flat = flat.replace(ch, "")
    return flat.replace("@", "@\u200b")[:limit]


def failed_serious(result: SuiteResult) -> list[TestVerdict]:
    return [
        t for t in result.tests if t.status is VerdictStatus.FAILED and t.criticality in SERIOUS
    ]


def _facts(result: SuiteResult, t: TestVerdict) -> str:
    rate = "not measured" if t.pass_rate is None else f"{t.pass_rate:.0%}"
    lines = [
        f"- **Run:** `{_md(result.run_id, 120)}` ({result.started_at:%Y-%m-%d %H:%M} UTC)",
        f"- **Suite:** `{_md(result.suite, 120)}` · **target:** `{_md(result.target, 200)}`",
        f"- **Criticality:** {t.criticality.value} · **pass rate:** {rate}"
        + (" · flaky" if t.flaky else ""),
        f"- **Why:** {_md(t.reason or 'failed', 500)}",
        f"- **Gate:** {result.gate.status} · exit code {result.exit_code}",
    ]
    return "\n".join(lines)


class GitHubIssuesSink(Sink):
    name = "github-issues"

    def __init__(
        self,
        repo: str,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        max_issues: int = 10,
        timeout: float = 15.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._repo = repo
        self._api = api_url.rstrip("/")
        self._headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
        }
        self._max = max_issues
        self._timeout = timeout
        self._client_factory = client_factory

    async def _call(
        self, client: httpx.AsyncClient, method: str, path: str, **kw: Any
    ) -> httpx.Response:
        response = await client.request(
            method, f"{self._api}{path}", headers=self._headers, timeout=self._timeout, **kw
        )
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub answered HTTP {response.status_code} to {method} {path}")
        return response

    async def _open_issues(self, client: httpx.AsyncClient) -> dict[str, int]:
        """Open opseval issues by marker -> issue number (first 300, newest first)."""
        found: dict[str, int] = {}
        for page in (1, 2, 3):
            response = await self._call(
                client,
                "GET",
                f"/repos/{self._repo}/issues",
                params={"labels": LABEL, "state": "open", "per_page": 100, "page": page},
            )
            items = response.json()
            if not isinstance(items, list):
                break
            for item in items:
                body = item.get("body") or ""
                start = body.find("<!-- opseval:")
                if start >= 0 and "pull_request" not in item:
                    end = body.find("-->", start)
                    found.setdefault(body[start : end + 3], int(item["number"]))
            if len(items) < 100:
                break
        return found

    async def publish(self, result: SuiteResult) -> str:
        failing = failed_serious(result)
        if not failing:
            return "no failed critical or blocker scenario: nothing to report"
        client = self._client_factory() if self._client_factory else httpx.AsyncClient()
        opened: list[int] = []
        commented: list[int] = []
        errors: list[str] = []
        try:
            existing = await self._open_issues(client)
            for t in failing[: self._max]:
                try:
                    await self._report(client, result, t, existing, opened, commented)
                except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
                    # One issue that could not be written must not stop the others.
                    errors.append(f"{t.test_id}: {type(exc).__name__}: {exc}")
        finally:
            await client.aclose()
        parts = []
        if opened:
            parts.append(f"opened {', '.join(f'#{n}' for n in opened)}")
        if commented:
            parts.append(f"commented on {', '.join(f'#{n}' for n in commented)}")
        if len(failing) > self._max:
            parts.append(f"{len(failing) - self._max} more not reported (limit {self._max})")
        summary = f"{self._repo}: " + "; ".join(parts or ["nothing written"])
        if errors:
            # A sink that was asked for and partly failed says so, with what it did manage.
            raise RuntimeError(f"{summary}; failed for {'; '.join(errors)}")
        return summary

    async def _report(
        self,
        client: httpx.AsyncClient,
        result: SuiteResult,
        t: TestVerdict,
        existing: dict[str, int],
        opened: list[int],
        commented: list[int],
    ) -> None:
        mark = marker(result.suite, t.test_id)
        facts = _facts(result, t)
        number = existing.get(mark)
        if number is not None:
            await self._call(
                client,
                "POST",
                f"/repos/{self._repo}/issues/{number}/comments",
                json={"body": f"Failed again.\n\n{facts}"},
            )
            commented.append(number)
            return
        response = await self._call(
            client,
            "POST",
            f"/repos/{self._repo}/issues",
            json={
                "title": f"[opseval] {_md(t.test_id, 80)} failed: {_md(t.name, 120)}",
                "labels": [LABEL],
                "body": (
                    f"{mark}\nOpseval scenario `{_md(t.test_id, 120)}` "
                    f"({_md(t.name, 200)}) failed.\n\n{facts}\n\n"
                    "The conversation is in the run's report; it is not copied here."
                ),
            },
        )
        opened.append(int(response.json()["number"]))


__all__ = ["GitHubIssuesSink", "failed_serious", "marker"]
