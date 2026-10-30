# SPDX-License-Identifier: Apache-2.0
"""The full-duplex voice driver and the voice metrics (ADR-0055).

Both halves of the one rule, for duplex: the harness's pauses (TTS, STT, the simulator) and
the caller's own planted silences and barge-ins are never the agent's latency, dead air or
talk-over, and an agent that keeps talking over the caller fails. The agent is an in-process
:class:`AudioSession` that speaks tones in real time and listens while it speaks; the speech
providers are the bridge tests' tone fakes behind the real guards.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from opseval.audio import AudioFormat, has_energy, level_dbfs, to_wav
from opseval.cli import app
from opseval.demo import tones
from opseval.driver import Driver
from opseval.errors import ConfigError
from opseval.models.profile import AudioConfig, SutProfile
from opseval.models.record import (
    NoiseInfo,
    RunRecord,
    TerminalState,
    TranscriptInfo,
    VoiceMetric,
    VoiceSpan,
    VoiceTimeline,
    WordTiming,
)
from opseval.models.results import ScoreStatus
from opseval.models.scenario import Scenario, ScorerSpec
from opseval.models.voice import Noise, VoiceSpec
from opseval.runner import _check_voice_scenarios
from opseval.scoring import REGISTRY, ScoringContext, ScoringEngine
from opseval.scoring.judges import PronunciationDialectFit
from opseval.scoring.voice_metrics import (
    LatencyParams,
    TurnLatency,
    VoiceTransfer,
    percentile,
)
from opseval.simulator import LLMSimulator, ScriptedSimulator
from opseval.speech.base import Audio, SpeechConfig, SpeechToText, TextToSpeech, Transcript
from opseval.speech.guard import GuardedSpeechToText, GuardedTextToSpeech
from opseval.transport.audio import (
    DUPLEX,
    AgentAudio,
    AgentFrame,
    AgentSpeechEnd,
    AgentSpeechStart,
    AgentTranscript,
    AttemptClock,
    AudioSession,
    AudioTransport,
    VoiceEvent,
)
from opseval.verdicts import attempt_verdict
from opseval.voice import AudioRecorder, SpeechKit, VoiceTransport
from opseval.voice import noise as noises
from opseval.voice.duplex import DuplexBridge
from opseval.voice.metrics import (
    NOT_MEASURABLE,
    PAUSE_FLOOR_MS,
    UNPLACEABLE,
    AgentTurn,
    CallerTurn,
    CallLog,
    PlannedBargeIn,
    compute,
)
from opseval.voice.playback import schedule

from .test_voice_bridge import ScriptedAgent, ToneSTT, ToneTTS, config, says

RATE = 8_000
CHUNK_MS = 20

# --- an agent that speaks in real time and listens while it speaks -------------------------


class DuplexAgent(AudioSession):
    """Answers each caller turn with ``reply`` in tones, 20 ms a chunk in real time. Caller
    audio with energy while it speaks stops it (``yields``), with a ``speech_end``.

    Faults and shapes for the review of #91: ``burst`` (agent turn numbers whose audio is sent
    all at once, followed by ``trailing_ms`` of silence; an interruption then sends one
    ``speech_end``, its play-back stopping), ``answer_delay`` (seconds of silence before
    answering a caller who interrupted it), ``garble`` (agent turns spoken as noise no STT
    reads), ``notify`` (a notification before the first turn's speech), ``while_cut`` (a
    data message sent while the caller interrupts), ``hangup_on_cut`` (the agent hangs up and
    closes the line on the ``hangup_on_cut``-th chunk of the caller's interruption), and
    ``phrases`` (a final transcript of the caller per phrase, after 300 ms of silence)."""

    capabilities: ClassVar[frozenset[str]] = frozenset({DUPLEX})

    def __init__(
        self,
        fmt: AudioFormat,
        *,
        reply: str = "Okay, I can help with that refund now.",
        yields: bool = True,
        transcripts: bool = False,
        greet: str | None = None,
        burst: frozenset[int] = frozenset(),
        trailing_ms: int = 0,
        answer_delay: float = 0.0,
        garble: frozenset[int] = frozenset(),
        notify: bool = False,
        while_cut: dict[str, Any] | None = None,
        hangup_on_cut: int | None = None,
        phrases: bool = False,
        replies: list[str] | None = None,
        hangup: bool = True,
    ) -> None:
        super().__init__(fmt, AttemptClock())
        self.reply, self.yields, self.transcripts, self.greet = reply, yields, transcripts, greet
        self.burst, self.trailing_ms, self.answer_delay = burst, trailing_ms, answer_delay
        self.garble, self.notify, self.while_cut = garble, notify, while_cut
        self.hangup_on_cut, self.phrases = hangup_on_cut, phrases
        self.replies, self.hangup = replies, hangup
        self._events: asyncio.Queue[VoiceEvent | BaseException] = asyncio.Queue()
        self._buffer: list[bytes] = []
        self.heard: list[str] = []
        self.received: list[bytes] = []
        self.sent: list[bytes] = []
        self._speaking: asyncio.Task[None] | None = None
        self._interrupted = False
        self._was_cut = False
        self._playing_until = 0.0
        self._turns = 0
        self._cut_chunks = 0
        self._closed = False
        self._quiet_ms = 0.0
        self._voiced = False

    async def start(self) -> None:
        if self.greet is not None:
            self._speaking = asyncio.create_task(self._speak(self.greet, None))

    def _talking(self) -> bool:
        loop = asyncio.get_running_loop()
        return (
            self._speaking is not None and not self._speaking.done()
        ) or loop.time() < self._playing_until

    async def send_audio(self, pcm16: bytes) -> None:
        if self._closed:
            from opseval.errors import TransportClosed

            raise TransportClosed("the agent closed the line")
        self._buffer.append(pcm16)
        self.received.append(pcm16)
        if self.phrases:
            if has_energy(pcm16):
                self._voiced, self._quiet_ms = True, 0.0
            else:
                self._quiet_ms += len(pcm16) / 2 / self.format.rate * 1000
                if self._voiced and self._quiet_ms >= 300:
                    self._voiced = False
                    self._final()
        if (self._talking() or self._cut_chunks) and has_energy(pcm16):
            first = not self._interrupted
            self._interrupted = True
            self._cut_chunks += 1
            if first and self.while_cut is not None:
                asyncio.get_running_loop().call_later(
                    0.1,
                    self._events.put_nowait,
                    AgentFrame(self.clock.now_ms() + 100, self.while_cut),
                )
            if first and self.yields and asyncio.get_running_loop().time() < self._playing_until:
                # Its buffered audio stops playing: a "clear".
                self._playing_until = 0.0
                await self._events.put(AgentSpeechEnd(self.clock.now_ms()))
            if self.hangup_on_cut is not None and self._cut_chunks == self.hangup_on_cut:
                from opseval.errors import TransportClosed
                from opseval.transport.audio import Hangup

                if self.hangup:
                    await self._events.put(Hangup(self.clock.now_ms(), "agent"))
                await self._events.put(TransportClosed("the agent closed the line"))
                self._closed = True

    def _final(self) -> None:
        said = tones.decode_all(b"".join(self._buffer), self.format.rate)
        self._buffer.clear()
        if said:
            self._events.put_nowait(AgentTranscript(self.clock.now_ms(), said, of="caller"))

    async def _speak(self, text: str, previous: asyncio.Task[None] | None) -> None:
        if previous is not None and not previous.done():
            await previous
        self._turns += 1
        number = self._turns
        if self._was_cut and self.answer_delay:
            await asyncio.sleep(self.answer_delay)
        self._interrupted = self._was_cut = False
        if self.replies:
            text = self.replies[min(number, len(self.replies)) - 1]
        if self.notify and number == 1:
            await self._events.put(AgentFrame(self.clock.now_ms(), {"type": "response.created"}))
        rate = self.format.rate
        pcm = tones.noise(3.0, rate) if number in self.garble else tones.encode(text, rate)
        step = rate * CHUNK_MS // 1000 * 2
        loop = asyncio.get_running_loop()
        began = loop.time()
        await self._events.put(AgentSpeechStart(self.clock.now_ms()))
        if number in self.burst:
            for i in range(0, len(pcm), step):
                self.sent.append(pcm[i : i + step])
                await self._events.put(AgentAudio(self.clock.now_ms(), pcm[i : i + step]))
            quiet = bytes(2 * (rate * self.trailing_ms // 1000))
            for i in range(0, len(quiet), step):
                await self._events.put(AgentAudio(self.clock.now_ms(), quiet[i : i + step]))
            self._playing_until = began + len(pcm) / 2 / rate
            return
        for k, i in enumerate(range(0, len(pcm), step)):
            if self.yields and self._interrupted:
                self._was_cut = True
                break
            ahead = began + k * CHUNK_MS / 1000 - loop.time()
            if ahead > 0:
                await asyncio.sleep(ahead)
            self.sent.append(pcm[i : i + step])
            await self._events.put(AgentAudio(self.clock.now_ms(), pcm[i : i + step]))
        await self._events.put(AgentSpeechEnd(self.clock.now_ms()))

    async def end_of_caller_turn(self, *, timeout: float | None = None) -> None:
        said = tones.decode_all(b"".join(self._buffer), self.format.rate)
        if self.phrases:
            self._final()
        self._buffer.clear()
        self.heard.append(said)
        if self.transcripts and not self.phrases:
            await self._events.put(AgentTranscript(self.clock.now_ms(), said, of="caller"))
        if self._interrupted and self.yields:
            self._was_cut = True
        self._cut_chunks = 0
        self._speaking = asyncio.create_task(self._speak(self.reply, self._speaking))

    async def next_event(self, timeout: float) -> VoiceEvent:
        try:
            item = await asyncio.wait_for(self._events.get(), timeout)
        except TimeoutError as exc:
            from opseval.errors import TransportTimeout

            raise TransportTimeout("nothing") from exc
        if isinstance(item, BaseException):
            self._events.put_nowait(item)
            raise item
        return item

    async def close(self) -> None:
        if self._speaking is not None:
            self._speaking.cancel()


class DuplexAudio(AudioTransport):
    name = "duplex-fake"
    capabilities = frozenset({DUPLEX})

    def __init__(self, **agent: Any) -> None:
        self.agent = agent
        self.sessions: list[DuplexAgent] = []

    @property
    def target(self) -> str:
        return "fake://duplex"

    async def open(self) -> DuplexAgent:
        session = DuplexAgent(AudioFormat("mulaw", RATE), **self.agent)
        self.sessions.append(session)
        return session


class SlowTTS(ToneTTS):
    def __init__(self, cfg: SpeechConfig, delay: float) -> None:
        super().__init__(cfg)
        self.delay = delay

    async def synthesize(self, text: str, *, voice: str | None, language: str) -> Audio:
        await asyncio.sleep(self.delay)
        return await super().synthesize(text, voice=voice, language=language)


class FlakySTT(ToneSTT):
    """Hears tones, but fails its ``fail_on``-th call."""

    def __init__(self, cfg: SpeechConfig, fail_on: int) -> None:
        super().__init__(cfg)
        self.fail_on = fail_on

    async def transcribe(
        self, audio: Audio, *, language: str, hint: str | None = None
    ) -> Transcript:
        if self.calls + 1 == self.fail_on:
            self.calls += 1
            raise ConnectionError("STT down")
        return await super().transcribe(audio, language=language, hint=hint)


def kit(
    *, tts: TextToSpeech | None = None, stt: SpeechToText | None = None, stt_timeout: float = 5.0
) -> SpeechKit:
    tcfg, scfg = config("tts"), config("stt", timeout=stt_timeout)
    return SpeechKit(
        tts=GuardedTextToSpeech(tts or ToneTTS(tcfg), tcfg),
        stt=GuardedSpeechToText(stt or ToneSTT(scfg), scfg),
        min_confidence=0.5,
        canary_max_wer=0.25,
    )


def profile() -> SutProfile:
    return SutProfile(
        name="voice",
        audio=AudioConfig(codec="mulaw", rate=RATE, end_of_turn_silence_ms=250),
        user_message_frame={"type": "user_message", "text": "{text}"},
        turn_quiet_ms=150,
    )


METRIC_SCORERS = [
    {"id": "voice.barge_in_yield_ms", "params": {"max_ms": 400}},
    {"id": "voice.talk_over_ratio", "params": {"max_ratio": 0.05}},
    {"id": "voice.turn_latency_ms", "params": {"p95_max_ms": 600}},
    {"id": "voice.dead_air_ms", "params": {"max_turn_ms": 800}},
]


def scenario(voice: dict[str, Any] | None, *turns: str, scorers: Any = None) -> Scenario:
    return Scenario.model_validate(
        {
            "id": "d-001",
            "name": "duplex",
            "goal": "Get a refund by voice.",
            "persona": {"role": "caller", "language": "en"},
            "conversation": {"scripted_turns": list(turns or ["Hi there.", "Yes please."])},
            "voice": voice,
            "scorers": scorers or [{"id": "output.terminal_state"}],
            "execution": {
                "repeats": 1,
                "turn_timeout_seconds": 3,
                "attempt_timeout_seconds": 40,
            },
        }
    )


BARGE = {"barge_in": [{"agent_turn": 1, "after_ms": 300, "say": "Wait."}]}


async def attempt(
    audio: AudioTransport, sc: Scenario, *, speech: SpeechKit | None = None
) -> RunRecord:
    prof = profile()
    transport = VoiceTransport(audio, prof, speech or kit()).for_attempt(
        AudioRecorder(None, sc.id, 0), "en", sc.voice
    )
    driver = Driver(
        scenario=sc,
        profile=prof,
        transport=transport,
        simulator=ScriptedSimulator(sc, 1),
        run_id="r",
        attempt_index=0,
        seed=1,
    )
    return await driver.run()


def rows(record: RunRecord, name: str) -> list[VoiceMetric]:
    assert record.voice is not None and record.voice.metrics is not None
    return [m for m in record.voice.metrics if m.name == name]


async def scores(record: RunRecord, sc: Scenario) -> dict[str, Any]:
    found = await ScoringEngine().score(record, sc, profile())
    return {s.scorer_id: s for s in found}


# --- the driver ---------------------------------------------------------------------------


async def test_a_yielding_agent_passes_and_the_interruption_is_a_customer_turn() -> None:
    sc = scenario({"duplex": True, **BARGE}, scorers=METRIC_SCORERS)
    record = await attempt(DuplexAudio(), sc)
    assert record.terminal_state is TerminalState.GOAL_REACHED, record.stop_reason
    assert record.voice is not None and record.voice.mode == "duplex"
    said = [(f.direction, f.text) for f in record.frames if f.text is not None]
    # The interruption sits between the agent's cut turn and its answer to it.
    assert said[0] == ("sent", "Hi there.")
    assert said[1][0] == "received"
    assert said[2] == ("sent", "Wait.")
    assert said[3][0] == "received"
    assert record.user_turns == 3
    (yielded,) = rows(record, "voice.barge_in_yield_ms")
    assert yielded.value is not None and yielded.value < 200
    callers = [s for s in record.voice.spans or () if s.speaker == "caller"]
    assert [c.barge_in for c in callers] == [False, True, False]
    assert any(e.kind == "barge_in" and e.speaker == "caller" for e in record.voice.events)
    result = await scores(record, sc)
    for spec in METRIC_SCORERS:
        assert result[spec["id"]].status is ScoreStatus.PASSED, result[spec["id"]]
    # The cut turn's fragment could not be read: never the agent's unintelligibility.
    assert result["voice.intelligible"].status is ScoreStatus.PASSED
    passed = attempt_verdict(record, list(result.values()), "x").passed
    assert passed is True


async def test_an_agent_that_talks_over_the_caller_fails_the_yield() -> None:
    sc = scenario({"duplex": True, **BARGE}, scorers=METRIC_SCORERS)
    record = await attempt(DuplexAudio(yields=False), sc)
    (yielded,) = rows(record, "voice.barge_in_yield_ms")
    assert yielded.value is not None and yielded.value > 800
    result = await scores(record, sc)
    assert result["voice.barge_in_yield_ms"].status is ScoreStatus.FAILED
    # The caller started the overlap: it is not the agent's talk-over, and the time the
    # agent was still talking is not its latency or dead air.
    assert result["voice.talk_over_ratio"].status is ScoreStatus.PASSED
    assert result["voice.dead_air_ms"].status is ScoreStatus.PASSED
    assert attempt_verdict(record, list(result.values()), "x").passed is False


async def test_an_interruption_planted_past_a_short_turn_never_happens() -> None:
    late = {"barge_in": [{"agent_turn": 1, "after_ms": 3000, "say": "Wait."}]}
    sc = scenario(late, "Hi there.", scorers=METRIC_SCORERS[:1])
    record = await attempt(DuplexAudio(reply="Ok."), sc)
    assert [f.text for f in record.frames if f.direction == "sent"] == ["Hi there."]
    (yielded,) = rows(record, "voice.barge_in_yield_ms")
    assert yielded.value is None and "before the planned interruption" in (yielded.reason or "")
    result = await scores(record, sc)
    assert result["voice.barge_in_yield_ms"].status is ScoreStatus.INFRA_ERROR


async def test_an_stt_outage_in_the_barge_in_turn_is_a_harness_error() -> None:
    scfg = config("stt")
    sc = scenario({"duplex": True, **BARGE}, scorers=METRIC_SCORERS)
    record = await attempt(DuplexAudio(), sc, speech=kit(stt=FlakySTT(scfg, fail_on=1)))
    assert record.terminal_state is TerminalState.HARNESS_FAILURE
    assert any("SttError" in e for e in record.infra_errors)
    result = await scores(record, sc)
    assert attempt_verdict(record, list(result.values()), "x").passed is None


async def test_harness_time_is_never_the_agents_latency_or_dead_air() -> None:
    tcfg, scfg = config("tts"), config("stt")
    slow = kit(tts=SlowTTS(tcfg, 0.4), stt=ToneSTT(scfg, delay=0.4))
    sc = scenario({"duplex": True}, scorers=METRIC_SCORERS)
    record = await attempt(DuplexAudio(), sc, speech=slow)
    assert record.terminal_state is TerminalState.GOAL_REACHED, record.stop_reason
    harness = [s for s in record.voice.spans or () if s.kind == "harness"]  # type: ignore[union-attr]
    assert sum(s.end_ms - s.start_ms for s in harness) > 1_500
    for row in rows(record, "voice.turn_latency_ms") + rows(record, "voice.dead_air_ms"):
        assert row.value is not None and row.value < 300, row


async def test_the_callers_talking_time_is_not_on_the_agents_deadline() -> None:
    long = "Hi there, my order 10482 arrived damaged."
    sc = scenario({"duplex": True}, long, long, long)
    sc = sc.model_copy(
        update={"execution": sc.execution.model_copy(update={"attempt_timeout_seconds": 3.5})}
    )
    record = await attempt(DuplexAudio(reply="Ok."), sc)
    spoken = sum(
        s.end_ms - s.start_ms
        for s in record.voice.spans or ()
        if s.speaker == "caller"  # type: ignore[union-attr]
    )
    assert spoken > 5_000  # longer than the whole attempt deadline
    assert record.terminal_state is TerminalState.GOAL_REACHED, record.stop_reason


async def test_a_planted_silence_is_never_dead_air() -> None:
    voice = {"duplex": True, "silences": [{"caller_turn": 1, "ms": 700, "at": "mid"}]}
    sc = scenario(voice, "Hi there, my order is broken.", "Yes please.")
    audio = DuplexAudio()
    record = await attempt(audio, sc)
    first = next(s for s in record.voice.spans or () if s.speaker == "caller")  # type: ignore[union-attr]
    assert first.end_ms - first.start_ms > 700  # the pause is inside the caller's turn
    for row in rows(record, "voice.dead_air_ms"):
        assert row.value is not None and row.value < 300, row
    # The agent heard both halves as one turn.
    assert audio.sessions[0].heard[0] == "Hi there, my order is broken."


async def test_noise_goes_to_the_agent_never_to_the_canary_or_the_agents_audio() -> None:
    audio = DuplexAudio(transcripts=True)
    voice = {"duplex": True, "noise": {"kind": "white", "snr_db": 20, "seed": 3}}
    sc = scenario(voice, "Hi there.")
    record = await attempt(audio, sc)
    agent = audio.sessions[0]
    heard_by_agent = b"".join(agent.received)
    clean = tones.encode("Hi there.", RATE)
    # What the agent got carries the noise (no exact silence between the tones)...
    assert heard_by_agent != clean
    assert record.voice is not None and record.voice.noise == NoiseInfo(
        generator=noises.GENERATOR, kind="white", snr_db=20.0, seed=3
    )
    # ...the agent's own audio was recorded as it sent it, untouched...
    agent_ref = next(f.audio for f in record.received() if f.audio is not None)
    wav = to_wav(b"".join(agent.sent), RATE)
    assert agent_ref is not None and agent_ref.sha256 == hashlib.sha256(wav).hexdigest()
    # ...and the WER reference is the caller's clean words.
    (wer,) = rows(record, "voice.agent_asr_wer")
    assert wer.value == 0.0


async def test_a_transport_that_cannot_run_duplex_is_refused() -> None:
    class TurnOnly(AudioTransport):
        name = "turn-only"

        @property
        def target(self) -> str:
            return "fake://turns"

        async def open(self) -> AudioSession:
            return ScriptedAgent(says("Hello."), AudioFormat("mulaw", RATE))

    sc = scenario({"duplex": True})
    record = await attempt(TurnOnly(), sc)
    assert record.terminal_state is TerminalState.HARNESS_FAILURE
    assert any("cannot run full duplex" in e for e in record.infra_errors)


async def test_the_turn_based_bridge_records_spans_and_says_what_it_cannot_measure() -> None:
    sc = scenario({"silences": [{"caller_turn": 1, "ms": 200, "at": "before"}]}, "Hi there.")
    record = await attempt(DuplexAudio(), sc)
    assert record.voice is not None and record.voice.mode == "turns"
    (talk,) = rows(record, "voice.talk_over_ratio")
    assert talk.value is None and talk.reason and NOT_MEASURABLE in talk.reason
    (latency,) = rows(record, "voice.turn_latency_ms")
    assert latency.value is not None


async def test_duplex_playback_keeps_the_recorded_overlap() -> None:
    sc = scenario({"duplex": True, **BARGE})
    record = await attempt(DuplexAudio(), sc)
    found = [
        type("T", (), {"seq": f.seq, "ref": f.audio, "duration_ms": f.audio.end_ms})()
        for f in record.frames
        if f.audio is not None
    ]
    when = schedule(record, found)  # type: ignore[arg-type]
    assert all(when[t.seq] == t.ref.start_ms for t in found)


# --- the metrics, from spans --------------------------------------------------------------


def caller(turn: int, start: float, end: float, *, barge_in: bool = False) -> VoiceSpan:
    return VoiceSpan(
        kind="speech", speaker="caller", start_ms=start, end_ms=end, turn=turn, barge_in=barge_in
    )


def agent(turn: int, start: float, end: float) -> VoiceSpan:
    return VoiceSpan(kind="speech", speaker="agent", start_ms=start, end_ms=end, turn=turn)


def harness(start: float, end: float, what: str = "stt") -> VoiceSpan:
    return VoiceSpan(kind="harness", start_ms=start, end_ms=end, detail=what)


def metric(found: tuple[VoiceMetric, ...], name: str) -> list[VoiceMetric]:
    return [m for m in found if m.name == name]


def test_latency_and_dead_air_from_the_callers_end() -> None:
    log = CallLog(mode="duplex", spans=[caller(1, 0, 1000), agent(1, 1300, 2000)])
    found = compute(log)
    assert metric(found, "voice.turn_latency_ms")[0].value == 300
    assert metric(found, "voice.dead_air_ms")[0].value == 300
    assert metric(found, "voice.time_to_first_audio_ms")[0].value == 300


def test_a_gap_the_harness_overlaps_is_left_out_and_degraded() -> None:
    spans = [caller(1, 0, 1000), harness(1100, 1200), agent(1, 1500, 2000)]
    found = compute(CallLog(mode="duplex", spans=spans))
    (latency,) = metric(found, "voice.turn_latency_ms")
    assert latency.value is None and latency.degraded
    assert latency.reason and UNPLACEABLE in latency.reason
    (air,) = metric(found, "voice.dead_air_ms")
    assert air.value == 0 and air.degraded


def test_pauses_inside_an_agent_turn_are_dead_air_but_word_gaps_are_not() -> None:
    spans = [
        caller(1, 0, 1000),
        agent(1, 1100, 2000),
        agent(1, 2000 + PAUSE_FLOOR_MS - 50, 3000),  # a word gap
        agent(1, 3500, 4000),  # a 500 ms pause
        caller(2, 4300, 4400),
    ]
    (air,) = metric(compute(CallLog(mode="duplex", spans=spans)), "voice.dead_air_ms")
    assert air.value == 100 + 500


def test_a_pause_while_the_caller_talks_is_not_dead_air() -> None:
    spans = [caller(1, 0, 1000), agent(1, 1100, 2000), caller(2, 2100, 2600), agent(1, 2700, 3000)]
    (air,) = metric(compute(CallLog(mode="duplex", spans=spans)), "voice.dead_air_ms")
    assert air.value == 100


def test_a_gap_that_ends_in_caller_speech_is_never_counted() -> None:
    spans = [caller(1, 0, 1000), caller(2, 3000, 3500), agent(1, 3600, 4000)]
    found = compute(CallLog(mode="duplex", spans=spans))
    assert metric(found, "voice.dead_air_ms")[0].value == 100


def test_the_agent_still_talking_when_the_caller_finished() -> None:
    spans = [
        caller(1, 0, 500),
        agent(1, 600, 3000),
        caller(2, 1000, 1500, barge_in=True),
        agent(2, 3200, 4000),
    ]
    log = CallLog(
        mode="duplex",
        spans=spans,
        barge_ins=[PlannedBargeIn(agent_turn=1, after_ms=400, fired_at_ms=1000)],
    )
    found = compute(log)
    second = [m for m in metric(found, "voice.turn_latency_ms") if m.turn == 2][0]
    assert second.value is None and "still speaking" in (second.reason or "")
    assert [m.value for m in metric(found, "voice.dead_air_ms") if m.turn == 2] == [200]
    assert metric(found, "voice.barge_in_yield_ms")[0].value == 2000
    # The caller started the overlap: not the agent's talk-over.
    assert metric(found, "voice.talk_over_ratio")[0].value == 0


def test_the_agent_starting_over_the_caller_is_talk_over() -> None:
    spans = [caller(1, 0, 1000), agent(1, 500, 1500)]
    found = compute(CallLog(mode="duplex", spans=spans))
    assert metric(found, "voice.talk_over_ratio")[0].value == 0.5
    (latency,) = metric(found, "voice.turn_latency_ms")
    assert latency.value is None and "before the caller finished" in (latency.reason or "")


def test_a_greeting_is_timed_from_the_calls_start() -> None:
    spans = [agent(1, 400, 900), caller(1, 1000, 1500)]
    found = compute(CallLog(mode="duplex", call_start_ms=100, spans=spans))
    assert metric(found, "voice.time_to_first_audio_ms")[0].value == 300
    (latency,) = metric(found, "voice.turn_latency_ms")
    assert latency.value is None and "before the caller said anything" in (latency.reason or "")


def test_barge_in_rows_say_why_they_were_not_measured() -> None:
    turns = compute(CallLog(mode="turns", spans=[caller(1, 0, 10)]))
    assert NOT_MEASURABLE in (metric(turns, "voice.barge_in_yield_ms")[0].reason or "")
    none = compute(CallLog(mode="duplex", spans=[caller(1, 0, 10)]))
    assert "no barge-in was planted" in (metric(none, "voice.barge_in_yield_ms")[0].reason or "")
    short = CallLog(
        mode="duplex",
        spans=[caller(1, 0, 10), agent(1, 20, 100)],
        barge_ins=[PlannedBargeIn(agent_turn=1, after_ms=500, reason="it ended first")],
    )
    (row,) = metric(compute(short), "voice.barge_in_yield_ms")
    assert row.value is None and row.reason == "it ended first"
    late = CallLog(
        mode="duplex",
        spans=[caller(1, 0, 10), agent(1, 20, 100)],
        barge_ins=[PlannedBargeIn(agent_turn=1, after_ms=10, fired_at_ms=150)],
    )
    (row,) = metric(compute(late), "voice.barge_in_yield_ms")
    assert row.value is None and "had stopped speaking" in (row.reason or "")


def test_agent_wer_and_confidence_rows() -> None:
    none = compute(CallLog(mode="duplex", callers=[CallerTurn(1, "hello there")]))
    (row,) = metric(none, "voice.agent_asr_wer")
    assert row.value is None and NOT_MEASURABLE in (row.reason or "")
    heard = CallLog(
        mode="duplex",
        callers=[CallerTurn(1, "hello there", 0, 500), CallerTurn(2, "yes", 1000, 1200)],
        heard=[(600.0, "hello where")],
        agents=[
            AgentTurn(1, 0.9, "stt"),
            AgentTurn(2, None, "stt"),
            AgentTurn(3, 0.1, "asr_failed"),
        ],
        agent_transcripts_seen=True,
    )
    found = compute(heard)
    assert [m.value for m in metric(found, "voice.agent_asr_wer")] == [0.5, None]
    assert [m.value for m in metric(found, "voice.asr_confidence")] == [0.9, None, None]


def test_a_metric_row_says_why_it_is_not_measured() -> None:
    with pytest.raises(ValidationError):
        VoiceMetric(name="voice.dead_air_ms", value=None, unit="ms")
    with pytest.raises(ValidationError):
        VoiceMetric(name="voice.dead_air_ms", value=3.0, unit="ms", degraded=True)
    with pytest.raises(ValidationError):
        VoiceSpan(kind="harness", speaker="agent", start_ms=0, end_ms=1)
    with pytest.raises(ValidationError):
        VoiceSpan(kind="speech", speaker="agent", start_ms=0, end_ms=1, barge_in=True)


# --- the scorers --------------------------------------------------------------------------


def voice_record(metrics: tuple[VoiceMetric, ...] | None, **timeline: Any) -> RunRecord:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return RunRecord(
        run_id="r",
        test_id="d-001",
        attempt_index=0,
        seed=1,
        target="fake://",
        started_at=now,
        ended_at=now,
        terminal_state=TerminalState.GOAL_REACHED,
        voice=VoiceTimeline(codec="mulaw", rate=RATE, metrics=metrics, **timeline),
    )


def ctx(sc: Scenario | None = None) -> ScoringContext:
    return ScoringContext(scenario=sc or scenario(None), profile=profile())


def latency_rows(*values: float | None) -> tuple[VoiceMetric, ...]:
    return tuple(
        VoiceMetric(
            name="voice.turn_latency_ms",
            turn=n,
            value=v,
            unit="ms",
            reason=None if v is not None else "agent first",
        )
        for n, v in enumerate(values, start=1)
    )


def test_percentiles_are_nearest_rank() -> None:
    values = [100.0, 200.0, 300.0, 400.0, 1000.0]
    assert percentile(values, 50) == 300
    assert percentile(values, 95) == 1000
    assert percentile([5.0], 95) == 5


async def test_latency_percentiles_are_computed_from_the_turns() -> None:
    spec = ScorerSpec(id="voice.turn_latency_ms", params={"p95_max_ms": 500, "p50_max_ms": 300})
    record = voice_record(latency_rows(100, 200, 300, None, 900))
    result = await TurnLatency().score(record, spec, ctx())
    assert result.status is ScoreStatus.FAILED
    assert result.details["p95_ms"] == 900 and result.details["p50_ms"] == 200
    assert result.details["not_measured"] == [{"turn": 4, "value": None, "reason": "agent first"}]
    ok = await TurnLatency().score(
        record.model_copy(), spec.model_copy(update={"params": {"p50_max_ms": 300}}), ctx()
    )
    assert ok.status is ScoreStatus.PASSED


async def test_nothing_measured_is_not_measured_never_a_pass() -> None:
    spec = ScorerSpec(id="voice.turn_latency_ms", params={"max_ms": 500})
    none = await TurnLatency().score(voice_record(latency_rows(None)), spec, ctx())
    assert none.status is ScoreStatus.INFRA_ERROR and none.score is None
    assert none.reason and none.reason.startswith(NOT_MEASURABLE)
    old = await TurnLatency().score(voice_record(None), spec, ctx())
    assert old.status is ScoreStatus.INFRA_ERROR and "before voice metrics" in (old.reason or "")
    text = voice_record(None).model_copy(update={"voice": None})
    assert (await TurnLatency().score(text, spec, ctx())).status is ScoreStatus.INFRA_ERROR


async def test_a_degraded_row_degrades_the_score() -> None:
    spec = ScorerSpec(id="voice.dead_air_ms", params={"max_turn_ms": 500})
    rows_ = (
        VoiceMetric(name="voice.dead_air_ms", turn=1, value=100, unit="ms"),
        VoiceMetric(
            name="voice.dead_air_ms", turn=2, value=0, unit="ms", degraded=True, reason="gap left"
        ),
    )
    result = await REGISTRY["voice.dead_air_ms"]().score(voice_record(rows_), spec, ctx())
    assert result.status is ScoreStatus.PASSED and result.degraded


async def test_an_interruption_that_never_happened_degrades_the_yield() -> None:
    spec = ScorerSpec(id="voice.barge_in_yield_ms", params={"max_ms": 500})
    rows_ = (
        VoiceMetric(name="voice.barge_in_yield_ms", turn=1, value=100, unit="ms"),
        VoiceMetric(
            name="voice.barge_in_yield_ms", turn=3, value=None, unit="ms", reason="ended first"
        ),
    )
    result = await REGISTRY["voice.barge_in_yield_ms"]().score(voice_record(rows_), spec, ctx())
    assert result.status is ScoreStatus.PASSED and result.degraded


def test_params_are_checked_at_load() -> None:
    with pytest.raises(ValidationError):
        LatencyParams.model_validate({})
    with pytest.raises(ValidationError):
        LatencyParams.model_validate({"p95_max": 100})
    for scorer in (
        "voice.time_to_first_audio_ms",
        "voice.turn_latency_ms",
        "voice.barge_in_yield_ms",
        "voice.talk_over_ratio",
        "voice.dead_air_ms",
        "voice.agent_asr_wer",
        "voice.asr_confidence",
        "voice.transfer",
        "judge.pronunciation_dialect_fit",
    ):
        cls = REGISTRY[scorer]
        assert cls.Params is not None
        if scorer.startswith("voice."):
            assert cls.reads_text is False


async def test_transfer_reads_the_timeline() -> None:
    from opseval.models.record import VoiceEventRecord

    moved = voice_record(
        (), events=(VoiceEventRecord(at_ms=1500, kind="transfer", speaker="agent", detail="tier2"),)
    )
    stay = voice_record(())
    scorer = VoiceTransfer()

    async def run(record: RunRecord, **params: Any) -> ScoreStatus:
        spec = ScorerSpec(id="voice.transfer", params=params)
        return (await scorer.score(record, spec, ctx())).status

    assert await run(moved, to="tier2", within_ms=2000) is ScoreStatus.PASSED
    assert await run(moved, to="billing") is ScoreStatus.FAILED
    assert await run(moved, within_ms=1000) is ScoreStatus.FAILED
    assert await run(stay) is ScoreStatus.FAILED
    assert await run(stay, expected=False) is ScoreStatus.PASSED
    assert await run(moved, expected=False) is ScoreStatus.FAILED


async def test_the_dialect_judge_names_unsure_words_and_needs_speech() -> None:
    from datetime import UTC, datetime

    from opseval.models.profile import FrameKind
    from opseval.models.record import FrameRecord

    judge = PronunciationDialectFit()
    text = voice_record(None).model_copy(update={"voice": None})
    assert judge.not_measurable(text, ctx()) is not None
    frame = FrameRecord(
        seq=1,
        at=datetime.now(UTC),
        direction="received",
        kind=FrameKind.MESSAGE,
        payload={"text": "ahlan ya basha"},
        text="ahlan ya basha",
        transcript=TranscriptInfo(
            source="opseval",
            confidence=0.8,
            words=(
                WordTiming(word="ahlan", start_ms=0, end_ms=100, confidence=0.9),
                WordTiming(word="basha", start_ms=200, end_ms=300, confidence=0.2),
            ),
        ),
    )
    spoken = voice_record(None).model_copy(update={"frames": (frame,)})
    assert judge.not_measurable(spoken, ctx()) is None
    facts = "\n".join(judge.harness_facts(spoken))
    # The unsure words are the agent's (untrusted): in the fenced turn, never in the facts.
    assert "basha" not in facts and "1 agent word(s)" in facts
    assert "agent turn 1: 0.80" in facts
    assert '"unsure_words": ["basha"]' in judge.digest(spoken, ctx())
    sc = scenario(None)
    arabic = sc.model_copy(update={"persona": sc.persona.model_copy(update={"language": "ar"})})
    assert judge.rubric_hash(ctx(sc)) != judge.rubric_hash(ctx(arabic))
    assert len(judge.prompt_hash()) == 16


# --- the scenario's voice block -----------------------------------------------------------


def test_the_voice_block_is_checked_at_load() -> None:
    assert VoiceSpec.model_validate(BARGE).runs_duplex
    assert not VoiceSpec.model_validate({"silences": [{"caller_turn": 1, "ms": 100}]}).runs_duplex
    bad = [
        {"barge_in": [BARGE["barge_in"][0], BARGE["barge_in"][0]]},
        {"code_switching": ["ar", "en"]},  # no caller_voice
        {"code_switching": ["ar"], "caller_voice": "bilingual"},
        {"noise": {"kind": "rain"}},
        {"silences": [{"caller_turn": 1, "ms": 10}]},
        {"unknown": 1},
    ]
    for spec in bad:
        with pytest.raises(ValidationError):
            VoiceSpec.model_validate(spec)
    with pytest.raises(ValidationError, match="max_turns"):
        Scenario.model_validate(
            {
                "id": "d",
                "name": "d",
                "goal": "g",
                "persona": {"role": "r"},
                "conversation": {"scripted_turns": ["hi"], "max_turns": 2},
                "voice": {"silences": [{"caller_turn": 3, "ms": 100}]},
            }
        )


def test_a_run_that_cannot_honour_the_voice_block_is_refused() -> None:
    with pytest.raises(ConfigError, match="only a voice transport"):
        _check_voice_scenarios([scenario({"noise": {"kind": "pink"}})], "websocket")
    with pytest.raises(ConfigError, match="audio-ws"):
        _check_voice_scenarios([scenario({"duplex": True})], "audio-http")
    _check_voice_scenarios([scenario({"duplex": True})], "audio-ws")
    _check_voice_scenarios([scenario(None)], "websocket")


def test_code_switching_is_told_to_an_llm_caller() -> None:
    sc = scenario({"code_switching": ["ar", "en"], "caller_voice": "bilingual"})
    sc = sc.model_copy(
        update={
            "conversation": sc.conversation.model_copy(update={"scripted_turns": [], "flow": "x"})
        }
    )
    prompt = LLMSimulator(sc, 1, client=None)._system()  # type: ignore[arg-type]
    assert "Code-switching" in prompt and "Arabic and English" in prompt
    plain = LLMSimulator(scenario(None), 1, client=None)._system()  # type: ignore[arg-type]
    assert "Code-switching" not in plain


async def test_the_caller_voice_goes_to_the_tts() -> None:
    seen: list[str | None] = []

    class Recording(ToneTTS):
        async def synthesize(self, text: str, *, voice: str | None, language: str) -> Audio:
            seen.append(voice)
            return await super().synthesize(text, voice=voice, language=language)

    sc = scenario({"caller_voice": "calm_female"}, "Hi there.")
    await attempt(DuplexAudio(), sc, speech=kit(tts=Recording(config("tts"))))
    assert seen and set(seen) == {"calm_female"}


# --- noise ----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["white", "pink", "babble"])
def test_noise_is_reproducible_and_at_the_asked_snr(kind: str) -> None:
    speech = tones.encode("hello there", RATE)
    spec = Noise(kind=kind, snr_db=10, seed=4)  # type: ignore[arg-type]
    mixed = noises.mix(speech, RATE, spec, turn=1)
    assert mixed == noises.mix(speech, RATE, spec, turn=1)
    assert mixed != noises.mix(speech, RATE, spec, turn=2)
    from array import array

    a, b = array("h"), array("h")
    a.frombytes(speech)
    b.frombytes(mixed)
    signal = math.sqrt(sum(x * x for x in a) / len(a))
    noise = math.sqrt(sum((y - x) ** 2 for x, y in zip(a, b, strict=True)) / len(a))
    assert 9 < 20 * math.log10(signal / noise) < 11


def test_silence_alone_gets_no_noise() -> None:
    quiet = noises.silence(200, RATE)
    assert noises.mix(quiet, RATE, Noise()) == quiet
    assert level_dbfs(quiet) == float("-inf")


# --- the demo -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ([], 0),
        (["--bug", "talks-over"], 1),
    ],
)
def test_the_duplex_demo_exit_codes(
    args: list[str], code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPSEVAL_TELEMETRY", "false")
    res = CliRunner().invoke(app, ["demo", "--voice", "--duplex", "--repeats", "1", *args])
    assert res.exit_code == code, res.output


@pytest.mark.parametrize(
    "args",
    [
        ["demo", "--duplex"],
        ["demo", "--voice", "--bug", "talks-over"],
        ["demo", "--voice", "--duplex", "--voice-transport", "audio-http"],
    ],
)
def test_the_duplex_demo_refuses_what_it_cannot_run(
    args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 3, res.output


def test_duplex_bridge_is_exported() -> None:
    assert DuplexBridge.mode == "duplex"
