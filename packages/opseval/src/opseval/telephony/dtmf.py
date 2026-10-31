# SPDX-License-Identifier: Apache-2.0
"""DTMF key presses as audio (ADR-0056): the caller presses keys in-band, in its own audio.

A media stream has no "press a key" message in the direction Twilio plays to the far end, and
replacing the call's TwiML to play digits would end the stream. So the caller's key presses
are the dual tones themselves (ITU-T Q.23), played on the line like any other caller audio, as
a handset does. :func:`detect` reads them back (Goertzel on 20 ms frames); the demo's phone
agent and the tests use it. Standard library only.
"""

from __future__ import annotations

import math
from array import array

ROWS = (697, 770, 852, 941)
COLUMNS = (1209, 1336, 1477, 1633)
KEYS = ("123A", "456B", "789C", "*0#D")
TONE_MS = 100
GAP_MS = 80
AMPLITUDE = 6_000
"""Per component: the two together stay well inside PCM16."""
FRAME_MS = 20
MIN_SHARE = 0.25
"""Each of the two tones must carry at least this share of a frame's energy, and together
:data:`MIN_TOTAL` of it, for the frame to be a key: speech and single tones never are."""
MIN_TOTAL = 0.7
MIN_RMS = 300.0


def tones(digits: str, rate: int) -> bytes:
    """``digits`` as PCM16 at ``rate``: each key ``TONE_MS`` of its two tones, then ``GAP_MS``
    of silence."""
    on = rate * TONE_MS // 1000
    off = rate * GAP_MS // 1000
    out = array("h")
    for key in digits:
        low, high = _freqs(key)
        a, b = 2 * math.pi * low / rate, 2 * math.pi * high / rate
        out.extend(round(AMPLITUDE * (math.sin(a * i) + math.sin(b * i))) for i in range(on))
        out.extend([0] * off)
    return out.tobytes()


def _freqs(key: str) -> tuple[int, int]:
    for row, keys in enumerate(KEYS):
        if key in keys:
            return ROWS[row], COLUMNS[keys.index(key)]
    raise ValueError(f"{key!r} is not a DTMF key (0-9, *, #, A-D)")


def _power(samples: array[int], start: int, n: int, freq: float, rate: int) -> float:
    coeff = 2 * math.cos(2 * math.pi * freq / rate)
    s1 = s2 = 0.0
    for i in range(start, start + n):
        s0 = samples[i] + coeff * s1 - s2
        s2, s1 = s1, s0
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def key_of(samples: array[int], start: int, n: int, rate: int) -> str | None:
    """The key one frame holds, or ``None`` (silence, speech, a single tone)."""
    energy = 0.0
    for i in range(start, start + n):
        energy += samples[i] * samples[i]
    if n == 0 or math.sqrt(energy / n) < MIN_RMS:
        return None
    # A sinusoid of amplitude A over n samples: Goertzel power (A n / 2)^2, energy n A^2 / 2.
    scale = n * energy / 2
    rows = [_power(samples, start, n, f, rate) / scale for f in ROWS]
    cols = [_power(samples, start, n, f, rate) / scale for f in COLUMNS]
    r = max(range(4), key=rows.__getitem__)
    c = max(range(4), key=cols.__getitem__)
    if rows[r] < MIN_SHARE or cols[c] < MIN_SHARE or rows[r] + cols[c] < MIN_TOTAL:
        return None
    return KEYS[r][c]


def frames(pcm16: bytes, rate: int) -> list[str | None]:
    """The key each ``FRAME_MS`` frame of ``pcm16`` holds."""
    samples = array("h")
    samples.frombytes(pcm16[: len(pcm16) // 2 * 2])
    n = rate * FRAME_MS // 1000
    return [key_of(samples, i, n, rate) for i in range(0, len(samples) - n + 1, n)]


def detect(pcm16: bytes, rate: int) -> str:
    """The keys pressed in ``pcm16``: a key held for two frames or more counts once, until a
    frame without it."""
    out: list[str] = []
    previous: str | None = None
    run = 0
    for key in frames(pcm16, rate):
        if key is not None and key == previous:
            run += 1
            if run == 2:
                out.append(key)
        else:
            run = 1 if key is not None else 0
        previous = key
    return "".join(out)


__all__ = ["COLUMNS", "KEYS", "ROWS", "detect", "frames", "key_of", "tones"]
