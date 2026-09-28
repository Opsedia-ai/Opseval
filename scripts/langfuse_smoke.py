# SPDX-License-Identifier: Apache-2.0
"""Check the Langfuse trace reader against a real Langfuse project (not shipped).

It sends one trace over the OTLP/HTTP endpoint: a root span carrying the session id and
three tool spans that do not (as with Langfuse's v4 ingestion, where only the root has
it), one of them failed. Then it runs Opseval's reader and checks what came back.

Settings come from the environment or ``.env`` (never printed):

    OPSEVAL_LANGFUSE_HOST=https://cloud.langfuse.com
    OPSEVAL_LANGFUSE_PUBLIC_KEY=pk-lf-...
    OPSEVAL_LANGFUSE_SECRET_KEY=sk-lf-...
    OPSEVAL_ALLOWED_HOSTS=cloud.langfuse.com

Run: ``uv run python scripts/langfuse_smoke.py [--wait 60] [--dump observations.json]``.
``--dump`` writes the raw v2 rows (anonymised: ids and project replaced) for fixtures.
Exit codes: 0 all checks passed · 1 a check failed · 3 configuration or connection error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from opseval.config import Settings, check_url_allowed
from opseval.errors import OpsevalError
from opseval.models.profile import SutProfile, ToolSpec, TraceConfig
from opseval.models.record import RunRecord, TerminalState
from opseval.traces import enrich
from opseval.traces.langfuse import LangfuseTraceSource

TOOLS = ("lookup_order", "check_eligibility", "create_refund")


def _attr(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def _span(
    trace_id: str,
    span_id: str,
    parent: str | None,
    name: str,
    start: int,
    end: int,
    attrs: list[dict[str, Any]],
) -> dict[str, Any]:
    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": attrs,
    }
    if parent:
        span["parentSpanId"] = parent
    return span


def build_trace(session_id: str, t0_ns: int) -> tuple[str, dict[str, Any]]:
    trace_id, root_id = secrets.token_hex(16), secrets.token_hex(8)
    ms = 1_000_000
    spans = [
        _span(
            trace_id,
            root_id,
            None,
            "opseval-smoke-agent",
            t0_ns,
            t0_ns + 900 * ms,
            [
                _attr("langfuse.session.id", session_id),
                _attr("langfuse.observation.type", "agent"),
                _attr("langfuse.observation.input", "Order 10482 arrived damaged"),
            ],
        )
    ]
    for i, tool in enumerate(TOOLS):
        attrs = [
            _attr("langfuse.observation.type", "tool"),
            _attr("langfuse.observation.input", json.dumps({"order": "10482", "step": i})),
        ]
        if tool == "create_refund":
            attrs.append(_attr("langfuse.observation.level", "ERROR"))
            attrs.append(_attr("langfuse.observation.status_message", "smoke: simulated failure"))
        start = t0_ns + (100 + 200 * i) * ms
        spans.append(
            _span(trace_id, secrets.token_hex(8), root_id, tool, start, start + 50 * ms, attrs)
        )
    body = {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr("service.name", "opseval-langfuse-smoke")]},
                "scopeSpans": [{"scope": {"name": "opseval-smoke"}, "spans": spans}],
            }
        ]
    }
    return trace_id, body


def anonymise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids: dict[str, str] = {}

    def alias(value: object, prefix: str) -> object:
        if not isinstance(value, str):
            return value
        return ids.setdefault(value, f"{prefix}-{len(ids) + 1}")

    out = []
    for row in rows:
        row = dict(row)
        row["id"] = alias(row.get("id"), "obs")
        row["traceId"] = alias(row.get("traceId"), "trace")
        row["parentObservationId"] = alias(row.get("parentObservationId"), "obs")
        row["projectId"] = "project-anon"
        out.append(row)
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wait", type=float, default=60.0, help="seconds to wait for ingestion")
    parser.add_argument("--dump", type=Path, help="write the anonymised v2 rows here")
    args = parser.parse_args()

    settings = Settings()
    host, public = settings.langfuse_host, settings.langfuse_public_key
    secret = settings.langfuse_secret_key
    if not (host and public and secret):
        print("set OPSEVAL_LANGFUSE_HOST, _PUBLIC_KEY and _SECRET_KEY", file=sys.stderr)
        return 3
    try:
        check_url_allowed(host, settings.allowlist(), secure_schemes=("https",))
    except OpsevalError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 3

    session_id = f"opseval-smoke-{uuid.uuid4().hex[:12]}"
    started = datetime.now(UTC)
    trace_id, body = build_trace(session_id, time.time_ns())
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{host.rstrip('/')}/api/public/otel/v1/traces",
            json=body,
            auth=(public, secret.get_secret_value()),
            headers={"x-langfuse-ingestion-version": "4"},
        )
    if resp.status_code >= 300:
        print(f"OTLP ingestion failed: HTTP {resp.status_code} {resp.text[:300]}", file=sys.stderr)
        return 3
    print(f"sent trace (session {session_id}, 1 root + {len(TOOLS)} tool spans)")

    profile = SutProfile(
        name="langfuse-smoke",
        tools={name: ToolSpec() for name in TOOLS},
        traces=TraceConfig(wait_seconds=args.wait, poll_interval_seconds=3),
    )
    record = RunRecord(
        run_id="smoke",
        test_id="langfuse-smoke",
        attempt_index=0,
        seed=0,
        session_id=session_id,
        target=host,
        started_at=started,
        ended_at=datetime.now(UTC) + timedelta(seconds=1),
        terminal_state=TerminalState.GOAL_REACHED,
    )
    raw_rows: list[dict[str, Any]] = []
    source = LangfuseTraceSource(host, public, secret.get_secret_value())
    if args.dump:
        original = source._query

        async def spy(*a: Any, **kw: Any) -> list[dict[str, Any]]:
            rows = await original(*a, **kw)
            raw_rows.extend(rows)
            return rows

        source._query = spy  # type: ignore[method-assign]
    enriched = await enrich(record, source, profile)

    checks = {
        "trace was read (trace_coverage=full)": enriched.trace_coverage == "full",
        "all three tool calls found": [c.name for c in enriched.tool_calls] == list(TOOLS),
        "failed tool call marked not ok": [c.ok for c in enriched.tool_calls]
        == [True, True, False],
        "arguments parsed from JSON": all(
            isinstance(c.arguments, dict) for c in enriched.tool_calls
        ),
    }
    for note in enriched.notes:
        print(f"note: {note}")
    for label, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if args.dump:
        args.dump.write_text(json.dumps(anonymise(raw_rows), indent=2), encoding="utf-8")
        print(f"wrote {len(raw_rows)} anonymised rows to {args.dump}")
    print(f"(trace id {trace_id[:8]}…, session {session_id})")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
