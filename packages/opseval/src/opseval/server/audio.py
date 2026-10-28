# SPDX-License-Identifier: Apache-2.0
"""Listening back in the console (ADR-0053, V0d): the audio routes of ``opseval serve``.

    GET /api/v1/runs/{run}/attempts/{test}/{n}/audio          every turn and its state
    GET /api/v1/runs/{run}/attempts/{test}/{n}/audio/call     the whole call (audio/wav)
    GET /api/v1/runs/{run}/attempts/{test}/{n}/audio/{seq}    one turn (audio/wav)

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
from opseval.server import schemas as s
from opseval.server.actions import ApiError, ServerContext
from opseval.storage import UnknownLayout
from opseval.voice.crypto import AudioCode, AudioKey, AudioUnavailable, load_key
from opseval.voice.playback import Turn, call_turns, key_problem, turn_wav, turns, whole_call

STATUS: dict[AudioCode, int] = {
    "AUDIO_NOT_FOUND": 404,
    "AUDIO_HASHES_ONLY": 404,
    "AUDIO_MISSING": 404,
    "AUDIO_PRUNED": 410,
    "AUDIO_KEY_MISSING": 409,
    "AUDIO_KEY_MISMATCH": 409,
    "AUDIO_UNDECODABLE": 409,
    "AUDIO_TAMPERED": 422,
}
_STATE: dict[AudioCode, s.AudioState] = {
    "AUDIO_PRUNED": "pruned",
    "AUDIO_HASHES_ONLY": "hashes_only",
    "AUDIO_MISSING": "missing",
    "AUDIO_UNDECODABLE": "undecodable",
    "AUDIO_KEY_MISSING": "key_missing",
    "AUDIO_KEY_MISMATCH": "key_mismatch",
}


class ServerKey:
    """The server's audio key, read once on first use. A malformed key, or a key without
    the voice extra, is said on every audio request (409 ``AUDIO_KEY_INVALID``) rather than
    stopping the console, which has other work to do."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._loaded = False
        self._key: AudioKey | None = None
        self._error: str | None = None

    def get(self) -> AudioKey | None:
        if not self._loaded:
            secret = self.settings.audio_encryption_key
            try:
                self._key = load_key(secret.get_secret_value() if secret else None)
            except ConfigError as exc:
                self._error = str(exc)
            self._loaded = True
        if self._error is not None:
            raise ApiError(409, "AUDIO_KEY_INVALID", self._error)
        return self._key

    def safe(self) -> AudioKey | None:
        """The key, or ``None`` when it cannot be used (for describing, not opening)."""
        try:
            return self.get()
        except ApiError:
            return None


def _turn_out(turn: Turn, key: AudioKey | None) -> s.AudioTurnOut:
    state: s.AudioState = turn.state
    note = turn.note
    if state == "available":
        problem = key_problem(turn, key)
        if problem is not None:
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


def describe(
    ctx: ServerContext, record: RunRecord, key: AudioKey | None
) -> s.AttemptAudioOut | None:
    """A voice attempt's audio as the console shows it; ``None`` for a text attempt."""
    run_dir = ctx.store.run_dir(record.run_id)
    found = turns(run_dir, record)
    if not found:
        return None
    call_state: s.AudioState = "available"
    call_note: str | None = None
    try:
        call_turns(run_dir, record, key)
    except AudioUnavailable as exc:
        call_state, call_note = _STATE.get(exc.code, "missing"), str(exc)
    if call_state == "available" and all(t.state == "undecodable" for t in found):
        call_state, call_note = "undecodable", "no turn of this attempt is playable audio"
    return s.AttemptAudioOut(
        turns=[_turn_out(t, key) for t in found],
        call_state=call_state,
        call_note=call_note,
        skipped=[t.seq for t in found if t.state == "undecodable"],
    )


def with_audio(
    out: s.AttemptRecordOut, ctx: ServerContext, run_id: str, key: ServerKey
) -> s.AttemptRecordOut:
    """The attempt detail with each spoken turn's audio descriptor attached. The record
    was read moments ago by ``views.attempt_record``; records are never rewritten."""
    record = ctx.store.load_record(run_id, out.test_id, out.index)
    if record is None:
        return out
    audio = describe(ctx, record, key.safe())
    if audio is None:
        return out
    by_seq = {t.seq: t for t in audio.turns}
    entries = [
        e.model_copy(update={"audio": by_seq.get(e.seq)}) if e.kind == "message" else e
        for e in out.entries
    ]
    return out.model_copy(update={"audio": audio, "entries": entries})


def _wav(data: bytes, name: str) -> Response:
    return Response(
        content=data,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'inline; filename="{name}.wav"',
        },
    )


def register(app: FastAPI, ctx: ServerContext, key: ServerKey, api: str) -> None:
    """Add the audio routes. ``run_record`` resolves and checks the attempt."""
    from opseval.server.app import RUN_ID

    def run_record(run_id: str, test_id: str, index: int) -> RunRecord:
        if index < 0 or not RUN_ID.match(test_id) or not RUN_ID.match(run_id) or ".." in run_id:
            raise ApiError(404, "UNKNOWN_ATTEMPT", "no such attempt")
        try:
            record = ctx.store.load_record(run_id, test_id, index)
        except UnknownLayout as exc:
            raise ApiError(409, "UNKNOWN_LAYOUT", str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise ApiError(
                500, "UNREADABLE_RECORD", f"cannot read {test_id} attempt {index}"
            ) from exc
        if record is None:
            raise ApiError(404, "UNKNOWN_ATTEMPT", f"no record of {test_id} attempt {index}")
        return record

    def unavailable(exc: AudioUnavailable) -> ApiError:
        return ApiError(STATUS.get(exc.code, 409), exc.code, str(exc))

    base = api + "/runs/{run_id}/attempts/{test_id}/{index}/audio"

    @app.get(base)
    def audio_list(run_id: str, test_id: str, index: int) -> s.AttemptAudioOut:
        record = run_record(run_id, test_id, index)
        out = describe(ctx, record, key.safe())
        if out is None:
            raise ApiError(404, "AUDIO_NOT_FOUND", "this attempt has no recorded audio")
        return out

    # Declared before /{seq}, so "call" is never read as a turn number.
    @app.get(base + "/call", response_class=Response)
    def audio_call(
        run_id: str, test_id: str, index: int, mono: Annotated[bool, Query()] = False
    ) -> Response:
        record = run_record(run_id, test_id, index)
        try:
            call = whole_call(ctx.store.run_dir(run_id), record, key.get(), stereo=not mono)
        except AudioUnavailable as exc:
            raise unavailable(exc) from exc
        return _wav(call.wav, f"{test_id}-attempt{index}-call")

    @app.get(base + "/{seq}", response_class=Response)
    def audio_turn(run_id: str, test_id: str, index: int, seq: int) -> Response:
        record = run_record(run_id, test_id, index)
        try:
            data = turn_wav(ctx.store.run_dir(run_id), record, seq, key.get())
        except AudioUnavailable as exc:
            raise unavailable(exc) from exc
        return _wav(data, f"{test_id}-attempt{index}-turn{seq}")


__all__ = ["STATUS", "ServerKey", "describe", "register", "with_audio"]
