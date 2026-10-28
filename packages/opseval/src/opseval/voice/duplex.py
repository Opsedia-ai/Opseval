# SPDX-License-Identifier: Apache-2.0
"""The full-duplex voice driver (ADR-0055): both parties can make sound at once.

The turn-based bridge (:mod:`opseval.voice.bridge`) sends the caller's audio as fast as the
line takes it and only then listens. :class:`DuplexBridge` schedules the same call on a
timeline instead:

- **The line is read all the time.** A reader task pulls every agent event off the session
  (stamped on arrival by the transport, on the attempt's monotonic clock) into an inbox, while
  the caller speaks, while the simulator thinks and while the STT works.
- **The caller speaks in real time.** Its audio is paced chunk by chunk on the attempt's
  clock, so its span is how long it really talked, and overlap is real overlap.
- **The caller can interrupt.** ``voice.barge_in`` plants an interruption ``after_ms`` into an
  agent turn: the phrase (spoken and mixed before the call, so no TTS runs on the agent's
  time) is streamed over the agent while the agent is still read. The agent's turn ends when
  it stops talking (its own end-of-speech event, which after an interruption is taken as its
  play-back stopping, or silence); the interruption becomes a customer turn in the record
  (:meth:`caller_interjections`), and the agent owes it an answer like any other turn.
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
import contextlib
from typing import Any

from opseval.audio import duration_ms
from opseval.errors import ConfigError, TransportTimeout
from opseval.models.profile import SutProfile, render_frame
from opseval.models.voice import BargeIn, VoiceSpec
from opseval.transport.audio import DUPLEX, AudioSession, VoiceEvent
from opseval.transport.base import FrameMedia
from opseval.voice import noise as noises
from opseval.voice.bridge import SpeechKit, VoiceBridge, _Turn
from opseval.voice.metrics import PAUSE_FLOOR_MS, PlannedBargeIn
from opseval.voice.recording import AudioRecorder

_INBOX_WAIT = 3_600.0
"""How long the reader waits for one event before simply waiting again: the attempt's own
deadline, not this, bounds the call."""


class DuplexBridge(VoiceBridge):
    mode = "duplex"

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
        self._inbox: asyncio.Queue[VoiceEvent | BaseException] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._plans: dict[int, tuple[BargeIn, PlannedBargeIn]] = {}
        for plan in self.voice.barge_in:
            planned = PlannedBargeIn(agent_turn=plan.agent_turn, after_ms=plan.after_ms)
            self._log.barge_ins.append(planned)
            self._plans[plan.agent_turn] = (plan, planned)
        self._phrases: dict[int, tuple[bytes, bytes]] = {}
        self._prepared = False
        self._firing: asyncio.Task[None] | None = None
        self._barged: _Turn | None = None
        self._voiced_end = 0.0
        """Where the current agent turn's last voiced chunk ends, on the play-out clock."""
        self._interjections: list[tuple[dict[str, Any], str]] = []
        self._interjection_media: list[tuple[dict[str, Any], FrameMedia]] = []

    # --- the line, read all the time --------------------------------------------------------
    def _listen(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        while True:
            try:
                event = await self.audio.next_event(_INBOX_WAIT)
            except TransportTimeout:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - handed to whoever reads next
                self._inbox.put_nowait(exc)
                return
            self._inbox.put_nowait(event)

    async def _next_event(self, wait: float) -> VoiceEvent:
        self._listen()
        if self._held:
            return self._held.popleft()
        try:
            item = self._inbox.get_nowait()
        except asyncio.QueueEmpty:
            if wait <= 0:
                raise TransportTimeout("nothing from the agent") from None
            try:
                item = await asyncio.wait_for(self._inbox.get(), timeout=wait)
            except TimeoutError as exc:
                raise TransportTimeout(f"nothing from the agent within {wait:.2f}s") from exc
        if isinstance(item, BaseException):
            self._inbox.put_nowait(item)  # a closed line stays closed for every later read
            raise item
        return item

    # --- the caller, in real time --------------------------------------------------------------
    async def _stream(self, pcm: bytes) -> None:
        """Send the caller's audio paced on the attempt's clock, one chunk per ``chunk_ms``."""
        self._listen()
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
        if not self._prepared:
            # Every planted interruption is spoken (and mixed) before the caller's first
            # word: no TTS ever runs while the agent is the one expected to speak.
            self._prepared = True
            for number, (plan, _) in sorted(self._plans.items()):
                clean = await self._say(plan.say)
                sent = clean
                if self.voice.noise is not None:
                    sent = noises.mix(clean, self.rate, self.voice.noise, turn=1_000 + number)
                self._phrases[number] = (clean, sent)
        # The caller talks in real time here: that is the caller's time, not the agent's, so
        # the attempt deadline is paused for it like for the simulator (ADR-0042).
        await self._clock(super().send(frame, timeout=timeout))

    # --- the agent's turn ---------------------------------------------------------------------
    def _played(self, turn: _Turn, at_ms: float, pcm: bytes, voiced: bool) -> None:
        super()._played(turn, at_ms, pcm, voiced)
        if voiced and turn.stretches:
            self._voiced_end = turn.stretches[-1][1]

    def _number(self, turn: _Turn) -> int:
        """The agent turn number this turn will be recorded under."""
        return turn.closed if turn.closed is not None else len(self._log.agents) + 1

    def _due(self, turn: _Turn) -> float | None:
        now = self.audio.clock.now_ms()
        waits = []
        silence = 0.0 if turn.said_end else self.cfg.end_of_turn_silence_ms
        if self._voiced_end + silence > now:
            waits.append((self._voiced_end + silence - now) / 1000)
        found = self._plans.get(self._number(turn))
        if found is not None and self._firing is None and turn.voiced_ms is not None:
            plan, planned = found
            if planned.fired_at_ms is None:
                fire_at = turn.voiced_ms + plan.after_ms
                if now >= fire_at:
                    # Only over speech: into silence it would interrupt nothing (and the
                    # yield would be not measured), so it waits for the agent to speak on.
                    if now - self._voiced_end < PAUSE_FLOOR_MS:
                        self._fire(turn, plan, planned)
                else:
                    waits.append((fire_at - now) / 1000)
        return min(waits) if waits else None

    def _playing(self, turn: _Turn) -> bool:
        silence = 0.0 if turn.said_end else self.cfg.end_of_turn_silence_ms
        return self.audio.clock.now_ms() < self._voiced_end + silence

    def _speech_ended(self, turn: _Turn, at_ms: float) -> bool:
        if turn is self._barged and turn.stretches:
            # After an interruption, the agent's end of speech is its play-back stopping (a
            # "clear" on most voice protocols): what it had sent and not yet played is gone.
            last = turn.stretches[-1]
            last[1] = max(last[0], min(last[1], at_ms))
            self._voiced_end = last[1]
            self._playout = min(self._playout, max(at_ms, last[1]))
        return self.audio.clock.now_ms() >= self._voiced_end

    def _fire(self, turn: _Turn, plan: BargeIn, planned: PlannedBargeIn) -> None:
        self._barged = turn
        planned.fired_at_ms = self.audio.clock.now_ms()
        self._firing = asyncio.create_task(self._interject(plan, planned))

    async def _interject(self, plan: BargeIn, planned: PlannedBargeIn) -> None:
        clean, sent = self._phrases[plan.agent_turn]
        start = self.audio.clock.now_ms()
        planned.fired_at_ms = start
        self._event(start, "speech_start", "caller")
        await self._stream(sent)
        media, number = await self._caller_done(
            plan.say, clean, sent, start, timeout=None, barge_in=True
        )
        self._sent = None  # recorded through caller_interjections, not send()
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

    def _cut(self, turn: _Turn) -> bool:
        return turn is self._barged

    def _turn_closed(self, turn: _Turn, number: int) -> None:
        found = self._plans.get(number)
        if found is None or found[1].fired_at_ms is not None or turn.voiced_ms is None:
            return
        plan, planned = found
        spoke = self._voiced_end - turn.voiced_ms
        planned.reason = (
            f"agent turn {number} stopped speaking {spoke:.0f} ms into its speech, before the "
            f"planned interruption at {plan.after_ms} ms"
        )

    async def _finish(self, turn: _Turn) -> Any:
        try:
            # The agent's (cut) turn is transcribed while the caller is still finishing its
            # interruption, so the STT works in the caller's time, not the agent's.
            return await super()._finish(turn)
        finally:
            firing, self._firing = self._firing, None
            if firing is not None:
                await firing
            self._voiced_end = 0.0

    # --- Session hooks -------------------------------------------------------------------------
    def caller_interjections(self) -> list[tuple[dict[str, Any], str]]:
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
        for task in (self._firing, self._reader):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        await super().close()


__all__ = ["DuplexBridge"]
