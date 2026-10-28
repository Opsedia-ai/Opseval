# SPDX-License-Identifier: Apache-2.0
"""Audio, in-house and dependency-free (ADR-0053).

Voice transports speak PCM16, μ-law or A-law at 8 to 48 kHz. ``audioop`` was removed in
Python 3.13 and the core runs on 3.11 to 3.13, so the conversions are written here: G.711
μ-law and A-law, linear resampling, WAV in and out (stdlib ``wave``), and the energy measure
the voice bridge uses to tell silence from speech.

Everything works on **PCM16 little-endian mono**, the internal format. A decoding problem is
an :class:`AudioDecodeError`, which a voice transport turns into the agent's fault (it sent
broken audio, ADR-0053), never into a harness error.
"""

from __future__ import annotations

import io
import math
import sys
import wave
from array import array
from dataclasses import dataclass
from functools import cache
from typing import Literal

Codec = Literal["pcm16", "mulaw", "alaw"]
CODECS: tuple[Codec, ...] = ("pcm16", "mulaw", "alaw")
RATES = (8_000, 16_000, 22_050, 24_000, 44_100, 48_000)
SILENCE_DBFS = -50.0
"""Below this level a chunk is silence (ADR-0053: silence is not an empty transcript). The
per-language calibrated value is ADR-0059's; this is the default."""


class AudioDecodeError(ValueError):
    """Audio that cannot be read as the format it claims to be."""


@dataclass(frozen=True)
class AudioFormat:
    codec: Codec = "pcm16"
    rate: int = 16_000

    def __post_init__(self) -> None:
        if self.codec not in CODECS:
            raise ValueError(f"unknown codec {self.codec!r}; one of {', '.join(CODECS)}")
        if self.rate not in RATES:
            raise ValueError(f"unsupported rate {self.rate}; one of {RATES}")

    @property
    def bytes_per_sample(self) -> int:
        return 2 if self.codec == "pcm16" else 1


# --- PCM16 as samples ------------------------------------------------------------------


def _samples(pcm16: bytes) -> array[int]:
    if len(pcm16) % 2:
        raise AudioDecodeError(f"PCM16 needs an even number of bytes, got {len(pcm16)}")
    out = array("h")
    out.frombytes(pcm16)
    if sys.byteorder == "big":
        out.byteswap()
    return out


def _bytes(samples: array[int]) -> bytes:
    if sys.byteorder == "big":
        samples = array("h", samples)
        samples.byteswap()
    return samples.tobytes()


# --- G.711 (ITU-T), μ-law and A-law ---------------------------------------------------

_BIAS = 0x84
_SEG_END = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _mulaw_byte(sample: int) -> int:
    # The reference G.711 encoder (Sun's g711.c, as audioop used it): on 14 bits.
    value = sample >> 2
    if value < 0:
        value, mask = -value, 0x7F
    else:
        mask = 0xFF
    value = min(value, 8159) + (_BIAS >> 2)
    segment = next((i for i, end in enumerate(_SEG_END) if value <= end), 8)
    if segment >= 8:
        return 0x7F ^ mask
    return ((segment << 4) | ((value >> (segment + 1)) & 0x0F)) ^ mask


def _mulaw_sample(byte: int) -> int:
    byte = ~byte & 0xFF
    sign, exponent, mantissa = byte & 0x80, (byte >> 4) & 0x07, byte & 0x0F
    magnitude = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -magnitude if sign else magnitude


def _alaw_byte(sample: int) -> int:
    sign = 0x00 if sample < 0 else 0x80
    magnitude = min(-sample - 1 if sample < 0 else sample, 32767) >> 3
    if magnitude < 32:
        code = magnitude >> 1
    else:
        exponent = magnitude.bit_length() - 5
        code = (exponent << 4) | ((magnitude >> exponent) & 0x0F)
    return (code | sign) ^ 0x55


def _alaw_sample(byte: int) -> int:
    byte ^= 0x55
    exponent, mantissa = (byte >> 4) & 0x07, byte & 0x0F
    magnitude = (
        (mantissa << 4) + 8 if exponent == 0 else ((mantissa << 4) + 0x108) << (exponent - 1)
    )
    return magnitude if byte & 0x80 else -magnitude


@cache
def _encode_table(codec: Codec) -> bytes:
    """One byte for every PCM16 value, indexed by the value as unsigned 16 bits."""
    to_byte = _mulaw_byte if codec == "mulaw" else _alaw_byte
    return bytes(to_byte(v - 65536 if v >= 32768 else v) for v in range(65536))


@cache
def _decode_table(codec: Codec) -> tuple[int, ...]:
    to_sample = _mulaw_sample if codec == "mulaw" else _alaw_sample
    return tuple(to_sample(b) for b in range(256))


def encode(pcm16: bytes, codec: Codec) -> bytes:
    """PCM16 to ``codec``."""
    if codec == "pcm16":
        _samples(pcm16)  # validates the length
        return pcm16
    table = _encode_table(codec)
    return bytes(table[s & 0xFFFF] for s in _samples(pcm16))


def decode(data: bytes, codec: Codec) -> bytes:
    """``codec`` to PCM16."""
    if codec == "pcm16":
        _samples(data)
        return data
    table = _decode_table(codec)
    return _bytes(array("h", (table[b] for b in data)))


# --- rate, level, length ---------------------------------------------------------------


def resample(pcm16: bytes, from_rate: int, to_rate: int) -> bytes:
    """Linear interpolation. Good enough for speech that a speech model will hear again;
    not a mastering resampler."""
    if from_rate == to_rate:
        return pcm16
    src = _samples(pcm16)
    if not src:
        return b""
    n = max(1, round(len(src) * to_rate / from_rate))
    step = (len(src) - 1) / (n - 1) if n > 1 else 0.0
    out = array("h")
    for i in range(n):
        pos = i * step
        left = int(pos)
        right = min(left + 1, len(src) - 1)
        frac = pos - left
        out.append(round(src[left] + (src[right] - src[left]) * frac))
    return _bytes(out)


def duration_ms(pcm16: bytes, rate: int) -> float:
    return len(pcm16) / 2 / rate * 1000


def level_dbfs(pcm16: bytes) -> float:
    """Root-mean-square level, in dB below full scale. ``-inf`` for no samples or silence."""
    samples = _samples(pcm16)
    if not samples:
        return -math.inf
    mean_square = sum(s * s for s in samples) / len(samples)
    return -math.inf if mean_square == 0 else 10 * math.log10(mean_square / (32768.0**2))


def has_energy(pcm16: bytes, threshold_dbfs: float = SILENCE_DBFS) -> bool:
    """Whether this audio is louder than silence (ADR-0053: an empty transcript for audio
    with energy is a question to settle; for silence it is not)."""
    return level_dbfs(pcm16) > threshold_dbfs


# --- WAV -------------------------------------------------------------------------------


def to_wav(pcm16: bytes, rate: int) -> bytes:
    _samples(pcm16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm16)
    return buffer.getvalue()


def from_wav(data: bytes) -> tuple[bytes, int]:
    """PCM16 mono and its rate. Stereo is mixed down; anything but 16-bit PCM is refused."""
    try:
        with wave.open(io.BytesIO(data), "rb") as src:
            channels, width, rate = src.getnchannels(), src.getsampwidth(), src.getframerate()
            frames = src.readframes(src.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AudioDecodeError(f"not a readable WAV file: {exc}") from exc
    if width != 2:
        raise AudioDecodeError(f"WAV must be 16-bit PCM, got {8 * width}-bit")
    if channels == 1:
        return frames, rate
    samples = _samples(frames)
    mixed = array(
        "h",
        (
            round(sum(samples[i : i + channels]) / channels)
            for i in range(0, len(samples), channels)
        ),
    )
    return _bytes(mixed), rate


__all__ = [
    "CODECS",
    "RATES",
    "SILENCE_DBFS",
    "AudioDecodeError",
    "AudioFormat",
    "Codec",
    "decode",
    "duration_ms",
    "encode",
    "from_wav",
    "has_energy",
    "level_dbfs",
    "resample",
    "to_wav",
]
