# SPDX-License-Identifier: Apache-2.0
"""Where a voice attempt's audio goes (ADR-0053).

One file per speaker per turn, under the run folder's ``audio/``:
``audio/<test id>/attempt<N>/<turn>-<speaker>.wav``. Caller and agent are separate files, so
either can be withheld or deleted alone. Every turn's SHA-256 is recorded on its
:class:`~opseval.models.record.AudioRef`, whatever is kept.

``OPSEVAL_RECORD_AUDIO=hashes`` keeps no bytes at all: only the hash, length, codec, rate and
duration. Audio the agent sent that could not be decoded is kept as it came (a ``.raw``
file), or as its hash alone in hashes mode.

Encryption at rest is the next voice step. Until it lands, :func:`check_audio_privacy`
refuses a run that would write audio files (exit 3), except the demo's synthetic voices.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from opseval.audio import Codec, duration_ms, to_wav
from opseval.config import Settings
from opseval.errors import ConfigError
from opseval.models.record import AudioRef

SAFE = re.compile(r"[^A-Za-z0-9_.-]")
Mode = Literal["files", "hashes"]


def check_audio_privacy(settings: Settings, *, synthetic: bool = False) -> Mode:
    """What a voice run may keep, or :class:`ConfigError` (exit 3) before anything is sent.

    A voice can be biometric data, so audio is only ever stored encrypted (ADR-0053). The
    encryption library is not a core dependency yet, so for now:

    - ``OPSEVAL_RECORD_AUDIO=hashes``: allowed, no bytes are written;
    - ``files`` without a key: refused, unless the voices are the demo's own synthetic ones;
    - a key set: refused, because the run would claim an encryption this version cannot do.
    """
    if settings.audio_encryption_key is not None:
        raise ConfigError(
            "OPSEVAL_AUDIO_ENCRYPTION_KEY is set, but audio encryption arrives in the next voice "
            "step and this version cannot encrypt: use OPSEVAL_RECORD_AUDIO=hashes for now"
        )
    mode: Mode = settings.record_audio
    if mode == "files" and not synthetic:
        raise ConfigError(
            "a voice run records the caller's and the agent's audio, which must be encrypted at "
            "rest (ADR-0053), and this version cannot encrypt yet: set "
            "OPSEVAL_RECORD_AUDIO=hashes to keep only each turn's hash and metadata"
        )
    return mode


@dataclass
class AudioRecorder:
    """Keeps one attempt's audio. ``run_dir`` is the run folder; ``None`` keeps hashes only
    (a run with no folder, such as a test)."""

    run_dir: Path | None
    test_id: str
    attempt: int
    mode: Mode = "hashes"

    def _relative(self, turn: int, speaker: str, suffix: str) -> str:
        test = SAFE.sub("_", self.test_id).strip(".") or "scenario"
        return f"audio/{test}/attempt{self.attempt}/{turn:03d}-{speaker}{suffix}"

    def _write(self, relative: str, data: bytes) -> str | None:
        if self.mode != "files" or self.run_dir is None:
            return None
        path = self.run_dir / relative
        root = (self.run_dir / "audio").resolve()
        if root not in path.resolve().parents:  # never outside audio/, whatever the id
            raise ValueError(f"audio path {relative!r} leaves the run folder's audio/")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return relative

    def keep(
        self,
        *,
        turn: int,
        speaker: Literal["caller", "agent"],
        pcm16: bytes,
        rate: int,
        start_ms: float,
    ) -> AudioRef:
        """One decoded turn, as a PCM16 WAV (or its hash alone)."""
        wav = to_wav(pcm16, rate)
        path = self._write(self._relative(turn, speaker, ".wav"), wav)
        return AudioRef(
            path=path,
            speaker=speaker,
            start_ms=max(0.0, start_ms),
            end_ms=max(0.0, start_ms) + duration_ms(pcm16, rate),
            sha256=hashlib.sha256(wav).hexdigest(),
            codec="pcm16",
            rate=rate,
            bytes=len(wav),
        )

    def keep_raw(
        self, *, turn: int, data: bytes, codec: Codec, rate: int, start_ms: float
    ) -> AudioRef:
        """Agent audio that could not be decoded: the bytes as they came, never re-encoded."""
        path = self._write(self._relative(turn, "agent", ".raw"), data)
        width = 2 if codec == "pcm16" else 1
        return AudioRef(
            path=path,
            speaker="agent",
            start_ms=max(0.0, start_ms),
            end_ms=max(0.0, start_ms) + len(data) / width / rate * 1000,
            sha256=hashlib.sha256(data).hexdigest(),
            codec=codec,
            rate=rate,
            bytes=len(data),
        )


__all__ = ["AudioRecorder", "Mode", "check_audio_privacy"]
