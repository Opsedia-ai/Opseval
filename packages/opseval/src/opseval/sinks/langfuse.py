# SPDX-License-Identifier: Apache-2.0
"""Langfuse score push (FR-INT-04, ADR-0027).

Pushes one score per scenario back to the session the run already traced, so a Langfuse
user sees Opseval's verdict next to the conversation it judged.

What it will **not** do is invent a number. A scenario that was not judged has no score
to push: `inconclusive`, `skipped_precondition` and `blocked` carry a comment saying so
and no value, because a 0 in a dashboard is indistinguishable from "the agent failed".
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from opseval.models.results import SuiteResult, VerdictStatus
from opseval.sinks.base import Sink

PUSHABLE = {VerdictStatus.PASSED: 1.0, VerdictStatus.FAILED: 0.0}
"""Only a measured verdict becomes a number."""


class LangfuseScoreSink(Sink):
    name = "langfuse-scores"

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        *,
        score_name: str = "opseval",
        timeout: float = 30.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._url = f"{host.rstrip('/')}/api/public/scores"
        self._auth = (public_key, secret_key)
        self._score_name = score_name
        self._timeout = timeout
        self._client_factory = client_factory

    def _payloads(self, result: SuiteResult) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for test in result.tests:
            for attempt in test.attempts:
                session = attempt.session_id
                if not session:
                    continue
                value = PUSHABLE.get(test.status)
                body: dict[str, object] = {
                    "name": self._score_name,
                    "sessionId": session,
                    "comment": f"{test.test_id}: {test.status.value}"
                    + (f" — {test.reason}" if test.reason else ""),
                    "metadata": {
                        "opseval_run_id": result.run_id,
                        "test_id": test.test_id,
                        "criticality": test.criticality.value,
                        "status": test.status.value,
                    },
                }
                if value is None:
                    # Not measured. A dataType of CATEGORICAL carries the state without
                    # pretending there is a number behind it.
                    body["dataType"] = "CATEGORICAL"
                    body["stringValue"] = test.status.value
                else:
                    body["dataType"] = "NUMERIC"
                    body["value"] = value
                out.append(body)
        return out

    async def publish(self, result: SuiteResult) -> str:
        payloads = self._payloads(result)
        if not payloads:
            return "nothing to push: no attempt recorded a session id"
        client = self._client_factory() if self._client_factory else httpx.AsyncClient()
        try:
            for body in payloads:
                response = await client.post(
                    self._url, json=body, auth=self._auth, timeout=self._timeout
                )
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"{self._url} answered HTTP {response.status_code} for "
                        f"{body.get('sessionId')!r}"
                    )
        finally:
            await client.aclose()
        measured = sum(1 for b in payloads if b.get("dataType") == "NUMERIC")
        return (
            f"pushed {len(payloads)} score(s) to Langfuse "
            f"({measured} measured, {len(payloads) - measured} not judged)"
        )


__all__ = ["LangfuseScoreSink", "PUSHABLE"]
