# SPDX-License-Identifier: Apache-2.0
"""The voice bridge (ADR-0053): every row of the fault table, both halves of the one rule.

The agent is a scripted :class:`AudioSession` in process; the speech providers are fakes that
speak and hear :mod:`opseval.demo.tones`, behind the real guards. Each test drives one attempt
through the unchanged driver and scores it with the unchanged engine.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from opseval.audio import AudioFormat, decode, encode, resample
from opseval.demo import tones
from opseval.driver import Driver
from opseval.errors import ConfigError, TransportClosed, TransportTimeout
from opseval.models.profile import AudioConfig, SutProfile
from opseval.models.record import VOICE_KEY, RunRecord, TerminalState
from opseval.models.results import (
    ASR_NO_TRANSCRIPT,
    UNREAD_REPLIES,
    VOICE_INTELLIGIBLE,
    ScoreResult,
    ScoreStatus,
)
from opseval.models.scenario import Scenario
from opseval.scoring import ScoringEngine
from opseval.scoring.base import _voice_degraded
from opseval.scoring.voice import voice_findings
from opseval.simulator import ScriptedSimulator
from opseval.speech.base import Audio, SpeechConfig, SpeechToText, TextToSpeech, Transcript
from opseval.speech.guard import GuardedSpeechToText, GuardedTextToSpeech
from opseval.transport.audio import (
    AgentAudio,
    AgentFrame,
    AgentSpeechEnd,
    AttemptClock,
    AudioSession,
    AudioTransport,
    Dtmf,
    Hangup,
    Transfer,
    UndecodableAudio,
    VoiceEvent,
)
from opseval.verdicts import attempt_verdict
from opseval.voice import AudioRecorder, SpeechKit, VoiceTransport, usable
from opseval.voice.bridge import VoiceBridge, _placeholder

RATE = 8_000

# --- fakes -------------------------------------------------------------------------------


class ToneTTS(TextToSpeech):
    name = "tones"

    def __init__(self, config: SpeechConfig, *, fail: BaseException | None = None) -> None:
        super().__init__(config)
        self.fail = fail
        self.spoken: list[str] = []

    async def synthesize(self, text: str, *, voice: str | None, language: str) -> Audio:
        if self.fail is not None:
            raise self.fail
        self.spoken.append(text)
        return Audio(pcm16=tones.encode(text, 24_000), rate=24_000)


class ToneSTT(SpeechToText):
    """Hears tones. ``deaf`` hears nothing; ``fail`` raises; ``delay`` is slow; ``replace``
    rewrites what was heard; ``confidence`` is reported as given."""

    name = "tones"

    def __init__(
        self,
        config: SpeechConfig,
        *,
        deaf: bool = False,
        fail: BaseException | None = None,
        delay: float = 0.0,
        confidence: float | None = 0.95,
        replace: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(config)
        self.deaf, self.fail, self.delay = deaf, fail, delay
        self.confidence, self.replace = confidence, replace
        self.calls = 0

    async def transcribe(
        self, audio: Audio, *, language: str, hint: str | None = None
    ) -> Transcript:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        text = "" if self.deaf else tones.decode(audio.pcm16, audio.rate)
        if self.replace is not None:
            text = self.replace(text)
        return Transcript(
            text=text,
            provider=self.config.provider,
            model=self.config.model,
            confidence=self.confidence,
        )


def config(
    role: str, provider: str = "fake", model: str = "m", timeout: float = 5.0
) -> SpeechConfig:
    return SpeechConfig(
        role=role,  # type: ignore[arg-type]
        provider=provider,
        url="http://127.0.0.1:1",
        model=model,
        timeout_seconds=timeout,
        label=role.upper(),
    )


def kit(
    *,
    tts: TextToSpeech | None = None,
    stt: SpeechToText | None = None,
    second: SpeechToText | None = None,
    stt_timeout: float = 5.0,
) -> SpeechKit:
    tcfg, scfg = config("tts"), config("stt", timeout=stt_timeout)
    return SpeechKit(
        tts=GuardedTextToSpeech(tts or ToneTTS(tcfg), tcfg),
        stt=GuardedSpeechToText(stt or ToneSTT(scfg), scfg),
        second=None
        if second is None
        else GuardedSpeechToText(second, config("stt", provider="other", model="m2")),
        min_confidence=0.5,
        canary_max_wer=0.25,
    )


Reply = Callable[[str, "ScriptedAgent"], list[tuple[float, VoiceEvent | str]]]
"""What the agent does when the caller's turn ends: (delay, event) pairs. A string event is
speech: the tones of that text."""


class ScriptedAgent(AudioSession):
    """A speaking agent in process. ``reply(said, agent)`` decides what follows a turn."""

    def __init__(self, reply: Reply, fmt: AudioFormat) -> None:
        super().__init__(fmt, AttemptClock())
        self.reply = reply
        self.heard: list[str] = []
        self._buffer: list[bytes] = []
        self._events: asyncio.Queue[VoiceEvent] = asyncio.Queue()
        self._feeder: asyncio.Task[None] | None = None
        self.started = self.closed = False
        self.turn_timeouts: list[float | None] = []
        self.dropped = False

    async def start(self) -> None:
        self.started = True

    async def send_audio(self, pcm16: bytes) -> None:
        self._buffer.append(pcm16)

    def speech(self, text: str) -> list[AgentAudio]:
        pcm = tones.encode(text, self.format.rate)
        step = self.format.rate // 50 * 2
        return [
            AgentAudio(self.clock.now_ms(), pcm[i : i + step]) for i in range(0, len(pcm), step)
        ]

    async def end_of_caller_turn(self, *, timeout: float | None = None) -> None:
        self.turn_timeouts.append(timeout)
        said = tones.decode(b"".join(self._buffer), self.format.rate)
        self._buffer.clear()
        self.heard.append(said)
        plan = self.reply(said, self)

        async def feed() -> None:
            for delay, event in plan:
                if delay:
                    await asyncio.sleep(delay)
                if isinstance(event, str):
                    for chunk in self.speech(event):
                        await self._events.put(chunk)
                else:
                    await self._events.put(event)

        self._feeder = asyncio.create_task(feed())

    async def next_event(self, timeout: float) -> VoiceEvent:
        if self.dropped:
            raise TransportClosed("dropped")
        try:
            return await asyncio.wait_for(self._events.get(), timeout)
        except TimeoutError as exc:
            raise TransportTimeout(f"nothing within {timeout:.1f}s") from exc

    async def close(self) -> None:
        self.closed = True
        if self._feeder is not None:
            self._feeder.cancel()


class FakeAudio(AudioTransport):
    name = "fake-audio"

    def __init__(self, reply: Reply, fmt: AudioFormat | None = None) -> None:
        self.reply = reply
        self.fmt = fmt or AudioFormat("mulaw", RATE)
        self.sessions: list[ScriptedAgent] = []

    @property
    def target(self) -> str:
        return "fake://agent"

    async def open(self) -> ScriptedAgent:
        session = ScriptedAgent(self.reply, self.fmt)
        self.sessions.append(session)
        return session


def says(text: str) -> Reply:
    def reply(_said: str, _agent: ScriptedAgent) -> list[tuple[float, VoiceEvent | str]]:
        return [(0, text), (0, AgentSpeechEnd(0))]

    return reply


def profile(**audio: Any) -> SutProfile:
    return SutProfile(
        name="voice",
        audio=AudioConfig(codec="mulaw", rate=RATE, end_of_turn_silence_ms=200, **audio),
        user_message_frame={"type": "user_message", "text": "{text}"},
        turn_quiet_ms=100,
    )


def scenario(
    *turns: str, language: str = "en", scorers: list[str] | None = None, **execution: Any
) -> Scenario:
    return Scenario.model_validate(
        {
            "id": "v-001",
            "name": "voice",
            "goal": "Get a refund by voice.",
            "persona": {"role": "caller", "language": language},
            "conversation": {"scripted_turns": list(turns or ["Hi, order 10482 is broken."])},
            "scorers": [
                {"id": s}
                for s in (scorers or ["output.terminal_state", "conversation.agent_replied"])
            ],
            "execution": {
                "repeats": 1,
                "turn_timeout_seconds": 1.5,
                "attempt_timeout_seconds": 20,
                **execution,
            },
        }
    )


async def attempt(
    reply: Reply | FakeAudio,
    *,
    speech: SpeechKit | None = None,
    sc: Scenario | None = None,
    prof: SutProfile | None = None,
    recorder: AudioRecorder | None = None,
) -> RunRecord:
    sc = sc or scenario()
    prof = prof or profile()
    audio = reply if isinstance(reply, FakeAudio) else FakeAudio(reply)
    transport = VoiceTransport(audio, prof, speech or kit()).for_attempt(
        recorder or AudioRecorder(None, sc.id, 0), sc.persona.language
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


async def scored(
    record: RunRecord, sc: Scenario | None = None, prof: SutProfile | None = None
) -> tuple[bool | None, dict[str, ScoreResult]]:
    sc, prof = sc or scenario(), prof or profile()
    scores = await ScoringEngine().score(record, sc, prof)
    return attempt_verdict(record, scores, "x").passed, {s.scorer_id: s for s in scores}


def agent_turns(record: RunRecord) -> list[dict[str, Any]]:
    return [
        f.payload[VOICE_KEY]
        for f in record.received()
        if isinstance(f.payload, dict) and VOICE_KEY in f.payload
    ]


# --- a healthy call ----------------------------------------------------------------------


class TestHealthyCall:
    async def test_the_agent_is_heard_and_the_attempt_passes(self) -> None:
        record = await attempt(says("I found order 10482. I can refund it."))
        assert record.terminal_state is TerminalState.GOAL_REACHED
        received = record.received()
        assert received[0].text == "I found order 10482. I can refund it."
        assert received[0].transcript is not None and received[0].transcript.provider == "fake"
        assert agent_turns(record)[0]["heard"] == "stt"
        passed, scores = await scored(record)
        assert passed is True
        assert scores[VOICE_INTELLIGIBLE].status is ScoreStatus.PASSED
        assert scores[UNREAD_REPLIES].status is ScoreStatus.PASSED

    async def test_the_agent_hears_what_the_customer_said_through_tts_and_mulaw(self) -> None:
        audio = FakeAudio(says("ok"))
        await attempt(audio)
        assert audio.sessions[0].heard == ["Hi, order 10482 is broken."]
        assert audio.sessions[0].started and audio.sessions[0].closed

    async def test_the_record_carries_audio_refs_and_a_timeline(self, tmp_path: Path) -> None:
        recorder = AudioRecorder(tmp_path, "v-001", 0, "files")
        record = await attempt(says("Hello there."), recorder=recorder)
        sent, got = record.frames[0], record.frames[1]
        assert sent.audio is not None and sent.audio.speaker == "caller"
        assert got.audio is not None and got.audio.speaker == "agent"
        for ref in (sent.audio, got.audio):
            assert ref.path is not None and ref.path.startswith("audio/v-001/attempt0/")
            assert (tmp_path / ref.path).is_file()
            assert (tmp_path / ref.path).resolve().is_relative_to((tmp_path / "audio").resolve())
            assert ref.codec == "pcm16" and ref.rate == RATE and len(ref.sha256) == 64
        assert record.voice is not None
        assert record.voice.codec == "mulaw" and record.voice.rate == RATE
        kinds = [(e.kind, e.speaker) for e in record.voice.events]
        assert kinds[:4] == [
            ("speech_start", "caller"),
            ("speech_end", "caller"),
            ("speech_start", "agent"),
            ("speech_end", "agent"),
        ]
        assert RunRecord.model_validate_json(record.model_dump_json()) == record

    async def test_hashes_only_stores_no_bytes(self, tmp_path: Path) -> None:
        record = await attempt(
            says("Hello."), recorder=AudioRecorder(tmp_path, "v-001", 0, "hashes")
        )
        refs = [f.audio for f in record.frames if f.audio is not None]
        assert refs and all(r.path is None and r.hashes_only and r.bytes > 0 for r in refs)
        assert not (tmp_path / "audio").exists()

    async def test_the_turn_clock_starts_after_tts_and_latency_excludes_stt(self) -> None:
        slow = kit(stt=ToneSTT(config("stt"), delay=0.4), tts=None)
        record = await attempt(says("Quick answer."), speech=slow)
        latency = record.received()[0].latency_ms
        assert latency is not None and latency < 300, "STT time is not the agent's"

    async def test_tts_and_stt_time_is_off_the_attempt_deadline(self) -> None:
        sc = scenario("one", "two", attempt_timeout_seconds=1.0)
        slow = kit(stt=ToneSTT(config("stt"), delay=0.45))
        record = await attempt(says("Fine."), speech=slow, sc=sc)
        assert record.terminal_state is TerminalState.GOAL_REACHED

    async def test_a_checkpoint_decision_is_spoken(self) -> None:
        tts = ToneTTS(config("tts"))
        bridge_kit = kit(tts=tts)
        prof = SutProfile(
            name="voice",
            audio=AudioConfig(codec="pcm16", rate=16_000),
            kinds={"needs_human": "needs_human"},
            checkpoints={"approve_refund": {}},
            turn_quiet_ms=100,
        )

        def reply(said: str, agent: ScriptedAgent) -> list[tuple[float, VoiceEvent | str]]:
            if said.startswith("Yes"):
                return [(0, "Refund issued."), (0, AgentSpeechEnd(0))]
            frame = {"type": "needs_human", "checkpoint": "approve_refund", "payload": {}}
            return [(0, "Shall I refund it?"), (0, AgentSpeechEnd(0)), (0, AgentFrame(0, frame))]

        sc = Scenario.model_validate(
            {
                "id": "v-002",
                "name": "voice checkpoint",
                "goal": "refund",
                "persona": {"role": "caller", "language": "en"},
                "conversation": {
                    "scripted_turns": ["Refund order 10482."],
                    "checkpoint_policy": {"approve_refund": {"action": "approve"}},
                },
                "scorers": [{"id": "conversation.agent_replied"}],
                "execution": {"repeats": 1, "turn_timeout_seconds": 1.5},
            }
        )
        audio = FakeAudio(says("x"), AudioFormat("pcm16", 16_000))
        audio.reply = reply
        record = await attempt(audio, speech=bridge_kit, sc=sc, prof=prof)
        assert tts.spoken == ["Refund order 10482.", "Yes, please go ahead."]
        assert [i.checkpoint for i in record.interrupts] == ["approve_refund"]
        assert record.received()[-1].text == "Refund issued."

    async def test_dtmf_transfer_and_hangup_are_frames_and_timeline_events(self) -> None:
        def reply(_s: str, _a: ScriptedAgent) -> list[tuple[float, VoiceEvent | str]]:
            return [
                (0, Dtmf(0, "1")),
                (0, "Transferring you now."),
                (0, AgentSpeechEnd(0)),
                (0, Transfer(0, "billing")),
                (0, Hangup(0, "agent")),
            ]

        record = await attempt(reply)
        events = {e.kind for e in record.voice.events} if record.voice else set()
        assert {"dtmf", "transfer", "hangup"} <= events
        notes = [f.payload.get("event") for f in record.received() if isinstance(f.payload, dict)]
        assert notes[:1] == ["dtmf"] and "transfer" in notes and "hangup" in notes

    async def test_an_agent_that_hangs_up_too_soon_is_its_fault(self) -> None:
        def reply(_s: str, _a: ScriptedAgent) -> list[tuple[float, VoiceEvent | str]]:
            return [(0, "Bye."), (0, AgentSpeechEnd(0)), (0, Hangup(0, "agent"))]

        record = await attempt(reply, sc=scenario("one", "two"))
        assert record.terminal_state is TerminalState.TIMEOUT
        assert "hung up" in (record.stop_reason or "")


# --- the fault table, row by row ---------------------------------------------------------


class TestAgentFaultsCountAgainstTheAgent:
    async def test_no_speech_within_the_turn_timeout_is_a_timeout(self) -> None:
        record = await attempt(lambda _s, _a: [])
        assert record.terminal_state is TerminalState.TIMEOUT
        passed, _ = await scored(record)
        assert passed is False

    async def test_silence_is_not_an_empty_transcript_it_times_out(self) -> None:
        def reply(_s: str, agent: ScriptedAgent) -> list[tuple[float, VoiceEvent | str]]:
            return [(0, AgentAudio(0, bytes(3200))), (0, AgentSpeechEnd(0))]

        stt = ToneSTT(config("stt"))
        record = await attempt(reply, speech=kit(stt=stt))
        assert record.terminal_state is TerminalState.TIMEOUT
        assert stt.calls == 0, "silence is never sent to the STT"
        assert (await scored(record))[0] is False

    async def test_speaking_past_max_turn_seconds_is_a_timeout(self) -> None:
        def reply(_s: str, agent: ScriptedAgent) -> list[tuple[float, VoiceEvent | str]]:
            loud = tones.noise(0.02, RATE)
            return [(0.01, AgentAudio(0, loud)) for _ in range(200)]

        record = await attempt(reply, prof=profile(max_turn_seconds=0.3))
        assert record.terminal_state is TerminalState.TIMEOUT
        assert (await scored(record))[0] is False

    async def test_undecodable_audio_is_an_error_frame_with_its_bytes_kept(
        self, tmp_path: Path
    ) -> None:
        def reply(_s: str, _a: ScriptedAgent) -> list[tuple[float, VoiceEvent | str]]:
            return [(0, UndecodableAudio(0, b"\x01\x02\x03", "odd PCM16"))]

        record = await attempt(reply, recorder=AudioRecorder(tmp_path, "v-001", 0, "files"))
        assert record.terminal_state is TerminalState.ERROR_FRAME
        bad = record.received()[-1]
        assert bad.audio is not None and bad.audio.path is not None
        assert (tmp_path / bad.audio.path).read_bytes() == b"\x01\x02\x03"
        passed, scores = await scored(record, scenario(scorers=["conversation.no_errors"]))
        assert passed is False and scores["conversation.no_errors"].status is ScoreStatus.FAILED

    async def test_undecodable_audio_keeps_only_its_hash_in_hashes_mode(
        self, tmp_path: Path
    ) -> None:
        def reply(_s: str, _a: ScriptedAgent) -> list[tuple[float, VoiceEvent | str]]:
            return [(0, UndecodableAudio(0, b"\x01\x02\x03", "odd PCM16"))]

        record = await attempt(reply, recorder=AudioRecorder(tmp_path, "v-001", 0, "hashes"))
        ref = record.received()[-1].audio
        assert ref is not None and ref.hashes_only and ref.bytes == 3 and ref.codec == "mulaw"
        assert not (tmp_path / "audio").exists()

    async def test_garbled_speech_with_a_healthy_canary_fails_voice_intelligible(self) -> None:
        def reply(_s: str, _a: ScriptedAgent) -> list[tuple[float, VoiceEvent | str]]:
            return [(0, AgentAudio(0, tones.noise(0.5, RATE))), (0, AgentSpeechEnd(0))]

        record = await attempt(reply)
        turn = agent_turns(record)[0]
        assert turn["heard"] == "unintelligible"
        assert turn["canary"]["healthy"] is True and turn["canary"]["wer"] == 0.0
        assert turn["canary"]["codec"] == "mulaw"
        passed, scores = await scored(record)
        assert passed is False
        unheard = scores[VOICE_INTELLIGIBLE]
        assert unheard.status is ScoreStatus.FAILED
        assert unheard.details["unintelligible"][0]["level_dbfs"] is not None
        # Counted as read, so it is reported once, not again as an unread reply.
        assert scores[UNREAD_REPLIES].status is ScoreStatus.PASSED

    async def test_a_transcript_with_no_word_in_the_language_is_garbage(self) -> None:
        stt = ToneSTT(config("stt"), replace=lambda t: "?? ..." if "refund" in t else t)
        record = await attempt(says("Your refund is done."), speech=kit(stt=stt))
        assert agent_turns(record)[0]["heard"] == "unintelligible"

    async def test_an_arabic_agent_heard_in_latin_letters_is_garbage(self) -> None:
        assert usable("تمام", "ar-EG") and not usable("tamam", "ar")
        assert usable("ok", "en") and not usable("١٢٣", "en") and not usable("", "fr")
        assert usable("你好", "zh-CN") and not usable("hello", "zh")


class TestHarnessFaultsAreNeverTheAgents:
    @pytest.mark.parametrize(
        "fail", [ConnectionError("down"), TimeoutError(), TransportTimeout("plugin's own")]
    )
    async def test_tts_failing_is_a_harness_failure(self, fail: BaseException) -> None:
        record = await attempt(says("hi"), speech=kit(tts=ToneTTS(config("tts"), fail=fail)))
        assert record.terminal_state is TerminalState.HARNESS_FAILURE
        assert (await scored(record))[0] is None

    @pytest.mark.parametrize(
        "fail", [ConnectionError("down"), TimeoutError(), TransportTimeout("plugin's own")]
    )
    async def test_stt_failing_is_a_harness_failure(self, fail: BaseException) -> None:
        record = await attempt(says("hi"), speech=kit(stt=ToneSTT(config("stt"), fail=fail)))
        assert record.terminal_state is TerminalState.HARNESS_FAILURE
        assert "SttError" in record.infra_errors[0]
        assert (await scored(record))[0] is None

    async def test_stt_over_its_own_timeout_is_a_harness_failure_not_the_agents(self) -> None:
        slow = kit(stt=ToneSTT(config("stt", timeout=0.2), delay=1.0), stt_timeout=0.2)
        record = await attempt(says("hi"), speech=slow)
        assert record.terminal_state is TerminalState.HARNESS_FAILURE
        assert "timed out" in (record.stop_reason or "")

    async def test_a_dropped_stream_is_not_judged(self) -> None:
        audio = FakeAudio(lambda _s, a: (setattr(a, "dropped", True), [])[1])
        record = await attempt(audio)
        assert record.terminal_state is TerminalState.TRANSPORT_FAILURE
        assert (await scored(record))[0] is None

    async def test_garbled_with_a_failing_canary_is_asr_no_transcript(self) -> None:
        # The STT hears the agent's tones as nothing, and the caller's own turn too.
        def reply(_s: str, _a: ScriptedAgent) -> list[tuple[float, VoiceEvent | str]]:
            return [(0, "I can refund it."), (0, AgentSpeechEnd(0))]

        record = await attempt(reply, speech=kit(stt=ToneSTT(config("stt"), deaf=True)))
        turn = agent_turns(record)[0]
        assert turn["heard"] == "asr_failed" and turn["canary"]["healthy"] is False
        assert turn["canary"]["wer"] == 1.0
        passed, scores = await scored(record)
        replied = scores["conversation.agent_replied"]
        assert replied.status is ScoreStatus.INFRA_ERROR
        assert replied.reason is not None and replied.reason.startswith(ASR_NO_TRANSCRIPT)
        assert "canary" in replied.reason and "fake (m)" in replied.reason
        assert scores[VOICE_INTELLIGIBLE].status is ScoreStatus.INFRA_ERROR
        # A check that reads no text still scores the attempt.
        assert scores["output.terminal_state"].status is ScoreStatus.PASSED
        assert passed is True, "the agent is not failed for Opseval's deaf STT"

    async def test_a_canary_call_that_fails_is_unhealthy_and_named(self) -> None:
        calls = {"n": 0}

        class FirstThenBroken(ToneSTT):
            async def transcribe(
                self, audio: Audio, *, language: str, hint: str | None = None
            ) -> Transcript:
                calls["n"] += 1
                if calls["n"] == 1:
                    return Transcript(text="", provider="fake", model="m")
                raise ConnectionError("canary down")

        record = await attempt(says("hello"), speech=kit(stt=FirstThenBroken(config("stt"))))
        turn = agent_turns(record)[0]
        assert turn["heard"] == "asr_failed" and "failed" in turn["canary"]["error"]
        _, scores = await scored(record)
        assert scores["conversation.agent_replied"].status is ScoreStatus.INFRA_ERROR


class TestSecondOpinionAndConfidence:
    async def test_a_second_stt_that_hears_it_is_used_and_text_scores_are_degraded(self) -> None:
        speech = kit(
            stt=ToneSTT(config("stt"), deaf=True), second=ToneSTT(config("stt", "other", "m2"))
        )
        record = await attempt(says("I can refund it."), speech=speech)
        turn = agent_turns(record)[0]
        assert turn["heard"] == "second_stt"
        assert [t["by"] for t in turn["transcripts"]] == ["stt", "second_stt"]
        assert record.received()[0].text == "I can refund it."
        assert record.received()[0].transcript is not None
        assert record.received()[0].transcript.provider == "other"
        passed, scores = await scored(record)
        assert passed is True
        replied = scores["conversation.agent_replied"]
        assert replied.degraded and "second STT" in replied.details["degraded_by"]
        assert scores[VOICE_INTELLIGIBLE].degraded
        assert not scores["output.terminal_state"].degraded, "it reads no text"

    async def test_a_second_stt_that_fails_falls_through_to_the_canary(self) -> None:
        speech = kit(
            stt=ToneSTT(config("stt"), replace=lambda t: "" if "refund" in t else t),
            second=ToneSTT(config("stt", "other", "m2"), fail=ConnectionError("x")),
        )
        record = await attempt(says("I can refund it."), speech=speech)
        turn = agent_turns(record)[0]
        assert turn["transcripts"][1]["error"]
        assert turn["heard"] == "unintelligible"

    async def test_low_confidence_degrades_judge_scores_and_is_named(self) -> None:
        stt = ToneSTT(config("stt"), confidence=0.2)
        record = await attempt(says("Hello."), speech=kit(stt=stt))
        found = voice_findings(record)
        assert found is not None and found.judge_degraded and "0.2 < 0.5" in found.judge_degraded
        judge = ScoreResult(
            scorer_id="judge.goal_achievement",
            axis="output",
            status="passed",
            score=1.0,
            threshold=0.5,
            weight=1.0,
        )
        marked = _voice_degraded(judge, found)
        assert marked.degraded and "OPSEVAL_STT_MIN_CONFIDENCE" in marked.details["degraded_by"]
        _, scores = await scored(record)
        assert not scores["conversation.agent_replied"].degraded, "only judges"

    async def test_a_text_attempt_is_untouched(self) -> None:
        assert (
            voice_findings(
                RunRecord(
                    run_id="r",
                    test_id="t",
                    attempt_index=0,
                    seed=1,
                    target="x",
                    started_at="2026-01-01T00:00:00Z",
                    ended_at="2026-01-01T00:00:00Z",
                    terminal_state="goal_reached",
                )
            )
            is None
        )


class TestRedactionAndPrivacy:
    async def test_a_credential_spoken_by_the_agent_is_redacted_from_the_transcript(self) -> None:
        token = "sk-proj-" + "A" * 24
        record = await attempt(says(f"Your key is {token} keep it safe."))
        text = record.received()[0].text or ""
        assert token not in text and "[redacted:openai_key]" in text
        assert token not in json.dumps(record.model_dump(mode="json"))
        assert "openai_key" in record.redaction.found

    def test_privacy_refuses_files_without_encryption(self) -> None:
        from opseval.config import Settings
        from opseval.voice import check_audio_privacy

        with pytest.raises(ConfigError, match="OPSEVAL_RECORD_AUDIO=hashes"):
            check_audio_privacy(Settings())
        assert check_audio_privacy(Settings(record_audio="hashes")) == "hashes"
        assert check_audio_privacy(Settings(), synthetic=True) == "files"
        with pytest.raises(ConfigError, match="next voice step"):
            check_audio_privacy(Settings(record_audio="hashes", audio_encryption_key="k" * 32))

    def test_an_audio_path_never_leaves_audio(self, tmp_path: Path) -> None:
        recorder = AudioRecorder(tmp_path, "../../etc", 0, "files")
        ref = recorder.keep(turn=1, speaker="agent", pcm16=bytes(320), rate=RATE, start_ms=0)
        assert ref.path is not None and ref.path.startswith("audio/")
        assert (tmp_path / ref.path).resolve().is_relative_to((tmp_path / "audio").resolve())


class TestBridgeDetails:
    def test_the_spoken_text_is_found_in_the_profiles_template(self) -> None:
        assert _placeholder({"type": "m", "body": {"text": "{text}"}}, "text") == "body.text"
        assert _placeholder({"a": ["x", "{text}"]}, "text") == "a.1"
        assert _placeholder({"a": "b"}, "text") is None

    async def test_a_frame_with_no_words_is_a_configuration_error(self) -> None:
        prof = SutProfile(
            name="voice",
            audio=AudioConfig(),
            user_message_frame={"type": "x", "q": "{text}"},
            resume_frame={"type": "r"},
        )
        bridge = VoiceBridge(
            ScriptedAgent(says("x"), AudioFormat()),
            prof,
            kit(),
            AudioRecorder(None, "t", 0),
            language="en",
        )
        with pytest.raises(ConfigError, match="no words to speak"):
            await bridge.send({"type": "r"})

    def test_a_voice_profile_cannot_declare_probes(self) -> None:
        with pytest.raises(ValueError, match="probes"):
            SutProfile(name="v", audio=AudioConfig(), probes=[{"name": "p", "send": {"a": 1}}])

    def test_an_unsupported_rate_is_refused(self) -> None:
        with pytest.raises(ValueError, match="rate"):
            AudioConfig(rate=11_025)

    def test_the_profile_rates_mirror_audio_rates(self) -> None:
        from opseval import audio
        from opseval.models.profile import AUDIO_RATES_ALLOWED

        assert audio.RATES == AUDIO_RATES_ALLOWED

    def test_tones_survive_every_codec_path(self) -> None:
        text = "please tell me the status of my refund"
        pcm = resample(tones.encode(text, 24_000), 24_000, RATE)
        for codec in ("mulaw", "alaw", "pcm16"):
            assert tones.decode(decode(encode(pcm, codec), codec), RATE) == text  # type: ignore[arg-type]
        assert tones.decode(tones.noise(1, RATE), RATE) == ""
