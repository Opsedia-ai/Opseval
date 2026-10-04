# SPDX-License-Identifier: Apache-2.0
"""OTLP trace source (FR-INT-03, ADR-0028).

OTLP is an *ingest* protocol: it has no standard query API. Reading traces therefore
means choosing a mechanism, and the choice matters, because R-08 wants OTLP precisely as
the trace path that does not depend on one vendor.

So this reads the **OTLP-JSON** that the OpenTelemetry Collector's ``file`` exporter
writes — one ``resourceSpans`` envelope per line. That is vendor-neutral by construction
and needs no backend credentials: in CI the agent exports spans next to the run and
Opseval reads them. Querying a backend (Tempo, Jaeger) would reuse the span mapping below
and is left as a follow-up rather than built on a guess about which backend.

The span → :class:`ToolCall` mapping is the reusable part, and it refuses to guess:

- a file with no spans for this session raises :class:`TraceUnavailable`, so the caller
  polls, exactly as the Langfuse reader does;
- an empty list is a **positive claim** — "the agent called no tools" — so it is never
  returned because parsing failed. Spans that could not be read are counted, and if
  nothing was usable the result is ``TraceUnavailable`` with that count, not ``[]``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opseval.errors import TraceUnavailable
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord, ToolCall
from opseval.traces import TraceSource

SPAN_STATUS_ERROR = 2
"""OTLP ``StatusCode``: 0 UNSET, 1 OK, 2 ERROR. Only ERROR means the call failed."""

DEFAULT_SESSION_KEYS = ("session.id", "gen_ai.conversation.id", "opseval.session_id")
"""Attributes that may carry the session, newest convention first. Configurable, because
there is no settled semantic convention for an agent session id."""

DEFAULT_ARGUMENT_KEYS = ("gen_ai.tool.arguments", "tool.arguments", "input.value")


def _any_value(value: Any) -> Any:
    """Decode one OTLP ``AnyValue``. Never raises."""
    if not isinstance(value, dict):
        return None
    for key, cast in (
        ("stringValue", str),
        ("boolValue", bool),
        ("intValue", int),
        ("doubleValue", float),
    ):
        if key in value:
            try:
                return cast(value[key])
            except (TypeError, ValueError):
                return value[key]
    if "arrayValue" in value:
        inner = value["arrayValue"]
        items = inner.get("values", []) if isinstance(inner, dict) else []
        return [_any_value(v) for v in items]
    if "kvlistValue" in value:
        inner = value["kvlistValue"]
        pairs = inner.get("values", []) if isinstance(inner, dict) else []
        return _attributes(pairs)
    if "bytesValue" in value:
        return value["bytesValue"]
    return None


def _attributes(raw: Any) -> dict[str, Any]:
    """OTLP's ``[{key, value}]`` as a plain mapping. Never raises."""
    out: dict[str, Any] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            out[item["key"]] = _any_value(item.get("value"))
    return out


def _nanos(value: Any) -> datetime | None:
    try:
        nanos = int(value)
    except (TypeError, ValueError):
        return None
    if nanos <= 0:
        return None
    return datetime.fromtimestamp(nanos / 1_000_000_000, tz=UTC)


def iter_spans(payload: Any) -> list[dict[str, Any]]:
    """Every span in an OTLP envelope, with resource attributes merged in. Never raises."""
    spans: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return spans
    for resource_span in payload.get("resourceSpans") or []:
        if not isinstance(resource_span, dict):
            continue
        resource = resource_span.get("resource")
        resource_attrs = _attributes(
            resource.get("attributes") if isinstance(resource, dict) else None
        )
        for scope_span in resource_span.get("scopeSpans") or []:
            if not isinstance(scope_span, dict):
                continue
            for span in scope_span.get("spans") or []:
                if isinstance(span, dict):
                    spans.append({"span": span, "resource": resource_attrs})
    return spans


class OtlpFileTraceSource(TraceSource):
    """Read OTLP-JSON spans from a file or a directory of files."""

    name = "otlp-file"

    def __init__(
        self,
        path: str | Path,
        *,
        session_keys: tuple[str, ...] = DEFAULT_SESSION_KEYS,
        argument_keys: tuple[str, ...] = DEFAULT_ARGUMENT_KEYS,
        tool_name_key: str | None = "gen_ai.tool.name",
    ) -> None:
        self._path = Path(path)
        self._session_keys = session_keys
        self._argument_keys = argument_keys
        self._tool_name_key = tool_name_key

    def _files(self) -> list[Path]:
        if self._path.is_dir():
            return sorted(p for p in self._path.rglob("*") if p.is_file())
        return [self._path] if self._path.is_file() else []

    def _envelopes(self) -> tuple[list[Any], int]:
        """Parsed envelopes and the number of lines that could not be read."""
        envelopes: list[Any] = []
        unreadable = 0
        for file in self._files():
            try:
                text = file.read_text("utf-8")
            except OSError:
                unreadable += 1
                continue
            stripped = text.strip()
            if not stripped:
                continue
            # The collector's file exporter writes one envelope per line; a single
            # pretty-printed document is also common, so both are accepted.
            try:
                envelopes.append(json.loads(stripped))
                continue
            except ValueError:
                pass
            for line in stripped.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    envelopes.append(json.loads(line))
                except ValueError:
                    unreadable += 1
        return envelopes, unreadable

    def _session_of(self, attrs: dict[str, Any], resource: dict[str, Any]) -> str | None:
        for key in self._session_keys:
            for source in (attrs, resource):
                value = source.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    async def fetch(
        self, session_id: str, record: RunRecord, profile: SutProfile
    ) -> list[ToolCall]:
        if not self._files():
            raise TraceUnavailable(f"no OTLP spans at {self._path} (yet)")
        envelopes, unreadable = self._envelopes()
        matched = 0
        calls: list[ToolCall] = []
        for envelope in envelopes:
            for entry in iter_spans(envelope):
                span, resource = entry["span"], entry["resource"]
                attrs = _attributes(span.get("attributes"))
                if self._session_of(attrs, resource) != session_id:
                    continue
                matched += 1
                name = span.get("name")
                if self._tool_name_key and isinstance(attrs.get(self._tool_name_key), str):
                    name = attrs[self._tool_name_key]
                if not isinstance(name, str) or not name:
                    unreadable += 1
                    continue
                # An undeclared name is kept: `Evidence.unknown` reports those, so they
                # are surfaced rather than dropped here.
                started = _nanos(span.get("startTimeUnixNano"))
                if started is None:
                    unreadable += 1
                    continue
                status = span.get("status")
                code = status.get("code") if isinstance(status, dict) else None
                arguments = next(
                    (attrs[k] for k in self._argument_keys if k in attrs),
                    None,
                )
                calls.append(
                    ToolCall(
                        name=name,
                        started_at=started,
                        ended_at=_nanos(span.get("endTimeUnixNano")),
                        ok=code != SPAN_STATUS_ERROR,
                        arguments=arguments,
                        observation_id=span.get("spanId")
                        if isinstance(span.get("spanId"), str)
                        else None,
                    )
                )

        if not matched:
            raise TraceUnavailable(
                f"no OTLP spans for session {session_id!r} in {self._path} (yet)"
            )
        if not calls:
            # Spans existed for this session but none could be read. An empty list claims
            # the agent called no tools, which would be a different and false statement.
            raise TraceUnavailable(
                f"{matched} span(s) for session {session_id!r} but none were readable "
                f"({unreadable} unreadable); check startTimeUnixNano and the span name"
            )
        return sorted(calls, key=lambda c: c.started_at)


__all__ = [
    "DEFAULT_ARGUMENT_KEYS",
    "DEFAULT_SESSION_KEYS",
    "SPAN_STATUS_ERROR",
    "OtlpFileTraceSource",
    "iter_spans",
]
