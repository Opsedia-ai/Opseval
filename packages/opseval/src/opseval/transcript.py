# SPDX-License-Identifier: Apache-2.0
"""A run record as a conversation: the one mapping the HTML report and the console share.

Checkpoints and decisions come from the record's own interrupt log, not from guessing at
payload keys, so a profile that names them differently (or a rejection sent as a customer
message) still shows what was decided. A frame's kind is never hidden behind its text: an
error the agent sent with a message is shown as an error, because hiding an agent failure
as an ordinary turn is the inverse of the rule this project exists for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from opseval.models.profile import FrameKind
from opseval.models.record import RunRecord

EntryKind = Literal[
    "message",
    "checkpoint",
    "decision",
    "result",
    "error",
    "notification",
    "unknown",
    "undecodable_audio",
]


@dataclass(frozen=True)
class Entry:
    seq: int
    kind: EntryKind
    who: Literal["customer", "agent"] | None = None
    text: str | None = None
    phase: str | None = None
    latency_ms: float | None = None
    detail: str | None = None


def short(value: Any, limit: int = 500) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _field(payload: Any, key: str) -> str | None:
    value = payload.get(key) if isinstance(payload, dict) else None
    return None if value is None else str(value)


_MARKED: dict[FrameKind, EntryKind] = {
    FrameKind.ERROR: "error",
    FrameKind.RESULT: "result",
    FrameKind.NOTIFICATION: "notification",
    FrameKind.UNKNOWN: "unknown",
}


def transcript(record: RunRecord) -> list[Entry]:
    """Everything said and decided, in frame order."""
    raised = {i.seq: i for i in record.interrupts}
    decided = {i.decision_seq: i for i in record.interrupts if i.decision_seq is not None}
    out: list[Entry] = []
    for f in record.frames:
        # Audio the agent sent that could not be decoded is the agent's error (the bridge
        # records it as one): its own entry, a failure, never a turn someone spoke. Its
        # bytes, when kept, can be downloaded as they came.
        undecodable = f.audio is not None and f.kind is FrameKind.ERROR
        # A spoken turn is shown even when nothing was heard in it (an empty transcript): it
        # is the turn someone needs to listen to, and hiding it would hide what failed.
        spoken = f.audio is not None and f.kind is FrameKind.MESSAGE
        if (f.text and not undecodable) or spoken:
            who: Literal["customer", "agent"] = "customer" if f.direction == "sent" else "agent"
            out.append(Entry(f.seq, "message", who, f.text, f.phase, f.latency_ms))
        if f.seq in raised:
            out.append(Entry(f.seq, "checkpoint", detail=raised[f.seq].checkpoint))
        elif f.kind is FrameKind.NEEDS_HUMAN:  # raised but never logged (e.g. stopped first)
            out.append(Entry(f.seq, "checkpoint", detail=_field(f.payload, "checkpoint")))
        if f.seq in decided:
            interrupt = decided[f.seq]
            via = f" (as a {interrupt.via})" if interrupt.via else ""
            out.append(Entry(f.seq, "decision", detail=f"{interrupt.action}{via}"))
        elif f.kind is FrameKind.RESUME:
            out.append(Entry(f.seq, "decision", detail=_field(f.payload, "action")))
        if undecodable:
            reason = _field(f.payload, "detail") or short(f.payload)
            out.append(Entry(f.seq, "undecodable_audio", "agent", None, f.phase, detail=reason))
        elif f.kind in _MARKED:
            out.append(Entry(f.seq, _MARKED[f.kind], detail=short(f.payload)))
    return out


def connection(record: RunRecord) -> list[str]:
    """Transport events, as the report shows them: kind, close code, attempt, detail."""
    out: list[str] = []
    for e in record.transport_events:
        line: str = e.kind
        if e.close_code:
            line += f" (code {e.close_code})"
        if e.attempt:
            line += f" #{e.attempt}"
        if e.detail:
            line += f": {e.detail}"
        out.append(line)
    return out
