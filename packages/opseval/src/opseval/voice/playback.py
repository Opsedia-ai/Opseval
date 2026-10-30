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
import math
import re
import struct
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from opseval.audio import AudioDecodeError, from_wav, resample
from opseval.models.record import AudioRef, RunRecord
from opseval.storage import AUDIO_MARKER, AudioPathRefused, FileRunAudio, RunAudio
from opseval.voice.crypto import (
    HEADER_BYTES,
    SUFFIX,
    TAG_BYTES,
    AudioKey,
    AudioUnavailable,
    FilePlace,
)

PRUNED_MARKER = AUDIO_MARKER
MAX_FILE_BYTES = 256 * 1024 * 1024
"""No turn's file is read past this, whatever the record says."""
MAX_CALL_SECONDS = 4 * 3600
MAX_CALL_BYTES = 256 * 1024 * 1024
"""A whole call longer or larger than this is refused (``AUDIO_TOO_LARGE``) before any file
is read or any buffer allocated: a record's times are data, and a corrupted one must not
make the server build a multi-gigabyte buffer. Such a call is exported turn by turn."""
WAV_HEADER = 44

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


Source = RunAudio | Path
"""Where a run's audio is: the store's :class:`~opseval.storage.RunAudio`, or a run folder
(``opseval audio`` on a folder named on the command line)."""


def _source(run_dir: Source) -> RunAudio:
    return FileRunAudio(run_dir) if isinstance(run_dir, Path) else run_dir


def _size(audio: RunAudio, ref: AudioRef) -> int | None:
    """The size of the file a ref names (``None``: not there), never outside the run's
    ``audio/`` (links included)."""
    if ref.path is None:
        raise AudioUnavailable("AUDIO_HASHES_ONLY", "only hashes were kept")
    try:
        return audio.size(ref.path)
    except AudioPathRefused as exc:
        raise AudioUnavailable(
            "AUDIO_TAMPERED", f"{ref.path} leaves the run folder's audio/"
        ) from exc


def turns(run_dir: Source, record: RunRecord) -> list[Turn]:
    """Every spoken turn of an attempt, in frame order, with what can be done with it."""
    audio = _source(run_dir)
    marker = audio.pruned()
    out: list[Turn] = []
    for frame in record.frames:
        ref = frame.audio
        if ref is None:
            continue
        if ref.pruned or (ref.path is not None and marker is not None):
            note = marker.note if marker is not None else "audio pruned by retention"
            out.append(Turn(frame.seq, ref, "pruned", note))
        elif ref.path is None:
            out.append(Turn(frame.seq, ref, "hashes_only", "only hashes were kept"))
        elif _kind(ref.path) == "raw":
            note = "the agent's audio could not be decoded; its bytes were kept as they came"
            out.append(Turn(frame.seq, ref, "undecodable", note))
        else:
            try:
                present = _size(audio, ref) is not None
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


def find(run_dir: Source, record: RunRecord, seq: int) -> Turn:
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


def _sealed_size(turn: Turn) -> int:
    """The most bytes the file of a turn can have, from the record: its WAV (or raw bytes),
    plus the header and tag when sealed."""
    return turn.ref.bytes + (HEADER_BYTES + TAG_BYTES if turn.encrypted else 0)


def read_bytes(
    run_dir: Source, record: RunRecord, turn: Turn, key: AudioKey | None, *, raw: bool = False
) -> bytes:
    """A turn's audio as it was recorded (a WAV; with ``raw``, an undecodable turn's bytes
    as the agent sent them), checked against the record's hash. Held in memory only.

    The file's size is checked before it is read or decrypted: larger than the record says
    it was written is a file that is not the turn's (``AUDIO_TAMPERED``), and nothing is
    allocated for it."""
    if not (raw and turn.state == "undecodable"):
        _refuse(turn)
    problem = key_problem(turn, key)
    if problem is not None:
        raise problem
    audio = _source(run_dir)
    try:
        size = _size(audio, turn.ref)
        if size is None:
            raise FileNotFoundError(turn.ref.path)
        if size > MAX_FILE_BYTES or size > _sealed_size(turn):
            raise AudioUnavailable(
                "AUDIO_TAMPERED",
                f"{turn.ref.path} is {size} bytes, larger than the record says it was written",
            )
        blob = audio.read(turn.ref.path or "")
    except FileNotFoundError as exc:
        # Retention may have emptied audio/ since the turn was listed: say so, not "missing".
        marker = audio.pruned()
        if marker is not None:
            raise AudioUnavailable("AUDIO_PRUNED", f"turn {turn.seq}: {marker.note}") from exc
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
    del blob
    if hashlib.sha256(data).hexdigest() != turn.ref.sha256:
        raise AudioUnavailable(
            "AUDIO_TAMPERED",
            f"turn {turn.seq}: the audio does not match the hash the record holds for it",
        )
    return data


def turn_wav(run_dir: Source, record: RunRecord, seq: int, key: AudioKey | None) -> bytes:
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
    data: memoryview,
    samples: array[int],
    at: int,
    busy: list[tuple[int, int]],
    *,
    channel: int,
    stride: int,
) -> None:
    """Add ``samples`` at frame ``at`` of one channel of the interleaved ``data``: copied
    where the channel is silent, summed and clipped to 16 bits where it overlaps audio
    already placed (a barge-in, or both speakers in mono)."""
    end = at + len(samples)
    first = at * stride + channel
    if not any(a < end and at < b for a, b in busy):
        data[first : first + len(samples) * stride : stride] = memoryview(samples)
    else:
        for i, value in enumerate(samples):
            where = first + i * stride
            total = data[where] + value
            data[where] = 32767 if total > 32767 else -32768 if total < -32768 else total
    busy.append((at, end))


@dataclass(frozen=True)
class Call:
    wav: bytearray
    """The WAV, built in place (header, then the samples): the one copy of the call."""
    rate: int
    channels: int
    skipped: tuple[int, ...]
    """Seqs of undecodable agent turns, left as silence (and named, never hidden)."""


Speaker = Literal["caller", "agent"]


def call_turns(
    run_dir: Source,
    record: RunRecord,
    key: AudioKey | None,
    *,
    only: Speaker | None = None,
    found: list[Turn] | None = None,
    max_bytes: int | None = None,
    stereo: bool = True,
) -> list[Turn]:
    """The turns a whole-call mix needs, or the first reason it cannot be made. Fail
    closed: a call with a turn missing for any reason but undecodable agent audio is
    refused, never played with a gap no one sees. ``found`` is :func:`turns` when the
    caller has it already. A call over ``max_bytes`` is ``AUDIO_TOO_LARGE``, decided from
    the record before any file is read."""
    max_bytes = MAX_CALL_BYTES if max_bytes is None else max_bytes
    every = turns(run_dir, record) if found is None else found
    picked = [t for t in every if only is None or t.ref.speaker == only]
    if not picked:
        who = f" from the {only}" if only else ""
        raise AudioUnavailable("AUDIO_NOT_FOUND", f"this attempt has no recorded audio{who}")
    for turn in picked:
        if turn.state != "undecodable":
            _refuse(turn)
            problem = key_problem(turn, key)
            if problem is not None:
                raise problem
    playable = [t for t in picked if t.state != "undecodable"]
    if playable:
        check_size(record, every, playable, stereo=stereo and only is None, max_bytes=max_bytes)
    return picked


def _call_rate(record: RunRecord, playable: list[Turn]) -> int:
    return record.voice.rate if record.voice is not None else playable[0].ref.rate


def _too_large(what: str, max_bytes: int) -> AudioUnavailable:
    mb = 1024 * 1024
    limit = f"{max_bytes // mb} MB" if max_bytes >= mb else f"{max_bytes} bytes"
    return AudioUnavailable(
        "AUDIO_TOO_LARGE",
        f"{what}, over the {limit} a whole call is allowed: export it turn by turn "
        "(`opseval audio export … --turn N`)",
    )


def check_size(
    record: RunRecord,
    every: list[Turn],
    playable: list[Turn],
    *,
    stereo: bool,
    max_bytes: int | None = None,
) -> int:
    """The whole call's size in bytes, from the record alone (no file read, nothing
    allocated), or ``AUDIO_TOO_LARGE``. A record's times and sizes are data: a corrupted
    one (a start hours into the call, a turn of gigabytes) is refused here, never built."""
    max_bytes = MAX_CALL_BYTES if max_bytes is None else max_bytes
    rate = _call_rate(record, playable)
    channels = 2 if stereo else 1
    files = sum(t.ref.bytes for t in playable)
    if files > max_bytes:
        raise _too_large(f"its turns hold {files} bytes of audio", max_bytes)
    when = schedule(record, every)
    end_ms = max(when[t.seq] + max(0.0, t.duration_ms) for t in playable)
    if not math.isfinite(end_ms) or end_ms > MAX_CALL_SECONDS * 1000:
        raise _too_large(f"the record's times put this call at {end_ms / 1000:.0f} s", max_bytes)
    size = WAV_HEADER + (math.ceil(end_ms * rate / 1000) + 1) * channels * 2
    if size > max_bytes:
        raise _too_large(
            f"the call is {end_ms / 1000:.0f} s, {size} bytes as a WAV at {rate} Hz", max_bytes
        )
    return size


_TAG = re.compile(r"\(turn (\d+)\)\s*$")
"""How the bridge ties a ``barge_in`` event to the agent turn it opened: the turn's file
number (``003-agent.wav`` is turn 3), at the end of the event's ``detail``."""


def _file_number(ref: AudioRef) -> int | None:
    found = _NAME.match(ref.path.rsplit("/", 1)[-1]) if ref.path else None
    return int(found.group(1)) if found else None


def barged(record: RunRecord, found: list[Turn]) -> set[int]:
    """The seqs of the agent turns a recorded ``barge_in`` opened.

    By the turn number the bridge writes into the event when the turn is kept. An event
    without one (a record from before it did, or a turn cut short) belongs to the last
    agent turn that started at or before it with no caller turn starting in between: the
    event is stamped when the agent's speech began, which is never before its turn's first
    chunk, but can be after the turn's last sample when the chunks are sparse (VAD-gated),
    so containment in ``[start, end]`` is not the test."""
    events = [e for e in (record.voice.events if record.voice else ()) if e.kind == "barge_in"]
    if not events:
        return set()
    ordered = sorted(found, key=lambda t: (t.ref.start_ms, t.seq))
    by_number = {
        n: t.seq
        for t in ordered
        if t.ref.speaker == "agent" and (n := _file_number(t.ref)) is not None
    }
    out: set[int] = set()
    for event in events:
        tag = _TAG.search(event.detail or "")
        if tag is not None and int(tag.group(1)) in by_number:
            out.add(by_number[int(tag.group(1))])
            continue
        owner: Turn | None = None
        for turn in ordered:
            if turn.ref.start_ms > event.at_ms:
                break
            if turn.ref.speaker == "agent":
                owner = turn
            elif owner is not None:
                owner = None  # the caller spoke after it: that agent turn is not this one
        if owner is not None:
            out.add(owner.seq)
    return out


def schedule(record: RunRecord, found: list[Turn]) -> dict[int, float]:
    """When each turn plays in the whole call, in ms from the attempt's start.

    A turn plays at its recorded ``start_ms`` (the attempt's clock), with the record's own
    silences between turns. One exception: a turn the record shows starting before the
    previous turn ended, with no ``barge_in`` event for it (:func:`barged`), is moved to
    that end, and the turns after it with it. Audio sent faster than it plays (a
    request/response transport such as ``audio-http``, or the demo) has start times closer
    together than the speech is long, and played as recorded it would be two people
    talking at once that never did. A barge-in the bridge recorded is kept as the overlap
    it was.
    """
    if record.voice is not None and record.voice.mode == "duplex":
        # The full-duplex driver plays both sides in real time (ADR-0055): the recorded
        # times are the call as it happened, overlaps included.
        return {turn.seq: turn.ref.start_ms for turn in found}
    kept = barged(record, found)
    out: dict[int, float] = {}
    shift = 0.0
    end = 0.0
    for turn in sorted(found, key=lambda t: (t.ref.start_ms, t.seq)):
        at = turn.ref.start_ms + shift
        if at < end and turn.seq not in kept:
            shift += end - at
            at = end
        out[turn.seq] = at
        end = max(end, at + turn.duration_ms)
    return out


def _header(rate: int, channels: int, data_bytes: int) -> bytes:
    """A PCM16 WAV header (the 44 bytes ``wave`` writes)."""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        rate,
        rate * channels * 2,
        channels * 2,
        16,
        b"data",
        data_bytes,
    )


def whole_call(
    run_dir: Source,
    record: RunRecord,
    key: AudioKey | None,
    *,
    stereo: bool = True,
    only: Speaker | None = None,
    found: list[Turn] | None = None,
    max_bytes: int | None = None,
) -> Call:
    """The attempt as one WAV on its own clock: each turn starts at sample
    ``round(at_ms * rate / 1000)``, ``at_ms`` from :func:`schedule` (its recorded start,
    unless sent faster than real time), with silence between. Stereo puts the caller on the
    left and the agent on the right; mono sums them, clipped to 16 bits where they overlap.
    ``only`` keeps one side (mono), timed as in the whole call.

    The size is checked from the record before any file is read (:func:`check_size`), and
    again from the decoded audio before the call is allocated; the WAV is then built once,
    in place: its header, then each turn written into it."""
    stereo = stereo and only is None
    max_bytes = MAX_CALL_BYTES if max_bytes is None else max_bytes
    every = turns(run_dir, record) if found is None else found
    picked = call_turns(
        run_dir, record, key, only=only, found=every, max_bytes=max_bytes, stereo=stereo
    )
    when = schedule(record, every)
    decoded: list[tuple[Turn, bytes, int]] = []
    skipped: list[int] = []
    for turn in picked:
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
    rate = _call_rate(record, [t for t, _, _ in decoded])
    channels = 2 if stereo else 1
    length = 0
    for turn, pcm, turn_rate in decoded:
        n = len(pcm) // 2 if turn_rate == rate else math.ceil(len(pcm) // 2 * rate / turn_rate)
        length = max(length, round(when[turn.seq] * rate / 1000) + n + 1)
    if WAV_HEADER + length * channels * 2 > max_bytes:
        raise _too_large(f"the decoded call is {length} samples at {rate} Hz", max_bytes)
    out = bytearray(WAV_HEADER + length * channels * 2)
    data = memoryview(out)[WAV_HEADER:].cast("h")
    busy: dict[int, list[tuple[int, int]]] = {}
    used = 0
    while decoded:
        turn, pcm, turn_rate = decoded.pop(0)  # each turn's audio is let go once placed
        if turn_rate != rate:
            pcm = resample(pcm, turn_rate, rate)
        samples = _samples(pcm)
        del pcm
        at = round(when[turn.seq] * rate / 1000)
        channel = 1 if stereo and turn.ref.speaker == "agent" else 0
        _place_into(
            data, samples, at, busy.setdefault(channel, []), channel=channel, stride=channels
        )
        used = max(used, at + len(samples))
    data.release()
    del out[WAV_HEADER + used * channels * 2 :]  # the estimate's spare samples
    out[:WAV_HEADER] = _header(rate, channels, used * channels * 2)
    if sys.byteorder == "big":
        body = array("h")
        body.frombytes(out[WAV_HEADER:])
        body.byteswap()
        out[WAV_HEADER:] = body.tobytes()
    return Call(out, rate, channels, tuple(skipped))


__all__ = [
    "MAX_CALL_BYTES",
    "MAX_CALL_SECONDS",
    "PRUNED_MARKER",
    "Call",
    "State",
    "Turn",
    "barged",
    "call_turns",
    "check_size",
    "find",
    "key_problem",
    "read_bytes",
    "schedule",
    "turn_wav",
    "turns",
    "whole_call",
]
