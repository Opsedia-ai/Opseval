# SPDX-License-Identifier: Apache-2.0
"""A record as a conversation: one mapping for the report and the console."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from opseval.driver import Driver
from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import (
    FrameRecord,
    InterruptRecord,
    RunRecord,
    TerminalState,
    TransportEvent,
)
from opseval.simulator import ScriptedSimulator
from opseval.transcript import connection, transcript
from opseval.transport import HttpJsonTransport

from .conftest import make_scenario

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def frame(
    seq: int, direction: str, kind: FrameKind, text: str | None = None, **payload: Any
) -> FrameRecord:
    return FrameRecord(seq=seq, at=NOW, direction=direction, kind=kind, payload=payload, text=text)  # type: ignore[arg-type]


def record(frames: list[FrameRecord], **kw: Any) -> RunRecord:
    return RunRecord(
        run_id="r",
        test_id="t",
        attempt_index=0,
        seed=1,
        target="x",
        started_at=NOW,
        ended_at=NOW,
        terminal_state=TerminalState.ERROR_FRAME,
        frames=tuple(frames),
        **kw,
    )


def test_an_error_with_text_is_still_an_error() -> None:
    """Hiding an agent failure as an ordinary turn is the inverse of the core rule."""
    entries = transcript(
        record([frame(0, "received", FrameKind.ERROR, "Internal failure", code=500)])
    )
    assert [e.kind for e in entries] == ["message", "error"]
    assert entries[0].text == "Internal failure" and '"code": 500' in (entries[1].detail or "")


def test_checkpoints_and_decisions_come_from_the_interrupt_log() -> None:
    """A profile naming the checkpoint differently, and a rejection sent as a customer
    message, still show what was raised and decided."""
    frames = [
        frame(0, "received", FrameKind.NEEDS_HUMAN, interrupt_type="proposal"),
        frame(1, "sent", FrameKind.USER_MESSAGE, "No, refund the card."),
    ]
    interrupts = (
        InterruptRecord(
            seq=0,
            checkpoint="proposal_approval",
            occurrence=1,
            action="reject",
            via="user_message",
            decision_seq=1,
        ),
    )
    entries = transcript(record(frames, interrupts=interrupts))
    assert [(e.kind, e.detail) for e in entries if e.kind != "message"] == [
        ("checkpoint", "proposal_approval"),
        ("decision", "reject (as a user_message)"),
    ]


def test_an_unclassified_frame_is_kept() -> None:
    entries = transcript(record([frame(0, "received", FrameKind.UNKNOWN, weird=True)]))
    assert entries[0].kind == "unknown" and "weird" in (entries[0].detail or "")


def test_connection_events_carry_their_detail() -> None:
    events = (
        TransportEvent(at=NOW, kind="resume_failed", attempt=1, detail="403"),
        TransportEvent(at=NOW, kind="dropped", close_code=1006),
    )
    assert connection(record([], transport_events=events)) == [
        "resume_failed #1: 403",
        "dropped (code 1006)",
    ]


async def test_exception_text_is_redacted_in_the_record(profile: SutProfile) -> None:
    secret = "sk-ant-api03-" + "A" * 40

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused: https://agent.example/chat?key={secret}")

    s = make_scenario()
    rec = await Driver(
        scenario=s,
        profile=profile,
        transport=HttpJsonTransport(
            "http://agent.local/chat",
            profile,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        ),
        simulator=ScriptedSimulator(s, 1),
        run_id="r",
        attempt_index=0,
        seed=1,
    ).run()
    dumped = rec.model_dump_json()
    assert secret not in dumped
    assert rec.infra_errors and rec.redaction.found
