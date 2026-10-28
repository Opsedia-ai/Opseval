# SPDX-License-Identifier: Apache-2.0
"""Synthetic speech for the voice demo and the tests: text as a sequence of tones.

No speech model runs in CI, so the demo's TTS writes text as tones that its STT reads back
exactly, through every step a real voice goes through: resampling, μ-law or A-law, WAV files
and HTTP. It is a reversible encoding, not speech:

- the message is a sync byte, a two-byte length, the UTF-8 text and a one-byte checksum;
- each byte is two symbols (its high and low nibble), each symbol 20 ms of one of 16 tones
  between 600 and 3300 Hz (all below the 4 kHz Nyquist limit of an 8 kHz phone line);
- :func:`decode` finds the first sound, measures each symbol's 16 tone powers on its middle
  part (Goertzel), and gives up (``""``) unless every symbol has one clearly dominant tone and
  the length and checksum agree. Noise therefore reads as nothing, like an STT hearing
  garbled speech.
"""

from __future__ import annotations

import math
import random
from array import array
from functools import cache
from itertools import starmap
from operator import mul

SYMBOL_MS = 20
FREQS = tuple(600 + 180 * k for k in range(16))
AMPLITUDE = 9_000
SYNC = 0xA5
ONSET = 1_500
"""A sample louder than this starts the message."""
DOMINANCE = 4.0
"""The strongest tone's power must be this many times the next one's."""


def _frame(text: str) -> bytes:
    data = text.encode("utf-8")
    if len(data) > 0xFFFF:
        raise ValueError("text too long for the tone encoding")
    body = bytes([SYNC]) + len(data).to_bytes(2, "big") + data
    return body + bytes([sum(body) & 0xFF])


def symbol_samples(rate: int) -> int:
    return rate * SYMBOL_MS // 1000


def encode(text: str, rate: int) -> bytes:
    """``text`` as PCM16 tones at ``rate``."""
    n = symbol_samples(rate)
    out = array("h")
    for byte in _frame(text):
        for nibble in (byte >> 4, byte & 0x0F):
            step = 2 * math.pi * FREQS[nibble] / rate
            out.extend(round(AMPLITUDE * math.sin(step * i)) for i in range(n))
    return out.tobytes()


def noise(seconds: float, rate: int, seed: int = 7) -> bytes:
    """Loud, deterministic noise: speech with energy that no STT can read."""
    rng = random.Random(seed)  # noqa: S311  # nosec B311 - a test signal
    return array("h", (rng.randint(-9_000, 9_000) for _ in range(int(seconds * rate)))).tobytes()


@cache
def _tables(rate: int, length: int) -> tuple[tuple[tuple[float, ...], tuple[float, ...]], ...]:
    return tuple(
        (
            tuple(math.cos(2 * math.pi * f * i / rate) for i in range(length)),
            tuple(math.sin(2 * math.pi * f * i / rate) for i in range(length)),
        )
        for f in FREQS
    )


def _symbol(window: array[int], rate: int) -> int | None:
    tables = _tables(rate, len(window))
    powers = [
        sum(starmap(mul, zip(window, cos, strict=True))) ** 2
        + sum(starmap(mul, zip(window, sin, strict=True))) ** 2
        for cos, sin in tables
    ]
    ranked = sorted(range(16), key=powers.__getitem__, reverse=True)
    best, second = powers[ranked[0]], powers[ranked[1]]
    if best <= 0 or best < DOMINANCE * second:
        return None
    return ranked[0]


def decode(pcm16: bytes, rate: int) -> str:
    """The text in ``pcm16``, or ``""`` when it is not a clean tone message."""
    samples = array("h")
    samples.frombytes(pcm16[: len(pcm16) - len(pcm16) % 2])
    start = next((i for i, s in enumerate(samples) if abs(s) > ONSET), None)
    if start is None:
        return ""
    n = symbol_samples(rate)
    lo, hi = n * 15 // 100, n * 85 // 100

    def nibble(k: int) -> int | None:
        base = start + k * n
        if base + hi > len(samples):
            return None
        return _symbol(samples[base + lo : base + hi], rate)

    def byte(k: int) -> int | None:
        high, low = nibble(2 * k), nibble(2 * k + 1)
        return None if high is None or low is None else (high << 4) | low

    head = [byte(k) for k in range(3)]
    if None in head or head[0] != SYNC:
        return ""
    length = (head[1] or 0) << 8 | (head[2] or 0)
    rest = [byte(3 + k) for k in range(length + 1)]
    if None in rest:
        return ""
    body = bytes([SYNC, head[1] or 0, head[2] or 0, *(b or 0 for b in rest[:-1])])
    if sum(body) & 0xFF != rest[-1]:
        return ""
    try:
        return body[3:].decode("utf-8")
    except UnicodeDecodeError:
        return ""


__all__ = ["FREQS", "SYMBOL_MS", "decode", "encode", "noise"]
