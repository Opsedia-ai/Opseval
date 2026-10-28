# SPDX-License-Identifier: Apache-2.0
"""Background noise for the caller's side of a call (ADR-0055), generated in-house.

ADR-0055 proposed bundled CC0 clips (street, café, car, call centre). Recorded clips bring a
licence to check per file, megabytes in the wheel, and the risk of shipping recorded speech,
so the noise is generated instead, deterministically from a seed:

- ``white``: flat-spectrum Gaussian noise;
- ``pink``: 1/f noise (Paul Kellett's filter over white noise), close to room and road hum;
- ``babble``: several band-limited noise "voices", each switched on and off at a syllable
  rate, summed: the rhythm of a crowd with no words in it, so no STT can hear speech in it.

:func:`mix` adds it to the caller's PCM16 at a signal-to-noise ratio measured against the
caller's speech (its frames with energy), and clips to 16 bits. It is only ever called on the
caller's audio as sent to the agent: the STT canary and every reference text keep the clean
audio and words (the one rule: planted noise cannot make a healthy STT look broken).
"""

from __future__ import annotations

import math
import random
import sys
from array import array

from opseval.audio import SILENCE_DBFS
from opseval.models.record import NoiseInfo
from opseval.models.voice import Noise, NoiseKind

GENERATOR = "opseval-noise/1"
"""Recorded with every noisy attempt: a change to the generator gives different noise, so it
bumps this."""

_FRAME_MS = 20
_BABBLE_VOICES = 6


def _rng(seed: int, turn: int) -> random.Random:
    # Not for security: reproducible test noise. A different stream per caller turn, so two
    # turns do not carry the same noise.
    return random.Random(seed * 1_000_003 + turn)  # noqa: S311  # nosec B311


def _white(n: int, rng: random.Random) -> list[float]:
    return [rng.gauss(0.0, 1.0) for _ in range(n)]


def _pink(n: int, rng: random.Random) -> list[float]:
    b0 = b1 = b2 = b3 = b4 = b5 = b6 = 0.0
    out = []
    for _ in range(n):
        w = rng.gauss(0.0, 1.0)
        b0 = 0.99886 * b0 + w * 0.0555179
        b1 = 0.99332 * b1 + w * 0.0750759
        b2 = 0.96900 * b2 + w * 0.1538520
        b3 = 0.86650 * b3 + w * 0.3104856
        b4 = 0.55000 * b4 + w * 0.5329522
        b5 = -0.7616 * b5 - w * 0.0168980
        out.append(b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * 0.5362)
        b6 = w * 0.115926
    return out


def _babble(n: int, rate: int, rng: random.Random) -> list[float]:
    total = [0.0] * n
    for _ in range(_BABBLE_VOICES):
        # A voice-band noise: low-passed (one pole, ~300 Hz to 1.2 kHz) minus a slower low
        # pass, so it has neither rumble nor hiss.
        cut = rng.uniform(300.0, 1_200.0)
        a_fast = math.exp(-2 * math.pi * cut / rate)
        a_slow = math.exp(-2 * math.pi * 120.0 / rate)
        syllable = rng.uniform(3.0, 6.0)  # syllables a second
        phase = rng.uniform(0.0, 2 * math.pi)
        fast = slow = 0.0
        for i in range(n):
            w = rng.gauss(0.0, 1.0)
            fast = a_fast * fast + (1 - a_fast) * w
            slow = a_slow * slow + (1 - a_slow) * w
            envelope = max(0.0, math.sin(2 * math.pi * syllable * i / rate + phase))
            total[i] += (fast - slow) * envelope
    return total


def generate(kind: NoiseKind, n: int, rate: int, *, seed: int, turn: int = 0) -> list[float]:
    """``n`` samples of noise, unscaled (mean zero), the same for the same arguments."""
    rng = _rng(seed, turn)
    if kind == "white":
        return _white(n, rng)
    if kind == "pink":
        return _pink(n, rng)
    return _babble(n, rate, rng)


def _speech_rms(samples: array[int], rate: int) -> float:
    """RMS over the frames with energy: the caller's speech, not the pauses in it."""
    step = max(1, rate * _FRAME_MS // 1000)
    total = count = 0
    for i in range(0, len(samples), step):
        frame = samples[i : i + step]
        power = sum(s * s for s in frame)
        rms = math.sqrt(power / len(frame))
        if rms > 0 and 20 * math.log10(rms / 32768) > SILENCE_DBFS:
            total += power
            count += len(frame)
    return math.sqrt(total / count) if count else 0.0


def mix(pcm16: bytes, rate: int, spec: Noise, *, turn: int = 0) -> bytes:
    """``pcm16`` with ``spec``'s noise added at ``spec.snr_db`` against its speech. Audio with
    no speech in it is returned unchanged: an SNR against nothing is undefined."""
    samples = array("h")
    samples.frombytes(pcm16)
    if sys.byteorder == "big":  # PCM16 is little-endian
        samples.byteswap()
    speech = _speech_rms(samples, rate)
    if not samples or speech == 0.0:
        return pcm16
    noise = generate(spec.kind, len(samples), rate, seed=spec.seed, turn=turn)
    power = math.sqrt(sum(x * x for x in noise) / len(noise)) or 1.0
    gain = speech / (10 ** (spec.snr_db / 20)) / power
    out = array(
        "h",
        (max(-32768, min(32767, round(s + gain * x))) for s, x in zip(samples, noise, strict=True)),
    )
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def info(spec: Noise) -> NoiseInfo:
    return NoiseInfo(generator=GENERATOR, kind=spec.kind, snr_db=spec.snr_db, seed=spec.seed)


def silence(ms: int, rate: int) -> bytes:
    """``ms`` of digital silence at ``rate`` (PCM16)."""
    return bytes(2 * (rate * ms // 1000))


__all__ = ["GENERATOR", "generate", "info", "mix", "silence"]
