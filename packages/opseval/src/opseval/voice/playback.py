# SPDX-License-Identifier: Apache-2.0
"""Listening back to a voice attempt (ADR-0053, V0d): the one reader the CLI
(``opseval audio``) and the console (``GET …/audio``) share.

A turn is addressed by its frame's ``seq``, as the transcript shows it. What a turn's audio
is, is decided here once, from the record and the run folder, without the key:

- ``available``: a file to play (sealed or, for the demo's synthetic voices, plain);
- ``pruned``: retention removed it (``AudioRef.pruned``, or ``audio/PRUNED.json`` in the run
  folder, which says when: records are never rewritten, ADR-0005);
- ``hashes_only``: no bytes were ever kept (``OPSEVAL_RECORD_AUDIO=hashes``);
- ``undecodable``: the agent's bytes as they came, which are not audio Opseval could read;
- ``missing``: the record names a file that is not there (and was not pruned).

Opening a file needs the key that sealed it. Every file is checked twice: the AES-GCM tag
(content and place) and the SHA-256 the record holds for the turn, so a sealed file put in
place of another, or a plain file edited on disk, is refused rather than played.

Decrypted audio is only ever held in memory: nothing here writes it anywhere.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import wave
from array import array
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from opseval.audio import AudioDecodeError, from_wav, resample
from opseval.models.record import AudioRef, RunRecord
from opseval.voice.crypto import SUFFIX, AudioKey, AudioUnavailable, FilePlace

PRUNED_MARKER = "PRUNED.json"
"""``audio/PRUNED.json``: written by ``opseval prune`` when it removes a run's audio."""
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_CALL_SECONDS = 4 * 3600
"""A whole call longer than this is refused rather than allocated: a record's times are
data, and a corrupted one must not make the server build a multi-gigabyte buffer."""

State = Literal["available", "pruned", "hashes_only", "undecodable", "missing"]
_NAME = re.compile(r"^(\d+)-(caller|agent)\.(wav|raw)(?:\.enc)?$")


@dataclass(frozen=True)
class Turn:
    seq: int
    ref: AudioRef
    state: State
    note: str | None = None
    """Why the turn cannot be played, in words (``None`` when it can)."""

    @property
    def duration_ms(self) -> float:
        return self.ref.end_ms - self.ref.start_ms

    @property
    def encrypted(self) -> bool:
        return self.ref.encryption != "none"


def pruned_marker(run_dir: Path) -> dict[str, object] | None:
    """What ``opseval prune`` left in place of a run's audio, or ``None``."""
    try:
        data = json.loads((run_dir / "audio" / PRUNED_MARKER).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pruned_note(marker: dict[str, object] | None) -> str:
    at = marker.get("pruned_at") if marker else None
    if isinstance(at, str):
        try:
            return f"audio pruned on {datetime.fromisoformat(at).date().isoformat()}"
        except ValueError:
            pass
    return "audio pruned by retention"


def _file(run_dir: Path, ref: AudioRef) -> Path:
    """The file a ref names, never outside the run folder's ``audio/`` (links included)."""
    if ref.path is None:
        raise AudioUnavailable("AUDIO_HASHES_ONLY", "only hashes were kept")
    path = run_dir / ref.path
    root = (run_dir / "audio").resolve()
    if root not in path.resolve().parents:
        raise AudioUnavailable("AUDIO_TAMPERED", f"{ref.path} leaves the run folder's audio/")
    return path


def turns(run_dir: Path, record: RunRecord) -> list[Turn]:
    """Every spoken turn of an attempt, in frame order, with what can be done with it."""
    marker = pruned_marker(run_dir)
    out: list[Turn] = []
    for frame in record.frames:
        ref = frame.audio
        if ref is None:
            continue
        if ref.pruned or (ref.path is not None and marker is not None):
            out.append(Turn(frame.seq, ref, "pruned", _pruned_note(marker)))
        elif ref.path is None:
            out.append(Turn(frame.seq, ref, "hashes_only", "only hashes were kept"))
        elif _kind(ref.path) == "raw":
            note = "the agent's audio could not be decoded; its bytes were kept as they came"
            out.append(Turn(frame.seq, ref, "undecodable", note))
        else:
            try:
                present = _file(run_dir, ref).is_file()
            except AudioUnavailable:
                present = False
            if present:
                out.append(Turn(frame.seq, ref, "available"))
            else:
                out.append(Turn(frame.seq, ref, "missing", f"{ref.path} is not in the run folder"))
    return out


def _kind(path: str) -> str:
    found = _NAME.match(path.rsplit("/", 1)[-1])
    return found.group(3) if found else "wav"


def _place(record: RunRecord, ref: AudioRef) -> FilePlace:
    if ref.path is None:
        raise AudioUnavailable("AUDIO_HASHES_ONLY", "only hashes were kept")
    found = _NAME.match(ref.path.rsplit("/", 1)[-1])
    if found is None:
        raise AudioUnavailable("AUDIO_TAMPERED", f"{ref.path} is not a name Opseval writes")
    return FilePlace(
        record.run_id, record.test_id, record.attempt_index, int(found.group(1)), ref.speaker
    )


def find(run_dir: Path, record: RunRecord, seq: int) -> Turn:
    for turn in turns(run_dir, record):
        if turn.seq == seq:
            return turn
    raise AudioUnavailable("AUDIO_NOT_FOUND", f"turn {seq} of this attempt has no audio")


def _refuse(turn: Turn) -> None:
    codes = {
        "pruned": "AUDIO_PRUNED",
        "hashes_only": "AUDIO_HASHES_ONLY",
        "missing": "AUDIO_MISSING",
        "undecodable": "AUDIO_UNDECODABLE",
    }
    if turn.state in codes:
        raise AudioUnavailable(codes[turn.state], f"turn {turn.seq}: {turn.note}")  # type: ignore[arg-type]


def key_problem(turn: Turn, key: AudioKey | None) -> AudioUnavailable | None:
    """Whether the key in hand can open this turn, decided from the record alone."""
    if not turn.encrypted:
        return None
    if key is None:
        return AudioUnavailable(
            "AUDIO_KEY_MISSING",
            f"turn {turn.seq} is encrypted (key {turn.ref.key_id}) and "
            "OPSEVAL_AUDIO_ENCRYPTION_KEY is not set here",
        )
    if key.key_id != turn.ref.key_id:
        return AudioUnavailable(
            "AUDIO_KEY_MISMATCH",
            f"turn {turn.seq} was encrypted with key {turn.ref.key_id}, but the configured "
            f"OPSEVAL_AUDIO_ENCRYPTION_KEY is key {key.key_id}",
        )
    return None


def read_bytes(
    run_dir: Path, record: RunRecord, turn: Turn, key: AudioKey | None, *, raw: bool = False
) -> bytes:
    """A turn's audio as it was recorded (a WAV; with ``raw``, an undecodable turn's bytes
    as the agent sent them), checked against the record's hash. Held in memory only."""
    if not (raw and turn.state == "undecodable"):
        _refuse(turn)
    problem = key_problem(turn, key)
    if problem is not None:
        raise problem
    path = _file(run_dir, turn.ref)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise AudioUnavailable("AUDIO_TAMPERED", f"{turn.ref.path} is larger than any turn")
        blob = path.read_bytes()
    except FileNotFoundError as exc:
        raise AudioUnavailable(
            "AUDIO_MISSING", f"{turn.ref.path} is not in the run folder"
        ) from exc
    if turn.encrypted:
        if key is None:  # key_problem said so already; kept for the type checker
            raise AudioUnavailable("AUDIO_KEY_MISSING", "OPSEVAL_AUDIO_ENCRYPTION_KEY is not set")
        if not turn.ref.path or not turn.ref.path.endswith(SUFFIX):
            raise AudioUnavailable("AUDIO_TAMPERED", f"{turn.ref.path} is not a sealed file")
        data = key.open(blob, _place(record, turn.ref))
    else:
        data = blob
    if hashlib.sha256(data).hexdigest() != turn.ref.sha256:
        raise AudioUnavailable(
            "AUDIO_TAMPERED",
            f"turn {turn.seq}: the audio does not match the hash the record holds for it",
        )
    return data


def turn_wav(run_dir: Path, record: RunRecord, seq: int, key: AudioKey | None) -> bytes:
    """One turn, as a playable WAV."""
    return read_bytes(run_dir, record, find(run_dir, record, seq), key)


# --- the whole call ---------------------------------------------------------------------


def _samples(pcm16: bytes) -> array[int]:
    out = array("h")
    out.frombytes(pcm16)
    if sys.byteorder == "big":
        out.byteswap()
    return out


def _place_into(
    track: array[int], samples: array[int], at: int, busy: list[tuple[int, int]]
) -> None:
    """Add ``samples`` at ``at``: copied where the track is silent, summed and clipped to
    16 bits where it overlaps audio already placed (a barge-in, or both speakers in mono)."""
    end = at + len(samples)
    if not any(a < end and at < b for a, b in busy):
        track[at:end] = samples
    else:
        for i, value in enumerate(samples):
            total = track[at + i] + value
            track[at + i] = 32767 if total > 32767 else -32768 if total < -32768 else total
    busy.append((at, end))


@dataclass(frozen=True)
class Call:
    wav: bytes
    rate: int
    channels: int
    skipped: tuple[int, ...]
    """Seqs of undecodable agent turns, left as silence (and named, never hidden)."""


Speaker = Literal["caller", "agent"]


def call_turns(
    run_dir: Path, record: RunRecord, key: AudioKey | None, *, only: Speaker | None = None
) -> list[Turn]:
    """The turns a whole-call mix needs, or the first reason it cannot be made. Fail
    closed: a call with a turn missing for any reason but undecodable agent audio is
    refused, never played with a gap no one sees."""
    found = [t for t in turns(run_dir, record) if only is None or t.ref.speaker == only]
    if not found:
        who = f" from the {only}" if only else ""
        raise AudioUnavailable("AUDIO_NOT_FOUND", f"this attempt has no recorded audio{who}")
    for turn in found:
        if turn.state != "undecodable":
            _refuse(turn)
            problem = key_problem(turn, key)
            if problem is not None:
                raise problem
    return found


def schedule(record: RunRecord, found: list[Turn]) -> dict[int, float]:
    """When each turn plays in the whole call, in ms from the attempt's start.

    A turn plays at its recorded ``start_ms`` (the attempt's clock), with the record's own
    silences between turns. One exception: a turn the record shows starting before the
    previous turn ended, with no ``barge_in`` event for it, is moved to that end, and the
    turns after it with it. Audio sent faster than it plays (a request/response transport
    such as ``audio-http``, or the demo) has start times closer together than the speech is
    long, and played as recorded it would be two people talking at once that never did. A
    barge-in the bridge recorded is kept as the overlap it was.
    """
    barge = [e.at_ms for e in (record.voice.events if record.voice else ()) if e.kind == "barge_in"]
    out: dict[int, float] = {}
    shift = 0.0
    end = 0.0
    for turn in sorted(found, key=lambda t: (t.ref.start_ms, t.seq)):
        at = turn.ref.start_ms + shift
        barged = turn.ref.speaker == "agent" and any(
            turn.ref.start_ms <= b <= turn.ref.end_ms for b in barge
        )
        if at < end and not barged:
            shift += end - at
            at = end
        out[turn.seq] = at
        end = max(end, at + turn.duration_ms)
    return out


def whole_call(
    run_dir: Path,
    record: RunRecord,
    key: AudioKey | None,
    *,
    stereo: bool = True,
    only: Speaker | None = None,
) -> Call:
    """The attempt as one WAV on its own clock: each turn starts at sample
    ``round(at_ms * rate / 1000)``, ``at_ms`` from :func:`schedule` (its recorded start,
    unless sent faster than real time), with silence between. Stereo puts the caller on the
    left and the agent on the right; mono sums them, clipped to 16 bits where they overlap.
    ``only`` keeps one side (mono), timed as in the whole call."""
    stereo = stereo and only is None
    found = call_turns(run_dir, record, key, only=only)
    when = schedule(record, turns(run_dir, record))
    decoded: list[tuple[Turn, bytes, int]] = []
    skipped: list[int] = []
    for turn in found:
        if turn.state == "undecodable":
            skipped.append(turn.seq)
            continue
        try:
            pcm, rate = from_wav(read_bytes(run_dir, record, turn, key))
        except AudioDecodeError as exc:
            raise AudioUnavailable("AUDIO_TAMPERED", f"turn {turn.seq}: {exc}") from exc
        decoded.append((turn, pcm, rate))
    if not decoded:
        raise AudioUnavailable("AUDIO_UNDECODABLE", "no turn of this attempt is playable audio")
    rate = record.voice.rate if record.voice is not None else decoded[0][2]
    placed: list[tuple[Literal["caller", "agent"], int, array[int]]] = []
    length = 0
    for turn, pcm, turn_rate in decoded:
        if turn_rate != rate:
            pcm = resample(pcm, turn_rate, rate)
        samples = _samples(pcm)
        at = round(when[turn.seq] * rate / 1000)
        placed.append((turn.ref.speaker, at, samples))
        length = max(length, at + len(samples))
    if length > MAX_CALL_SECONDS * rate:
        raise AudioUnavailable("AUDIO_TAMPERED", "the record's times put this call over 4 hours")
    tracks = {"caller": array("h", bytes(2 * length)), "agent": array("h", bytes(2 * length))}
    if not stereo:
        tracks["agent"] = tracks["caller"]
    busy: dict[int, list[tuple[int, int]]] = {}
    for speaker, at, samples in placed:
        track = tracks[speaker]
        _place_into(track, samples, at, busy.setdefault(id(track), []))
    if stereo:
        frames = array("h", bytes(4 * length))
        frames[0::2] = tracks["caller"]
        frames[1::2] = tracks["agent"]
    else:
        frames = tracks["caller"]
    if sys.byteorder == "big":
        frames.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(2 if stereo else 1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames.tobytes())
    return Call(buffer.getvalue(), rate, 2 if stereo else 1, tuple(skipped))


__all__ = [
    "MAX_CALL_SECONDS",
    "PRUNED_MARKER",
    "Call",
    "State",
    "Turn",
    "call_turns",
    "find",
    "key_problem",
    "pruned_marker",
    "read_bytes",
    "schedule",
    "turn_wav",
    "turns",
    "whole_call",
]
