# SPDX-License-Identifier: Apache-2.0
"""Audio, in-house and dependency-free (ADR-0053).

Voice transports speak PCM16, μ-law or A-law at 8 to 48 kHz. ``audioop`` was removed in
Python 3.13 and the core runs on 3.11 to 3.13, so the conversions are written here: G.711
μ-law and A-law, linear resampling, WAV in and out (stdlib ``wave``), and the energy measure
the voice bridge uses to tell silence from speech. Transports call :func:`warm` for their
codec at session start, so the lookup tables are built before the attempt's clock matters.

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
def _decode_table(codec: Codec) -> tuple[bytes, ...]:
    """Two little-endian PCM16 bytes for every code: 256 entries, 512 bytes in all."""
    to_sample = _mulaw_sample if codec == "mulaw" else _alaw_sample
    return tuple(to_sample(b).to_bytes(2, "little", signed=True) for b in range(256))


def warm(codec: Codec) -> None:
    """Build ``codec``'s tables now. The encode table has 65536 entries and takes tens of
    milliseconds to build, so a voice transport calls this at session start, before the
    attempt's clock matters, rather than paying for it on the first turn (ADR-0053)."""
    if codec not in CODECS:
        raise ValueError(f"unknown codec {codec!r}; one of {', '.join(CODECS)}")
    if codec != "pcm16":
        _encode_table(codec)
        _decode_table(codec)


def encode(pcm16: bytes, codec: Codec) -> bytes:
    """PCM16 to ``codec``."""
    if len(pcm16) % 2:
        raise AudioDecodeError(f"PCM16 needs an even number of bytes, got {len(pcm16)}")
    if codec == "pcm16":
        return pcm16
    unsigned = array("H")
    unsigned.frombytes(pcm16)
    if sys.byteorder == "big":
        unsigned.byteswap()
    return bytes(map(_encode_table(codec).__getitem__, unsigned))


def decode(data: bytes, codec: Codec) -> bytes:
    """``codec`` to PCM16."""
    if codec == "pcm16":
        _samples(data)
        return data
    return b"".join(map(_decode_table(codec).__getitem__, data))


# --- rate, level, length ---------------------------------------------------------------


def _check_rate(name: str, rate: int) -> None:
    if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
        raise ValueError(f"{name} must be a positive whole number of hertz, got {rate!r}")


class Resampler:
    """Linear-interpolation resampling of a PCM16 stream, chunk by chunk.

    Output sample ``i`` sits at input position ``i * from_rate / to_rate`` exactly: the
    position is kept as an integer fraction across chunks, so there is no per-chunk rounding
    drift, and the last input sample of one chunk is interpolated with the first of the next,
    so there is no discontinuity at a chunk boundary. Feeding a buffer in any number of
    chunks gives the same bytes as feeding it at once. Good enough for speech that a speech
    model will hear again; not a mastering resampler.
    """

    def __init__(self, from_rate: int, to_rate: int) -> None:
        _check_rate("from_rate", from_rate)
        _check_rate("to_rate", to_rate)
        self.from_rate, self.to_rate = from_rate, to_rate
        g = math.gcd(from_rate, to_rate)
        self._up, self._down = from_rate // g, to_rate // g  # input step per output sample
        self._reset()

    def _reset(self) -> None:
        self._buf = array("h")
        self._base = 0  # absolute index of self._buf[0]
        self._total = 0  # input samples fed so far
        self._next = 0  # index of the next output sample

    def _emit(self, out: array[int], limit: int, last: int) -> None:
        """Output samples up to index ``limit`` (exclusive), clamping reads at ``last``."""
        up, down, buf, base = self._up, self._down, self._buf, self._base
        i = self._next
        while i < limit:
            num = i * up
            left = num // down
            if left >= last:  # at or past the last sample (only on flush): hold it
                left, frac = last, 0
            else:
                frac = num - left * down
            a = buf[left - base]
            if frac:
                b = buf[left + 1 - base]
                out.append(round((a * (down - frac) + b * frac) / down))
            else:
                out.append(a)
            i += 1
        self._next = i

    def feed(self, pcm16: bytes) -> bytes:
        """Resample one chunk. Returns what can be computed so far; the rest comes with the
        next chunk or with :meth:`flush`."""
        samples = _samples(pcm16)
        if self._up == self._down:
            return pcm16
        self._buf.extend(samples)
        self._total += len(samples)
        out = array("h")
        # An output sample needs the input sample after its position.
        limit = -(-(self._total - 1) * self._down // self._up) if self._total > 1 else 0
        self._emit(out, limit, self._total - 1)
        keep_from = min(self._next * self._up // self._down, self._total - 1)
        drop = max(0, keep_from - self._base)
        if drop:
            del self._buf[:drop]
            self._base += drop
        return _bytes(out)

    def flush(self) -> bytes:
        """The end of the stream: the remaining samples, so the whole output is
        ``round(fed * to_rate / from_rate)`` samples long. The resampler then starts over."""
        out = array("h")
        if self._up != self._down and self._total:
            n = max(1, (2 * self._total * self._down + self._up) // (2 * self._up))
            self._emit(out, n, self._total - 1)
        self._reset()
        return _bytes(out)


def resample(pcm16: bytes, from_rate: int, to_rate: int) -> bytes:
    """A whole buffer through a :class:`Resampler`."""
    resampler = Resampler(from_rate, to_rate)
    if from_rate == to_rate:
        _samples(pcm16)  # the even-length check holds on every path
        return pcm16
    return resampler.feed(pcm16) + resampler.flush()


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
            declared = src.getnframes()
            frames = src.readframes(declared)
    except (wave.Error, EOFError) as exc:
        raise AudioDecodeError(f"not a readable WAV file: {exc}") from exc
    if width != 2:
        raise AudioDecodeError(f"WAV must be 16-bit PCM, got {8 * width}-bit")
    if rate not in RATES:
        # Refused rather than resampled: the transport decides the rate it speaks.
        raise AudioDecodeError(f"WAV rate {rate} Hz is not supported; one of {RATES}")
    if len(frames) % (channels * 2):
        raise AudioDecodeError(
            f"WAV data is {len(frames)} bytes, not a whole number of {channels}-channel frames"
        )
    if len(frames) != declared * channels * 2:
        raise AudioDecodeError(f"WAV data is cut short: {declared} frames declared")
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
    "Resampler",
    "decode",
    "duration_ms",
    "encode",
    "from_wav",
    "has_energy",
    "level_dbfs",
    "resample",
    "to_wav",
    "warm",
]
