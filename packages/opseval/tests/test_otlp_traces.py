# SPDX-License-Identifier: Apache-2.0
"""OTLP trace source (FR-INT-03).

The honesty rule this source has to hold is specific: an **empty list is a positive
claim** — "the agent called no tools" — and `tools.*` scorers act on it. So it is never
returned because parsing failed. No spans yet, no spans for this session, or spans that
could not be read are all `TraceUnavailable`, which the caller polls on and which becomes
`infra_error` rather than a passing vacuous score.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opseval.errors import TraceUnavailable
from opseval.models.profile import SutProfile, ToolSpec, TraceConfig
from opseval.models.record import RunRecord, TerminalState
from opseval.traces.otlp import OtlpFileTraceSource, iter_spans

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
NANOS = int(T0.timestamp() * 1_000_000_000)
SESSION = "sess-42"


def profile(**tools: ToolSpec) -> SutProfile:
    declared = tools or {"create_refund": ToolSpec(), "lookup_order": ToolSpec()}
    return SutProfile(name="p", tools=declared, traces=TraceConfig(source="otlp-file"))


def record() -> RunRecord:
    return RunRecord(
        run_id="r",
        test_id="t-001",
        attempt_index=0,
        seed=1,
        target="x",
        started_at=T0,
        ended_at=T0,
        terminal_state=TerminalState.GOAL_REACHED,
    )


def span(
    name: str = "create_refund",
    *,
    session: str | None = SESSION,
    start: int | None = NANOS,
    end: int | None = NANOS + 5_000_000,
    status: int | None = 1,
    attrs: dict[str, Any] | None = None,
    span_id: str = "abc123",
) -> dict[str, Any]:
    attributes = []
    if session is not None:
        attributes.append({"key": "session.id", "value": {"stringValue": session}})
    for key, value in (attrs or {}).items():
        attributes.append({"key": key, "value": value})
    out: dict[str, Any] = {"name": name, "spanId": span_id, "attributes": attributes}
    if start is not None:
        out["startTimeUnixNano"] = str(start)
    if end is not None:
        out["endTimeUnixNano"] = str(end)
    if status is not None:
        out["status"] = {"code": status}
    return out


def envelope(*spans: dict[str, Any], resource: dict[str, Any] | None = None) -> dict[str, Any]:
    res: dict[str, Any] = {}
    if resource:
        res["attributes"] = [{"key": k, "value": v} for k, v in resource.items()]
    return {"resourceSpans": [{"resource": res, "scopeSpans": [{"spans": list(spans)}]}]}


def write(path: Path, *envelopes: dict[str, Any], one_per_line: bool = True) -> Path:
    if one_per_line:
        path.write_text("\n".join(json.dumps(e) for e in envelopes), encoding="utf-8")
    else:
        path.write_text(json.dumps(envelopes[0], indent=2), encoding="utf-8")
    return path


async def fetch(path: Path, prof: SutProfile | None = None, **kw: Any) -> Any:
    return await OtlpFileTraceSource(path, **kw).fetch(SESSION, record(), prof or profile())


# --- it never fabricates an empty trace -----------------------------------------------


class TestNeverClaimsNoToolCalls:
    async def test_a_missing_file_is_unavailable(self, tmp_path: Path) -> None:
        with pytest.raises(TraceUnavailable, match="no OTLP spans at"):
            await fetch(tmp_path / "nope.jsonl")

    async def test_an_empty_file_is_unavailable(self, tmp_path: Path) -> None:
        path = tmp_path / "spans.jsonl"
        path.write_text("", encoding="utf-8")
        with pytest.raises(TraceUnavailable):
            await fetch(path)

    async def test_spans_for_another_session_are_unavailable(self, tmp_path: Path) -> None:
        path = write(tmp_path / "s.jsonl", envelope(span(session="someone-else")))
        with pytest.raises(TraceUnavailable, match="no OTLP spans for session"):
            await fetch(path)

    async def test_unreadable_spans_for_this_session_are_unavailable(self, tmp_path: Path) -> None:
        """An empty list would say "the agent called no tools", which is a different
        and false statement."""
        path = write(tmp_path / "s.jsonl", envelope(span(start=None)))
        with pytest.raises(TraceUnavailable, match="none were readable"):
            await fetch(path)

    async def test_a_span_with_no_name_is_unreadable(self, tmp_path: Path) -> None:
        path = write(tmp_path / "s.jsonl", envelope(span(name="")))
        with pytest.raises(TraceUnavailable, match="none were readable"):
            await fetch(path)

    async def test_garbage_lines_do_not_crash_the_reader(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text("not json\n" + json.dumps(envelope(span())) + "\n{\n", encoding="utf-8")
        calls = await fetch(path)
        assert [c.name for c in calls] == ["create_refund"]


# --- the mapping -----------------------------------------------------------------------


class TestMapping:
    async def test_a_span_becomes_a_tool_call(self, tmp_path: Path) -> None:
        calls = await fetch(write(tmp_path / "s.jsonl", envelope(span())))
        assert len(calls) == 1
        call = calls[0]
        assert call.name == "create_refund"
        assert call.started_at == T0
        assert call.ended_at is not None and call.ended_at > T0
        assert call.ok is True
        assert call.observation_id == "abc123"

    async def test_only_status_error_means_failure(self, tmp_path: Path) -> None:
        for code, ok in ((0, True), (1, True), (2, False), (None, True)):
            path = write(tmp_path / f"s{code}.jsonl", envelope(span(status=code)))
            assert (await fetch(path))[0].ok is ok, code

    async def test_calls_are_sorted_by_start_time(self, tmp_path: Path) -> None:
        path = write(
            tmp_path / "s.jsonl",
            envelope(
                span("create_refund", start=NANOS + 9_000_000),
                span("lookup_order", start=NANOS),
            ),
        )
        assert [c.name for c in await fetch(path)] == ["lookup_order", "create_refund"]

    async def test_arguments_are_decoded(self, tmp_path: Path) -> None:
        path = write(
            tmp_path / "s.jsonl",
            envelope(span(attrs={"gen_ai.tool.arguments": {"stringValue": '{"order_id":"A-1"}'}})),
        )
        assert (await fetch(path))[0].arguments == '{"order_id":"A-1"}'

    async def test_a_tool_name_attribute_overrides_the_span_name(self, tmp_path: Path) -> None:
        path = write(
            tmp_path / "s.jsonl",
            envelope(
                span("POST /refunds", attrs={"gen_ai.tool.name": {"stringValue": "create_refund"}})
            ),
        )
        assert (await fetch(path))[0].name == "create_refund"

    async def test_the_session_may_live_on_the_resource(self, tmp_path: Path) -> None:
        path = write(
            tmp_path / "s.jsonl",
            envelope(span(session=None), resource={"session.id": {"stringValue": SESSION}}),
        )
        assert len(await fetch(path)) == 1

    async def test_the_session_attribute_is_configurable(self, tmp_path: Path) -> None:
        """There is no settled convention for an agent session id."""
        path = write(
            tmp_path / "s.jsonl",
            envelope(span(session=None, attrs={"my.session": {"stringValue": SESSION}})),
        )
        with pytest.raises(TraceUnavailable):
            await fetch(path)
        calls = await fetch(path, session_keys=("my.session",))
        assert len(calls) == 1

    async def test_an_undeclared_tool_is_kept_for_evidence_to_report(self, tmp_path: Path) -> None:
        path = write(tmp_path / "s.jsonl", envelope(span("mystery_tool")))
        assert [c.name for c in await fetch(path)] == ["mystery_tool"]


class TestFileFormats:
    async def test_one_envelope_per_line(self, tmp_path: Path) -> None:
        path = write(
            tmp_path / "s.jsonl", envelope(span("lookup_order")), envelope(span("create_refund"))
        )
        assert len(await fetch(path)) == 2

    async def test_a_single_pretty_printed_document(self, tmp_path: Path) -> None:
        path = write(tmp_path / "s.json", envelope(span()), one_per_line=False)
        assert len(await fetch(path)) == 1

    async def test_a_directory_of_files(self, tmp_path: Path) -> None:
        folder = tmp_path / "spans"
        (folder / "nested").mkdir(parents=True)
        write(folder / "a.jsonl", envelope(span("lookup_order")))
        write(folder / "nested" / "b.jsonl", envelope(span("create_refund")))
        assert sorted(c.name for c in await fetch(folder)) == ["create_refund", "lookup_order"]

    async def test_several_resource_and_scope_blocks(self, tmp_path: Path) -> None:
        payload = {
            "resourceSpans": [
                {"resource": {}, "scopeSpans": [{"spans": [span("lookup_order")]}]},
                {"resource": {}, "scopeSpans": [{"spans": [span("create_refund")]}]},
            ]
        }
        path = write(tmp_path / "s.jsonl", payload)
        assert len(await fetch(path)) == 2


class TestIterSpans:
    def test_it_never_raises_on_rubbish(self) -> None:
        for payload in (None, [], {}, {"resourceSpans": None}, {"resourceSpans": [1, 2]}):
            assert iter_spans(payload) == []

    def test_resource_attributes_are_merged_in(self) -> None:
        entries = iter_spans(envelope(span(), resource={"service.name": {"stringValue": "a"}}))
        assert entries[0]["resource"]["service.name"] == "a"


# --- wiring ----------------------------------------------------------------------------


class TestWiring:
    def test_the_runner_builds_it(self, tmp_path: Path) -> None:
        from opseval.config import Settings
        from opseval.runner import RunOptions, build_trace_source

        source = build_trace_source(
            RunOptions(traces=True),
            Settings(otlp_spans_path=str(tmp_path)),
            profile(),
        )
        assert isinstance(source, OtlpFileTraceSource)

    def test_a_missing_path_is_a_config_error(self) -> None:
        from opseval.config import Settings
        from opseval.errors import ConfigError
        from opseval.runner import RunOptions, build_trace_source

        with pytest.raises(ConfigError, match="OPSEVAL_OTLP_SPANS_PATH"):
            build_trace_source(RunOptions(traces=True), Settings(), profile())

    def test_the_source_is_named_for_the_manifest(self) -> None:
        assert OtlpFileTraceSource.name == "otlp-file"
