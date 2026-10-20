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
from urllib.parse import quote

import httpx

from opseval.models.results import SuiteResult, TestVerdict
from opseval.sinks.issues import SERIOUS, IssueSink, StopWriting, failed_serious

LABEL = "opseval"
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


class LabelNotApplied(StopWriting):
    """GitHub created an issue but dropped the label that de-duplication depends on."""


class GitHubIssuesSink(IssueSink):
    name = "github-issues"
    service = "GitHub"

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
        super().__init__(
            repo,
            {
                "authorization": f"Bearer {token}",
                "accept": "application/vnd.github+json",
                "x-github-api-version": "2022-11-28",
            },
            max_issues=max_issues,
            timeout=timeout,
            client_factory=client_factory,
        )
        self._repo = repo
        self._api = api_url.rstrip("/")

    @property
    def _base(self) -> str:
        return self._api

    def _tag(self, result: SuiteResult, t: TestVerdict) -> str:
        return marker(result.suite, t.test_id)

    def _show(self, ref: str) -> str:
        return f"#{ref}"

    async def _open_issues(self, client: httpx.AsyncClient) -> dict[str, str]:
        found: dict[str, str] = {}
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
                    found.setdefault(match.group(0), str(int(item["number"])))
            if len(items) < 100:
                return found
        raise RuntimeError(
            f"more than {MAX_PAGES * 100} open '{LABEL}' issues: close some, so existing "
            "issues can be found instead of duplicated"
        )

    async def _comment(
        self, client: httpx.AsyncClient, ref: str, result: SuiteResult, t: TestVerdict
    ) -> None:
        await self._call(
            client,
            "POST",
            f"/repos/{self._repo}/issues/{ref}/comments",
            json={"body": f"Failed again.\n\n{_facts(result, t)}"},
        )

    async def _create(
        self, client: httpx.AsyncClient, result: SuiteResult, t: TestVerdict, tag: str
    ) -> tuple[str, StopWriting | None]:
        response = await self._call(
            client,
            "POST",
            f"/repos/{self._repo}/issues",
            json={
                "title": f"[opseval] {_title(t.test_id, 80)} failed: {_title(t.name, 120)}",
                "labels": [LABEL],
                "body": (
                    f"{tag}\nOpseval scenario {_code(t.test_id, 120)} "
                    f"({_code(t.name, 200)}) failed.\n\n{_facts(result, t)}\n\n"
                    "The conversation is in the run's report; it is not copied here."
                ),
            },
        )
        try:
            created = response.json()
            number = str(int(created["number"]))
        except Exception as exc:
            raise StopWriting(
                f"{t.test_id}: GitHub created an issue but its answer had no number "
                f"({type(exc).__name__}): no further issues are written"
            ) from exc
        labels = {str(label.get("name")) for label in created.get("labels") or []}
        if LABEL not in labels:
            return number, LabelNotApplied(
                f"issue #{number} was created without the '{LABEL}' label (the token cannot "
                "apply labels?): without it the next run cannot find it and would open a "
                "duplicate, so no further issues are written"
            )
        return number, None


__all__ = ["SERIOUS", "GitHubIssuesSink", "LabelNotApplied", "failed_serious", "marker"]
