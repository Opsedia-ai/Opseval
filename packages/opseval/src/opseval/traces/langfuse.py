# SPDX-License-Identifier: Apache-2.0
"""Langfuse trace source (public API v2 observations, Langfuse Cloud and self-hosted v4).

Reads ``GET {host}/api/public/v2/observations`` in two steps:

1. Find the traces of the session: observations whose ``sessionId`` matches, ``core``
   fields only. On the v4 ingestion path a session id set only on the root span is not
   copied to its children, so this step may see only the root; that is enough to learn
   the trace ids.
2. Read every observation of those traces (``core,basic,io``) and keep the tool calls.

Every condition goes into the ``filter`` parameter, including the start-time window:
when ``filter`` is present it takes precedence over the fixed query parameters, and some
versions ignore the ``sessionId`` shorthand. The fixed ``fromStartTime``/``toStartTime``
are sent as well, for versions that honour both. Pagination follows ``meta.cursor``.

An observation is a tool call when its type is ``TOOL`` or its name matches a tool
declared in the SUT profile (``tools.<name>.trace_name``, default the tool name).
Level ``ERROR`` marks a failed call. The v2 API returns ``input`` as a raw string; it is
parsed as JSON when possible.
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
MAX_TRACES = 20
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


def _parse_io(value: object) -> object:
    """v2 returns I/O as raw strings; decode JSON when it is JSON, else keep the string."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _condition(column: str, value: str) -> dict[str, Any]:
    return {"type": "string", "column": column, "operator": "=", "value": value}


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
        start, end = record.started_at - WINDOW_MARGIN, record.ended_at + WINDOW_MARGIN
        window = [
            {"type": "datetime", "column": "startTime", "operator": ">=", "value": _iso(start)},
            {"type": "datetime", "column": "startTime", "operator": "<", "value": _iso(end)},
        ]
        base = {"fromStartTime": _iso(start), "toStartTime": _iso(end), "limit": PAGE_LIMIT}
        client = self._client_factory() if self._client_factory else httpx.AsyncClient()
        try:
            roots = await self._query(
                client, {**base, "fields": "core"}, [_condition("sessionId", session_id), *window]
            )
            trace_ids = sorted({r["traceId"] for r in roots if isinstance(r.get("traceId"), str)})
            if not trace_ids:
                raise TraceUnavailable(f"no observations for session {session_id!r} (yet)")
            if len(trace_ids) > MAX_TRACES:
                raise TraceUnavailable(
                    f"session {session_id!r} has {len(trace_ids)} traces (more than "
                    f"{MAX_TRACES}); use a per-attempt session id (traces.session_id)"
                )
            rows: list[dict[str, Any]] = []
            for trace_id in trace_ids:
                rows += await self._query(
                    client,
                    {**base, "fields": "core,basic,io"},
                    [_condition("traceId", trace_id), *window],
                )
        finally:
            if self._client_factory is None:
                await client.aclose()
        return self._tool_calls(rows, profile)

    async def _query(
        self, client: httpx.AsyncClient, params: dict[str, Any], conditions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        params = {**params, "filter": json.dumps(conditions)}
        rows: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            body = await self._page(client, params)
            data = body.get("data")
            if not isinstance(data, list):
                raise TraceUnavailable("Langfuse returned an unexpected response body")
            rows += [r for r in data if isinstance(r, dict)]
            cursor = (body.get("meta") or {}).get("cursor")
            if not cursor:
                return rows
            params = {**params, "cursor": cursor}
        raise TraceUnavailable(f"more than {MAX_PAGES * PAGE_LIMIT} observations")

    async def _page(self, client: httpx.AsyncClient, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await client.get(
                self._url, params=params, auth=self._auth, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise TraceUnavailable(f"cannot reach Langfuse: {exc}") from exc
        if resp.status_code in (401, 403):
            raise TraceUnavailable("Langfuse rejected the API keys (check OPSEVAL_LANGFUSE_*)")
        if resp.status_code == 404:
            raise TraceUnavailable(
                "Langfuse has no v2 observations API here (HTTP 404); self-hosted Langfuse "
                "needs v4 or later"
            )
        if resp.status_code == 400:
            raise TraceUnavailable(f"Langfuse rejected the query (HTTP 400): {resp.text[:200]}")
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
        seen: set[str] = set()
        for row in rows:
            name = row.get("name")
            started = _parse_time(row.get("startTime"))
            if not isinstance(name, str) or started is None:
                continue
            if row.get("type") != "TOOL" and name not in declared:
                continue
            obs_id = row.get("id") if isinstance(row.get("id"), str) else None
            if obs_id is not None:
                if obs_id in seen:
                    continue
                seen.add(obs_id)
            calls.append(
                ToolCall(
                    name=name,
                    started_at=started,
                    ended_at=_parse_time(row.get("endTime")),
                    ok=row.get("level") != "ERROR",
                    arguments=_parse_io(row.get("input")),
                    observation_id=obs_id,
                )
            )
        return sorted(calls, key=lambda c: c.started_at)
