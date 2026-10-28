# SPDX-License-Identifier: Apache-2.0
"""The conversation driver: runs one attempt over one session.

It records everything and judges nothing (record first, judge later). Every exit path
ends in exactly one :class:`TerminalState`; harness problems end in
``transport_failure`` or ``harness_failure`` and never look like agent failures.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from opseval.errors import (
    HarnessError,
    RateLimited,
    SimulatorError,
    TransportError,
    TransportTimeout,
    TurnNeverEnded,
)
from opseval.models.profile import FrameKind, Probe, SutProfile, render_frame
from opseval.models.record import (
    SCRIPT_EXHAUSTED,
    AudioRef,
    CreatedArtifact,
    FrameRecord,
    InterruptRecord,
    ProbeResult,
    RedactionInfo,
    RunRecord,
    TerminalState,
    TranscriptInfo,
)
from opseval.models.scenario import (
    CheckpointPolicy,
    Decision,
    DecisionAction,
    DecisionChannel,
    PolicyAction,
    Scenario,
)
from opseval.paths import dig
from opseval.redaction import redact_frame, redact_text, redact_value
from opseval.simulator import Simulator
from opseval.transport.base import Session, Transport

T = TypeVar("T")

ARTIFACT_ID_MAX = 500
RATE_LIMITED_NOTE = "rate_limited"
"""Noted on a record whose attempt a rate limit ended (ADR-0035)."""

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
        simulator_timeout_seconds: float = 300.0,
    ) -> None:
        self.s = scenario
        self.p = profile
        self.transport = transport
        self.sim = simulator
        self.run_id = run_id
        self.attempt_index = attempt_index
        self.seed = seed
        self.allow_commit = allow_commit and scenario.conversation.allow_commit
        self.simulator_timeout = simulator_timeout_seconds
        self._deadline: asyncio.Timeout | None = None
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
        self._redacted: set[str] = set()
        self._session: Session | None = None

    # --- recording ------------------------------------------------------------------
    def _record(
        self,
        direction: str,
        payload: Any,
        kind: FrameKind,
        text: str | None,
        *,
        at: datetime | None = None,
        audio: AudioRef | None = None,
        transcript: TranscriptInfo | None = None,
    ) -> int:
        latency = None
        if direction == "received" and self._last_sent_at is not None:
            latency = round((time.monotonic() - self._last_sent_at) * 1000, 1)
        # A voice session knows what was spoken: the audio, its transcript, and the latency
        # on the attempt's own clock, which time spent in STT must not inflate (ADR-0053).
        media = self._session.frame_media(direction, payload) if self._session else None
        if media is not None:
            audio = media.audio or audio
            transcript = media.transcript or transcript
            at = media.at or at
            text = media.text if media.text is not None else text
            if direction == "received":
                # The session's own measure, or not measured at all (a barge-in): the
                # driver's clock would count the session's STT time as the agent's.
                latency = None if media.latency_ms is None else round(media.latency_ms, 1)
        phase = None
        if direction == "received" and self.p.phase_field and isinstance(payload, dict):
            raw = payload.get(self.p.phase_field)
            phase = raw if isinstance(raw, str) else None
        seq = len(self._frames)
        # `redact` removes credential-shaped *keys*; `redact_frame` removes credential
        # *values* from every free-text field (payload strings, text, and a voice turn's word
        # timings: FR-REC-03, ADR-0053), so no frame is kept without passing through it.
        frame, kinds = redact_frame(
            FrameRecord(
                seq=seq,
                at=at or datetime.now(UTC),
                direction="sent" if direction == "sent" else "received",
                kind=kind,
                payload=redact(payload),
                latency_ms=latency,
                phase=phase,
                text=text,
                audio=audio,
                transcript=transcript,
            )
        )
        self._redacted.update(kinds)
        self._frames.append(frame)
        return seq

    def transcript(self) -> list[tuple[str, str]]:
        return [
            ("customer" if f.direction == "sent" else "agent", f.text)
            for f in self._frames
            if f.text
        ]

    async def _send(
        self,
        session: Session,
        frame: dict[str, Any],
        kind: FrameKind,
        text: str | None,
        *,
        timeout: float,
        then: Callable[[int], None] | None = None,
    ) -> int:
        """Send a frame and record it at the moment it reached the agent (ADR-0042).

        Where ``send`` waits for the answer (``session.replies_in_send``: http-json,
        openai-chat, sse, python), a timestamp taken afterwards would place the frame after
        everything the agent did in reply: a tool it rightly ran after an approval would look
        as if it ran before it, and the agent would be failed for the harness's own clock. So
        there the frame is stamped when sending began, which also makes the reply's latency
        the real round trip. A streaming ``send`` (websocket) may first refresh a token or
        reconnect and resend, so there the frame reached the agent when ``send`` returned.

        ``then`` gets the frame's seq once it is recorded, also when the agent then failed to
        answer in time or the attempt deadline cut the wait short: the frame was sent, and the
        record must show what the agent was asked.
        """
        before = datetime.now(UTC), time.monotonic()

        def sent() -> int:
            at, clock = before if session.replies_in_send else (datetime.now(UTC), time.monotonic())
            self._last_sent_at = clock
            seq = self._record("sent", frame, kind, text, at=at)
            if then is not None:
                then(seq)
            return seq

        try:
            await session.send(frame, timeout=timeout)
        except (TransportTimeout, asyncio.CancelledError):
            sent()
            raise
        return sent()

    async def _send_user(self, session: Session, text: str) -> None:
        if self._user_turns >= self.s.conversation.max_turns:
            raise _Stop(TerminalState.MAX_TURNS_EXHAUSTED, "turn budget spent")
        frame = render_frame(
            self.p.user_message_frame, {"text": text, "timezone": self.s.persona.timezone}
        )

        def counted(_seq: int) -> None:
            self._user_turns += 1

        await self._send(
            session,
            frame,
            FrameKind.USER_MESSAGE,
            text,
            timeout=self.s.execution.turn_timeout_seconds,
            then=counted,
        )

    # --- main loop -------------------------------------------------------------------
    async def run(self) -> RunRecord:
        if self._used:
            raise RuntimeError("a Driver runs exactly one attempt")
        self._used = True
        started = datetime.now(UTC)
        state, reason = TerminalState.HARNESS_FAILURE, "not started"
        session: Session | None = None
        try:
            async with asyncio.timeout(self.s.execution.attempt_timeout_seconds) as deadline:
                self._deadline = deadline
                try:
                    session = await self.transport.connect()
                    self._session = session
                    session.use_harness_clock(self._off_clock)
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
            self._note_rate_limit(exc)
        except HarnessError as exc:
            state, reason = TerminalState.HARNESS_FAILURE, str(exc)
            self._infra.append(f"harness: {type(exc).__name__}: {exc}")
            self._note_rate_limit(exc)
        except Exception as exc:  # noqa: BLE001 - any other bug is ours, never the agent's
            state, reason = TerminalState.HARNESS_FAILURE, f"{type(exc).__name__}: {exc}"
            self._infra.append(f"harness: unexpected {type(exc).__name__}: {exc}")
        artifacts: tuple[CreatedArtifact, ...] = ()
        artifacts_checked = bool(self.p.artifacts)
        if artifacts_checked:
            try:
                artifacts = self._created_artifacts()
            except Exception as exc:  # noqa: BLE001 - a bug here must not lose the record
                artifacts_checked = False
                self._notes.append(f"created artifacts could not be read: {type(exc).__name__}")
        # Exception text can carry a credential (a URL with ?token=…): it is part of the
        # record, so it goes through the same value redaction as the conversation.
        reason = self._scrub(reason) if reason else reason
        infra = tuple(self._scrub(e) for e in self._infra)
        notes = tuple(self._scrub(n) for n in self._notes)
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
            infra_errors=infra,
            notes=notes,
            planted_injections=tuple(self._injections),
            transport_events=session.transport_events() if session else (),
            redaction=RedactionInfo(applied=True, found=tuple(sorted(self._redacted))),
            created_artifacts=artifacts,
            artifacts_checked=artifacts_checked,
            voice=session.voice_timeline() if session else None,
        )

    def _note_rate_limit(self, exc: Exception) -> None:
        """Say so when a rate limit ended the attempt, for ``stop_on_rate_limit``."""
        if isinstance(exc, RateLimited):
            self._notes.append(RATE_LIMITED_NOTE)

    @contextlib.asynccontextmanager
    async def _paused(self) -> AsyncIterator[None]:
        """Pause the attempt deadline for a harness call and move it back by however long
        the call took: the deadline measures the agent, and time spent waiting on the
        harness (the simulator, the voice bridge's TTS and STT) is not the agent's."""
        loop = asyncio.get_running_loop()
        when = self._deadline.when() if self._deadline is not None else None
        remaining = None if when is None else when - loop.time()
        if self._deadline is not None:
            self._deadline.reschedule(None)
        try:
            yield
        finally:
            if self._deadline is not None and remaining is not None:
                self._deadline.reschedule(loop.time() + remaining)

    async def _simulate(self, call: Awaitable[T]) -> T:
        """Run one simulator call off the agent's clock (the core rule).

        The call has its own limit instead, and overrunning it is a :class:`SimulatorError`:
        a harness failure, never the agent's ``TIMEOUT``.
        """
        async with self._paused():
            try:
                async with asyncio.timeout(self.simulator_timeout):
                    return await call
            except TimeoutError as exc:
                raise SimulatorError(
                    f"the simulator did not answer within {self.simulator_timeout:g}s "
                    "(OPSEVAL_SIMULATOR_TIMEOUT_SECONDS)"
                ) from exc

    async def _off_clock(self, call: Awaitable[T]) -> T:
        """Run one harness call (the voice bridge's TTS or STT) off the agent's clock, as
        :meth:`_simulate` does. The call has its own limit (``OPSEVAL_TTS_TIMEOUT_SECONDS``,
        ``OPSEVAL_STT_TIMEOUT_SECONDS``, in the speech guard), so it is never the agent's
        timeout (ADR-0053)."""
        async with self._paused():
            return await call

    def _created_artifacts(self) -> tuple[CreatedArtifact, ...]:
        """Ids of what the agent created, from the (already redacted) frames it sent.

        Probe replies are not the conversation and are left out. An id that redaction replaced
        is kept as an artifact whose id is unknown, one per frame, never merged into one.
        """
        probe_frames = {p.received_seq for p in self._probes if p.received_seq is not None}
        found: dict[tuple[str, str], CreatedArtifact] = {}
        for spec in self.p.artifacts:
            for frame in self._frames:
                if (
                    frame.direction != "received"
                    or frame.kind not in spec.frame_kinds
                    or frame.seq in probe_frames
                ):
                    continue
                try:
                    value = dig(frame.payload, spec.path)
                except KeyError:
                    continue
                if isinstance(value, bool) or not isinstance(value, str | int):
                    continue
                full = str(value)
                redacted = full == REDACTED or full.startswith("[redacted:")
                key = (spec.kind, f"{full}@{frame.seq}" if redacted else full)
                found.setdefault(
                    key,
                    CreatedArtifact(
                        kind=spec.kind,
                        id=full[:ARTIFACT_ID_MAX],
                        seq=frame.seq,
                        id_redacted=redacted,
                        id_truncated=len(full) > ARTIFACT_ID_MAX,
                    ),
                )
        return tuple(found.values())

    def _scrub(self, text: str) -> str:
        clean, kinds = redact_text(text)
        self._redacted.update(kinds)
        return clean

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
            seqs: list[int] = []
            try:
                # On a request/response transport the answer is awaited in `send` itself.
                await self._send(
                    session,
                    frame,
                    FrameKind.UNKNOWN,
                    None,
                    timeout=probe.timeout_seconds,
                    then=seqs.append,
                )
                payload = await session.receive(probe.timeout_seconds)
            except TransportTimeout:
                self._probes.append(
                    ProbeResult(
                        name=probe.name,
                        passed=False,
                        answered=False,
                        reason=f"no answer within {probe.timeout_seconds:g}s",
                        sent_seq=seqs[0] if seqs else None,
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
                    sent_seq=seqs[0],
                    received_seq=received,
                )
            )

    async def _converse(self, session: Session) -> None:
        await self._probe(session)
        turn = await self._simulate(self.sim.opening())
        self._injections += turn.injections
        await self._send_user(session, turn.text)
        while True:
            ended_by, frame = await self._agent_turn(session)
            if ended_by == "checkpoint":
                await self._checkpoint(session, frame)
                continue
            turn = await self._simulate(self.sim.next_turn(self.transcript()))
            self._injections += turn.injections
            if turn.script_exhausted:
                self._notes.append(SCRIPT_EXHAUSTED)
                raise _Stop(TerminalState.GOAL_REACHED, "the scripted turns ran out")
            if turn.goal_reached:
                raise _Stop(TerminalState.GOAL_REACHED, "the customer considers the goal reached")
            await self._send_user(session, turn.text)

    async def _agent_turn(self, session: Session) -> tuple[str, Any]:
        """Read frames until the agent's turn ends. Returns (how it ended, last frame).

        Until the agent has *answered* (``SutProfile.answers``), the turn's own deadline applies;
        after that, the quiet window ends the turn. A handshake, a typing notification or a frame
        with no words is not an answer: before, any of them started the quiet timer, the customer
        replied before the agent had, and the agent's answer was filed under the next turn (I12).
        On a request/response transport the whole reply is already here, so the first frame
        that is not transport chatter ends the wait, as it always has.
        """
        loop = asyncio.get_running_loop()
        turn = self.s.execution.turn_timeout_seconds
        deadline = loop.time() + turn
        waiting = True
        quiet = self.p.turn_quiet_ms / 1000
        while True:
            try:
                payload = await session.receive(
                    max(0.0, deadline - loop.time()) if waiting else quiet
                )
            except TurnNeverEnded as exc:
                raise _Stop(TerminalState.TIMEOUT, str(exc)) from None
            except TransportTimeout:
                if waiting:
                    raise _Stop(TerminalState.TIMEOUT, f"no reply within {turn:.0f}s") from None
                return "quiet", None
            kind = self.p.classify(payload)
            text = payload.get(self.p.text_field) if isinstance(payload, dict) else None
            text = text if isinstance(text, str) else None
            # Transport chatter (e.g. a late resume confirmation) is never the agent's reply.
            if waiting and kind is not FrameKind.HANDSHAKE:
                waiting = not (
                    session.replies_in_send
                    or self.p.answers(kind, text, payload)
                    or session.ends_turn(payload)
                )
            self._record("received", payload, kind, text)
            if self._interjected(session):
                # The caller talked over the agent (a planted barge-in, ADR-0055): that is a
                # customer turn, and the agent owes it an answer within the turn's time.
                waiting = True
                deadline = loop.time() + turn
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

    def _interjected(self, session: Session) -> bool:
        """Record the customer turns the session spoke on its own (the duplex voice driver's
        barge-ins), in the order they happened, right after the agent frame they cut into."""
        spoken = session.caller_interjections()
        for frame, text in spoken:
            self._last_sent_at = time.monotonic()
            self._record("sent", frame, FrameKind.USER_MESSAGE, text)
            self._user_turns += 1
        return bool(spoken)

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
                    payload=redact_value(redact(body))[0],
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
            decision = await self._simulate(self.sim.decide(name, body, self.transcript()))

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

        def answered(sent: int) -> None:
            # Recorded even when the agent then fails to answer in time: the decision was
            # sent, and a tool the agent ran on it must be checked against it.
            self._user_turns += 1
            interrupt(decision, sent)

        await self._send(
            session,
            frame,
            kind,
            text,
            timeout=self.s.execution.turn_timeout_seconds,
            then=answered,
        )
