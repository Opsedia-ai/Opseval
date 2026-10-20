# SPDX-License-Identifier: Apache-2.0
"""Jira sink: an issue for each failed critical scenario (FR-INT-05, ADR-0045).

The same rules as the GitHub Issues sink (ADR-0040), in Jira's terms:

- **Only failed ``critical`` or ``blocker`` scenarios**, never an inconclusive, skipped or
  blocked one.
- **One issue per scenario.** Jira has no hidden comments, so the scenario's tag is a label,
  ``opseval-<digest of suite and scenario id>``, next to the ``opseval`` label. Every open
  issue with the ``opseval`` label is read once per run; a repeat failure comments on the
  scenario's open issue. A project whose create screen drops labels makes the create fail
  (Jira answers 400 for a field it cannot set), so a duplicate is never opened silently.
- **No conversation content, no URLs.** Scenario-supplied text sits inside ``{noformat}``,
  where Jira renders no mention, link or macro.
- **Bounded**, and a partial failure is reported with what was written.

Cloud (``OPSEVAL_JIRA_EMAIL`` set) authenticates with the account email and an API token and
searches with ``/rest/api/3/search/jql``; Data Center uses a personal access token as a bearer
and ``/rest/api/2/search``. Both create issues and comments through the v2 API, which takes a
plain wiki-markup string.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable
from typing import Any

import httpx

from opseval.models.results import SuiteResult, TestVerdict
from opseval.sinks.issues import IssueSink, StopWriting

LABEL = "opseval"
PAGE = 100
MAX_PAGES = 20
"""Open labelled issues read to find existing ones. Beyond this the sink stops rather than
guess, because a missed issue would be opened again on every run."""
_DIGEST_LEN = 20
_CLOSER = re.compile(r"\{\s*noformat", re.IGNORECASE)
"""Any spelling that could end a ``{noformat}`` block: Jira's macro names are not
case-sensitive, so ``{NOFORMAT}`` closes one too."""


def scenario_label(suite: str, test_id: str) -> str:
    """The label that finds a scenario's open issue. A digest, because a Jira label cannot hold
    spaces and is limited in length; the NUL separator keeps ``("a", "b/c")`` and ``("a/b",
    "c")`` apart."""
    digest = hashlib.sha256(f"{suite}\0{test_id}".encode()).hexdigest()[:_DIGEST_LEN]
    return f"{LABEL}-{digest}"


def _noformat(text: str, limit: int = 300) -> str:
    """Scenario or result text as inert wiki markup: one line inside ``{noformat}``, where Jira
    renders no mention, link or macro. Any closing tag in the text, in any case, is broken."""
    flat = _CLOSER.sub("{no format", " ".join(text.split()))[:limit]
    return "{noformat}" + flat + "{noformat}"


def _summary(text: str, limit: int) -> str:
    """Text for an issue summary: one line, with a user mention (``[~``) broken."""
    return " ".join(text.split()).replace("[~", "[ ~")[:limit]


def _facts(result: SuiteResult, t: TestVerdict) -> str:
    rate = "not measured" if t.pass_rate is None else f"{t.pass_rate:.0%}"
    return "\n".join(
        [
            f"* *Run:* {_noformat(result.run_id, 120)} ({result.started_at:%Y-%m-%d %H:%M} UTC)",
            f"* *Suite:* {_noformat(result.suite, 120)} *profile:* "
            f"{_noformat(result.profile, 120)}",
            f"* *Criticality:* {t.criticality.value} · *pass rate:* {rate}"
            + (" · flaky" if t.flaky else ""),
            f"* *Why:* {_noformat(t.reason or 'failed', 500)}",
            f"* *Gate:* {result.gate.status} · exit code {result.exit_code}",
        ]
    )


class JiraSink(IssueSink):
    name = "jira"
    service = "Jira"

    def __init__(
        self,
        base_url: str,
        project: str,
        token: str,
        *,
        email: str | None = None,
        issue_type: str = "Bug",
        max_issues: int = 10,
        timeout: float = 15.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        # An empty email (a blank line in .env) is "not set": Data Center, not Cloud.
        self._cloud = bool(email and email.strip())
        if self._cloud:
            pair = base64.b64encode(f"{(email or '').strip()}:{token}".encode()).decode()
            auth = f"Basic {pair}"
        else:
            auth = f"Bearer {token}"
        super().__init__(
            project,
            base_url,
            {"authorization": auth, "accept": "application/json"},
            max_issues=max_issues,
            timeout=timeout,
            client_factory=client_factory,
        )
        self._issue_type = issue_type

    def _tag(self, result: SuiteResult, t: TestVerdict) -> str:
        return scenario_label(result.suite, t.test_id)

    def _jql(self) -> str:
        # A fixed order, so an issue closed between two pages cannot shift another one
        # across the page boundary unseen.
        return (
            f'project = "{self._where}" AND labels = "{LABEL}" AND statusCategory != Done '
            "ORDER BY key ASC"
        )

    async def _search_page(
        self, client: httpx.AsyncClient, *, start: int, token: str | None
    ) -> dict[str, Any]:
        if self._cloud:
            body: dict[str, Any] = {"jql": self._jql(), "fields": ["labels"], "maxResults": PAGE}
            if token:
                body["nextPageToken"] = token
            response = await self._call(client, "POST", "/rest/api/3/search/jql", json=body)
        else:
            params = {"jql": self._jql(), "fields": "labels", "startAt": start, "maxResults": PAGE}
            response = await self._call(client, "GET", "/rest/api/2/search", params=params)
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("issues"), list):
            raise RuntimeError("Jira's search answer had no issue list: cannot find open issues")
        return data

    async def _open_issues(self, client: httpx.AsyncClient) -> dict[str, str]:
        found: dict[str, str] = {}
        seen = 0
        token: str | None = None
        for _ in range(MAX_PAGES):
            data = await self._search_page(client, start=seen, token=token)
            issues: list[Any] = data["issues"]
            seen += len(issues)
            for issue in issues:
                labels = (issue.get("fields") or {}).get("labels") or []
                for label in labels:
                    if isinstance(label, str) and label.startswith(f"{LABEL}-"):
                        found.setdefault(label, str(issue["key"]))
            if self._cloud:
                token = data.get("nextPageToken")
                if token:
                    continue
                if data.get("isLast") is False:
                    # "Not the last page", and no way on: the rest cannot be read, so say so
                    # rather than treat what was read as everything.
                    raise RuntimeError(
                        "Jira's search said there were more results but gave no page token: "
                        "cannot find every open issue"
                    )
                return found
            else:
                # Data Center caps maxResults by an admin setting, so a short page is not the
                # end: page by what came back, and stop at the total or an empty page.
                total = data.get("total")
                if not issues or (isinstance(total, int) and seen >= total):
                    return found
        raise RuntimeError(
            f"more than {MAX_PAGES} pages of open '{LABEL}' issues: close some, so existing "
            "issues can be found instead of duplicated"
        )

    async def _comment(
        self, client: httpx.AsyncClient, ref: str, result: SuiteResult, t: TestVerdict
    ) -> None:
        await self._call(
            client,
            "POST",
            f"/rest/api/2/issue/{ref}/comment",
            json={"body": f"Failed again.\n\n{_facts(result, t)}"},
        )

    async def _create(
        self, client: httpx.AsyncClient, result: SuiteResult, t: TestVerdict, tag: str
    ) -> tuple[str, StopWriting | None]:
        response = await self._call(
            client,
            "POST",
            "/rest/api/2/issue",
            json={
                "fields": {
                    "project": {"key": self._where},
                    "issuetype": {"name": self._issue_type},
                    "summary": f"[opseval] {_summary(t.test_id, 80)} failed: "
                    f"{_summary(t.name, 120)}",
                    "labels": [LABEL, tag],
                    "description": (
                        f"Opseval scenario {_noformat(t.test_id, 120)} "
                        f"({_noformat(t.name, 200)}) failed.\n\n{_facts(result, t)}\n\n"
                        "The conversation is in the run's report; it is not copied here."
                    ),
                }
            },
        )
        try:
            key = str(response.json()["key"])
        except Exception as exc:
            raise StopWriting(
                f"{t.test_id}: Jira created an issue but its answer had no key "
                f"({type(exc).__name__}): no further issues are written"
            ) from exc
        # Read the labels back: a create screen or a post-function on the create transition
        # can drop them, and an issue without its label would be opened again on every run.
        # (An automation rule that runs later, asynchronously, is not caught here.)
        try:
            answer = await self._call(
                client, "GET", f"/rest/api/2/issue/{key}", params={"fields": "labels"}
            )
            labels = (answer.json().get("fields") or {}).get("labels") or []
        except Exception as exc:  # noqa: BLE001 - the issue exists: count it, then stop
            return key, StopWriting(
                f"issue {key} was created, but its labels could not be read back "
                f"({type(exc).__name__}: {exc}): no further issues are written"
            )
        if LABEL not in labels or tag not in labels:
            return key, StopWriting(
                f"issue {key} lost its '{LABEL}' labels when it was created (a create screen "
                "or post-function?): the next run cannot find it and would open a duplicate, "
                "so no further issues are written"
            )
        return key, None


__all__ = ["JiraSink", "scenario_label"]
