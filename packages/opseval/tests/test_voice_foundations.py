# SPDX-License-Identifier: Apache-2.0
"""Voice foundations (ADR-0053): in-house audio, the AudioSession contract, and the record.

The codecs are checked bit for bit against the reference G.711 tables. ``audioop`` produced
them on Python 3.12, and it is gone on 3.13, so they are pinned here as SHA-256 checksums.
"""

from __future__ import annotations

import hashlib
import io
import math
import wave
from array import array
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from opseval import audio
from opseval.errors import ConfigError, TransportTimeout, UnsupportedCapability
from opseval.models.record import (
    AUDIO_RATES,
    AudioRef,
    FrameRecord,
    RunRecord,
    TerminalState,
    TranscriptInfo,
    VoiceEventRecord,
    VoiceTimeline,
    WordTiming,
)
from opseval.redaction import redact_frame, redact_words
from opseval.transport.audio import (
    DTMF,
    AgentAudio,
    AgentSpeechEnd,
    AttemptClock,
    AudioSession,
    VoiceEvent,
)

EVERY_SAMPLE = array("h", range(-32768, 32768)).tobytes()
EVERY_BYTE = bytes(range(256))
REFERENCE = {  # sha256 of audioop.lin2ulaw / lin2alaw / ulaw2lin / alaw2lin, Python 3.12
    ("mulaw", "encode"): "81d633c9e6972a18c74a58720b96cb8ca0bdd096d4060b646dd708c3b846019a",
    ("alaw", "encode"): "38488f6fd710f4686360edc4d38639f96c491595ef93f8eb8d62d5e07ca6ce7b",
    ("mulaw", "decode"): "3dab54339e520bb2c924826e3b72a917a2b612e9fd12fc867500f1d983a75827",
    ("alaw", "decode"): "e04788d110e58ff8c70c93b8480190d973e3b67876b6119abbaec766cc75c174",
}


def tone(ms: int, rate: int = 8_000, amplitude: int = 8_000, hz: int = 440) -> bytes:
    n = rate * ms // 1000
    return array(
        "h", (round(amplitude * math.sin(2 * math.pi * hz * i / rate)) for i in range(n))
    ).tobytes()


# --- codecs ------------------------------------------------------------------------------


@pytest.mark.parametrize("codec", ["mulaw", "alaw"])
def test_g711_matches_the_reference_bit_for_bit(codec: audio.Codec) -> None:
    # Little-endian PCM, as the reference was taken on a little-endian machine.
    encoded = audio.encode(EVERY_SAMPLE, codec)
    assert hashlib.sha256(encoded).hexdigest() == REFERENCE[(codec, "encode")]
    decoded = audio.decode(EVERY_BYTE, codec)
    assert hashlib.sha256(decoded).hexdigest() == REFERENCE[(codec, "decode")]


@pytest.mark.parametrize("codec", ["mulaw", "alaw"])
def test_speech_survives_a_g711_round_trip(codec: audio.Codec) -> None:
    speech = tone(200)
    back = audio.decode(audio.encode(speech, codec), codec)
    assert len(back) == len(speech)
    assert abs(audio.level_dbfs(back) - audio.level_dbfs(speech)) < 0.5


def test_pcm16_passes_through_and_odd_bytes_are_refused() -> None:
    assert audio.encode(b"\x01\x00", "pcm16") == b"\x01\x00"
    with pytest.raises(audio.AudioDecodeError, match="even number"):
        audio.decode(b"\x01", "pcm16")


def test_resampling_rates_and_lengths_are_checked_on_every_path() -> None:
    for bad in (0, -8_000, 8_000.0, True):
        with pytest.raises(ValueError, match="positive whole number"):
            audio.resample(b"", bad, 16_000)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="positive whole number"):
            audio.Resampler(16_000, bad)  # type: ignore[arg-type]
    for rates in ((8_000, 8_000), (8_000, 16_000)):
        with pytest.raises(audio.AudioDecodeError, match="even number"):
            audio.resample(b"\x01", *rates)


def test_streaming_resampling_matches_one_shot_with_no_drift() -> None:
    speech = tone(1024 * 1000 * 1000 // 44_100, rate=44_100, hz=300)[: 1024 * 1000 * 2]
    speech += bytes(1024 * 1000 * 2 - len(speech))
    one_shot = audio.resample(speech, 44_100, 16_000)
    assert abs(len(one_shot) // 2 - round(1024 * 1000 * 16_000 / 44_100)) <= 1
    stream = audio.Resampler(44_100, 16_000)
    chunks = [stream.feed(speech[i : i + 2048]) for i in range(0, len(speech), 2048)]
    chunked = b"".join(chunks) + stream.flush()
    # Same bytes, so no drift and no discontinuity where one chunk meets the next.
    assert chunked == one_shot
    assert sum(len(c) for c in chunks) > 0  # it streams, rather than holding it all


@pytest.mark.parametrize(
    ("src", "dst"), [(8_000, 16_000), (16_000, 8_000), (8_000, 44_100), (48_000, 22_050)]
)
def test_odd_chunks_resample_as_one_buffer(src: int, dst: int) -> None:
    speech = tone(300, rate=src)
    stream = audio.Resampler(src, dst)
    out, i = b"", 0
    for size in [2, 6, 160, 2, 322, 1000] * 100:
        out += stream.feed(speech[i : i + size])
        i += size
    out += stream.feed(speech[i:]) + stream.flush()
    one_shot = audio.resample(speech, src, dst)
    assert out == one_shot
    assert abs(len(one_shot) // 2 - round(len(speech) // 2 * dst / src)) <= 1
    assert stream.flush() == b""  # a flushed resampler starts over
    assert audio.Resampler(src, src).feed(speech[:4]) == speech[:4]


def test_resampling_keeps_duration_and_level() -> None:
    speech = tone(500, rate=16_000)
    down = audio.resample(speech, 16_000, 8_000)
    assert audio.duration_ms(down, 8_000) == pytest.approx(500, abs=1)
    assert audio.level_dbfs(down) == pytest.approx(audio.level_dbfs(speech), abs=0.5)
    assert audio.resample(b"", 8_000, 16_000) == b""
    assert audio.resample(speech, 16_000, 16_000) is speech


def test_silence_has_no_energy_and_speech_does() -> None:
    assert audio.level_dbfs(b"") == -math.inf
    assert not audio.has_energy(bytes(1600))
    assert not audio.has_energy(tone(100, amplitude=20))  # line hiss, about -64 dBFS
    assert audio.has_energy(tone(100))


def test_wav_round_trip_stereo_mix_down_and_broken_files() -> None:
    speech = tone(100)
    assert audio.from_wav(audio.to_wav(speech, 8_000)) == (speech, 8_000)
    stereo = io.BytesIO()
    with wave.open(stereo, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(8_000)
        out.writeframes(array("h", [100, 300, -100, -300]).tobytes())
    mono, rate = audio.from_wav(stereo.getvalue())
    assert (array("h", mono).tolist(), rate) == ([200, -200], 8_000)
    eight_bit = io.BytesIO()
    with wave.open(eight_bit, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(1)
        out.setframerate(8_000)
        out.writeframes(b"\x80\x80")
    for broken in (b"not audio", eight_bit.getvalue()):
        with pytest.raises(audio.AudioDecodeError):
            audio.from_wav(broken)
    with pytest.raises(audio.AudioDecodeError, match="11025 Hz is not supported"):
        audio.from_wav(audio.to_wav(speech, 11_025))
    # Cut short: the data is 6 bytes, one and a half stereo frames.
    with pytest.raises(audio.AudioDecodeError, match="whole number of 2-channel frames"):
        audio.from_wav(stereo.getvalue()[:-2])
    with pytest.raises(audio.AudioDecodeError, match="cut short"):
        audio.from_wav(stereo.getvalue()[:-4])


def test_codec_tables_warm_up_and_stay_bit_exact() -> None:
    for codec in audio.CODECS:
        audio.warm(codec)
    with pytest.raises(ValueError, match="codec"):
        audio.warm("opus")  # type: ignore[arg-type]
    for codec in ("mulaw", "alaw"):
        table = audio._decode_table(codec)  # type: ignore[arg-type]
        assert len(b"".join(table)) == 512
        with pytest.raises(audio.AudioDecodeError, match="even number"):
            audio.encode(b"\x01\x02\x03", codec)  # type: ignore[arg-type]
        assert audio.decode(b"", codec) == b"" == audio.encode(b"", codec)  # type: ignore[arg-type]


def test_the_record_accepts_exactly_the_audio_rates() -> None:
    assert AUDIO_RATES == audio.RATES


def test_an_audio_format_is_checked() -> None:
    assert audio.AudioFormat("mulaw", 8_000).bytes_per_sample == 1
    with pytest.raises(ValueError, match="codec"):
        audio.AudioFormat("opus", 48_000)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="rate"):
        audio.AudioFormat("pcm16", 11_025)


# --- the session contract --------------------------------------------------------------


class Scripted(AudioSession):
    """The smallest voice transport: replays events, or times out when they run out."""

    def __init__(self, events: list[VoiceEvent]) -> None:
        super().__init__(audio.AudioFormat("pcm16", 8_000))
        self.events, self.sent, self.turns = list(events), b"", 0

    async def start(self) -> None: ...

    async def send_audio(self, pcm16: bytes) -> None:
        self.sent += pcm16

    async def end_of_caller_turn(self, *, timeout: float | None = None) -> None:
        self.turns += 1

    async def next_event(self, timeout: float) -> VoiceEvent:
        if not self.events:
            raise TransportTimeout(f"no agent speech within {timeout}s")
        return self.events.pop(0)

    async def close(self) -> None: ...


async def test_a_voice_transport_needs_only_the_contract() -> None:
    session = Scripted([AgentAudio(10.0, tone(20)), AgentSpeechEnd(30.0)])
    await session.start()
    await session.send_audio(tone(20))
    await session.end_of_caller_turn()
    assert isinstance(await session.next_event(1.0), AgentAudio)
    assert isinstance(await session.next_event(1.0), AgentSpeechEnd)
    with pytest.raises(TransportTimeout):  # the agent said nothing more: its fault
        await session.next_event(0.01)
    assert session.transport_events() == () and session.turns == 1
    assert DTMF not in Scripted.capabilities
    # Pressing keys on a transport without DTMF is configuration (exit 3), never dropped and
    # never the agent's fault.
    with pytest.raises(UnsupportedCapability, match="cannot send DTMF") as caught:
        await session.send_dtmf("1")
    assert isinstance(caught.value, ConfigError)


def test_the_attempt_clock_is_monotonic_milliseconds() -> None:
    clock = AttemptClock()
    first, second = clock.now_ms(), clock.now_ms()
    assert 0 <= first <= second < 1_000


# --- the record ------------------------------------------------------------------------


def ref(**over: Any) -> AudioRef:
    base: dict[str, Any] = {
        "path": "audio/t-001__attempt1/0003-agent.wav",
        "speaker": "agent",
        "start_ms": 1200.0,
        "end_ms": 2950.5,
        "sha256": "a" * 64,
        "codec": "mulaw",
        "rate": 8_000,
        "bytes": 14_004,
    }
    return AudioRef.model_validate({**base, **over})


@pytest.mark.parametrize(
    "path",
    [
        "../outside.wav",
        "audio/../../etc/passwd",
        "/etc/passwd",
        "records/x.wav",
        "audio//x.wav",
        "audio\\x.wav",
        "C:audio/x.wav",
        "audio/./x.wav",
    ],
)
def test_an_audio_path_never_leaves_the_run_folder(path: str) -> None:
    with pytest.raises(ValidationError, match="audio"):
        ref(path=path)


def test_hashes_only_and_pruned_audio_are_still_evidence() -> None:
    kept = ref(path=None)
    assert kept.path is None and kept.sha256 == "a" * 64 and kept.hashes_only
    pruned = ref(path=None, pruned=True)
    assert pruned.pruned and not pruned.hashes_only
    assert not ref().hashes_only
    with pytest.raises(ValidationError):
        ref(sha256="not-a-hash")


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"start_ms": -1.0, "end_ms": 5.0}, "greater than or equal"),
        ({"start_ms": 10.0, "end_ms": 9.0}, "end before it starts"),
        ({"rate": 11_025}, "unsupported audio rate"),
        ({"pruned": True}, "path must be None"),
        ({"encryption": "aes-256-gcm"}, "key_id"),
        ({"key_id": "0123456789abcdef"}, "key_id"),
        ({"key_id": "NOT-A-FINGERPRINT", "encryption": "aes-256-gcm"}, "pattern"),
        (
            {"path": None, "encryption": "aes-256-gcm", "key_id": "0123456789abcdef"},
            "nothing is encrypted",
        ),
    ],
)
def test_an_audio_ref_is_consistent(over: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ref(**over)


def test_encrypted_audio_names_its_key_never_the_key() -> None:
    sealed = ref(encryption="aes-256-gcm", key_id="0123456789abcdef")
    assert sealed.key_id == "0123456789abcdef" and ref().encryption == "none"
    gone = ref(path=None, pruned=True, encryption="aes-256-gcm", key_id="0123456789abcdef")
    assert gone.encryption == "aes-256-gcm"  # it was encrypted before retention removed it
    with pytest.raises(ValidationError, match="unsupported audio rate"):
        VoiceTimeline(codec="pcm16", rate=11_025)


# --- credentials spoken aloud (ADR-0033 for word timings) --------------------------------

TOKEN = "sk-proj-abcDEF1234567890xyz"


def words(*spoken: str) -> tuple[WordTiming, ...]:
    return tuple(
        WordTiming(word=w, start_ms=100.0 * i, end_ms=100.0 * i + 90) for i, w in enumerate(spoken)
    )


def said(timings: tuple[WordTiming, ...]) -> list[str]:
    return [t.word for t in timings]


def test_a_token_spoken_as_one_word_is_redacted() -> None:
    clean, kinds = redact_words(words("my", "key", "is", TOKEN + ","))
    assert said(clean) == ["my", "key", "is", "[redacted:openai_key]"]
    assert kinds == ["openai_key"]
    assert [t.start_ms for t in clean] == [0.0, 100.0, 200.0, 300.0]  # timings are kept


def test_a_token_spoken_word_by_word_is_redacted() -> None:
    clean, kinds = redact_words(words("my", "key", "is", "sk-", "proj-", "abcDEF", "1234567890xyz"))
    assert said(clean)[:3] == ["my", "key", "is"]
    assert set(said(clean)[3:]) == {"[redacted:openai_key]"}
    assert kinds and set(kinds) == {"openai_key"}
    assert not any(piece in " ".join(said(clean)) for piece in ("abcDEF", "1234567890"))
    # Pieces separated by spaces, as some speech-to-text writes a spelled-out header.
    clean, _ = redact_words(words("Bearer", "abcdef0123456789XYZ", "please"))
    assert said(clean) == ["[redacted:bearer_token]", "[redacted:bearer_token]", "please"]


def test_words_follow_the_turn_text_and_ordinary_words_stay() -> None:
    # The words do not join back into the token, but the turn's text does.
    spoken = words("a", "test", "key", "sk", "test", "abc1234567890XYZabc", "ok")
    text = "a test key sk_test_abc1234567890XYZabc ok"
    clean, kinds = redact_words(spoken, text)
    assert said(clean) == ["a", "test", "key"] + ["[redacted:stripe_key]"] * 3 + ["ok"]
    assert set(kinds) == {"stripe_key"}
    assert redact_words(words("the", "password", "is", "forgotten")) == (
        words("the", "password", "is", "forgotten"),
        [],
    )


def test_the_driver_pass_covers_word_timings() -> None:
    frame = FrameRecord(
        seq=0,
        at=datetime(2026, 10, 28, tzinfo=UTC),
        direction="sent",
        kind="message",
        payload={"note": f"key {TOKEN}"},
        text=f"my key is {TOKEN}",
        transcript=TranscriptInfo(source="opseval", words=words("my", "key", "is", TOKEN)),
    )
    clean, kinds = redact_frame(frame)
    dumped = clean.model_dump_json()
    assert TOKEN not in dumped and "1234567890" not in dumped
    assert kinds.count("openai_key") == 3  # payload, text, words: each counted
    assert clean.transcript and said(clean.transcript.words)[-1] == "[redacted:openai_key]"
    untouched = frame.model_copy(update={"text": "hello", "payload": {}, "transcript": None})
    assert redact_frame(untouched) == (untouched, [])


def test_a_voice_record_round_trips_and_a_text_record_is_unchanged() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    spoken = FrameRecord(
        seq=3,
        at=now,
        direction="received",
        kind="message",
        payload={"type": "message"},
        text="أهلاً، كيف أقدر أساعدك؟",
        audio=ref(),
        transcript=TranscriptInfo(
            source="opseval",
            provider="openai-compatible",
            model="whisper-1",
            confidence=None,
            words=(WordTiming(word="أهلاً", start_ms=1200, end_ms=1500),),
        ),
    )
    record = RunRecord(
        run_id="r",
        test_id="t-001",
        attempt_index=1,
        seed=1,
        target="ws://127.0.0.1:1",
        started_at=now,
        ended_at=now,
        terminal_state=TerminalState.GOAL_REACHED,
        frames=(spoken,),
        voice=VoiceTimeline(
            codec="mulaw",
            rate=8_000,
            events=(
                VoiceEventRecord(at_ms=1200, kind="speech_start", speaker="agent"),
                VoiceEventRecord(at_ms=2950.5, kind="speech_end", speaker="agent"),
            ),
        ),
    )
    again = RunRecord.model_validate_json(record.model_dump_json())
    assert again == record
    assert again.transcript() == [("agent", "أهلاً، كيف أقدر أساعدك؟")]
    assert again.frames[0].transcript and again.frames[0].transcript.confidence is None
    text_only = record.model_copy(
        update={
            "voice": None,
            "frames": (spoken.model_copy(update={"audio": None, "transcript": None}),),
        }
    )
    dumped = text_only.model_dump(mode="json")
    assert dumped["voice"] is None and dumped["frames"][0]["audio"] is None
