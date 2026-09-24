# SPDX-License-Identifier: Apache-2.0
"""Langfuse trace source (public API v2 observations).

Reads ``GET {host}/api/public/v2/observations`` filtered by trace session id, with
cursor pagination and a start-time window around the attempt. The session filter uses
the ``filter`` parameter because the ``sessionId`` shorthand is not honoured by every
Langfuse version.

An observation is a tool call when its type is ``TOOL`` or its name matches a tool
declared in the SUT profile (``tools.<name>.trace_name``, default the tool name).
Level ``ERROR`` marks a failed call.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx

from opseval.errors import TraceUnavailable
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord, ToolCall
from opseval.traces import TraceSource

PAGE_LIMIT = 1000
MAX_PAGES = 20
WINDOW_MARGIN = timedelta(minutes=10)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class LangfuseTraceSource(TraceSource):
    name = "langfuse"

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        *,
        timeout: float = 30.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._url = f"{host.rstrip('/')}/api/public/v2/observations"
        self._auth = (public_key, secret_key)
        self._timeout = timeout
        self._client_factory = client_factory

    async def fetch(
        self, session_id: str, record: RunRecord, profile: SutProfile
    ) -> list[ToolCall]:
        params: dict[str, Any] = {
            "filter": json.dumps(
                [{"type": "string", "column": "sessionId", "operator": "=", "value": session_id}]
            ),
            "fromStartTime": _iso(record.started_at - WINDOW_MARGIN),
            "toStartTime": _iso(record.ended_at + WINDOW_MARGIN),
            "fields": "core,basic,io",
            "limit": PAGE_LIMIT,
        }
        rows: list[dict[str, Any]] = []
        client = self._client_factory() if self._client_factory else httpx.AsyncClient()
        try:
            for _ in range(MAX_PAGES):
                body = await self._page(client, params)
                data = body.get("data")
                if not isinstance(data, list):
                    raise TraceUnavailable("Langfuse returned an unexpected response body")
                rows += [r for r in data if isinstance(r, dict)]
                cursor = (body.get("meta") or {}).get("cursor")
                if not cursor:
                    break
                params = {**params, "cursor": cursor}
            else:
                raise TraceUnavailable(f"more than {MAX_PAGES * PAGE_LIMIT} observations")
        finally:
            if self._client_factory is None:
                await client.aclose()
        if not rows:
            raise TraceUnavailable(f"no observations for session {session_id!r} (yet)")
        return self._tool_calls(rows, profile)

    async def _page(self, client: httpx.AsyncClient, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await client.get(
                self._url, params=params, auth=self._auth, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise TraceUnavailable(f"cannot reach Langfuse: {exc}") from exc
        if resp.status_code in (401, 403):
            raise TraceUnavailable("Langfuse rejected the API keys (check OPSEVAL_LANGFUSE_*)")
        if resp.status_code != 200:
            raise TraceUnavailable(f"Langfuse answered HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise TraceUnavailable("Langfuse returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise TraceUnavailable("Langfuse returned an unexpected response body")
        return body

    @staticmethod
    def _tool_calls(rows: list[dict[str, Any]], profile: SutProfile) -> list[ToolCall]:
        declared = profile.trace_names()
        calls = []
        for row in rows:
            name = row.get("name")
            started = _parse_time(row.get("startTime"))
            if not isinstance(name, str) or started is None:
                continue
            if row.get("type") != "TOOL" and name not in declared:
                continue
            calls.append(
                ToolCall(
                    name=name,
                    started_at=started,
                    ended_at=_parse_time(row.get("endTime")),
                    ok=row.get("level") != "ERROR",
                    arguments=row.get("input"),
                    observation_id=row.get("id") if isinstance(row.get("id"), str) else None,
                )
            )
        return sorted(calls, key=lambda c: c.started_at)
