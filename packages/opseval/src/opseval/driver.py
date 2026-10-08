# SPDX-License-Identifier: Apache-2.0
"""The conversation driver: runs one attempt over one session.

It records everything and judges nothing (record first, judge later). Every exit path
ends in exactly one :class:`TerminalState`; harness problems end in
``transport_failure`` or ``harness_failure`` and never look like agent failures.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from opseval.errors import HarnessError, TransportError, TransportTimeout
from opseval.models.profile import FrameKind, Probe, SutProfile, render_frame
from opseval.models.record import (
    FrameRecord,
    InterruptRecord,
    ProbeResult,
    RedactionInfo,
    RunRecord,
    TerminalState,
)
from opseval.models.scenario import (
    CheckpointPolicy,
    Decision,
    DecisionAction,
    DecisionChannel,
    PolicyAction,
    Scenario,
)
from opseval.redaction import redact_text, redact_value
from opseval.simulator import Simulator
from opseval.transport.base import Session, Transport

SECRET_KEY_RE = re.compile(r"token|secret|password|api[_-]?key|authorization|signature", re.I)
REDACTED = "***redacted***"


def redact(value: Any) -> Any:
    """Redact credential-shaped keys anywhere in a payload."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and SECRET_KEY_RE.search(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class _Stop(Exception):
    def __init__(self, state: TerminalState, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


def _probe_problem(probe: Probe, payload: Any, kind: FrameKind) -> str | None:
    """Why the answer does not meet the probe's expectation, or ``None``.

    Field checks are a subset match: an agent may send more than the probe asks about.
    An empty expectation asserts that it answered *and did not error*: an agent replying
    "unknown_frame" has demonstrably not handled the probe, so treating that as a pass
    would make the loosest expectation the most misleading one. A probe that is *about*
    an error path says so with ``expect.kind: error``.
    """
    want = probe.expect
    if want.kind is not None and kind is not want.kind:
        return f"expected a {want.kind.value} frame, got {kind.value}"
    if want.kind is None and kind is FrameKind.ERROR:
        code = payload.get("code") if isinstance(payload, dict) else None
        detail = f" ({code})" if isinstance(code, str) else ""
        return (
            f"the agent answered with an error frame{detail}; if that is the expected "
            "answer, declare expect.kind: error"
        )
    if (want.fields or want.has_fields) and not isinstance(payload, dict):
        return f"expected an object with fields, got {type(payload).__name__}"
    if isinstance(payload, dict):
        for key in want.has_fields:
            if key not in payload:
                return f"missing field {key!r}"
        for key, expected in want.fields.items():
            if key not in payload:
                return f"missing field {key!r}"
            if payload[key] != expected:
                return f"field {key!r} is {payload[key]!r}, expected {expected!r}"
    return None


class Driver:
    """One attempt as a state machine.

    CONNECT -> PROBES -> OPENING -> (AWAIT_RESPONSE -> SIMULATE_TURN | CHECKPOINT)* -> TERMINAL

    Probes (FR-RUN-07) check the raw protocol once, before the conversation. They never
    abort it: a protocol fault and whatever it causes downstream are both worth seeing in
    one record, and `protocol.probes_passed` is what grades them.
    """

    def __init__(
        self,
        *,
        scenario: Scenario,
        profile: SutProfile,
        transport: Transport,
        simulator: Simulator,
        run_id: str,
        attempt_index: int,
        seed: int,
        allow_commit: bool = False,
    ) -> None:
        self.s = scenario
        self.p = profile
        self.transport = transport
        self.sim = simulator
        self.run_id = run_id
        self.attempt_index = attempt_index
        self.seed = seed
        self.allow_commit = allow_commit and scenario.conversation.allow_commit
        self._frames: list[FrameRecord] = []
        self._interrupts: list[InterruptRecord] = []
        self._probes: list[ProbeResult] = []
        self._notes: list[str] = []
        self._infra: list[str] = []
        self._injections: list[str] = []
        self._user_turns = 0
        self._occurrences: Counter[str] = Counter()
        self._rejections: Counter[str] = Counter()
        self._last_sent_at: float | None = None
        self._used = False
        self._redacted: list[str] = []

    # --- recording ------------------------------------------------------------------
    def _record(self, direction: str, payload: Any, kind: FrameKind, text: str | None) -> int:
        latency = None
        if direction == "received" and self._last_sent_at is not None:
            latency = round((time.monotonic() - self._last_sent_at) * 1000, 1)
        phase = None
        if direction == "received" and self.p.phase_field and isinstance(payload, dict):
            raw = payload.get(self.p.phase_field)
            phase = raw if isinstance(raw, str) else None
        seq = len(self._frames)
        # `redact` removes credential-shaped *keys*; `redact_value` removes credential
        # *values*, which is the only thing that can help a free-text turn (FR-REC-03).
        payload_clean, payload_kinds = redact_value(redact(payload))
        text_clean, text_kinds = (None, []) if text is None else redact_text(text)
        self._redacted.extend(payload_kinds + text_kinds)
        self._frames.append(
            FrameRecord(
                seq=seq,
                at=datetime.now(UTC),
                direction="sent" if direction == "sent" else "received",
                kind=kind,
                payload=payload_clean,
                latency_ms=latency,
                phase=phase,
                text=text_clean,
            )
        )
        return seq

    def transcript(self) -> list[tuple[str, str]]:
        return [
            ("customer" if f.direction == "sent" else "agent", f.text)
            for f in self._frames
            if f.text
        ]

    async def _send(
        self, session: Session, frame: dict[str, Any], kind: FrameKind, text: str | None
    ) -> int:
        await session.send(frame)
        self._last_sent_at = time.monotonic()
        return self._record("sent", frame, kind, text)

    async def _send_user(self, session: Session, text: str) -> None:
        if self._user_turns >= self.s.conversation.max_turns:
            raise _Stop(TerminalState.MAX_TURNS_EXHAUSTED, "turn budget spent")
        frame = render_frame(
            self.p.user_message_frame, {"text": text, "timezone": self.s.persona.timezone}
        )
        await self._send(session, frame, FrameKind.USER_MESSAGE, text)
        self._user_turns += 1

    # --- main loop -------------------------------------------------------------------
    async def run(self) -> RunRecord:
        if self._used:
            raise RuntimeError("a Driver runs exactly one attempt")
        self._used = True
        started = datetime.now(UTC)
        state, reason = TerminalState.HARNESS_FAILURE, "not started"
        session: Session | None = None
        try:
            async with asyncio.timeout(self.s.execution.attempt_timeout_seconds):
                try:
                    session = await self.transport.connect()
                    await self._converse(session)
                finally:
                    if session is not None:
                        await session.close()
        except _Stop as stop:
            state, reason = stop.state, stop.reason
        except TimeoutError:
            state, reason = TerminalState.TIMEOUT, "attempt deadline exceeded"
        except TransportTimeout as exc:
            state, reason = TerminalState.TIMEOUT, str(exc)
        except TransportError as exc:
            state, reason = TerminalState.TRANSPORT_FAILURE, str(exc)
            self._infra.append(f"transport: {exc}")
        except HarnessError as exc:
            state, reason = TerminalState.HARNESS_FAILURE, str(exc)
            self._infra.append(f"harness: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - any other bug is ours, never the agent's
            state, reason = TerminalState.HARNESS_FAILURE, f"{type(exc).__name__}: {exc}"
            self._infra.append(f"harness: unexpected {type(exc).__name__}: {exc}")
        return RunRecord(
            run_id=self.run_id,
            test_id=self.s.id,
            attempt_index=self.attempt_index,
            seed=self.seed,
            session_id=session.session_id if session else None,
            target=self.transport.target,
            started_at=started,
            ended_at=datetime.now(UTC),
            terminal_state=state,
            stop_reason=reason,
            frames=tuple(self._frames),
            probes=tuple(self._probes),
            interrupts=tuple(self._interrupts),
            user_turns=self._user_turns,
            persona=self.s.persona.model_dump(mode="json"),
            infra_errors=tuple(self._infra),
            notes=tuple(self._notes),
            planted_injections=tuple(self._injections),
            transport_events=session.transport_events() if session else (),
            redaction=RedactionInfo(applied=True, found=tuple(self._redacted)),
        )

    async def _probe(self, session: Session) -> None:
        """Send each declared probe and record what came back.

        A probe that goes unanswered within its own timeout is the agent failing to
        answer, which counts against it, so it is recorded as a failed probe rather than
        ending the attempt. A transport error is not caught here: that is the harness
        losing the session, and `run` turns it into an invalidated attempt.
        """
        for probe in self.p.probes:
            frame = render_frame(
                probe.send, {"session_id": session.session_id, "text": None, "timezone": None}
            )
            sent = await self._send(session, frame, FrameKind.UNKNOWN, None)
            try:
                payload = await session.receive(probe.timeout_seconds)
            except TransportTimeout:
                self._probes.append(
                    ProbeResult(
                        name=probe.name,
                        passed=False,
                        answered=False,
                        reason=f"no answer within {probe.timeout_seconds:g}s",
                        sent_seq=sent,
                    )
                )
                continue
            kind = self.p.classify(payload)
            text = payload.get(self.p.text_field) if isinstance(payload, dict) else None
            received = self._record(
                "received", payload, kind, text if isinstance(text, str) else None
            )
            problem = _probe_problem(probe, payload, kind)
            self._probes.append(
                ProbeResult(
                    name=probe.name,
                    passed=problem is None,
                    reason=problem,
                    sent_seq=sent,
                    received_seq=received,
                )
            )

    async def _converse(self, session: Session) -> None:
        await self._probe(session)
        turn = await self.sim.opening()
        self._injections += turn.injections
        await self._send_user(session, turn.text)
        while True:
            ended_by, frame = await self._agent_turn(session)
            if ended_by == "checkpoint":
                await self._checkpoint(session, frame)
                continue
            turn = await self.sim.next_turn(self.transcript())
            self._injections += turn.injections
            if turn.goal_reached:
                raise _Stop(TerminalState.GOAL_REACHED, "the customer considers the goal reached")
            await self._send_user(session, turn.text)

    async def _agent_turn(self, session: Session) -> tuple[str, Any]:
        """Read frames until the agent's turn ends. Returns (how it ended, last frame)."""
        first = True
        quiet = self.p.turn_quiet_ms / 1000
        while True:
            try:
                payload = await session.receive(
                    self.s.execution.turn_timeout_seconds if first else quiet
                )
            except TransportTimeout:
                if first:
                    raise _Stop(
                        TerminalState.TIMEOUT,
                        f"no reply within {self.s.execution.turn_timeout_seconds:.0f}s",
                    ) from None
                return "quiet", None
            kind = self.p.classify(payload)
            # Transport chatter (e.g. a late resume confirmation) is not the agent's reply.
            first = first and kind is FrameKind.HANDSHAKE
            text = payload.get(self.p.text_field) if isinstance(payload, dict) else None
            self._record("received", payload, kind, text if isinstance(text, str) else None)
            if kind is FrameKind.ERROR:
                raise _Stop(TerminalState.ERROR_FRAME, "the agent sent an error frame")
            if kind is FrameKind.RESULT:
                outcome = payload.get(self.p.outcome_field) if isinstance(payload, dict) else None
                if outcome is not None and outcome == self.p.committed_outcome:
                    raise _Stop(TerminalState.OUTCOME_COMMITTED, f"committed outcome {outcome!r}")
                continue
            if kind is FrameKind.NEEDS_HUMAN:
                return "checkpoint", payload
            if (
                kind is FrameKind.MESSAGE
                and self.p.turn_end_field
                and isinstance(payload, dict)
                and payload.get(self.p.turn_end_field)
            ):
                return "reply", payload
            # handshake, notification, unknown and mid-turn messages: keep listening

    def _policy(self, name: str) -> CheckpointPolicy:
        return self.s.conversation.checkpoint_policy.get(name, CheckpointPolicy())

    def _stop_checkpoint(self) -> str | None:
        stop_at = self.s.conversation.stop_at
        if stop_at == "commit_gate":
            return self.p.commit_gate
        if stop_at in ("outcome_committed", "goal_reached"):
            return None
        return stop_at

    async def _checkpoint(self, session: Session, payload: Any) -> None:
        name_raw = payload.get(self.p.checkpoint_field) if isinstance(payload, dict) else None
        name = name_raw if isinstance(name_raw, str) else "<unnamed>"
        if name not in self.p.checkpoints:
            self._notes.append(f"checkpoint {name!r} is not declared in the SUT profile")
        self._occurrences[name] += 1
        occurrence = self._occurrences[name]
        seq = len(self._frames) - 1
        body = payload.get(self.p.payload_field) if isinstance(payload, dict) else None
        policy = self._policy(name)

        def interrupt(decision: Decision | None = None, decision_seq: int | None = None) -> None:
            self._interrupts.append(
                InterruptRecord(
                    seq=seq,
                    checkpoint=name,
                    occurrence=occurrence,
                    payload=redact(body),
                    action=decision.action.value if decision else None,
                    feedback=decision.feedback if decision else None,
                    via=policy.via.value if decision else None,
                    expected_target_phase=policy.expected_target_phase,
                    decision_seq=decision_seq,
                )
            )

        if name == self._stop_checkpoint():
            interrupt()
            raise _Stop(TerminalState.STOPPED_AT_CHECKPOINT, f"stop_at {name}")
        if name == self.p.commit_gate and not self.allow_commit:
            interrupt()
            self._notes.append("commit_guard")
            raise _Stop(
                TerminalState.STOPPED_AT_CHECKPOINT,
                f"commit guard: refused to answer {name!r} (needs allow_commit and --allow-commit)",
            )

        if policy.action is PolicyAction.APPROVE:
            decision = Decision(action=DecisionAction.APPROVE)
        elif policy.action is PolicyAction.REJECT:
            decision = Decision(action=DecisionAction.REJECT, feedback=policy.feedback)
        elif policy.action is PolicyAction.SEQUENCE:
            decision = policy.sequence[min(occurrence, len(policy.sequence)) - 1]
        else:
            decision = await self.sim.decide(name, body, self.transcript())

        if decision.action is DecisionAction.REJECT:
            self._rejections[name] += 1
            if self._rejections[name] > policy.max_rejections:
                interrupt()
                raise _Stop(TerminalState.MAX_TURNS_EXHAUSTED, f"rejection loop guard at {name!r}")
        if self._user_turns >= self.s.conversation.max_turns:
            interrupt()
            raise _Stop(TerminalState.MAX_TURNS_EXHAUSTED, "turn budget spent")

        values = {
            "checkpoint": name,
            "action": decision.action.value,
            "feedback": decision.feedback,
            "timezone": self.s.persona.timezone,
        }
        text: str | None
        if decision.action is DecisionAction.REJECT and policy.via is DecisionChannel.USER_MESSAGE:
            text = decision.feedback or "No."
            frame = render_frame(self.p.user_message_frame, {**values, "text": text})
            kind = FrameKind.USER_MESSAGE
        else:
            frame = render_frame(self.p.resume_frame, values)
            kind = FrameKind.RESUME
            text = decision.feedback
        sent = await self._send(session, frame, kind, text)
        self._user_turns += 1
        interrupt(decision, sent)
