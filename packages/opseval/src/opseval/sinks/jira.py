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
from collections.abc import Callable
from typing import Any

import httpx

from opseval.models.results import SuiteResult, TestVerdict
from opseval.sinks.base import Sink
from opseval.sinks.github_issues import failed_serious

LABEL = "opseval"
PAGE = 100
MAX_PAGES = 20
"""Open labelled issues read to find existing ones. Beyond this the sink stops rather than
guess, because a missed issue would be opened again on every run."""
_DIGEST_LEN = 20


def scenario_label(suite: str, test_id: str) -> str:
    """The label that finds a scenario's open issue. A digest, because a Jira label cannot hold
    spaces and is limited in length; the NUL separator keeps ``("a", "b/c")`` and ``("a/b",
    "c")`` apart."""
    digest = hashlib.sha256(f"{suite}\0{test_id}".encode()).hexdigest()[:_DIGEST_LEN]
    return f"{LABEL}-{digest}"


def _noformat(text: str, limit: int = 300) -> str:
    """Scenario or result text as inert wiki markup: one line inside ``{noformat}``, where Jira
    renders no mention, link or macro. The closing tag is broken if the text contains it."""
    flat = " ".join(text.split()).replace("{noformat}", "{no format}")[:limit]
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


class JiraSink(Sink):
    name = "jira"

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
        self._base = base_url.rstrip("/")
        self._project = project
        self._cloud = email is not None
        if email is not None:
            pair = base64.b64encode(f"{email}:{token}".encode()).decode()
            auth = f"Basic {pair}"
        else:
            auth = f"Bearer {token}"
        self._headers = {"authorization": auth, "accept": "application/json"}
        self._issue_type = issue_type
        self._max = max_issues
        self._timeout = timeout
        self._client_factory = client_factory

    async def _call(
        self, client: httpx.AsyncClient, method: str, path: str, **kw: Any
    ) -> httpx.Response:
        response = await client.request(
            method, f"{self._base}{path}", headers=self._headers, timeout=self._timeout, **kw
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Jira answered HTTP {response.status_code} to {method} {path}")
        return response

    def _jql(self) -> str:
        return f'project = "{self._project}" AND labels = "{LABEL}" AND statusCategory != Done'

    async def _open_issues(self, client: httpx.AsyncClient) -> dict[str, str]:
        """Every open opseval issue, by scenario label -> issue key. Raises rather than guess."""
        found: dict[str, str] = {}
        token: str | None = None
        for page in range(MAX_PAGES):
            if self._cloud:
                body: dict[str, Any] = {
                    "jql": self._jql(),
                    "fields": ["labels"],
                    "maxResults": PAGE,
                }
                if token:
                    body["nextPageToken"] = token
                data = (
                    await self._call(client, "POST", "/rest/api/3/search/jql", json=body)
                ).json()
            else:
                params = {
                    "jql": self._jql(),
                    "fields": "labels",
                    "startAt": page * PAGE,
                    "maxResults": PAGE,
                }
                data = (await self._call(client, "GET", "/rest/api/2/search", params=params)).json()
            issues = data.get("issues") if isinstance(data, dict) else None
            if not isinstance(issues, list):
                raise RuntimeError(
                    "Jira's search answer had no issue list: cannot find open issues"
                )
            for issue in issues:
                labels = (issue.get("fields") or {}).get("labels") or []
                for label in labels:
                    if isinstance(label, str) and label.startswith(f"{LABEL}-"):
                        found.setdefault(label, str(issue["key"]))
            if self._cloud:
                token = data.get("nextPageToken")
                if data.get("isLast") or not token:
                    return found
            elif len(issues) < PAGE:
                return found
        raise RuntimeError(
            f"more than {MAX_PAGES * PAGE} open '{LABEL}' issues: close some, so existing "
            "issues can be found instead of duplicated"
        )

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
                    await self._report(client, result, t, existing, opened, commented)
                except Exception as exc:  # noqa: BLE001 - one issue must not stop the others
                    errors.append(f"{t.test_id}: {type(exc).__name__}: {exc}")
        finally:
            await client.aclose()
        parts = []
        if opened:
            parts.append(f"opened {', '.join(opened)}")
        if commented:
            parts.append(f"commented on {', '.join(commented)}")
        if len(failing) > self._max:
            parts.append(f"{len(failing) - self._max} more not reported (limit {self._max})")
        summary = f"{self._project}: " + "; ".join(parts or ["nothing written"])
        if errors:
            raise RuntimeError(f"{summary}; failed for {'; '.join(errors)}")
        return summary

    async def _report(
        self,
        client: httpx.AsyncClient,
        result: SuiteResult,
        t: TestVerdict,
        existing: dict[str, str],
        opened: list[str],
        commented: list[str],
    ) -> None:
        label = scenario_label(result.suite, t.test_id)
        facts = _facts(result, t)
        key = existing.get(label)
        if key is not None:
            await self._call(
                client,
                "POST",
                f"/rest/api/2/issue/{key}/comment",
                json={"body": f"Failed again.\n\n{facts}"},
            )
            commented.append(key)
            return
        response = await self._call(
            client,
            "POST",
            "/rest/api/2/issue",
            json={
                "fields": {
                    "project": {"key": self._project},
                    "issuetype": {"name": self._issue_type},
                    "summary": f"[opseval] {_summary(t.test_id, 80)} failed: "
                    f"{_summary(t.name, 120)}",
                    "labels": [LABEL, label],
                    "description": (
                        f"Opseval scenario {_noformat(t.test_id, 120)} "
                        f"({_noformat(t.name, 200)}) failed.\n\n{facts}\n\n"
                        "The conversation is in the run's report; it is not copied here."
                    ),
                }
            },
        )
        key = str(response.json()["key"])
        opened.append(key)
        existing[label] = key  # a second failure of it in this run comments, not duplicates


__all__ = ["JiraSink", "scenario_label"]
