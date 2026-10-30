# SPDX-License-Identifier: Apache-2.0
"""The full-duplex voice driver (ADR-0055): both parties can make sound at once.

The turn-based bridge (:mod:`opseval.voice.bridge`) sends the caller's audio as fast as the
line takes it and only then listens. :class:`DuplexBridge` schedules the same call on a
timeline instead:

- **The line is stamped all the time.** A ``duplex`` session stamps every agent event on
  arrival with its own reader, on the attempt's monotonic clock, while the caller speaks, the
  simulator thinks and the STT works; the driver reads those stamps, so its own work never
  moves the agent's times (and runs beside the agent's path: ``VoiceSpan.holds_line``).
- **The caller speaks in real time.** Its audio is paced chunk by chunk on the attempt's
  clock, so its span is how long it really talked, and overlap is real overlap.
- **The caller can interrupt.** ``voice.barge_in`` plants an interruption ``after_ms`` into an
  agent turn: the phrase (spoken and mixed before the call, so no TTS runs on the agent's
  time) is streamed over the agent while the agent is still read. The agent's turn ends when
  it stops talking (its own end-of-speech event, which after an interruption is taken as its
  play-back stopping, or silence); the interruption becomes a customer turn in the record
  (:meth:`caller_interjections`), right after everything the agent sent before it ended, and
  the agent owes it an answer that begins after it, like any other turn.
- **Audio plays out.** An agent turn is not over until the audio already sent has had time to
  play (a chunk cannot play before it arrives, nor over the one before it): a caller that
  started talking over buffered agent audio would be the harness barging in.

Everything else is the bridge's: the same session, providers, STT decision and canary,
redaction, recording and record. The text driver still runs the conversation (checkpoints,
the commit guard, the simulator), so ADR-0008 holds unchanged. ADR-0055 placed the duplex
driver in ``voice/driver.py`` driving the session directly; it is a bridge subclass instead,
so the two schedulers cannot drift apart on anything but scheduling.
"""

from __future__ import annotations

import asyncio
from typing import Any

from opseval.audio import duration_ms
from opseval.errors import ConfigError, TransportError, TransportTimeout
from opseval.models.profile import SutProfile, render_frame
from opseval.models.voice import BargeIn, VoiceSpec
from opseval.redaction import redact_text
from opseval.transport.audio import DUPLEX, AudioSession, Hangup, VoiceEvent
from opseval.transport.base import FrameMedia
from opseval.voice.bridge import SpeechKit, VoiceBridge, _Turn
from opseval.voice.metrics import PAUSE_FLOOR_MS, PlannedBargeIn
from opseval.voice.recording import AudioRecorder

CUT_YIELD_MS = 1_000.0
"""An interrupted agent turn counts as cut short only when the agent stopped within this long
of the caller cutting in. One that kept talking was not cut: if no STT can read it, that is the
agent's unintelligibility, decided by the canary like any turn (review of #91)."""

CUT_FRAGMENT_MS = 2_000.0
"""...and only when it said at most this much before it stopped: a longer stretch is enough
for any STT, so an unreadable one is not excused as a fragment."""

_MIN_WAIT = 0.001
"""The shortest read: a zero timeout would never hand over an event already queued on some
Python versions (``asyncio.wait_for`` cancels before the getter runs)."""

_DRAIN_WAIT = 0.05


class DuplexBridge(VoiceBridge):
    mode = "duplex"
    holds_line = False

    def __init__(
        self,
        audio: AudioSession,
        profile: SutProfile,
        kit: SpeechKit,
        recorder: AudioRecorder,
        *,
        language: str,
        voice: VoiceSpec | None = None,
    ) -> None:
        if DUPLEX not in audio.capabilities:
            raise ConfigError(
                f"{type(audio).__name__} cannot run full duplex: it hears the caller only once "
                "a turn is over. Use a streaming transport (audio-ws) for voice.duplex and "
                "voice.barge_in"
            )
        super().__init__(audio, profile, kit, recorder, language=language, voice=voice)
        self._plans: dict[int, tuple[BargeIn, PlannedBargeIn]] = {}
        for plan in self.voice.barge_in:
            planned = PlannedBargeIn(agent_turn=plan.agent_turn, after_ms=plan.after_ms)
            self._log.barge_ins.append(planned)
            self._plans[plan.agent_turn] = (plan, planned)
        self._phrases: dict[int, tuple[bytes, bytes]] = {}
        self._prepared = False
        self._firing: asyncio.Task[None] | None = None
        self._barged: _Turn | None = None
        self._barged_at = 0.0
        self._playout = 0.0
        """Where the agent's audio has played out to, on the attempt's clock: a chunk cannot
        play before it arrives, nor over the one before it. Re-anchored when the caller starts
        a turn, so audio left unplayed then never delays the agent's next turn."""
        self._voiced_end = 0.0
        """Where the current agent turn's last voiced chunk ends, on the play-out clock."""
        self._floor: float | None = None
        """When the caller's latest interruption ended: a data message that arrived before it
        cannot be the answer to it."""
        self._interjections: list[tuple[dict[str, Any], str]] = []
        self._interjection_media: list[tuple[dict[str, Any], FrameMedia]] = []
        self._broken: BaseException | None = None
        """An interruption that could not be finished for a reason other than the agent hanging
        up: raised by the next read or send, after the turn it cut into was handed over."""

    # --- the line --------------------------------------------------------------------------
    async def _next_event(self, wait: float) -> VoiceEvent:
        if self._held:
            return self._held.popleft()
        return await self.audio.next_event(max(wait, _MIN_WAIT))

    # --- the caller, in real time --------------------------------------------------------------
    async def _stream(self, pcm: bytes) -> None:
        """Send the caller's audio paced on the attempt's clock, one chunk per ``chunk_ms``."""
        step = max(2, self.rate * self.cfg.chunk_ms // 1000 * 2)
        began = self.audio.clock.now_ms()
        for k, i in enumerate(range(0, len(pcm), step)):
            ahead = began + k * self.cfg.chunk_ms - self.audio.clock.now_ms()
            if ahead > 0:
                await asyncio.sleep(ahead / 1000)
            await self.audio.send_audio(pcm[i : i + step])
        # The caller's turn is over when its last chunk has played, not when it was sent:
        # an agent answering inside that chunk would otherwise be talking over the caller.
        rest = began + duration_ms(pcm, self.rate) - self.audio.clock.now_ms()
        if rest > 0:
            await asyncio.sleep(rest / 1000)

    def _sent_ms(self, pcm: bytes) -> float:
        return duration_ms(pcm, self.rate)

    async def send(self, frame: dict[str, Any], *, timeout: float | None = None) -> None:
        if self._broken is not None and not self._hung_up:
            raise self._broken
        if not self._prepared:
            # Every planted interruption is spoken (and mixed) before the caller's first
            # word: no TTS ever runs while the agent is the one expected to speak.
            self._prepared = True
            for number, (plan, _) in sorted(self._plans.items()):
                clean = await self._say(plan.say)
                self._phrases[number] = (clean, await self._mix(clean, 1_000 + number))
        # A new caller turn: audio the agent left unplayed no longer delays its next turn.
        self._playout = self.audio.clock.now_ms()
        self._floor = None
        # The caller talks in real time here: that is the caller's time, not the agent's, so
        # the attempt deadline is paused for it like for the simulator (ADR-0042).
        await self._clock(super().send(frame, timeout=timeout))

    async def receive(self, timeout: float) -> Any:
        if not self._queue and self._broken is not None and not self._hung_up:
            raise self._broken
        return await super().receive(timeout)

    # --- the agent's turn ---------------------------------------------------------------------
    def _played(self, turn: _Turn, at_ms: float, pcm: bytes, voiced: bool) -> None:
        """Lay a chunk on the play-out clock: it plays when it arrived, or when the chunk
        before it has finished, whichever is later."""
        turn.last_ms = at_ms
        start = max(at_ms, self._playout)
        end = start + duration_ms(pcm, self.rate)
        self._playout = end
        if not voiced:
            return
        if turn.stretches and start - turn.stretches[-1][1] < PAUSE_FLOOR_MS:
            turn.stretches[-1][1] = end
        else:
            turn.stretches.append([start, end])
        self._voiced_end = turn.stretches[-1][1]

    def _burst(self, turn: _Turn) -> bool:
        return False  # placed on the play-out clock: its pauses are where they play

    def _number(self, turn: _Turn) -> int:
        """The agent turn number this turn will be recorded under."""
        return turn.closed if turn.closed is not None else len(self._log.agents) + 1

    def _due(self, turn: _Turn) -> float | None:
        now = self.audio.clock.now_ms()
        waits = []
        silence = 0.0 if turn.done else self.cfg.end_of_turn_silence_ms
        if self._voiced_end + silence > now:
            waits.append((self._voiced_end + silence - now) / 1000)
        found = self._plans.get(self._number(turn))
        if found is not None and self._firing is None and turn.stretches:
            plan, planned = found
            if planned.fired_at_ms is None:
                # Against the play-out clock: into the agent's speech as the caller hears it,
                # however fast its audio arrived.
                fire_at = turn.stretches[0][0] + plan.after_ms
                if now >= fire_at:
                    # Only over speech: into silence it would interrupt nothing (and the
                    # yield would be not measured), so it waits for the agent to speak on.
                    if now - self._voiced_end < PAUSE_FLOOR_MS:
                        self._fire(turn, plan, planned)
                else:
                    waits.append((fire_at - now) / 1000)
        return min(waits) if waits else None

    def _playing(self, turn: _Turn) -> bool:
        silence = 0.0 if turn.done else self.cfg.end_of_turn_silence_ms
        return self.audio.clock.now_ms() < self._voiced_end + silence

    def _speech_ended(self, turn: _Turn, at_ms: float) -> bool:
        if turn is self._barged and turn.stretches and at_ms >= self._barged_at:
            # After an interruption, the agent's end of speech is its play-back stopping (a
            # "clear" on most voice protocols): what it had sent and not yet played is gone.
            last = turn.stretches[-1]
            last[1] = max(last[0], min(last[1], at_ms))
            self._voiced_end = last[1]
            self._playout = min(self._playout, max(at_ms, last[1]))
        return self.audio.clock.now_ms() >= self._voiced_end

    def _stale(self, at_ms: float) -> bool:
        return self._floor is not None and at_ms < self._floor

    def _fire(self, turn: _Turn, plan: BargeIn, planned: PlannedBargeIn) -> None:
        self._barged = turn
        self._barged_at = planned.fired_at_ms = self.audio.clock.now_ms()
        self._firing = asyncio.create_task(self._interject(plan, planned))

    async def _interject(self, plan: BargeIn, planned: PlannedBargeIn) -> None:
        clean, sent = self._phrases[plan.agent_turn]
        start = self.audio.clock.now_ms()
        self._barged_at = planned.fired_at_ms = start
        self._event(start, "speech_start", "caller")
        await self._stream(sent)
        media, number = await self._caller_done(
            plan.say, clean, sent, start, timeout=None, barge_in=True
        )
        self._sent = None  # recorded through caller_interjections, not send()
        assert self._caller_end_ms is not None  # nosec B101 - set by _caller_done
        planned.ended_at_ms = self._floor = self._caller_end_ms
        # "(turn N)": the caller's file number, as the bridge tags an agent's barge-in.
        self._event(
            start,
            "barge_in",
            "caller",
            f"the caller cut in on agent turn {plan.agent_turn} (turn {number})",
        )
        frame = render_frame(self.profile.user_message_frame, {"text": plan.say, "timezone": None})
        self._interjections.append((frame, plan.say))
        self._interjection_media.append((frame, media))

    def _yielded(self, turn: _Turn) -> bool:
        return (
            turn is self._barged
            and bool(turn.stretches)
            and self._voiced_end - self._barged_at <= CUT_YIELD_MS
        )

    def _cut(self, turn: _Turn) -> bool:
        spoke = sum(end - start for start, end in turn.stretches)
        return self._yielded(turn) and spoke <= CUT_FRAGMENT_MS

    def _turn_closed(self, turn: _Turn, number: int) -> None:
        found = self._plans.get(number)
        if found is None or found[1].fired_at_ms is not None or turn.voiced_ms is None:
            return
        plan, planned = found
        spoke = self._voiced_end - (turn.stretches[0][0] if turn.stretches else turn.voiced_ms)
        planned.reason = (
            f"agent turn {number} stopped speaking {spoke:.0f} ms into its speech, before the "
            f"planned interruption at {plan.after_ms} ms"
        )

    async def _finish(self, turn: _Turn) -> Any:
        try:
            # The agent's (cut) turn is transcribed while the caller is still finishing its
            # interruption, so the STT works in the caller's time, not the agent's.
            result = await super()._finish(turn)
        except BaseException:
            # The turn's own failure (an SttError: Opseval's) is what ended it; the
            # interruption's outcome is only noted, never put in its place.
            await self._settle()
            raise
        await self._settle()
        return result

    async def _settle(self) -> None:
        """Wait for the interruption to finish, and keep its failure from replacing the turn's
        result (review of #91). The agent hanging up during it is the agent ending the call,
        as for any hang-up; any other failure is noted and raised by the next read or send,
        after the turn it cut into was handed over."""
        firing, self._firing = self._firing, None
        self._voiced_end = 0.0
        if firing is None:
            return
        (outcome,) = await asyncio.gather(firing, return_exceptions=True)
        if not isinstance(outcome, BaseException):
            return
        what, _ = redact_text(f"{type(outcome).__name__}: {outcome}")
        self._event(
            self.audio.clock.now_ms(),
            "barge_in",
            "caller",
            f"the caller's interruption could not be finished ({what})",
        )
        if isinstance(outcome, TransportError) and await self._hangup_behind():
            return
        if isinstance(outcome, Exception):
            self._broken = outcome

    async def _hangup_behind(self) -> bool:
        """Whether the agent hung up before the line failed: its events still queued on the
        session are read (and kept for the next turn), and a hang-up among them ends the call
        as the agent's (the customer's next turn is then unheard: TIMEOUT)."""
        if self._hung_up:
            return True
        kept: list[VoiceEvent] = []
        try:
            while True:
                event = await self.audio.next_event(_DRAIN_WAIT)
                if isinstance(event, Hangup):
                    self._event(event.at_ms, "hangup", event.by)
                    self._hung_up = True
                    note = self._notification("hangup", by=event.by)
                    self._enders.append(note)
                    self._queue.append(note)
                    return True
                kept.append(event)
        except (TransportTimeout, TransportError):
            return False
        finally:
            self._held.extend(kept)

    # --- Session hooks -------------------------------------------------------------------------
    def caller_interjections(self) -> list[tuple[dict[str, Any], str]]:
        """The interruption is released once everything the agent sent before it ended has
        been handed over, so the record shows the cut turn first, and none of those frames
        can be taken for the answer to it (review of #91)."""
        if self._queue:
            return []
        found, self._interjections = self._interjections, []
        return found

    def frame_media(self, direction: str, payload: Any) -> FrameMedia | None:
        if direction == "sent":
            for i, (kept, media) in enumerate(self._interjection_media):
                if kept is payload:
                    del self._interjection_media[i]
                    return media
        return super().frame_media(direction, payload)

    async def close(self) -> None:
        task, self._firing = self._firing, None
        try:
            if task is not None and not task.done():
                current = asyncio.current_task()
                before = current.cancelling() if current is not None else 0
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    # Only the cancellation this close asked for is swallowed: one from
                    # outside (the attempt being cancelled) goes on (review of #91).
                    if current is not None and current.cancelling() > before:
                        raise
                except Exception:  # noqa: BLE001, S110 - closing: its failure is moot  # nosec B110
                    pass
            elif task is not None and not task.cancelled():
                task.exception()  # retrieved, so never "exception was never retrieved"
        finally:
            await super().close()


__all__ = ["CUT_FRAGMENT_MS", "CUT_YIELD_MS", "DuplexBridge"]
