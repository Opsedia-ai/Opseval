# SPDX-License-Identifier: Apache-2.0
"""Listening back in the console (ADR-0053, V0d): the audio routes of ``opseval serve``.

    GET /api/v1/runs/{run}/attempts/{test}/{n}/audio          every turn and its state
    GET /api/v1/runs/{run}/attempts/{test}/{n}/audio/call     the whole call (audio/wav)
    GET /api/v1/runs/{run}/attempts/{test}/{n}/audio/{seq}    one turn (audio/wav)
    GET …/audio/{seq}?raw=true      an undecodable agent turn's bytes, as the agent sent them

They sit behind the same guard as every ``/api`` route (loopback ``Host``, bearer token).
Audio is decrypted in memory for the response and never written anywhere in the clear; the
answer is ``Cache-Control: no-store`` so no browser or proxy keeps a copy. What a turn's
audio is (playable, pruned, hashes only, no key here, …) is decided here, from the record
alone, and the console only says it (the browser computes nothing, ADR-0029).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Query, Response

from opseval.config import Settings
from opseval.errors import ConfigError
from opseval.models.record import RunRecord
from opseval.server import lookup
from opseval.server import schemas as s
from opseval.server.actions import ApiError, ServerContext
from opseval.voice.crypto import AudioCode, AudioKey, AudioUnavailable, ExtraMissing, load_key
from opseval.voice.playback import (
    Turn,
    call_turns,
    find,
    key_problem,
    read_bytes,
    turns,
    whole_call,
)

STATUS: dict[AudioCode, int] = {
    "AUDIO_NOT_FOUND": 404,
    "AUDIO_HASHES_ONLY": 404,
    "AUDIO_MISSING": 404,
    "AUDIO_PRUNED": 410,
    "AUDIO_KEY_MISSING": 409,
    "AUDIO_KEY_MISMATCH": 409,
    "AUDIO_KEY_INVALID": 409,
    "AUDIO_EXTRA_MISSING": 409,
    "AUDIO_UNDECODABLE": 409,
    "AUDIO_TOO_LARGE": 413,
    "AUDIO_TAMPERED": 422,
}
_STATE: dict[AudioCode, s.AudioState] = {
    "AUDIO_PRUNED": "pruned",
    "AUDIO_HASHES_ONLY": "hashes_only",
    "AUDIO_MISSING": "missing",
    "AUDIO_UNDECODABLE": "undecodable",
    "AUDIO_KEY_MISSING": "key_missing",
    "AUDIO_KEY_MISMATCH": "key_mismatch",
    "AUDIO_KEY_INVALID": "key_invalid",
    "AUDIO_EXTRA_MISSING": "extra_missing",
    "AUDIO_TOO_LARGE": "too_large",
}


class ServerKey:
    """The server's audio key, read once on first use. Three ways to have none, each said
    as itself on every audio request rather than stopping the console, which has other
    work to do: unset (``key_missing``, ``AUDIO_KEY_MISSING``), malformed (``key_invalid``,
    ``AUDIO_KEY_INVALID``) and set without the voice extra installed (``extra_missing``,
    ``AUDIO_EXTRA_MISSING``). Plain (unencrypted) files play in all three."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._loaded = False
        self._key: AudioKey | None = None
        self._error: AudioUnavailable | None = None

    def state(self) -> tuple[AudioKey | None, AudioUnavailable | None]:
        """The key, and why there is none when one is set but cannot be used."""
        if not self._loaded:
            secret = self.settings.audio_encryption_key
            try:
                self._key = load_key(secret.get_secret_value() if secret else None)
            except ExtraMissing as exc:
                self._error = AudioUnavailable("AUDIO_EXTRA_MISSING", str(exc))
            except ConfigError as exc:
                self._error = AudioUnavailable("AUDIO_KEY_INVALID", str(exc))
            self._loaded = True
        return self._key, self._error

    def explain(self, exc: AudioUnavailable) -> AudioUnavailable:
        """A turn that needs a key the server does not have, said as why it has none."""
        error = self.state()[1]
        return error if exc.code == "AUDIO_KEY_MISSING" and error is not None else exc


def _turn_out(turn: Turn, key: ServerKey) -> s.AudioTurnOut:
    state: s.AudioState = turn.state
    note = turn.note
    if state == "available":
        problem = key_problem(turn, key.state()[0])
        if problem is not None:
            problem = key.explain(problem)
            state, note = _STATE[problem.code], str(problem)
    return s.AudioTurnOut(
        seq=turn.seq,
        speaker=turn.ref.speaker,
        state=state,
        start_ms=turn.ref.start_ms,
        duration_ms=turn.duration_ms,
        encrypted=turn.encrypted,
        note=note,
    )


def describe(ctx: ServerContext, record: RunRecord, key: ServerKey) -> s.AttemptAudioOut | None:
    """A voice attempt's audio as the console shows it; ``None`` for a text attempt. The
    turns are listed once and the whole call is judged from them, before any file is read
    (its size included: a call too large to build is ``too_large``, never attempted)."""
    run_dir = ctx.store.run_dir(record.run_id)
    found = turns(run_dir, record)
    if not found:
        return None
    call_state: s.AudioState = "available"
    call_note: str | None = None
    try:
        call_turns(run_dir, record, key.state()[0], found=found)
    except AudioUnavailable as exc:
        exc = key.explain(exc)
        call_state, call_note = _STATE.get(exc.code, "missing"), str(exc)
    if call_state == "available" and all(t.state == "undecodable" for t in found):
        call_state, call_note = "undecodable", "no turn of this attempt is playable audio"
    return s.AttemptAudioOut(
        turns=[_turn_out(t, key) for t in found],
        call_state=call_state,
        call_note=call_note,
        skipped=[t.seq for t in found if t.state == "undecodable"],
    )


SPOKEN = frozenset({"message", "undecodable_audio"})
"""The transcript entries a recorded turn is attached to."""


def with_audio(
    out: s.AttemptRecordOut, ctx: ServerContext, record: RunRecord, key: ServerKey
) -> s.AttemptRecordOut:
    """The attempt detail with each spoken turn's audio descriptor attached, from the
    record the route already read (never read twice)."""
    audio = describe(ctx, record, key)
    if audio is None:
        return out
    by_seq = {t.seq: t for t in audio.turns}
    entries = [
        e.model_copy(update={"audio": by_seq.get(e.seq)}) if e.kind in SPOKEN else e
        for e in out.entries
    ]
    return out.model_copy(update={"audio": audio, "entries": entries})


def _audio(data: bytes | bytearray, name: str, *, raw: bool = False) -> Response:
    # A bytearray is the whole call built in place: sent as a view, never copied again.
    body = memoryview(data) if isinstance(data, bytearray) else data
    return Response(
        content=body,
        media_type="application/octet-stream" if raw else "audio/wav",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="{name}.raw"' if raw else f'inline; filename="{name}.wav"'
            ),
        },
    )


def register(app: FastAPI, ctx: ServerContext, key: ServerKey, api: str) -> None:
    """Add the audio routes. Each resolves and checks the attempt with ``lookup.attempt``,
    the same check as the attempt route."""

    def unavailable(exc: AudioUnavailable) -> ApiError:
        exc = key.explain(exc)
        return ApiError(STATUS.get(exc.code, 409), exc.code, str(exc))

    base = api + "/runs/{run_id}/attempts/{test_id}/{index}/audio"

    @app.get(base)
    def audio_list(run_id: str, test_id: str, index: int) -> s.AttemptAudioOut:
        record = lookup.attempt(ctx, run_id, test_id, index)
        out = describe(ctx, record, key)
        if out is None:
            raise ApiError(404, "AUDIO_NOT_FOUND", "this attempt has no recorded audio")
        return out

    # Declared before /{seq}, so "call" is never read as a turn number.
    @app.get(base + "/call", response_class=Response)
    def audio_call(
        run_id: str, test_id: str, index: int, mono: Annotated[bool, Query()] = False
    ) -> Response:
        record = lookup.attempt(ctx, run_id, test_id, index)
        try:
            call = whole_call(ctx.store.run_dir(run_id), record, key.state()[0], stereo=not mono)
        except AudioUnavailable as exc:
            raise unavailable(exc) from exc
        return _audio(call.wav, f"{test_id}-attempt{index}-call")

    @app.get(base + "/{seq}", response_class=Response)
    def audio_turn(
        run_id: str, test_id: str, index: int, seq: int, raw: Annotated[bool, Query()] = False
    ) -> Response:
        """One turn as a WAV; with ``raw``, an undecodable agent turn's bytes as they came."""
        record = lookup.attempt(ctx, run_id, test_id, index)
        run_dir = ctx.store.run_dir(run_id)
        try:
            turn = find(run_dir, record, seq)
            data = read_bytes(run_dir, record, turn, key.state()[0], raw=raw)
        except AudioUnavailable as exc:
            raise unavailable(exc) from exc
        name = f"{test_id}-attempt{index}-turn{seq}"
        return _audio(data, name, raw=raw and turn.state == "undecodable")


__all__ = ["SPOKEN", "STATUS", "ServerKey", "describe", "register", "with_audio"]
