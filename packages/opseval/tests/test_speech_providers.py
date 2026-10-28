# SPDX-License-Identifier: Apache-2.0
"""Speech providers (ADR-0054, voice step V0b).

Fault-injection view of the one rule:
- harness half: a TTS or STT that is unreachable, answers 5xx or 429, stalls past its own
  timeout, or returns audio or a body Opseval cannot read is a ``TtsError`` / ``SttError``
  (``HarnessError``): never the agent's fault, and never a ``TransportTimeout``, whatever a
  plugin raises;
- agent half: an *empty* transcript is not an error here. It is evidence for the STT canary
  to weigh (ADR-0053), so the provider layer returns it as heard; and a deaf STT is caught by
  the canary in preflight rather than passed off as a silent agent.

Every provider runs behind ``httpx.MockTransport`` on loopback: nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
import math
from array import array
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from opseval import audio as codecs
from opseval.config import REDACTED, SECRET_FIELDS, Settings, config_digest
from opseval.errors import (
    AllowedHostError,
    ConfigError,
    HarnessError,
    RateLimited,
    SttError,
    SttRateLimited,
    SttRequestError,
    TransportTimeout,
    TtsError,
    TtsRateLimited,
    TtsRequestError,
)
from opseval.llm import SPEECH_PRICES, UsageMeter
from opseval.models.preflight import CheckStatus
from opseval.models.profile import SutProfile
from opseval.models.results import ModelUsage
from opseval.plugins import STT_GROUP, TELEPHONY_GROUP, TTS_GROUP, reset_for_tests
from opseval.preflight import canary_phrase, check_stt, check_tts, preflight, speech_checks
from opseval.speech import Audio, SpeechConfig, SpeechToText, TextToSpeech, Transcript
from opseval.speech import openai as builtin
from opseval.speech.guard import GuardedSpeechToText, GuardedTextToSpeech, resolve_voice
from opseval.speech.providers import (
    check_second_stt,
    second_speech_to_text,
    speech_to_text,
    text_to_speech,
)
from opseval.speech.wer import edit_distance, normalise, word_error_rate

from .test_plugins import FakeEntryPoint

SECRET = "sk-speech-secret-value"
TTS_URL = "http://127.0.0.1:9101"
STT_URL = "http://127.0.0.1:9102"


def tone(rate: int = 16_000, seconds: float = 0.5) -> bytes:
    n = int(rate * seconds)
    return array(
        "h", (int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n))
    ).tobytes()


class FakeSpeechServer:
    """An OpenAI-compatible TTS and STT. ``tts``/``stt`` pick a behaviour; the STT echoes the
    last phrase the TTS spoke (a perfect ear) unless told what to hear."""

    def __init__(self) -> None:
        self.tts = "wav"
        self.stt = "ok"
        self.heard: str | None = None
        self.last_spoken = ""
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/audio/speech":
            return self._speech(request)
        if request.url.path == "/v1/audio/transcriptions":
            return self._transcribe(request)
        return httpx.Response(404)

    def _speech(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.last_spoken = body["input"]
        mode = self.tts
        if mode == "timeout":
            raise httpx.ReadTimeout("stalled", request=request)
        if mode == "refused":
            raise httpx.ConnectError("refused", request=request)
        if mode.isdigit():
            return httpx.Response(int(mode), text="nope")
        if mode == "garbage":
            return httpx.Response(
                200, content=b"\x00\x01garbage", headers={"content-type": "audio/wav"}
            )
        if mode == "json":
            return httpx.Response(200, json={"error": "quota"})
        if mode == "empty":
            return httpx.Response(200, content=b"", headers={"content-type": "audio/wav"})
        if mode == "pcm":
            return httpx.Response(200, content=tone(24_000), headers={"content-type": "audio/pcm"})
        if mode == "odd-pcm":
            return httpx.Response(
                200, content=b"\x01\x02\x03", headers={"content-type": "audio/pcm"}
            )
        return httpx.Response(
            200, content=codecs.to_wav(tone(), 16_000), headers={"content-type": "audio/wav"}
        )

    def _transcribe(self, request: httpx.Request) -> httpx.Response:
        mode = self.stt
        if mode == "timeout":
            raise httpx.ReadTimeout("stalled", request=request)
        if mode.isdigit():
            return httpx.Response(int(mode), text="nope")
        if mode == "not-json":
            return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})
        if mode == "no-text":
            return httpx.Response(200, json={"segments": []})
        text = self.heard if self.heard is not None else self.last_spoken
        if mode == "plain":
            return httpx.Response(200, json={"text": text})
        return httpx.Response(
            200,
            json={
                "text": f" {text} ",
                "words": [
                    {"word": w, "start": i * 0.3, "end": i * 0.3 + 0.25}
                    for i, w in enumerate(text.split())
                ],
                "segments": [{"avg_logprob": -0.1}, {"avg_logprob": -0.3}],
            },
        )


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeSpeechServer:
    fake = FakeSpeechServer()
    monkeypatch.setattr(
        builtin,
        "new_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> Any:
    async def instant(_s: float) -> None:
        return None

    monkeypatch.setattr(builtin, "backoff_sleep", instant)
    reset_for_tests()
    yield
    reset_for_tests()


def settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "allowed_hosts": "127.0.0.1",
        "tts_url": TTS_URL,
        "tts_model": "tts-1",
        "tts_key": SecretStr(SECRET),
        "tts_voice_map": {"default": "alloy", "ar": "onyx", "calm": "nova"},
        "stt_url": STT_URL,
        "stt_model": "whisper-1",
        "stt_key": SecretStr(SECRET),
    }
    return Settings(**{**base, **kw})


def tts(s: Settings | None = None, meter: UsageMeter | None = None) -> GuardedTextToSpeech:
    built = text_to_speech(s or settings(), meter or UsageMeter())
    assert built is not None
    return built


def stt(s: Settings | None = None, meter: UsageMeter | None = None) -> GuardedSpeechToText:
    built = speech_to_text(s or settings(), meter or UsageMeter())
    assert built is not None
    return built


AUDIO = Audio(pcm16=tone(), rate=16_000)


# --- TTS -------------------------------------------------------------------------------


class TestTextToSpeech:
    async def test_wav_is_decoded_to_pcm16_with_its_rate(self, server: FakeSpeechServer) -> None:
        audio = await tts().synthesize("hello there", voice=None, language="en")
        assert audio.rate == 16_000 and audio.pcm16 == tone()
        body = json.loads(server.requests[0].content)
        assert body == {
            "model": "tts-1",
            "input": "hello there",
            "voice": "alloy",
            "response_format": "wav",
        }
        assert server.requests[0].headers["authorization"] == f"Bearer {SECRET}"

    async def test_raw_pcm_takes_the_configured_rate(self, server: FakeSpeechServer) -> None:
        server.tts = "pcm"
        audio = await tts(settings(tts_format="pcm")).synthesize("hi", voice=None, language="en")
        assert audio.rate == 24_000 and len(audio.pcm16) == len(tone(24_000))
        assert json.loads(server.requests[0].content)["response_format"] == "pcm"

    async def test_the_voice_map_is_consulted(self, server: FakeSpeechServer) -> None:
        client = tts()
        await client.synthesize("a", voice="calm", language="en")
        await client.synthesize("b", voice=None, language="ar-EG")
        await client.synthesize("c", voice="shimmer", language="fr")
        voices = [json.loads(r.content)["voice"] for r in server.requests]
        assert voices == ["nova", "onyx", "alloy"]

    def test_voice_resolution_order(self) -> None:
        assert resolve_voice({}, "shimmer", "en") == "shimmer"
        assert resolve_voice({}, None, "en") is None
        assert resolve_voice({"ar-EG": "x", "ar": "y"}, None, "ar-EG") == "x"
        assert resolve_voice({"ar": "y", "default": "z"}, "unmapped", "ar-SA") == "y"

    async def test_no_voice_at_all_is_a_configuration_error_of_opseval(
        self, server: FakeSpeechServer
    ) -> None:
        with pytest.raises(TtsRequestError, match="OPSEVAL_TTS_VOICE_MAP"):
            await tts(settings(tts_voice_map={})).synthesize("a", voice=None, language="en")
        assert server.requests == []

    @pytest.mark.parametrize("mode", ["500", "503", "timeout", "refused"])
    async def test_an_outage_is_a_tts_error_after_retries(
        self, server: FakeSpeechServer, mode: str
    ) -> None:
        server.tts = mode
        with pytest.raises(TtsError, match="unavailable after 6 attempts") as caught:
            await tts().synthesize("a", voice=None, language="en")
        assert isinstance(caught.value, HarnessError)
        assert not isinstance(caught.value, TransportTimeout | RateLimited)
        assert len(server.requests) == 6

    async def test_a_429_that_outlasts_its_retries_is_rate_limited(
        self, server: FakeSpeechServer
    ) -> None:
        server.tts = "429"
        meter = UsageMeter()
        with pytest.raises(TtsRateLimited) as caught:
            await tts(meter=meter).synthesize("abc", voice=None, language="en")
        assert isinstance(caught.value, RateLimited | TtsError)
        (usage,) = meter.snapshot()
        assert usage.rate_limited_calls == 1 and usage.audio_out_seconds is None

    async def test_another_4xx_fails_at_once(self, server: FakeSpeechServer) -> None:
        server.tts = "400"
        with pytest.raises(TtsRequestError, match="HTTP 400"):
            await tts().synthesize("a", voice=None, language="en")
        assert len(server.requests) == 1

    @pytest.mark.parametrize("mode", ["garbage", "json", "empty", "odd-pcm"])
    async def test_audio_opseval_cannot_decode_is_the_providers_fault(
        self, server: FakeSpeechServer, mode: str
    ) -> None:
        server.tts = mode
        fmt = "pcm" if mode == "odd-pcm" else "wav"
        with pytest.raises(TtsError) as caught:
            await tts(settings(tts_format=fmt)).synthesize("a", voice=None, language="en")
        assert not isinstance(caught.value, TransportTimeout)

    async def test_its_own_timeout_bounds_the_whole_call(self) -> None:
        class Stalls(TextToSpeech):
            name = "stalls"

            async def synthesize(self, text: str, *, voice: str | None, language: str) -> Audio:
                await asyncio.sleep(10)
                raise AssertionError("unreachable")

        config = SpeechConfig(
            role="tts", provider="stalls", url=None, model=None, timeout_seconds=0.01
        )
        guard = GuardedTextToSpeech(Stalls(config), config)
        with pytest.raises(TtsError, match="timed out.*OPSEVAL_TTS_TIMEOUT_SECONDS"):
            await guard.synthesize("a", voice=None, language="en")


# --- STT -------------------------------------------------------------------------------


class TestSpeechToText:
    async def test_verbose_json_gives_words_and_confidence(self, server: FakeSpeechServer) -> None:
        server.heard = "my refund please"
        transcript = await stt().transcribe(AUDIO, language="ar-EG", hint="order")
        assert transcript.text == "my refund please"
        assert [w.word for w in transcript.words] == ["my", "refund", "please"]
        assert transcript.words[1].start_ms == pytest.approx(300)
        expected = (math.exp(-0.1) + math.exp(-0.3)) / 2
        assert transcript.confidence == pytest.approx(expected)
        assert (transcript.provider, transcript.model) == ("openai", "whisper-1")
        info = transcript.info()
        assert info.source == "opseval" and info.confidence == transcript.confidence
        body = server.requests[0].content
        assert b'name="language"\r\n\r\nar\r\n' in body
        assert b"verbose_json" in body and b'name="prompt"' in body
        assert b'filename="audio.wav"' in body and b"RIFF" in body

    async def test_a_field_the_provider_omits_is_not_measured(
        self, server: FakeSpeechServer
    ) -> None:
        server.stt, server.heard = "plain", "hello"
        transcript = await stt().transcribe(AUDIO, language="en")
        assert transcript.confidence is None and transcript.words == ()

    async def test_an_empty_transcript_is_evidence_not_an_error(
        self, server: FakeSpeechServer
    ) -> None:
        server.heard = ""
        transcript = await stt().transcribe(AUDIO, language="en")
        assert transcript.text == "" and transcript.words == ()

    def test_malformed_word_timings_are_dropped_whole(self) -> None:
        good = {"word": "a", "start": 0, "end": 0.1}
        assert builtin._words([good, {"word": "b", "start": 0.2}]) == ()
        assert builtin._words([good, {"word": "b", "start": 0.5, "end": 0.1}]) == ()
        assert builtin._words("nope") == ()
        assert builtin._words([{**good, "probability": 2.0}])[0].confidence is None
        assert builtin._words([{**good, "probability": 0.8}])[0].confidence == 0.8
        assert builtin._confidence([{"avg_logprob": -0.1}, {}]) is None
        assert builtin._confidence([{"avg_logprob": True}]) is None
        assert builtin._confidence([]) is None

    @pytest.mark.parametrize("mode", ["not-json", "no-text"])
    async def test_an_unreadable_body_is_an_stt_error(
        self, server: FakeSpeechServer, mode: str
    ) -> None:
        server.stt = mode
        with pytest.raises(SttError):
            await stt().transcribe(AUDIO, language="en")

    @pytest.mark.parametrize(
        ("mode", "error"), [("503", SttError), ("timeout", SttError), ("429", SttRateLimited)]
    )
    async def test_an_outage_is_an_stt_error_never_the_agents_timeout(
        self, server: FakeSpeechServer, mode: str, error: type[Exception]
    ) -> None:
        server.stt = mode
        meter = UsageMeter()
        with pytest.raises(error) as caught:
            await stt(meter=meter).transcribe(AUDIO, language="en")
        assert isinstance(caught.value, HarnessError)
        assert not isinstance(caught.value, TransportTimeout)
        (usage,) = meter.snapshot()
        assert usage.role == "stt" and usage.audio_in_seconds == pytest.approx(0.5)

    async def test_a_rejected_request_fails_at_once(self, server: FakeSpeechServer) -> None:
        server.stt = "401"
        with pytest.raises(SttRequestError):
            await stt().transcribe(AUDIO, language="en")
        assert len(server.requests) == 1


# --- guards around plugins ------------------------------------------------------------


class Raises(SpeechToText):
    name = "raises"
    error: BaseException = TimeoutError(f"slow; key was {SECRET}")

    async def transcribe(
        self, audio: Audio, *, language: str, hint: str | None = None
    ) -> Transcript:
        raise self.error


def guarded(plugin: type[SpeechToText], **kw: Any) -> GuardedSpeechToText:
    config = SpeechConfig(
        role="stt", provider=plugin.name, url=None, model="m", timeout_seconds=5, **kw
    )
    return GuardedSpeechToText(plugin(config), config)


class TestGuards:
    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError(f"slow {SECRET}"),
            TransportTimeout(f"agent? {SECRET}"),
            RuntimeError(SECRET),
            SttError(f"plugin's own message {SECRET}"),
        ],
    )
    async def test_anything_a_plugin_raises_is_an_stt_error_without_its_message(
        self, error: BaseException
    ) -> None:
        class Plugin(Raises):
            pass

        Plugin.error = error
        with pytest.raises(SttError) as caught:
            await guarded(Plugin).transcribe(AUDIO, language="en")
        assert SECRET not in str(caught.value)
        assert not isinstance(caught.value, TransportTimeout)
        assert "raises" in str(caught.value)

    async def test_a_plugins_rate_limit_stays_typed(self) -> None:
        class Limited(SttError, RateLimited):
            pass

        class Plugin(Raises):
            error = Limited("429")

        with pytest.raises(SttRateLimited):
            await guarded(Plugin).transcribe(AUDIO, language="en")

    @pytest.mark.parametrize("answer", ["text", Transcript("x", "p", "m", confidence=1.5)])
    async def test_an_answer_that_breaks_the_contract_is_an_stt_error(self, answer: Any) -> None:
        class Plugin(SpeechToText):
            name = "odd"

            async def transcribe(
                self, audio: Audio, *, language: str, hint: str | None = None
            ) -> Transcript:
                return answer  # type: ignore[no-any-return]

        with pytest.raises(SttError):
            await guarded(Plugin).transcribe(AUDIO, language="en")

    async def test_tts_that_returns_nothing_is_a_tts_error(self) -> None:
        class Silent(TextToSpeech):
            name = "silent"

            async def synthesize(self, text: str, *, voice: str | None, language: str) -> Audio:
                return Audio(pcm16=b"", rate=16_000)

        config = SpeechConfig(
            role="tts", provider="silent", url=None, model=None, timeout_seconds=5
        )
        with pytest.raises(TtsError, match="no audio"):
            await GuardedTextToSpeech(Silent(config), config).synthesize(
                "a", voice=None, language="en"
            )

    def test_audio_validates_itself(self) -> None:
        with pytest.raises(ValueError):
            Audio(pcm16=b"\x00", rate=16_000)
        with pytest.raises(ValueError):
            Audio(pcm16=b"", rate=0)


# --- usage ----------------------------------------------------------------------------


class TestUsage:
    async def test_speech_is_counted_per_role_and_model(self, server: FakeSpeechServer) -> None:
        meter = UsageMeter()
        await tts(meter=meter).synthesize("hello", voice=None, language="en")
        await stt(meter=meter).transcribe(AUDIO, language="en")
        meter.record("judge", "gpt", {"prompt_tokens": 3, "completion_tokens": 1}, 0)
        by_role = {u.role: u for u in meter.snapshot()}
        assert by_role["tts"].characters == 5
        assert by_role["tts"].audio_out_seconds == pytest.approx(0.5)
        assert by_role["tts"].audio_in_seconds is None and by_role["tts"].cost_usd is None
        assert by_role["stt"].audio_in_seconds == pytest.approx(0.5)
        assert by_role["stt"].characters is None
        judge = by_role["judge"]
        assert (judge.audio_in_seconds, judge.audio_out_seconds, judge.characters) == (
            None,
            None,
            None,
        )

    def test_a_price_per_character_or_per_second(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(SPEECH_PRICES, "tts-1", (None, 0.00001))
        monkeypatch.setitem(SPEECH_PRICES, "whisper-1", (0.0001, None))
        meter = UsageMeter()
        meter.record_speech("tts", "tts-1", characters=1000, audio_out_seconds=2.0)
        meter.record_speech("stt", "whisper-1", audio_in_seconds=60.0)
        meter.record_speech("stt", "unpriced", audio_in_seconds=60.0)
        costs = {u.model: u.cost_usd for u in meter.snapshot()}
        assert costs["tts-1"] == pytest.approx(0.01)
        assert costs["whisper-1"] == pytest.approx(0.006)
        assert costs["unpriced"] is None

    def test_a_plugin_and_the_built_in_with_one_model_id_are_counted_apart(self) -> None:
        config = SpeechConfig(
            role="stt", provider="acme", url=None, model="whisper-1", timeout_seconds=1
        )
        assert config.model_label == "acme:whisper-1"

    def test_a_result_written_before_speech_usage_still_reads(self) -> None:
        old = {"role": "judge", "model": "m", "calls": 1, "input_tokens": 2, "output_tokens": 3}
        usage = ModelUsage.model_validate(old)
        assert usage.audio_in_seconds is None and usage.characters is None


# --- configuration --------------------------------------------------------------------


class TestConfiguration:
    def test_not_configured_is_none(self) -> None:
        s = settings(tts_url=None, stt_model=None)
        assert text_to_speech(s, UsageMeter()) is None
        assert speech_to_text(s, UsageMeter()) is None

    @pytest.mark.parametrize("field", ["tts_url", "stt_url"])
    def test_a_host_not_allowlisted_is_refused(self, field: str) -> None:
        s = settings(**{field: "https://speech.example.com"})
        with pytest.raises(AllowedHostError, match="speech.example.com"):
            (text_to_speech if field == "tts_url" else speech_to_text)(s, UsageMeter())

    def test_plain_http_off_loopback_is_refused(self) -> None:
        s = settings(allowed_hosts="speech.example.com", stt_url="http://speech.example.com")
        with pytest.raises(AllowedHostError, match="must use"):
            speech_to_text(s, UsageMeter())

    def test_tls_on_an_allowlisted_host_is_accepted(self) -> None:
        s = settings(allowed_hosts="speech.example.com", tts_url="https://speech.example.com")
        assert isinstance(text_to_speech(s, UsageMeter()), GuardedTextToSpeech)

    def test_every_speech_key_is_a_redacted_secret(self) -> None:
        s = settings(stt_second_key=SecretStr(SECRET))
        digest = config_digest(s)
        for field in ("tts_key", "stt_key", "stt_second_key"):
            assert field in SECRET_FIELDS
            assert digest[field] == REDACTED
        assert SECRET not in json.dumps(digest, default=str)

    def test_the_voice_map_reads_from_the_environment_as_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPSEVAL_TTS_VOICE_MAP", '{"ar": "onyx"}')
        assert Settings(_env_file=None).tts_voice_map == {"ar": "onyx"}


class TestSecondStt:
    def test_the_same_provider_and_model_twice_is_refused(self) -> None:
        s = settings(stt_second_url="http://127.0.0.1:9103", stt_second_model="whisper-1")
        with pytest.raises(ConfigError, match="proves nothing"):
            second_speech_to_text(s, UsageMeter())

    def test_a_different_model_is_a_second_opinion(self) -> None:
        s = settings(stt_second_url="http://127.0.0.1:9103", stt_second_model="large-v3")
        second = second_speech_to_text(s, UsageMeter())
        assert second is not None and second.identity == ("openai", "large-v3")
        assert second.config.label == "second STT"

    def test_unset_is_none(self) -> None:
        assert second_speech_to_text(settings(), UsageMeter()) is None

    def test_partly_configured_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="needs OPSEVAL_STT_SECOND_URL"):
            check_second_stt(settings(stt_second_model="large-v3"))

    def test_without_a_first_it_is_refused(self) -> None:
        s = settings(stt_url=None, stt_second_url=STT_URL, stt_second_model="large-v3")
        with pytest.raises(ConfigError, match="no first STT"):
            check_second_stt(s)

    def test_it_is_allowlisted_like_the_first(self) -> None:
        s = settings(stt_second_url="https://other.example.com", stt_second_model="large-v3")
        with pytest.raises(AllowedHostError):
            second_speech_to_text(s, UsageMeter())


# --- plugins --------------------------------------------------------------------------


class AcmeStt(SpeechToText):
    name = "acme"
    built: list[SpeechConfig] = []

    def __init__(self, config: SpeechConfig) -> None:
        super().__init__(config)
        AcmeStt.built.append(config)

    async def transcribe(
        self, audio: Audio, *, language: str, hint: str | None = None
    ) -> Transcript:
        return Transcript("heard", self.name, self.config.model)


class AcmeTts(TextToSpeech):
    name = "acme"

    async def synthesize(self, text: str, *, voice: str | None, language: str) -> Audio:
        return AUDIO


def install(monkeypatch: pytest.MonkeyPatch, group: str, *entries: FakeEntryPoint) -> None:
    monkeypatch.setattr(
        "opseval.plugins._iter_entry_points",
        lambda g: iter(entries if g == group else ()),
    )


class TestPlugins:
    def test_the_groups(self) -> None:
        assert (TTS_GROUP, STT_GROUP, TELEPHONY_GROUP) == (
            "opseval.tts",
            "opseval.stt",
            "opseval.telephony",
        )

    async def test_the_named_plugin_is_built_from_its_settings_and_guarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(
            monkeypatch,
            STT_GROUP,
            FakeEntryPoint("acme", AcmeStt),
            FakeEntryPoint("other", RuntimeError("must not be imported")),
        )
        AcmeStt.built.clear()
        client = stt(settings(stt_provider="acme", stt_url=None, stt_model="acme-1"))
        assert not client.trusted
        assert (await client.transcribe(AUDIO, language="en")).text == "heard"
        (config,) = AcmeStt.built
        assert config.key == SECRET and config.model == "acme-1" and config.url is None
        assert SECRET not in repr(config)

    async def test_a_tts_plugin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install(monkeypatch, TTS_GROUP, FakeEntryPoint("acme", AcmeTts))
        client = tts(settings(tts_provider="acme"))
        assert (await client.synthesize("x", voice=None, language="en")) == AUDIO

    def test_a_name_nobody_provides_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install(monkeypatch, STT_GROUP, FakeEntryPoint("acme", AcmeStt))
        with pytest.raises(ConfigError, match=r"OPSEVAL_STT_PROVIDER names 'gcc'.*acme"):
            speech_to_text(settings(stt_provider="gcc"), UsageMeter())

    def test_the_second_stt_names_its_own_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install(monkeypatch, STT_GROUP)
        s = settings(stt_second_provider="gcc")
        with pytest.raises(ConfigError, match="OPSEVAL_STT_SECOND_PROVIDER names 'gcc'"):
            second_speech_to_text(s, UsageMeter())

    def test_an_unimportable_plugin_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install(monkeypatch, TTS_GROUP, FakeEntryPoint("acme", ImportError("no acme")))
        with pytest.raises(ConfigError, match="could not be imported"):
            text_to_speech(settings(tts_provider="acme"), UsageMeter())

    class Abstract(SpeechToText):
        name = "abstract"

    class Sync(SpeechToText):
        name = "sync"

        def transcribe(  # type: ignore[override]
            self, audio: Audio, *, language: str, hint: str | None = None
        ) -> Transcript:
            return Transcript("x", "sync", None)

    class Unnamed(SpeechToText):
        async def transcribe(
            self, audio: Audio, *, language: str, hint: str | None = None
        ) -> Transcript:
            return Transcript("x", "x", None)

    class PosesAsBuiltin(Unnamed):
        name = "openai"

    @pytest.mark.parametrize(
        ("obj", "message"),
        [
            (AcmeTts, "deriving from opseval.speech.SpeechToText"),
            ("not a class", "deriving from"),
            (Abstract, "abstract"),
            (Sync, "async def transcribe"),
            (Unnamed, "its own 'name'"),
            (PosesAsBuiltin, "its own 'name'"),
        ],
    )
    def test_a_contract_violation_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, obj: Any, message: str
    ) -> None:
        install(monkeypatch, STT_GROUP, FakeEntryPoint("acme", obj))
        with pytest.raises(ConfigError, match=message):
            speech_to_text(settings(stt_provider="acme"), UsageMeter())

    def test_a_constructor_that_raises_is_refused_without_its_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Broken(AcmeStt):
            def __init__(self, config: SpeechConfig) -> None:
                raise ValueError(f"bad key {config.key}")

        install(monkeypatch, STT_GROUP, FakeEntryPoint("acme", Broken))
        with pytest.raises(ConfigError, match="could not be created: ValueError") as caught:
            speech_to_text(settings(stt_provider="acme"), UsageMeter())
        assert SECRET not in str(caught.value)

    def test_the_plugin_url_is_allowlisted_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install(monkeypatch, STT_GROUP, FakeEntryPoint("acme", AcmeStt))
        s = settings(stt_provider="acme", stt_url="https://gcc.example.com")
        with pytest.raises(AllowedHostError):
            speech_to_text(s, UsageMeter())


# --- preflight ------------------------------------------------------------------------


class TestPreflight:
    async def test_nothing_is_sent_when_no_scenario_speaks(self, server: FakeSpeechServer) -> None:
        checks = await speech_checks(settings(allowed_hosts=""), 0)
        assert [c.status for c in checks] == [CheckStatus.NOT_NEEDED] * 2
        assert server.requests == []

    async def test_a_healthy_canary(self, server: FakeSpeechServer) -> None:
        tts_check, stt_check = await speech_checks(settings(), 2)
        assert tts_check.status is CheckStatus.OK and tts_check.needed_by == 2
        assert stt_check.status is CheckStatus.OK, stt_check.detail
        assert "WER 0.00" in stt_check.detail
        assert server.last_spoken == canary_phrase("en")

    async def test_an_arabic_canary(self, server: FakeSpeechServer) -> None:
        server.heard = "أُرِيدُ أن أعرف، حالة طلب الاسترداد؟"  # diacritics and Arabic punctuation
        check = await check_stt(settings(), 1, language="ar-EG")
        assert check.status is CheckStatus.OK, check.detail
        assert server.last_spoken == canary_phrase("ar")

    async def test_a_deaf_stt_is_unavailable_not_an_agent_failure(
        self, server: FakeSpeechServer
    ) -> None:
        server.heard = ""
        check = await check_stt(settings(), 1)
        assert check.status is CheckStatus.UNAVAILABLE and check.blocking
        assert "WER 1.00 over OPSEVAL_STT_CANARY_MAX_WER 0.25" in check.detail

    async def test_a_tts_outage_is_unavailable(self, server: FakeSpeechServer) -> None:
        server.tts = "503"
        check = await check_tts(settings(), 1)
        assert check.status is CheckStatus.UNAVAILABLE and "after 6 attempts" in check.detail

    async def test_without_known_speech_the_stt_is_only_unverified(
        self, server: FakeSpeechServer
    ) -> None:
        check = await check_stt(settings(tts_url=None), 1)
        assert check.status is CheckStatus.UNVERIFIED
        assert "OPSEVAL_STT_CANARY_WAV" in check.detail
        assert len(server.requests) == 1  # a second of silence proved it answers

    async def test_an_operator_wav_is_a_canary(
        self, server: FakeSpeechServer, tmp_path: Path
    ) -> None:
        wav = tmp_path / "canary.wav"
        wav.write_bytes(codecs.to_wav(tone(), 16_000))
        server.heard = "Hello, world!"
        s = settings(tts_url=None, stt_canary_wav=str(wav), stt_canary_text="hello world")
        check = await check_stt(s, 1)
        assert check.status is CheckStatus.OK, check.detail

    async def test_an_unreadable_operator_wav_is_named(
        self, server: FakeSpeechServer, tmp_path: Path
    ) -> None:
        s = settings(tts_url=None, stt_canary_wav=str(tmp_path / "x.wav"), stt_canary_text="x")
        check = await check_stt(s, 1)
        assert check.status is CheckStatus.UNVERIFIED and "could not be read" in check.detail

    async def test_an_stt_outage_is_unavailable(self, server: FakeSpeechServer) -> None:
        server.stt = "500"
        check = await check_stt(settings(), 1)
        assert check.status is CheckStatus.UNAVAILABLE

    async def test_not_configured(self) -> None:
        s = settings(tts_url=None, stt_url=None)
        assert (await check_tts(s, 1)).status is CheckStatus.NOT_CONFIGURED
        assert (await check_stt(s, 1)).status is CheckStatus.NOT_CONFIGURED

    async def test_the_second_stt_is_checked_too(self, server: FakeSpeechServer) -> None:
        s = settings(stt_second_url="http://127.0.0.1:9103", stt_second_model="large-v3")
        names = [c.name for c in await speech_checks(s, 1)]
        assert names == ["tts", "stt", "stt_second"]

    async def test_a_refused_host_stops_preflight_before_anything_is_probed(
        self, server: FakeSpeechServer
    ) -> None:
        s = settings(stt_url="https://asr.example.com")
        with pytest.raises(AllowedHostError):
            await preflight(
                [], SutProfile(name="p"), s, target="", transport="python", voice_scenarios=1
            )
        assert server.requests == []

    async def test_a_suite_without_voice_has_no_speech_checks(self) -> None:
        report = await preflight(
            [],
            SutProfile(name="p"),
            settings(stt_url="https://asr.example.com"),
            target="",
            transport="python",
        )
        assert {c.name for c in report.checks}.isdisjoint({"tts", "stt", "stt_second"})

    async def test_a_voice_suite_carries_them(self, server: FakeSpeechServer) -> None:
        report = await preflight(
            [], SutProfile(name="p"), settings(), target="", transport="python", voice_scenarios=1
        )
        assert report.get("stt") is not None and report.get("tts") is not None


# --- WER ------------------------------------------------------------------------------


class TestWordErrorRate:
    @pytest.mark.parametrize(
        ("reference", "hypothesis", "wer"),
        [
            ("please check my refund", "please check my refund", 0.0),
            ("Please check my refund.", "please, CHECK my refund!", 0.0),
            ("please check my refund", "please check a refund", 0.25),
            ("please check my refund", "please my refund", 0.25),
            ("please check my refund", "please do check my refund now", 0.5),
            ("please check my refund", "", 1.0),
            ("", "", 0.0),
            ("", "noise", 1.0),
            ("hi", "a b c", 3.0),
            # Arabic: diacritics, tatweel and Arabic punctuation are not errors
            ("أريد أن أعرف حالة طلبي", "أُرِيدُ أنْ أعـــرف، حالة طلبي؟", 0.0),
            ("أريد أن أعرف حالة طلبي", "أريد أعرف حالة طلبك", 0.4),
        ],
    )
    def test_cases(self, reference: str, hypothesis: str, wer: float) -> None:
        assert word_error_rate(reference, hypothesis) == pytest.approx(wer)

    def test_normalisation(self) -> None:
        assert normalise("ﻻ") == ["لا"]  # an Arabic presentation form, by NFKC
        assert normalise("Ｈｅｌｌｏ؛ World") == ["hello", "world"]
        assert normalise("STRASSE straße") == ["strasse", "strasse"]

    def test_edit_distance(self) -> None:
        assert edit_distance([], ["a"]) == 1
        assert edit_distance(["a", "b"], ["b", "a"]) == 2
