# SPDX-License-Identifier: Apache-2.0
"""``audio-ws`` and ``audio-http`` against fake agents on loopback, the runner's voice rules,
and ``opseval demo --voice`` (ADR-0053)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner
from websockets.asyncio.server import ServerConnection, serve

from opseval.audio import decode, encode, from_wav, to_wav
from opseval.cli import app
from opseval.config import Settings
from opseval.demo import tones, voice_catalogue_dir
from opseval.demo.loopback import Request, Response, serve_http
from opseval.demo.voice_agent import run_voice_agent
from opseval.driver import Driver
from opseval.errors import AllowedHostError, ConfigError, UnsupportedCapability
from opseval.models.profile import AudioConfig, AudioEnvelope, AudioHttp, SutProfile
from opseval.models.record import RunRecord, TerminalState
from opseval.runner import RunOptions, build_transport_factory, launch, speech_kit
from opseval.simulator import ScriptedSimulator
from opseval.speech.base import Audio, Transcript
from opseval.transport.audio import DTMF
from opseval.transport.audio_http import AudioHttpTransport
from opseval.transport.audio_ws import AudioWsSession, AudioWsTransport
from opseval.voice import AudioRecorder, SpeechKit, VoiceTransport

from .test_voice_bridge import ToneSTT, ToneTTS, config, kit, scenario

RATE = 8_000


def ws_profile(**envelope: Any) -> SutProfile:
    return SutProfile(
        name="voice-ws",
        audio=AudioConfig(
            codec="mulaw",
            rate=RATE,
            end_of_turn_silence_ms=200,
            envelope=AudioEnvelope(
                start_frame={"type": "start", "codec": "{codec}", "rate": "{rate}"}, **envelope
            ),
        ),
        turn_quiet_ms=100,
    )


def http_profile(**http: Any) -> SutProfile:
    return SutProfile(
        name="voice-http",
        audio=AudioConfig(codec="pcm16", rate=16_000, http=AudioHttp(**http)),
        turn_quiet_ms=100,
    )


async def run(
    audio: Any,
    prof: SutProfile,
    *turns: str,
    timeout: float = 1.5,
    speech: SpeechKit | None = None,
) -> RunRecord:
    sc = scenario(*turns, turn_timeout_seconds=timeout)
    transport = VoiceTransport(audio, prof, speech or kit()).for_attempt(
        AudioRecorder(None, sc.id, 0), "en"
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


WsHandler = Callable[[ServerConnection], Awaitable[None]]


@asynccontextmanager
async def ws_agent(handler: WsHandler) -> AsyncIterator[str]:
    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


async def caller_turn(conn: ServerConnection, seen: list[dict[str, Any]]) -> str:
    """Read until the caller's end of turn; what the caller said."""
    wire = b""
    async for message in conn:
        if isinstance(message, bytes):
            wire += message
            continue
        frame = json.loads(message)
        seen.append(frame)
        if frame["type"] == "audio":
            wire += base64.b64decode(frame["audio"])
        if frame["type"] == "end_of_turn":
            break
    return tones.decode(decode(wire, "mulaw"), RATE)


def b64(pcm: bytes) -> str:
    return base64.b64encode(encode(pcm, "mulaw")).decode()


# --- audio-ws ----------------------------------------------------------------------------


class TestAudioWs:
    async def test_the_demo_agent_is_heard_over_mulaw(self) -> None:
        prof = ws_profile()
        async with run_voice_agent() as url:
            record = await run(AudioWsTransport(url, prof), prof, "Hi, order 10482 is broken.")
        assert record.terminal_state is TerminalState.GOAL_REACHED
        assert "10482" in (record.received()[0].text or "")

    async def test_the_start_frame_and_the_envelope(self) -> None:
        seen: list[dict[str, Any]] = []

        async def agent(conn: ServerConnection) -> None:
            await caller_turn(conn, seen)
            await conn.send(json.dumps({"type": "speech_start"}))
            await conn.send(json.dumps({"type": "audio", "audio": b64(tones.encode("Hi.", RATE))}))
            await conn.send(json.dumps({"type": "transcript", "text": "Hi."}))
            await conn.send(json.dumps({"type": "speech_end"}))

        prof = ws_profile()
        async with ws_agent(agent) as url:
            record = await run(AudioWsTransport(url, prof), prof, "Hello.")
        assert seen[0] == {"type": "start", "codec": "mulaw", "rate": RATE}
        assert record.received()[0].text == "Hi."
        assert record.received()[0].payload["_opseval_voice"]["agent_transcript"] == "Hi."

    async def test_binary_frames_both_ways(self) -> None:
        got_binary: list[bool] = []

        async def agent(conn: ServerConnection) -> None:
            wire = b""
            async for message in conn:
                if isinstance(message, bytes):
                    got_binary.append(True)
                    wire += message
                elif json.loads(message)["type"] == "end_of_turn":
                    break
            said = tones.decode(decode(wire, "mulaw"), RATE)
            await conn.send(encode(tones.encode(f"You said {said}", RATE), "mulaw"))
            await conn.send(json.dumps({"type": "speech_end"}))

        prof = ws_profile(binary=True)
        async with ws_agent(agent) as url:
            record = await run(AudioWsTransport(url, prof), prof, "binary works")
        assert got_binary and record.received()[0].text == "You said binary works"

    async def test_energy_based_end_of_turn_without_a_speech_end_event(self) -> None:
        async def agent(conn: ServerConnection) -> None:
            await caller_turn(conn, [])
            await conn.send(
                json.dumps({"type": "audio", "audio": b64(tones.encode("No end.", RATE))})
            )
            for _ in range(15):  # 300 ms of silence, past end_of_turn_silence_ms
                await conn.send(json.dumps({"type": "audio", "audio": b64(bytes(320))}))
            await conn.wait_closed()

        prof = ws_profile()
        async with ws_agent(agent) as url:
            record = await run(AudioWsTransport(url, prof), prof, "Hello.")
        assert record.received()[0].text == "No end."

    async def test_a_dropped_connection_is_not_judged(self) -> None:
        async def agent(conn: ServerConnection) -> None:
            await caller_turn(conn, [])
            await conn.close()

        prof = ws_profile()
        async with ws_agent(agent) as url:
            record = await run(AudioWsTransport(url, prof), prof, "Hello.")
        assert record.terminal_state is TerminalState.TRANSPORT_FAILURE

    async def test_silence_past_the_timeout_is_the_agents_timeout(self) -> None:
        async def agent(conn: ServerConnection) -> None:
            await caller_turn(conn, [])
            for _ in range(80):
                await conn.send(json.dumps({"type": "audio", "audio": b64(bytes(320))}))
            await conn.wait_closed()

        prof = ws_profile()
        async with ws_agent(agent) as url:
            record = await run(AudioWsTransport(url, prof), prof, "Hello.", timeout=1.0)
        assert record.terminal_state is TerminalState.TIMEOUT

    async def test_bad_base64_is_undecodable_audio_the_agents_fault(self) -> None:
        async def agent(conn: ServerConnection) -> None:
            await caller_turn(conn, [])
            await conn.send(json.dumps({"type": "audio", "audio": "not base64!!"}))
            await conn.wait_closed()

        prof = ws_profile()
        async with ws_agent(agent) as url:
            record = await run(AudioWsTransport(url, prof), prof, "Hello.")
        assert record.terminal_state is TerminalState.ERROR_FRAME
        assert record.received()[-1].audio is not None

    async def test_other_json_is_a_data_frame_for_the_driver(self) -> None:
        async def agent(conn: ServerConnection) -> None:
            await caller_turn(conn, [])
            await conn.send(json.dumps({"type": "result", "outcome": "done"}))
            await conn.send(
                json.dumps({"type": "audio", "audio": b64(tones.encode("Done.", RATE))})
            )
            await conn.send(json.dumps({"type": "speech_end"}))

        prof = ws_profile().model_copy(update={"kinds": {"result": "result"}})
        async with ws_agent(agent) as url:
            record = await run(AudioWsTransport(url, prof), prof, "Hello.")
        kinds = [f.kind.value for f in record.received()]
        assert kinds[:2] == ["result", "message"]

    async def test_a_notification_before_speech_keeps_the_wait_open(self) -> None:
        async def agent(conn: ServerConnection) -> None:
            await caller_turn(conn, [])
            await conn.send(json.dumps({"type": "response.created", "id": "r1"}))
            await asyncio.sleep(0.3)
            await conn.send(json.dumps({"type": "audio", "audio": b64(tones.encode("Hi.", RATE))}))
            await conn.send(json.dumps({"type": "speech_end"}))
            await conn.wait_closed()

        prof = ws_profile()
        async with ws_agent(agent) as url:
            record = await run(AudioWsTransport(url, prof), prof, "Hello.")
        received = record.received()
        assert received[0].payload == {"type": "response.created", "id": "r1"}
        assert received[1].text == "Hi."
        assert record.terminal_state is TerminalState.GOAL_REACHED

    async def test_a_data_frame_and_then_silence_is_the_agents_timeout(self) -> None:
        async def agent(conn: ServerConnection) -> None:
            await caller_turn(conn, [])
            await conn.send(json.dumps({"type": "response.created"}))
            await conn.wait_closed()

        prof = ws_profile()
        async with ws_agent(agent) as url:
            record = await run(AudioWsTransport(url, prof), prof, "Hello.", timeout=1.0)
        assert record.terminal_state is TerminalState.TIMEOUT
        assert record.received()[0].payload == {"type": "response.created"}

    async def test_a_hangup_and_then_a_close_is_the_agent_ending_the_call(self) -> None:
        async def agent(conn: ServerConnection) -> None:
            await caller_turn(conn, [])
            await conn.send(
                json.dumps({"type": "audio", "audio": b64(tones.encode("Goodbye.", RATE))})
            )
            await conn.send(json.dumps({"type": "speech_end"}))
            await conn.send(json.dumps({"type": "hangup"}))
            await conn.close()

        prof = ws_profile()
        async with ws_agent(agent) as url:
            record = await run(AudioWsTransport(url, prof), prof, "one", "two")
        assert record.terminal_state is TerminalState.TIMEOUT, "judged: never not-judged"
        assert "hung up" in (record.stop_reason or "") and not record.infra_errors
        async with ws_agent(agent) as url:
            done = await run(AudioWsTransport(url, prof), prof, "one")
        assert done.terminal_state is TerminalState.GOAL_REACHED
        assert done.voice is not None and "hangup" in [e.kind for e in done.voice.events]

    async def test_audio_is_stamped_when_it_arrives_not_when_it_is_read(self) -> None:
        async def agent(conn: ServerConnection) -> None:
            await caller_turn(conn, [])
            await conn.send(
                json.dumps({"type": "audio", "audio": b64(tones.encode("First.", RATE))})
            )
            await conn.send(json.dumps({"type": "speech_end"}))
            await asyncio.sleep(0.4)  # while Opseval is busy with the caller's next turn
            await conn.send(
                json.dumps({"type": "audio", "audio": b64(tones.encode("Second.", RATE))})
            )
            await conn.send(json.dumps({"type": "speech_end"}))
            await caller_turn(conn, [])
            await conn.wait_closed()

        class SlowTTS(ToneTTS):
            async def synthesize(self, text: str, *, voice: str | None, language: str) -> Audio:
                if text == "two":
                    await asyncio.sleep(0.7)
                return await super().synthesize(text, voice=voice, language=language)

        prof = ws_profile()
        speech = kit(tts=SlowTTS(config("tts")))
        async with ws_agent(agent) as url:
            record = await run(AudioWsTransport(url, prof), prof, "one", "two", speech=speech)
        spoken = [f for f in record.received() if f.text]
        assert [f.text for f in spoken] == ["First.", "Second."]
        assert spoken[0].latency_ms is not None
        assert spoken[1].latency_ms is None, "a barge-in: not measured, never 0.0"
        assert record.voice is not None
        assert "barge_in" in [e.kind for e in record.voice.events]

    async def test_an_unreachable_agent_is_a_transport_failure(self) -> None:
        prof = ws_profile()
        record = await run(AudioWsTransport("ws://127.0.0.1:1", prof, open_timeout=1), prof, "x")
        assert record.terminal_state is TerminalState.TRANSPORT_FAILURE

    async def test_dtmf_is_a_declared_capability(self) -> None:
        seen: list[dict[str, Any]] = []

        async def agent(conn: ServerConnection) -> None:
            async for message in conn:
                seen.append(json.loads(message))

        assert DTMF in AudioWsSession.capabilities
        prof = ws_profile()
        async with ws_agent(agent) as url:
            session = await AudioWsTransport(url, prof).open()
            await session.start()
            await session.send_dtmf("12#")
            await session.close()
        assert {"type": "dtmf", "digits": "12#"} in seen
        prof = ws_profile(dtmf_event=None)
        async with ws_agent(agent) as url:
            session = await AudioWsTransport(url, prof).open()
            await session.start()
            with pytest.raises(UnsupportedCapability):
                await session.send_dtmf("1")
            await session.close()


# --- audio-http --------------------------------------------------------------------------


Handler = Callable[[Request], Awaitable[Response]]


class TestAudioHttp:
    async def test_the_demo_agent_over_wav(self) -> None:
        prof = http_profile()
        async with run_voice_agent(transport="audio-http") as url:
            record = await run(
                AudioHttpTransport(url, prof), prof, "Order 10482 is broken.", "Yes."
            )
        assert record.terminal_state is TerminalState.GOAL_REACHED
        texts = [f.text for f in record.received()]
        assert "10482" in (texts[0] or "") and (texts[1] or "").startswith("Done.")

    async def test_raw_requests_json_answers_and_one_session_header(self) -> None:
        seen: list[Request] = []

        async def agent(request: Request) -> Response:
            seen.append(request)
            said = tones.decode(request.body, 16_000)
            wav = to_wav(tones.encode(f"Heard {said}", 16_000), 16_000)
            body = {"audio": base64.b64encode(wav).decode(), "transcript": "x"}
            return Response(200, json.dumps(body).encode(), "application/json")

        prof = http_profile(request_format="raw")
        async with serve_http(agent) as url:
            record = await run(AudioHttpTransport(url, prof), prof, "one", "two")
        assert [f.text for f in record.received()] == ["Heard one", "Heard two"]
        assert seen[0].headers["content-type"] == "application/octet-stream"
        ids = {r.headers["x-opseval-session"] for r in seen}
        assert len(ids) == 1 and "" not in ids

    async def test_a_json_answer_keeps_its_checkpoint_beside_the_audio(self) -> None:
        async def agent(_r: Request) -> Response:
            wav = to_wav(tones.encode("Shall I refund it?", 16_000), 16_000)
            body = {
                "audio": base64.b64encode(wav).decode(),
                "type": "needs_human",
                "checkpoint": "approve_refund",
                "payload": {"amount": 20},
            }
            return Response(200, json.dumps(body).encode(), "application/json")

        prof = SutProfile.model_validate(
            {
                **http_profile().model_dump(),
                "kinds": {"needs_human": "needs_human"},
                "checkpoints": {"approve_refund": {}},
                "commit_gate": "approve_refund",
            }
        )
        async with serve_http(agent) as url:
            record = await run(AudioHttpTransport(url, prof), prof, "Refund 10482.")
        assert record.terminal_state is TerminalState.STOPPED_AT_CHECKPOINT, "commit guard"
        assert [i.checkpoint for i in record.interrupts] == ["approve_refund"]
        kinds = [f.kind.value for f in record.received()]
        assert kinds == ["message", "needs_human"]
        assert record.received()[0].text == "Shall I refund it?"
        assert "audio" not in record.received()[1].payload

    async def test_a_json_answer_that_commits_is_seen(self) -> None:
        async def agent(_r: Request) -> Response:
            wav = to_wav(tones.encode("Refunded.", 16_000), 16_000)
            body = {
                "audio": base64.b64encode(wav).decode(),
                "type": "result",
                "outcome": "refunded",
            }
            return Response(200, json.dumps(body).encode(), "application/json")

        prof = SutProfile.model_validate(
            {
                **http_profile().model_dump(),
                "kinds": {"result": "result", "needs_human": "needs_human"},
                "checkpoints": {"approve_refund": {}},
                "commit_gate": "approve_refund",
                "committed_outcome": "refunded",
            }
        )
        async with serve_http(agent) as url:
            record = await run(AudioHttpTransport(url, prof), prof, "Refund 10482.")
        assert record.terminal_state is TerminalState.OUTCOME_COMMITTED

    async def test_ten_minutes_of_audio_at_once_is_the_agents_timeout_not_stt(self) -> None:
        heard: list[float] = []

        class Measuring(ToneSTT):
            async def transcribe(
                self, audio: Audio, *, language: str, hint: str | None = None
            ) -> Transcript:
                heard.append(len(audio.pcm16) / 2 / audio.rate)
                return await super().transcribe(audio, language=language, hint=hint)

        loud = b"\x00\x40\x00\xc0" * (16_000 * 600 // 2)  # 10 minutes with energy

        async def agent(_r: Request) -> Response:
            return Response(200, to_wav(loud, 16_000), "audio/wav")

        prof = http_profile()
        assert prof.audio is not None and prof.audio.max_turn_seconds == 60
        speech = kit(stt=Measuring(config("stt")))
        async with serve_http(agent) as url:
            record = await run(AudioHttpTransport(url, prof), prof, "x", timeout=10, speech=speech)
        assert record.terminal_state is TerminalState.TIMEOUT
        assert "max_turn_seconds" in (record.stop_reason or "")
        assert not record.infra_errors, "never an SttError"
        assert all(seconds <= 60 for seconds in heard), heard

    async def test_an_agent_5xx_is_an_error_frame(self) -> None:
        async def agent(_r: Request) -> Response:
            return Response(500, b"boom", "text/plain")

        prof = http_profile()
        async with serve_http(agent) as url:
            record = await run(AudioHttpTransport(url, prof), prof, "x")
        assert record.terminal_state is TerminalState.ERROR_FRAME

    async def test_a_4xx_is_opsevals_request_refused_not_judged(self) -> None:
        async def agent(_r: Request) -> Response:
            return Response(400, b"no", "text/plain")

        prof = http_profile()
        async with serve_http(agent) as url:
            record = await run(AudioHttpTransport(url, prof), prof, "x")
        assert record.terminal_state is TerminalState.TRANSPORT_FAILURE

    async def test_an_empty_answer_is_silence_the_agents_timeout(self) -> None:
        async def agent(_r: Request) -> Response:
            return Response(200, b"", "audio/wav")

        prof = http_profile()
        async with serve_http(agent) as url:
            record = await run(AudioHttpTransport(url, prof), prof, "x")
        assert record.terminal_state is TerminalState.TIMEOUT

    async def test_an_unreadable_wav_is_undecodable_audio(self) -> None:
        async def agent(_r: Request) -> Response:
            return Response(200, b"RIFFnot a wav at all", "audio/wav")

        prof = http_profile()
        async with serve_http(agent) as url:
            record = await run(AudioHttpTransport(url, prof), prof, "x")
        assert record.terminal_state is TerminalState.ERROR_FRAME

    async def test_a_wav_at_another_rate_is_resampled(self) -> None:
        async def agent(_r: Request) -> Response:
            return Response(200, to_wav(tones.encode("Resampled.", 24_000), 24_000), "audio/wav")

        prof = http_profile()
        async with serve_http(agent) as url:
            record = await run(AudioHttpTransport(url, prof), prof, "x")
        assert record.received()[0].text == "Resampled."

    @pytest.mark.parametrize(
        ("exc", "state"),
        [
            (httpx.ConnectTimeout("never connected"), TerminalState.TRANSPORT_FAILURE),
            (httpx.PoolTimeout("pool"), TerminalState.TRANSPORT_FAILURE),
            (httpx.ConnectError("refused"), TerminalState.TRANSPORT_FAILURE),
            (httpx.ReadTimeout("slow agent"), TerminalState.TIMEOUT),
        ],
    )
    async def test_request_faults_land_on_their_side_of_the_rule(
        self, exc: Exception, state: TerminalState
    ) -> None:
        def boom(_r: httpx.Request) -> httpx.Response:
            raise exc

        prof = http_profile()
        transport = AudioHttpTransport(
            "http://127.0.0.1:1/turn",
            prof,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        )
        record = await run(transport, prof, "x")
        assert record.terminal_state is state

    async def test_the_agent_is_sent_a_wav_of_what_the_caller_said(self) -> None:
        seen: list[bytes] = []

        async def agent(request: Request) -> Response:
            seen.append(request.body)
            return Response(200, to_wav(tones.encode("ok", 16_000), 16_000), "audio/wav")

        prof = http_profile()
        async with serve_http(agent) as url:
            await run(AudioHttpTransport(url, prof), prof, "the words")
        pcm, rate = from_wav(seen[0])
        assert rate == 16_000 and tones.decode(pcm, rate) == "the words"


# --- the runner's rules ------------------------------------------------------------------


class TestRunnerVoiceRules:
    def test_a_voice_target_is_allowlisted_and_needs_tls_off_loopback(self) -> None:
        prof = ws_profile()
        speech = kit()
        with pytest.raises(AllowedHostError):
            build_transport_factory(
                RunOptions(target="ws://agent.example.com/v", transport="audio-ws"),
                Settings(allowed_hosts="agent.example.com"),
                kit=speech,
            )
        factory = build_transport_factory(
            RunOptions(target="wss://agent.example.com/v", transport="audio-ws"),
            Settings(allowed_hosts="agent.example.com"),
            kit=speech,
        )
        built = factory(prof, None)
        assert isinstance(built, VoiceTransport) and built.target == "wss://agent.example.com/v"
        with pytest.raises(AllowedHostError):
            build_transport_factory(
                RunOptions(target="http://agent.example.com/t", transport="audio-http"),
                Settings(allowed_hosts="agent.example.com"),
                kit=speech,
            )

    def test_a_voice_run_needs_tts_stt_and_an_audio_block(self) -> None:
        settings = Settings(allowed_hosts="127.0.0.1")
        from opseval.llm import UsageMeter

        with pytest.raises(ConfigError, match="OPSEVAL_TTS_"):
            speech_kit(settings, ws_profile(), UsageMeter())
        with pytest.raises(ConfigError, match="audio: block"):
            speech_kit(settings, SutProfile(name="text"), UsageMeter())

    async def test_launch_refuses_unencrypted_audio_before_anything_is_sent(
        self, tmp_path: Path
    ) -> None:
        cat = voice_catalogue_dir()
        settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path))
        options = RunOptions(
            target="ws://127.0.0.1:1", transport="audio-ws", profile=cat / "profile.yaml"
        )
        with pytest.raises(ConfigError, match="only ever stored encrypted"):
            await launch(cat / "suites" / "smoke.yaml", options, settings)
        assert not any(tmp_path.iterdir())

    async def test_launch_closes_the_speech_providers_however_it_ends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.demo.voice_agent import run_voice_demo

        closed: list[SpeechKit] = []
        original = SpeechKit.aclose

        async def aclose(self: SpeechKit) -> None:
            closed.append(self)
            await original(self)

        monkeypatch.setattr(SpeechKit, "aclose", aclose)
        cat = voice_catalogue_dir()
        async with run_voice_demo() as (_url, speech):
            settings = Settings(
                allowed_hosts="127.0.0.1",
                reports_dir=str(tmp_path),
                record_audio="hashes",
                tts_url=speech,
                tts_model="t",
                tts_voice_map={"default": "demo"},
                stt_url=speech,
                stt_model="s",
                dotenv=False,
            )
            options = RunOptions(
                target="ws://127.0.0.1:1",  # nobody there: the run is refused
                transport="audio-ws",
                profile=cat / "profile.yaml",
                repeats=1,
                only_ids=["vr-001"],
            )
            with pytest.raises(ConfigError):
                await launch(cat / "suites" / "smoke.yaml", options, settings)
        assert len(closed) == 1

    async def test_launch_with_hashes_only_keeps_no_audio(self, tmp_path: Path) -> None:
        from opseval.demo.voice_agent import run_voice_demo

        cat = voice_catalogue_dir()
        async with run_voice_demo() as (url, speech):
            settings = Settings(
                allowed_hosts="127.0.0.1",
                reports_dir=str(tmp_path),
                record_audio="hashes",
                tts_url=speech,
                tts_model="t",
                tts_voice_map={"default": "demo"},
                stt_url=speech,
                stt_model="s",
                dotenv=False,
            )
            options = RunOptions(
                target=url,
                transport="audio-ws",
                profile=cat / "profile.yaml",
                repeats=1,
                only_ids=["vr-001"],
            )
            result = await launch(cat / "suites" / "smoke.yaml", options, settings)
        assert result.exit_code == 0
        run_dir = tmp_path / result.run_id
        assert not (run_dir / "audio").exists()
        records = list((run_dir / "records").glob("*.json"))
        assert records and '"sha256"' in records[0].read_text()
        assert json.loads((run_dir / "manifest.json").read_text())["layout"] == "opseval-runs/6"


# --- opseval demo --voice ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ([], 0),
        (["--bug", "silent"], 1),
        (["--bug", "garbled"], 1),
        (["--stt-deaf"], 3),
        (["--voice-transport", "audio-http"], 0),
        (["--bug", "crash"], 3),
    ],
)
def test_the_voice_demo_exit_codes(
    args: list[str], code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPSEVAL_TELEMETRY", "false")
    res = CliRunner().invoke(app, ["demo", "--voice", "--repeats", "1", *args])
    assert res.exit_code == code, res.output


def test_stt_down_is_exit_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(app, ["demo", "--voice", "--repeats", "1", "--stt-down"])
    assert res.exit_code == 3, res.output
    assert "STT provider is not usable" in res.output
