# SPDX-License-Identifier: Apache-2.0
"""Trace sources: read what the agent really did from its own tracing (ADR-0011).

A trace source never judges. It turns the agent's observations into
:class:`~opseval.models.record.ToolCall` rows attached to the run record. If traces
cannot be read, the record keeps ``trace_coverage = "frames_only"`` with a note, and
tool scorers fall back to frame evidence or report ``infra_error``, never a failure.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from opseval.errors import TraceUnavailable
from opseval.models.profile import SutProfile, TraceConfig
from opseval.models.record import RunRecord, ToolCall


class TraceSource(ABC):
    name: str = "trace"

    @abstractmethod
    async def fetch(
        self, session_id: str, record: RunRecord, profile: SutProfile
    ) -> list[ToolCall]:
        """Return the tool calls observed for ``session_id``.

        Raise :class:`TraceUnavailable` when nothing can be read yet (so the caller can
        poll) or the source fails. An empty list means the trace exists and shows no
        tool calls.
        """


def trace_session_id(cfg: TraceConfig, record: RunRecord) -> str:
    values = {
        "session_id": record.session_id,
        "run_id": record.run_id,
        "test_id": record.test_id,
        "attempt_index": record.attempt_index,
    }
    if "{session_id}" in cfg.session_id and not record.session_id:
        raise TraceUnavailable("the agent reported no session id to look its trace up by")
    try:
        return cfg.session_id.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        raise TraceUnavailable(f"invalid traces.session_id template: {exc}") from exc


async def enrich(
    record: RunRecord,
    source: TraceSource,
    profile: SutProfile,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RunRecord:
    """Attach trace tool calls to ``record``; on failure, note why and keep frames only.

    Traces are ingested asynchronously, so the source is polled until two consecutive
    reads agree (or ``wait_seconds`` runs out).
    """
    cfg = profile.traces or TraceConfig()
    if record.terminal_state.is_infra:
        return record  # the attempt is not judged anyway
    try:
        session_id = trace_session_id(cfg, record)
        waited = 0.0
        previous: list[ToolCall] | None = None
        last_error: TraceUnavailable | None = None
        while True:
            try:
                calls = await source.fetch(session_id, record, profile)
            except TraceUnavailable as exc:
                calls, last_error = None, exc
            if calls is not None and previous is not None and len(calls) == len(previous):
                break  # stable across two reads
            if calls is not None and cfg.wait_seconds == 0:
                break
            previous = calls if calls is not None else previous
            if waited >= cfg.wait_seconds:
                if previous is None:
                    raise last_error or TraceUnavailable("no trace found")
                calls = previous
                break
            await sleep(cfg.poll_interval_seconds)
            waited += cfg.poll_interval_seconds
    except TraceUnavailable as exc:
        return record.model_copy(
            update={"notes": (*record.notes, f"trace_unavailable: {source.name}: {exc}")}
        )
    return record.model_copy(update={"trace_coverage": "full", "tool_calls": tuple(calls)})


__all__ = ["TraceSource", "enrich", "trace_session_id"]
