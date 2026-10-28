# SPDX-License-Identifier: Apache-2.0
"""The voice bridge (ADR-0053): an :class:`AudioSession` presented as a text ``Session``.

The driver, the scorers and the judges run on a speaking agent unchanged:

- **Out.** ``send(frame)`` takes the customer's words from the frame (the profile's
  ``user_message_frame``; a checkpoint decision sent through ``resume_frame`` is spoken as
  its feedback, or as a plain yes or no), speaks them with the guarded TTS, resamples to the
  session's rate, streams the audio and ends the caller's turn. The agent's turn clock starts
  there (ADR-0042).
- **In.** ``receive(timeout)`` collects the agent's events until its own end of speech, or
  ``audio.end_of_turn_silence_ms`` of silence after it spoke, transcribes the turn with the
  guarded STT and returns one ``message`` frame whose text is the transcript. The evidence
  goes in the frame (``VOICE_KEY``): level, length, every transcript tried and, for an empty
  or unreadable one, the decision of ADR-0053 and the canary behind it.

TTS and STT run off the agent's clock (``HarnessClock``), each under its own limit, and any
failure is a ``TtsError``/``SttError``: a harness error, never the agent's timeout.

The one rule, for voice:

- no speech (or only silence) within the turn's time: ``TransportTimeout``, the agent's fault;
- speech past ``audio.max_turn_seconds``: ``TransportTimeout``, the agent's fault;
- audio that cannot be decoded: a synthetic ``error`` frame (the agent's fault), its bytes
  kept as they came (or its hash alone);
- speech with energy that the STT cannot read: a second STT if configured (used if it reads
  it, and the text scores are degraded), else the canary (the caller's own preceding turn,
  through the same STT, model and codec path): healthy means the agent's audio failed
  (``voice.intelligible``), unhealthy means Opseval's listening failed (``ASR_NO_TRANSCRIPT``).
"""

from __future__ import annotations

import asyncio
import math
import unicodedata
from collections import deque
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from opseval.audio import (
    Resampler,
    decode,
    duration_ms,
    encode,
    has_energy,
    level_dbfs,
)
from opseval.errors import ConfigError, SttError, TransportTimeout
from opseval.models.profile import AudioConfig, SutProfile
from opseval.models.record import (
    VOICE_KEY,
    TranscriptInfo,
    TransportEvent,
    VoiceEventRecord,
    VoiceTimeline,
)
from opseval.paths import dig
from opseval.speech.base import Audio, Transcript
from opseval.speech.guard import GuardedSpeechToText, GuardedTextToSpeech
from opseval.speech.wer import word_error_rate
from opseval.transport.audio import (
    AgentAudio,
    AgentFrame,
    AgentSpeechEnd,
    AgentSpeechStart,
    AgentTranscript,
    AudioSession,
    AudioTransport,
    Dtmf,
    Hangup,
    Mark,
    Transfer,
    UndecodableAudio,
)
from opseval.transport.base import (
    SYNTHETIC_KIND,
    FrameMedia,
    HarnessClock,
    Session,
    Transport,
)
from opseval.voice.recording import AudioRecorder

T = TypeVar("T")

Heard = Literal["stt", "second_stt", "unintelligible", "asr_failed"]
"""How a spoken reply was heard: by the STT, by the second STT only, by neither while the
canary proved the STT works (the agent's fault), or by neither with the canary failing
(Opseval's)."""

SPOKEN_DECISIONS = {
    "en": ("Yes, please go ahead.", "No."),
    "ar": ("نعم، تفضل.", "لا."),
}
"""What the caller says to a checkpoint decision that has no words of its own."""

SCRIPTS = {"ar": "ARABIC", "fa": "ARABIC", "ur": "ARABIC", "zh": "CJK", "ja": "CJK"}
"""The Unicode script a transcript must have a word in, by primary language; Latin otherwise."""


async def _direct(call: Awaitable[T]) -> T:
    return await call


def usable(text: str, language: str) -> bool:
    """Whether a transcript has at least one letter in the language's script (ADR-0053:
    "garbage" means no word in the scenario language's script)."""
    script = SCRIPTS.get(language.split("-")[0].split("_")[0].lower(), "LATIN")
    return any(ch.isalpha() and unicodedata.name(ch, "").startswith(script) for ch in text)


def _placeholder(template: Any, name: str, at: str = "") -> str | None:
    """The dotted path of the ``"{name}"`` value in a frame template, or ``None``."""
    if template == "{" + name + "}":
        return at
    items: list[tuple[str, Any]] = []
    if isinstance(template, dict):
        items = [(str(k), v) for k, v in template.items()]
    elif isinstance(template, list):
        items = [(str(i), v) for i, v in enumerate(template)]
    for key, value in items:
        found = _placeholder(value, name, f"{at}.{key}" if at else key)
        if found is not None:
            return found
    return None


@dataclass
class SpeechKit:
    """The guarded speech providers a voice run uses, and the bounds of ADR-0053."""

    tts: GuardedTextToSpeech
    stt: GuardedSpeechToText
    second: GuardedSpeechToText | None = None
    min_confidence: float = 0.5
    canary_max_wer: float = 0.25

    async def aclose(self) -> None:
        for provider in (self.tts, self.stt, self.second):
            if provider is not None:
                await provider.aclose()


@dataclass
class _Turn:
    """The agent events gathered for one turn."""

    chunks: list[bytes] = field(default_factory=list)
    first_ms: float | None = None
    voiced_ms: float | None = None
    before: list[dict[str, Any]] = field(default_factory=list)
    after: list[dict[str, Any]] = field(default_factory=list)
    own_transcript: str | None = None

    def reset_audio(self) -> None:
        self.chunks.clear()
        self.first_ms = None


class VoiceBridge(Session):
    whole_turns = True

    def __init__(
        self,
        audio: AudioSession,
        profile: SutProfile,
        kit: SpeechKit,
        recorder: AudioRecorder,
        *,
        language: str,
    ) -> None:
        if profile.audio is None:
            raise ConfigError(f"profile {profile.name!r} has no audio: block for a voice run")
        self.audio = audio
        self.profile = profile
        self.cfg: AudioConfig = profile.audio
        self.kit = kit
        self.recorder = recorder
        self.language = profile.audio.language or language
        self.rate = audio.format.rate
        self.session_id = audio.session_id
        self._clock: HarnessClock = _direct
        self._events: list[VoiceEventRecord] = []
        self._queue: deque[dict[str, Any]] = deque()
        self._sent: FrameMedia | None = None
        self._received: list[tuple[dict[str, Any], FrameMedia]] = []
        self._files = 0
        self._caller: tuple[str, bytes] | None = None  # what was said, as the wire carried it
        self._caller_end_ms: float | None = None
        self._hung_up = False
        self._text_path = _placeholder(profile.user_message_frame, "text")
        self._feedback_path = _placeholder(profile.resume_frame, "feedback")
        self._action_path = _placeholder(profile.resume_frame, "action")

    # --- Session hooks -----------------------------------------------------------------
    def use_harness_clock(self, clock: HarnessClock) -> None:
        self._clock = clock

    def frame_media(self, direction: str, payload: Any) -> FrameMedia | None:
        if direction == "sent":
            media, self._sent = self._sent, None
            return media
        for i, (kept, media) in enumerate(self._received):
            if kept is payload:
                del self._received[i]
                return media
        return None

    def voice_timeline(self) -> VoiceTimeline:
        return VoiceTimeline(
            codec=self.audio.format.codec, rate=self.rate, events=tuple(self._events)
        )

    def transport_events(self) -> tuple[TransportEvent, ...]:
        return self.audio.transport_events()

    def _event(
        self,
        at_ms: float,
        kind: Literal["speech_start", "speech_end", "dtmf", "transfer", "hangup", "mark"],
        speaker: Literal["caller", "agent"] | None = None,
        detail: str | None = None,
    ) -> None:
        self._events.append(
            VoiceEventRecord(at_ms=max(0.0, at_ms), kind=kind, speaker=speaker, detail=detail)
        )

    def _next_file(self) -> int:
        self._files += 1
        return self._files

    # --- out: the caller speaks --------------------------------------------------------
    def _speakable(self, frame: dict[str, Any]) -> str:
        for path in (self._text_path, self._feedback_path):
            if path is None:
                continue
            try:
                value = dig(frame, path)
            except KeyError:
                continue
            if isinstance(value, str) and value.strip():
                return value
        if self._action_path is not None:
            try:
                action = dig(frame, self._action_path)
            except KeyError:
                action = None
            if action in ("approve", "reject"):
                primary = self.language.split("-")[0].lower()
                yes, no = SPOKEN_DECISIONS.get(primary, SPOKEN_DECISIONS["en"])
                return yes if action == "approve" else no
        raise ConfigError(
            "the voice bridge found no words to speak in a frame: user_message_frame needs a "
            '"{text}" value (and resume_frame "{action}" or "{feedback}")'
        )

    async def send(self, frame: dict[str, Any], *, timeout: float | None = None) -> None:
        if self._hung_up:
            # The agent ended the call while the customer still had something to say.
            raise TransportTimeout("the agent hung up: nobody heard the customer's next turn")
        text = self._speakable(frame)
        spoken = await self._clock(
            self.kit.tts.synthesize(text, voice=None, language=self.language)
        )
        resampler = Resampler(spoken.rate, self.rate)
        pcm = resampler.feed(spoken.pcm16) + resampler.flush()
        codec = self.audio.format.codec
        # The canary hears the caller exactly as the wire carried it (ADR-0053).
        wire = pcm if codec == "pcm16" else decode(encode(pcm, codec), codec)
        step = max(2, self.rate * self.cfg.chunk_ms // 1000 * 2)
        start = self.audio.clock.now_ms()
        self._event(start, "speech_start", "caller")
        for i in range(0, len(pcm), step):
            await self.audio.send_audio(pcm[i : i + step])
        ref = self.recorder.keep(
            turn=self._next_file(), speaker="caller", pcm16=pcm, rate=self.rate, start_ms=start
        )
        self._sent = FrameMedia(audio=ref)
        self._caller = (text, wire)
        await self.audio.end_of_caller_turn(timeout=timeout)
        end = self.audio.clock.now_ms()
        self._caller_end_ms = end
        self._event(end, "speech_end", "caller")

    # --- in: the agent speaks ----------------------------------------------------------
    def _notification(self, event: str, **fields: Any) -> dict[str, Any]:
        return {SYNTHETIC_KIND: "notification", "event": event, **fields}

    async def receive(self, timeout: float) -> Any:
        if self._queue:
            return self._queue.popleft()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        silence_ms = self.cfg.end_of_turn_silence_ms
        turn = _Turn()
        max_deadline = math.inf  # set when the agent first speaks
        trailing = 0.0  # ms of silence since the agent last had energy
        while True:
            now = loop.time()
            if turn.voiced_ms is None:
                wait = deadline - now
            else:
                wait = min(silence_ms / 1000, max_deadline - now)
                if max_deadline - now <= 0:
                    raise TransportTimeout(
                        f"the agent spoke for more than {self.cfg.max_turn_seconds:g}s in one "
                        "turn (audio.max_turn_seconds)"
                    )
            if wait <= 0 and turn.voiced_ms is None:
                return self._nothing_heard(turn, timeout)
            try:
                event = await self.audio.next_event(max(0.0, wait))
            except TransportTimeout:
                if turn.voiced_ms is None:
                    return self._nothing_heard(turn, timeout)
                if loop.time() >= max_deadline:
                    raise
                break  # silence after it spoke: the end of its turn
            if isinstance(event, AgentAudio):
                if turn.first_ms is None:
                    turn.first_ms = event.at_ms
                turn.chunks.append(event.pcm16)
                if has_energy(event.pcm16, self.cfg.silence_dbfs):
                    trailing = 0.0
                    if turn.voiced_ms is None:
                        turn.voiced_ms = event.at_ms
                        max_deadline = loop.time() + self.cfg.max_turn_seconds
                        self._event(event.at_ms, "speech_start", "agent")
                elif turn.voiced_ms is not None:
                    trailing += duration_ms(event.pcm16, self.rate)
                    if trailing >= silence_ms:
                        break
            elif isinstance(event, AgentSpeechEnd):
                if turn.voiced_ms is not None:
                    break
                turn.reset_audio()  # silence is not speech: keep waiting for words
            elif isinstance(event, AgentSpeechStart):
                continue  # speech starts with its first chunk that has energy
            elif isinstance(event, AgentTranscript):
                if event.of == "agent":
                    turn.own_transcript = event.text
            elif isinstance(event, UndecodableAudio):
                return self._undecodable(event)
            elif isinstance(event, AgentFrame):
                payload = event.payload
                frame = payload if isinstance(payload, dict) else {"_raw": payload}
                if turn.voiced_ms is None:
                    if not turn.chunks and not turn.before:
                        return frame  # nothing else of this turn yet: hand it over now
                    turn.before.append(frame)
                    continue
                turn.after.append(frame)
                break  # a data message after the speech: the speech is over
            elif isinstance(event, Hangup):
                self._event(event.at_ms, "hangup", event.by)
                self._hung_up = True
                (turn.before if turn.voiced_ms is None else turn.after).append(
                    self._notification("hangup", by=event.by)
                )
                if turn.voiced_ms is None:
                    return self._nothing_heard(turn, timeout)
                break
            elif isinstance(event, Dtmf | Transfer | Mark):
                self._side_event(event, turn)
        return await self._finish(turn)

    def _side_event(self, event: Dtmf | Transfer | Mark, turn: _Turn) -> None:
        where = turn.before if turn.voiced_ms is None else turn.after
        if isinstance(event, Dtmf):
            self._event(event.at_ms, "dtmf", "agent", event.digits)
            where.append(self._notification("dtmf", digits=event.digits))
        elif isinstance(event, Transfer):
            self._event(event.at_ms, "transfer", "agent", event.to)
            where.append(self._notification("transfer", to=event.to))
        else:
            self._event(event.at_ms, "mark", "agent", event.name)

    def _nothing_heard(self, turn: _Turn, timeout: float) -> Any:
        """No speech in time. Anything else the agent sent is still handed over; with nothing
        at all, the agent did not answer (ADR-0053: silence is not an empty transcript)."""
        frames = [*turn.before, *turn.after]
        if frames:
            self._queue.extend(frames[1:])
            return frames[0]
        raise TransportTimeout(f"no speech from the agent within {timeout:.1f}s")

    def _undecodable(self, event: UndecodableAudio) -> dict[str, Any]:
        ref = self.recorder.keep_raw(
            turn=self._next_file(),
            data=event.data,
            codec=self.audio.format.codec,
            rate=self.rate,
            start_ms=event.at_ms,
        )
        payload = {
            SYNTHETIC_KIND: "error",
            "code": "undecodable_audio",
            "detail": event.reason,
            VOICE_KEY: {"undecodable": event.reason, "bytes": len(event.data)},
        }
        self._received.append((payload, FrameMedia(audio=ref)))
        return payload

    async def _finish(self, turn: _Turn) -> Any:
        pcm = b"".join(turn.chunks)
        start = turn.first_ms if turn.first_ms is not None else self.audio.clock.now_ms()
        end = self.audio.clock.now_ms()
        self._event(end, "speech_end", "agent")
        ref = self.recorder.keep(
            turn=self._next_file(), speaker="agent", pcm16=pcm, rate=self.rate, start_ms=start
        )
        text, info, evidence = await self._hear(pcm)
        if turn.own_transcript is not None:
            evidence["agent_transcript"] = turn.own_transcript
        payload: dict[str, Any] = {
            SYNTHETIC_KIND: "message",
            self.profile.text_field: text,
            VOICE_KEY: evidence,
        }
        latency = None
        if self._caller_end_ms is not None and turn.voiced_ms is not None:
            latency = max(0.0, turn.voiced_ms - self._caller_end_ms)
        self._received.append((payload, FrameMedia(audio=ref, transcript=info, latency_ms=latency)))
        frames = [*turn.before, payload, *turn.after]
        self._queue.extend(frames[1:])
        return frames[0]

    # --- hearing: the empty-transcript decision of ADR-0053 ---------------------------
    @staticmethod
    def _tried(by: str, heard: Transcript) -> dict[str, Any]:
        return {
            "by": by,
            "provider": heard.provider,
            "model": heard.model,
            "text": heard.text,
            "confidence": heard.confidence,
        }

    async def _hear(self, pcm: bytes) -> tuple[str, TranscriptInfo, dict[str, Any]]:
        audio = Audio(pcm16=pcm, rate=self.rate)
        level = level_dbfs(pcm)
        evidence: dict[str, Any] = {
            "level_dbfs": round(level, 1) if level != float("-inf") else None,
            "duration_ms": round(duration_ms(pcm, self.rate), 1),
        }
        first = await self._clock(self.kit.stt.transcribe(audio, language=self.language))
        tried = [self._tried("stt", first)]
        evidence["transcripts"] = tried
        if usable(first.text, self.language):
            evidence["heard"] = "stt"
            self._confidence(first, evidence)
            return first.text, first.info(), evidence
        second = self.kit.second
        if second is not None:
            try:
                other = await self._clock(second.transcribe(audio, language=self.language))
            except SttError as exc:
                tried.append({"by": "second_stt", "error": str(exc)})
            else:
                tried.append(self._tried("second_stt", other))
                if usable(other.text, self.language):
                    evidence["heard"] = "second_stt"
                    self._confidence(other, evidence)
                    return other.text, other.info(), evidence
        canary = await self._canary()
        evidence["canary"] = canary
        heard: Heard = "unintelligible" if canary["healthy"] else "asr_failed"
        evidence["heard"] = heard
        return first.text, first.info(), evidence

    def _confidence(self, heard: Transcript, evidence: dict[str, Any]) -> None:
        bound = self.kit.min_confidence
        if heard.confidence is not None and heard.confidence < bound:
            evidence["low_confidence"] = {"confidence": heard.confidence, "min": bound}

    async def _canary(self) -> dict[str, Any]:
        """The caller turn just before, heard by the same STT through the same codec path.
        Healthy when its WER is at most ``OPSEVAL_STT_CANARY_MAX_WER``."""
        stt = self.kit.stt
        base: dict[str, Any] = {
            "provider": stt.config.provider,
            "model": stt.config.model,
            "codec": self.audio.format.codec,
            "rate": self.rate,
            "max_wer": self.kit.canary_max_wer,
        }
        if self._caller is None:
            return {**base, "healthy": False, "error": "no caller turn to check the STT on"}
        said, wire = self._caller
        try:
            heard = await self._clock(
                stt.transcribe(Audio(pcm16=wire, rate=self.rate), language=self.language)
            )
        except SttError as exc:
            return {**base, "healthy": False, "error": str(exc)}
        wer = word_error_rate(said, heard.text)
        return {
            **base,
            "said": said,
            "heard": heard.text,
            "wer": round(wer, 3),
            "healthy": wer <= self.kit.canary_max_wer,
        }

    async def close(self) -> None:
        await self.audio.close()


class VoiceTransport(Transport):
    """A voice transport behind the bridge: ``connect`` opens and starts an audio session
    and wraps it. :meth:`for_attempt` gives each attempt its own recorder and language."""

    def __init__(
        self,
        audio: AudioTransport,
        profile: SutProfile,
        kit: SpeechKit,
        *,
        recorder: AudioRecorder | None = None,
        language: str = "en",
    ) -> None:
        self.audio = audio
        self.profile = profile
        self.kit = kit
        self.recorder = recorder or AudioRecorder(None, "attempt", 0)
        self.language = language
        self.name = audio.name

    def for_attempt(self, recorder: AudioRecorder, language: str) -> VoiceTransport:
        return VoiceTransport(
            self.audio, self.profile, self.kit, recorder=recorder, language=language
        )

    @property
    def target(self) -> str:
        return self.audio.target

    async def connect(self) -> VoiceBridge:
        session = await self.audio.open()
        try:
            await session.start()
        except BaseException:
            await session.close()
            raise
        return VoiceBridge(session, self.profile, self.kit, self.recorder, language=self.language)


__all__ = ["SpeechKit", "VoiceBridge", "VoiceTransport", "usable"]
