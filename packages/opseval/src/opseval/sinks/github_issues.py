# SPDX-License-Identifier: Apache-2.0
"""GitHub Issues sink: an issue for each failed critical scenario (FR-INT-05, ADR-0040).

Rules, beyond those every sink follows (ADR-0027):

- **Only failed ``critical`` or ``blocker`` scenarios.** An inconclusive, skipped or blocked
  scenario is not the agent failing; an issue saying it was would be the core rule broken in
  someone else's tracker.
- **One issue per scenario, not per run.** A hidden marker (suite and scenario id, encoded so
  two different names never share one) finds the open issue a scenario already has; a repeat
  failure adds a comment to it. Finding it depends on the ``opseval`` label, so a token that
  cannot apply the label stops the sink rather than opening a duplicate on every run.
- **No conversation content, no URLs.** An issue tracker is another audience, often a wider
  one: a transcript can hold personal data and a target URL can hold a credential or an
  internal host. The issue names the scenario, the checks that failed, the pass rate, the run,
  the suite and the profile. Scenario-supplied text is shown as inert code, so it cannot
  mention a person, link, or reference another repository's issue.
- **Bounded.** At most ``max_issues`` issues are opened or commented on per run; the rest are
  counted in the outcome, so a broken agent cannot flood a tracker.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from opseval.models.results import SuiteResult, TestVerdict, VerdictStatus
from opseval.models.scenario import Criticality
from opseval.sinks.base import Sink

LABEL = "opseval"
SERIOUS = (Criticality.CRITICAL, Criticality.BLOCKER)
MAX_PAGES = 20
"""Open labelled issues read to find existing ones (100 per page). Beyond this the sink stops
rather than guess, because a missed issue would be opened again on every run."""
MARKER = re.compile(r"<!-- opseval:[A-Za-z0-9_.%-]+:[A-Za-z0-9_.%-]+ -->")


def marker(suite: str, test_id: str) -> str:
    """The hidden tag that finds a scenario's open issue. Percent-encoded, so it is one-to-one
    (two names never share a marker) and nothing in it can end the HTML comment."""
    safe = "-_."
    return f"<!-- opseval:{quote(suite, safe=safe)}:{quote(test_id, safe=safe)} -->"


def _code(text: str, limit: int = 300) -> str:
    """Scenario or result text for an issue body: one line, inside a code span, where GitHub
    renders no mention, link, image or issue reference."""
    flat = " ".join(text.split()).replace("`", "'")[:limit]
    return f"`{flat}`" if flat else "``"


def _title(text: str, limit: int) -> str:
    """Text for an issue title, where code spans do not render: mentions and issue
    references are broken with a zero-width space so they notify no one."""
    flat = " ".join(text.split())
    for ch in "@#":
        flat = flat.replace(ch, ch + "​")
    return flat[:limit]


def failed_serious(result: SuiteResult) -> list[TestVerdict]:
    return [
        t for t in result.tests if t.status is VerdictStatus.FAILED and t.criticality in SERIOUS
    ]


def _facts(result: SuiteResult, t: TestVerdict) -> str:
    rate = "not measured" if t.pass_rate is None else f"{t.pass_rate:.0%}"
    return "\n".join(
        [
            f"- **Run:** {_code(result.run_id, 120)} ({result.started_at:%Y-%m-%d %H:%M} UTC)",
            f"- **Suite:** {_code(result.suite, 120)} · **profile:** {_code(result.profile, 120)}",
            f"- **Criticality:** {t.criticality.value} · **pass rate:** {rate}"
            + (" · flaky" if t.flaky else ""),
            f"- **Why:** {_code(t.reason or 'failed', 500)}",
            f"- **Gate:** {result.gate.status} · exit code {result.exit_code}",
        ]
    )


class LabelNotApplied(RuntimeError):
    """GitHub created an issue but dropped the label that de-duplication depends on."""


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
        """Every open opseval issue, by marker -> issue number. Raises rather than guess."""
        found: dict[str, int] = {}
        for page in range(1, MAX_PAGES + 1):
            response = await self._call(
                client,
                "GET",
                f"/repos/{self._repo}/issues",
                params={"labels": LABEL, "state": "open", "per_page": 100, "page": page},
            )
            items = response.json()
            if not isinstance(items, list):
                raise RuntimeError("GitHub's issue list was not a list: cannot find open issues")
            for item in items:
                if not isinstance(item, dict) or "pull_request" in item:
                    continue
                match = MARKER.search(str(item.get("body") or ""))
                if match:
                    found.setdefault(match.group(0), int(item["number"]))
            if len(items) < 100:
                return found
        raise RuntimeError(
            f"more than {MAX_PAGES * 100} open '{LABEL}' issues: close some, so existing "
            "issues can be found instead of duplicated"
        )

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
                except LabelNotApplied as exc:
                    errors.append(str(exc))
                    break  # every further issue would be a future duplicate too
                except Exception as exc:  # noqa: BLE001 - one issue must not stop the others
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
                "title": f"[opseval] {_title(t.test_id, 80)} failed: {_title(t.name, 120)}",
                "labels": [LABEL],
                "body": (
                    f"{mark}\nOpseval scenario {_code(t.test_id, 120)} "
                    f"({_code(t.name, 200)}) failed.\n\n{facts}\n\n"
                    "The conversation is in the run's report; it is not copied here."
                ),
            },
        )
        created = response.json()
        number = int(created["number"])
        opened.append(number)
        existing[mark] = number  # a second failure of it in this run comments, not duplicates
        labels = {str(label.get("name")) for label in created.get("labels") or []}
        if LABEL not in labels:
            raise LabelNotApplied(
                f"issue #{number} was created without the '{LABEL}' label (the token cannot "
                "apply labels?): without it the next run cannot find it and would open a "
                "duplicate, so no further issues are written"
            )


__all__ = ["GitHubIssuesSink", "LabelNotApplied", "failed_serious", "marker"]
