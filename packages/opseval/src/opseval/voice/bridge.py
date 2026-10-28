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
from opseval.errors import ConfigError, SttError, TransportTimeout, TurnNeverEnded
from opseval.models.profile import AudioConfig, SutProfile, placeholder_path
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


@dataclass(frozen=True)
class Spoken:
    """How a caller says a checkpoint decision in one language."""

    approve: str
    """A bare approval."""
    reject: str
    """A bare rejection."""
    yes: str
    """Before an approval's feedback ("Yes. <feedback>")."""
    no: str
    """Before a rejection's feedback ("No. <feedback>")."""


SPOKEN_DECISIONS = {
    "en": Spoken("Yes, please go ahead.", "No.", "Yes.", "No."),
    "ar": Spoken("نعم، تفضل.", "لا.", "نعم.", "لا."),
}
"""What the caller says for a checkpoint decision, by primary language (English otherwise)."""

SCRIPTS: dict[str, tuple[str, ...]] = {
    "ar": ("ARABIC",),
    "fa": ("ARABIC",),
    "ur": ("ARABIC",),
    "zh": ("CJK",),
    "ja": ("CJK", "HIRAGANA", "KATAKANA"),
    "ko": ("HANGUL",),
    "ru": ("CYRILLIC",),
    "uk": ("CYRILLIC",),
    "bg": ("CYRILLIC",),
    "sr": ("CYRILLIC", "LATIN"),
    "he": ("HEBREW",),
    "yi": ("HEBREW",),
    "hi": ("DEVANAGARI",),
    "mr": ("DEVANAGARI",),
    "ne": ("DEVANAGARI",),
    "el": ("GREEK",),
    "th": ("THAI",),
}
"""The Unicode scripts a transcript is expected in, by primary language; Latin otherwise.
Only an annotation (:func:`in_script`): whether a transcript is usable never depends on it."""


def _primary(language: str) -> str:
    return language.split("-")[0].split("_")[0].lower()


async def _direct(call: Awaitable[T]) -> T:
    return await call


def usable(text: str) -> bool:
    """Whether a transcript has words at all: at least one letter (in any script: Latin,
    Arabic, CJK, kana, Hangul, Cyrillic, Hebrew, Devanagari, Thai, Greek, ...) or digit.

    "Garbage" (ADR-0053) is a transcript with neither: empty, punctuation, symbols, music
    notes. It is never decided by the scenario language's script, because a per-language
    list would call a Russian or Korean agent that spoke perfectly "unintelligible" and blame
    it for a gap in Opseval's own table (the one rule). Whether the words are in the expected
    script is :func:`in_script`, an annotation that degrades, never a failure."""
    return any(
        unicodedata.category(ch)[0] == "L" or unicodedata.category(ch) == "Nd" for ch in text
    )


def in_script(text: str, language: str) -> bool:
    """Whether a usable transcript has a letter in the language's expected script (Latin
    unless :data:`SCRIPTS` says otherwise). A transcript of digits alone is in every script."""
    scripts = SCRIPTS.get(_primary(language), ("LATIN",))
    letters = [ch for ch in text if unicodedata.category(ch)[0] == "L"]
    if not letters:
        return True
    return any(unicodedata.name(ch, "").startswith(scripts) for ch in letters)


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
    audio_ms: float = 0.0
    """How much agent audio this turn holds, at the session's rate (``max_turn_seconds``)."""
    barge_in: bool = False
    barge_event: int | None = None
    """Where this turn's ``barge_in`` event is in the timeline: tagged with the turn's file
    number when the turn is kept, so playback ties the overlap to this turn exactly."""

    def reset_audio(self) -> None:
        self.chunks.clear()
        self.first_ms = None
        self.audio_ms = 0.0


class VoiceBridge(Session):
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
        self._enders: list[dict[str, Any]] = []  # frames that end the agent's turn
        self._text_path = placeholder_path(profile.user_message_frame, "text")
        self._feedback_path = placeholder_path(profile.resume_frame, "feedback")
        self._action_path = placeholder_path(profile.resume_frame, "action")

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
        kind: Literal[
            "speech_start", "speech_end", "barge_in", "dtmf", "transfer", "hangup", "mark"
        ],
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
    def _words(self, frame: dict[str, Any], path: str | None) -> str | None:
        if path is None:
            return None
        try:
            value = dig(frame, path)
        except KeyError:
            return None
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _speakable(self, frame: dict[str, Any]) -> str:
        """What the caller says for a frame: the customer's words, or a checkpoint decision.

        A decision is said the way a caller would say it, in the scenario's language: a
        rejection with feedback is "No. <feedback>", an approval with feedback "Yes.
        <feedback>", a bare approval "Yes, please go ahead." and a bare rejection "No." The
        spoken words also become the sent frame's ``text`` (:meth:`frame_media`), so a judge
        reads what the agent heard, not a silent decision."""
        text = self._words(frame, self._text_path)
        if text is not None:
            return text
        feedback = self._words(frame, self._feedback_path)
        action = None
        if self._action_path is not None:
            try:
                action = dig(frame, self._action_path)
            except KeyError:
                action = None
        if action in ("approve", "reject"):
            words = SPOKEN_DECISIONS.get(_primary(self.language), SPOKEN_DECISIONS["en"])
            if feedback is None:
                return words.approve if action == "approve" else words.reject
            lead = words.yes if action == "approve" else words.no
            return f"{lead} {feedback}"
        if feedback is not None:
            return feedback
        raise ConfigError(
            "the voice bridge found no words to speak in a frame: user_message_frame needs a "
            '"{text}" value (and resume_frame "{action}" or "{feedback}")'
        )

    async def send(self, frame: dict[str, Any], *, timeout: float | None = None) -> None:
        if self._hung_up:
            # The agent ended the call while the customer still had something to say.
            raise TransportTimeout("the agent hung up: nobody heard the customer's next turn")
        text = self._speakable(frame)
        # A new caller turn: the agent's next latency is measured from its end, never from
        # an earlier one's.
        self._caller_end_ms = None
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
        self._sent = FrameMedia(audio=ref, text=text)
        self._caller = (text, wire)
        # The last caller sample has gone: the agent's turn clock starts here (ADR-0042),
        # before end_of_caller_turn, which on a request/response transport sends the turn.
        end = self.audio.clock.now_ms()
        self._caller_end_ms = end
        self._event(end, "speech_end", "caller")
        await self.audio.end_of_caller_turn(timeout=timeout)

    # --- in: the agent speaks ----------------------------------------------------------
    def _notification(self, event: str, **fields: Any) -> dict[str, Any]:
        return {SYNTHETIC_KIND: "notification", "event": event, **fields}

    def ends_turn(self, payload: Any) -> bool:
        """A spoken turn (even with an empty transcript: whether those were words is
        ``voice.intelligible``'s question, never a turn timeout's) and the agent hanging up
        end the wait for the agent's answer. A data message answers only by the profile's
        own rule (``SutProfile.answers``), as on any transport."""
        return any(kept is payload for kept in self._enders)

    def _too_long(self) -> TurnNeverEnded:
        return TurnNeverEnded(
            f"the agent spoke for more than {self.cfg.max_turn_seconds:g}s in one turn "
            "(audio.max_turn_seconds)"
        )

    def _answers(self, frame: dict[str, Any]) -> bool:
        kind = self.profile.classify(frame)
        text = frame.get(self.profile.text_field)
        return self.profile.answers(kind, text if isinstance(text, str) else None, frame)

    async def receive(self, timeout: float) -> Any:
        if self._queue:
            return self._queue.popleft()
        if self._hung_up:
            # The call is over: nothing more will come, and the connection closing after a
            # hang-up is the agent ending the call, never a lost session (TRANSPORT_FAILURE).
            raise TransportTimeout("the agent hung up: nothing more will come on this call")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        silence_ms = self.cfg.end_of_turn_silence_ms
        limit_ms = self.cfg.max_turn_seconds * 1000
        turn = _Turn()
        max_deadline = math.inf  # wall clock, set when the agent first speaks
        trailing = 0.0  # ms of silence since the agent last had energy
        while True:
            now = loop.time()
            if turn.voiced_ms is None:
                wait = deadline - now
                if wait <= 0:
                    return self._nothing_heard(turn, timeout)
            else:
                if max_deadline - now <= 0:
                    raise self._too_long()
                wait = min(silence_ms / 1000, max_deadline - now)
            try:
                event = await self.audio.next_event(wait)
            except TransportTimeout:
                if turn.voiced_ms is None:
                    return self._nothing_heard(turn, timeout)
                if loop.time() >= max_deadline:
                    raise self._too_long() from None
                break  # silence after it spoke: the end of its turn
            if isinstance(event, AgentAudio):
                if turn.first_ms is None:
                    turn.first_ms = event.at_ms
                turn.chunks.append(event.pcm16)
                turn.audio_ms += duration_ms(event.pcm16, self.rate)
                if has_energy(event.pcm16, self.cfg.silence_dbfs):
                    trailing = 0.0
                    if turn.voiced_ms is None:
                        self._speech_starts(turn, event.at_ms)
                        max_deadline = loop.time() + self.cfg.max_turn_seconds
                elif turn.voiced_ms is not None:
                    trailing += duration_ms(event.pcm16, self.rate)
                    if trailing >= silence_ms:
                        break
                if turn.audio_ms > limit_ms:
                    # Measured by the audio itself as well as the wall clock: an answer that
                    # arrives all at once (audio-http) is ten minutes long however fast it
                    # came. The STT never gets more than max_turn_seconds of it.
                    if turn.voiced_ms is not None:
                        raise self._too_long()
                    turn.reset_audio()  # that much silence is not speech: drop it, keep waiting
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
                return self._hand_over([*turn.before, self._undecodable(event), *turn.after])
            elif isinstance(event, AgentFrame):
                payload = event.payload
                frame = payload if isinstance(payload, dict) else {"_raw": payload}
                answers = self._answers(frame)
                if turn.voiced_ms is None:
                    turn.before.append(frame)
                    if answers:
                        # A checkpoint, a result, an error, words: the agent answered.
                        return self._hand_over([*turn.before, *turn.after])
                    # A notification ("response.created") or anything else that is not an
                    # answer is kept, in order, and the wait for the spoken answer goes on.
                    continue
                turn.after.append(frame)
                if answers:
                    break  # a data message that answers, after the speech: the speech is over
            elif isinstance(event, Hangup):
                self._event(event.at_ms, "hangup", event.by)
                self._hung_up = True
                note = self._notification("hangup", by=event.by)
                self._enders.append(note)
                if turn.voiced_ms is None:
                    return self._hand_over([*turn.before, note, *turn.after])
                turn.after.append(note)
                break
            elif isinstance(event, Dtmf | Transfer | Mark):
                self._side_event(event, turn)
        return await self._finish(turn)

    def _speech_starts(self, turn: _Turn, at_ms: float) -> None:
        """The agent's first chunk with energy, stamped when it arrived (the transport's own
        receive loop, on the attempt's clock), not when the bridge read it: audio buffered
        while the bridge was busy (in STT) must not look like an instant answer.

        Speech that began before the caller finished is a barge-in: that turn's latency is
        not measured (``None``, a ``barge_in`` timeline event), never ``0.0``."""
        turn.voiced_ms = at_ms
        self._event(at_ms, "speech_start", "agent")
        caller_end = self._caller_end_ms
        if caller_end is not None and at_ms < caller_end:
            turn.barge_in = True
            turn.barge_event = len(self._events)
            self._event(at_ms, "barge_in", "agent", "agent speech began before the caller finished")

    def _hand_over(self, frames: list[dict[str, Any]]) -> dict[str, Any]:
        self._queue.extend(frames[1:])
        return frames[0]

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
        """No speech in time. Anything else the agent sent is still handed over, to be
        recorded; none of it answered, so the driver's wait goes on and ends in the agent's
        ``TIMEOUT``. With nothing at all, the agent did not answer (ADR-0053: silence is not
        an empty transcript)."""
        frames = [*turn.before, *turn.after]
        if frames:
            return self._hand_over(frames)
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
        number = self._next_file()
        ref = self.recorder.keep(
            turn=number, speaker="agent", pcm16=pcm, rate=self.rate, start_ms=start
        )
        if turn.barge_event is not None:
            # "(turn N)": the file number playback matches (voice.playback.barged). The
            # event's own time is when the speech began, which sparse (VAD-gated) chunks
            # can put after the turn's last recorded sample.
            event = self._events[turn.barge_event]
            tagged = f"{event.detail} (turn {number})" if event.detail else f"(turn {number})"
            self._events[turn.barge_event] = event.model_copy(update={"detail": tagged})
        text, info, evidence = await self._hear(pcm)
        if turn.own_transcript is not None:
            evidence["agent_transcript"] = turn.own_transcript
        payload: dict[str, Any] = {
            SYNTHETIC_KIND: "message",
            self.profile.text_field: text,
            VOICE_KEY: evidence,
        }
        latency = None
        if self._caller_end_ms is not None and turn.voiced_ms is not None and not turn.barge_in:
            latency = turn.voiced_ms - self._caller_end_ms
        self._received.append((payload, FrameMedia(audio=ref, transcript=info, latency_ms=latency)))
        self._enders.append(payload)
        return self._hand_over([*turn.before, payload, *turn.after])

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
        if usable(first.text):
            evidence["heard"] = "stt"
            self._annotate(first, evidence)
            return first.text, first.info(), evidence
        second = self.kit.second
        if second is not None:
            try:
                other = await self._clock(second.transcribe(audio, language=self.language))
            except SttError as exc:
                tried.append({"by": "second_stt", "error": str(exc)})
            else:
                tried.append(self._tried("second_stt", other))
                if usable(other.text):
                    evidence["heard"] = "second_stt"
                    self._annotate(other, evidence)
                    return other.text, other.info(), evidence
        canary = await self._canary()
        evidence["canary"] = canary
        heard: Heard = "unintelligible" if canary["healthy"] else "asr_failed"
        evidence["heard"] = heard
        return first.text, first.info(), evidence

    def _annotate(self, heard: Transcript, evidence: dict[str, Any]) -> None:
        """Weak evidence is named, never decided on: a low confidence, or words heard in
        another script than the language's (a wrong-language reply, or the STT writing the
        right one in another script). Both degrade scores; neither fails the agent here."""
        bound = self.kit.min_confidence
        if heard.confidence is not None and heard.confidence < bound:
            evidence["low_confidence"] = {"confidence": heard.confidence, "min": bound}
        if not in_script(heard.text, self.language):
            expected = SCRIPTS.get(_primary(self.language), ("LATIN",))
            evidence["off_script"] = {"language": self.language, "expected": list(expected)}

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


__all__ = ["SpeechKit", "VoiceBridge", "VoiceTransport", "in_script", "usable"]
