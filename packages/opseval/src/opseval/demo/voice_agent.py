# SPDX-License-Identifier: Apache-2.0
"""The voice demo (ADR-0053): a speaking refund agent and fake speech providers, on loopback.

- :func:`run_speech` serves an OpenAI-compatible TTS (``/v1/audio/speech``) and STT
  (``/v1/audio/transcriptions``) that speak and hear :mod:`opseval.demo.tones`, so the demo
  exercises the real built-in speech clients with no speech model and no key. ``mode``
  ``down`` answers 503 to everything; ``deaf`` hears nothing (an empty transcript), even the
  canary. Only the STT is affected: the TTS keeps speaking.
- :func:`run_voice_agent` serves a small speaking refund agent over ``audio-ws`` (μ-law or
  any codec the profile says, base64 JSON envelope) and ``audio-http`` (WAV in, WAV out). It
  hears the caller by decoding the tones and answers in tones. ``bug`` injects a defect:
  ``silent`` sends only silence, ``garbled`` sends loud noise no STT can read.
- With ``duplex`` (ADR-0055) the ``audio-ws`` agent speaks in real time, 20 ms a chunk, and
  listens while it speaks: caller audio with energy stops it mid-sentence (it sends
  ``speech_end``, its play-back cleared), and it answers what the caller said over it. It also
  sends its own transcript of each caller turn (``transcript`` with ``of: caller``). The
  ``talks-over`` bug never stops for the caller.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from opseval.audio import AudioDecodeError, decode, encode, from_wav, has_energy, to_wav
from opseval.demo import tones
from opseval.demo.loopback import Request, Response, multipart_file, serve_http

VoiceBug = Literal["silent", "garbled", "talks-over"]
VOICE_BUGS: tuple[str, ...] = ("silent", "garbled")
DUPLEX_BUGS: tuple[str, ...] = ("talks-over",)
CHUNK_MS = 20
SpeechMode = Literal["ok", "down", "deaf"]
TTS_RATE = 24_000
ORDER_RE = re.compile(r"\d{4,}")
ARABIC_RE = re.compile(r"[؀-ۿ]")
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
YES = ("yes", "go ahead", "please do", "نعم", "ايوه", "أيوه", "تمام")

REPLIES = {
    "ask_order": (
        "Sorry to hear that. Could you tell me your order number?",
        "آسفين جدا على كده. ممكن رقم الطلب؟",
    ),
    "found": (
        "I found order {order}, a blender. I can refund the full amount to your original card. "
        "Shall I go ahead?",
        "لقيت الطلب رقم {order}، خلاط. أقدر أرجعلك المبلغ كله على الكارت الأصلي. أكمل؟",
    ),
    "done": (
        "Done. Your refund is on its way. Is there anything else I can help with?",
        "تمام. الاسترجاع في الطريق. في حاجة تانية أقدر أساعدك فيها؟",
    ),
}


# --- fake speech providers ---------------------------------------------------------------


@asynccontextmanager
async def run_speech(mode: SpeechMode = "ok") -> AsyncIterator[str]:
    """Fake OpenAI-compatible TTS and STT on one loopback URL."""

    async def handle(request: Request) -> Response:
        if mode == "down" and request.path == "/v1/audio/transcriptions":
            return Response(503, b'{"error": {"code": "unavailable"}}', "application/json")
        if request.method != "POST":
            return Response(404, b"not found", "text/plain")
        if request.path == "/v1/audio/speech":
            try:
                text = json.loads(request.body)["input"]
            except (ValueError, KeyError, TypeError):
                return Response(400, b'{"error": {"code": "bad_request"}}', "application/json")
            wav = to_wav(tones.encode(str(text), TTS_RATE), TTS_RATE)
            return Response(200, wav, "audio/wav")
        if request.path == "/v1/audio/transcriptions":
            data = multipart_file(request)
            if data is None:
                return Response(400, b'{"error": {"code": "no_file"}}', "application/json")
            try:
                pcm, rate = from_wav(data)
            except AudioDecodeError:
                return Response(400, b'{"error": {"code": "bad_audio"}}', "application/json")
            heard = "" if mode == "deaf" else tones.decode_all(pcm, rate)
            body = {"text": heard, "segments": [{"avg_logprob": -0.05}] if heard else []}
            return Response(200, json.dumps(body).encode(), "application/json")
        return Response(404, b"not found", "text/plain")

    async with serve_http(handle) as url:
        yield url


# --- the speaking agent ------------------------------------------------------------------


@dataclass
class VoiceConversation:
    bug: VoiceBug | None = None
    order: str | None = None
    arabic: bool = False
    heard: list[str] = field(default_factory=list)

    def reply(self, text: str) -> str:
        self.heard.append(text)
        if ARABIC_RE.search(text):
            self.arabic = True
        match = ORDER_RE.search(text.translate(ARABIC_DIGITS))
        if match:
            self.order = match.group(0)
            key = "found"
        elif self.order and any(word in text.lower() for word in YES):
            key = "done"
        else:
            key = "ask_order"
        en, ar = REPLIES[key]
        return (ar if self.arabic else en).format(order=self.order)

    def speak(self, text: str, rate: int) -> bytes:
        """The agent's turn as PCM16 at ``rate``: tones, or the injected defect."""
        if self.bug == "silent":
            return bytes(2 * rate)  # one second of digital silence
        if self.bug == "garbled":
            return tones.noise(1.5, rate)
        return tones.encode(text, rate)


def hear(pcm16: bytes, rate: int) -> str:
    return tones.decode_all(pcm16, rate)


@dataclass
class _WsCall:
    codec: str = "mulaw"
    rate: int = 8_000
    buffer: list[bytes] = field(default_factory=list)


async def _ws_handler(conn: ServerConnection, bug: VoiceBug | None) -> None:
    call = _WsCall()
    conversation = VoiceConversation(bug=bug)
    try:
        async for message in conn:
            if isinstance(message, bytes):
                call.buffer.append(message)
                continue
            frame = json.loads(message)
            kind = frame.get("type")
            if kind == "start":
                call.codec, call.rate = (
                    str(frame.get("codec", "mulaw")),
                    int(frame.get("rate", 8000)),
                )
            elif kind == "audio":
                call.buffer.append(base64.b64decode(frame.get("audio", "")))
            elif kind == "end_of_turn":
                wire = b"".join(call.buffer)
                call.buffer.clear()
                said = hear(decode(wire, call.codec), call.rate)  # type: ignore[arg-type]
                answer = conversation.reply(said)
                pcm = conversation.speak(answer, call.rate)
                await conn.send(json.dumps({"type": "speech_start"}))
                step = call.rate * 20 // 1000 * 2
                for i in range(0, len(pcm), step):
                    chunk = encode(pcm[i : i + step], call.codec)  # type: ignore[arg-type]
                    await conn.send(
                        json.dumps({"type": "audio", "audio": base64.b64encode(chunk).decode()})
                    )
                if bug != "silent":
                    await conn.send(json.dumps({"type": "speech_end"}))
    except ConnectionClosed:
        return


@dataclass
class _DuplexCall(_WsCall):
    speaking: asyncio.Task[None] | None = None
    interrupted: bool = False


async def _speak_paced(
    conn: ServerConnection, call: _DuplexCall, pcm: bytes, *, yields: bool
) -> None:
    """One reply in real time, a chunk every 20 ms; stops at once when the caller cuts in."""
    step = call.rate * CHUNK_MS // 1000 * 2
    loop = asyncio.get_running_loop()
    began = loop.time()
    await conn.send(json.dumps({"type": "speech_start"}))
    for k, i in enumerate(range(0, len(pcm), step)):
        if yields and call.interrupted:
            break
        ahead = began + k * CHUNK_MS / 1000 - loop.time()
        if ahead > 0:
            await asyncio.sleep(ahead)
        chunk = encode(pcm[i : i + step], call.codec)  # type: ignore[arg-type]
        await conn.send(json.dumps({"type": "audio", "audio": base64.b64encode(chunk).decode()}))
    await conn.send(json.dumps({"type": "speech_end"}))


async def _ws_duplex_handler(conn: ServerConnection, bug: VoiceBug | None) -> None:
    """The agent of ``opseval demo --voice --duplex``: it listens while it speaks."""
    call = _DuplexCall()
    # Silent and garbled speech are the conversation's own defects; talking over is this
    # handler's.
    conversation = VoiceConversation(bug=None if bug == "talks-over" else bug)
    yields = bug != "talks-over"

    async def answer(said: str, previous: asyncio.Task[None] | None) -> None:
        if previous is not None and not previous.done():
            await previous  # it never stops for the caller: it finishes, then answers
        call.interrupted = False
        await conn.send(json.dumps({"type": "transcript", "of": "caller", "text": said}))
        pcm = conversation.speak(conversation.reply(said), call.rate)
        await _speak_paced(conn, call, pcm, yields=yields)

    try:
        async for message in conn:
            frame = json.loads(message) if isinstance(message, str) else {"type": "audio"}
            kind = frame.get("type")
            if kind == "start":
                call.codec, call.rate = (
                    str(frame.get("codec", "mulaw")),
                    int(frame.get("rate", 8000)),
                )
            elif kind == "audio":
                wire = (
                    message
                    if isinstance(message, bytes)
                    else base64.b64decode(frame.get("audio", ""))
                )
                call.buffer.append(wire)
                pcm = decode(wire, call.codec)  # type: ignore[arg-type]
                talking = call.speaking is not None and not call.speaking.done()
                if talking and has_energy(pcm):
                    call.interrupted = True
            elif kind == "end_of_turn":
                wire = b"".join(call.buffer)
                call.buffer.clear()
                said = hear(decode(wire, call.codec), call.rate)  # type: ignore[arg-type]
                call.speaking = asyncio.create_task(answer(said, call.speaking))
    except ConnectionClosed:
        return
    finally:
        if call.speaking is not None and not call.speaking.done():
            call.speaking.cancel()


@asynccontextmanager
async def run_voice_agent(
    bug: VoiceBug | None = None,
    *,
    transport: Literal["audio-ws", "audio-http"] = "audio-ws",
    duplex: bool = False,
) -> AsyncIterator[str]:
    """The speaking agent on loopback; yields its URL for ``transport``."""
    if transport == "audio-ws":

        async def handler(conn: ServerConnection) -> None:
            if duplex:
                await _ws_duplex_handler(conn, bug)
            else:
                await _ws_handler(conn, bug)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            yield f"ws://127.0.0.1:{port}/voice"
        return

    calls: dict[str, VoiceConversation] = {}

    async def handle(request: Request) -> Response:
        if request.method != "POST":
            return Response(200, b"speaking refund agent", "text/plain")
        session = request.headers.get("x-opseval-session", "")
        conversation = calls.setdefault(session, VoiceConversation(bug=bug))
        try:
            pcm, rate = from_wav(request.body)
        except AudioDecodeError:
            return Response(400, b"not a WAV", "text/plain")
        answer = conversation.reply(hear(pcm, rate))
        out = conversation.speak(answer, rate)
        return Response(200, to_wav(out, rate), "audio/wav")

    async with serve_http(handle) as url:
        yield f"{url}/turn"


@asynccontextmanager
async def run_voice_demo(
    bug: VoiceBug | None = None,
    *,
    speech: SpeechMode = "ok",
    transport: Literal["audio-ws", "audio-http"] = "audio-ws",
    duplex: bool = False,
) -> AsyncIterator[tuple[str, str]]:
    """The agent and the speech providers together; yields (agent URL, speech URL)."""
    async with AsyncExitStack() as stack:
        agent = await stack.enter_async_context(
            run_voice_agent(bug, transport=transport, duplex=duplex)
        )
        speech_url = await stack.enter_async_context(run_speech(speech))
        yield agent, speech_url


__all__ = [
    "DUPLEX_BUGS",
    "VOICE_BUGS",
    "VoiceConversation",
    "run_speech",
    "run_voice_agent",
    "run_voice_demo",
]
