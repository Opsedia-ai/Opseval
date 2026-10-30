# SPDX-License-Identifier: Apache-2.0
"""Where a voice attempt's audio goes (ADR-0053).

One file per speaker per turn, under the run folder's ``audio/``:
``audio/<test id>/attempt<N>/<turn>-<speaker>.wav``. Caller and agent are separate files, so
either can be withheld or deleted alone. Every turn's SHA-256 is recorded on its
:class:`~opseval.models.record.AudioRef`, whatever is kept.

``OPSEVAL_RECORD_AUDIO=hashes`` keeps no bytes at all: only the hash, length, codec, rate and
duration. Audio the agent sent that could not be decoded is kept as it came (a ``.raw``
file), or as its hash alone in hashes mode.

Files are encrypted at rest (``voice/crypto.py``, V0d): with ``OPSEVAL_AUDIO_ENCRYPTION_KEY``
each file is sealed on its own with AES-256-GCM and gets ``.enc`` after its name
(``003-agent.wav.enc``). :func:`check_audio_privacy` refuses at launch (exit 3) a run that
would write audio files without the key, except the demo's synthetic voices, which are
written as plain WAV unless a key is set.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from opseval.audio import Codec, duration_ms, to_wav
from opseval.config import Settings
from opseval.errors import ConfigError
from opseval.models.record import AudioRef
from opseval.storage import SAFE_ID, FileRunAudio, RunAudio
from opseval.voice.crypto import SUFFIX, AudioKey, FilePlace, load_key, parse_key

SAFE = re.compile(r"[^A-Za-z0-9_.-]")
Mode = Literal["files", "hashes"]


@dataclass(frozen=True)
class AudioPlan:
    """What a voice run keeps, and the key that seals it (``None``: plain files, the demo's
    synthetic voices only, or hashes, which have no file to seal)."""

    mode: Mode
    key: AudioKey | None = None


def check_audio_privacy(settings: Settings, *, synthetic: bool = False) -> AudioPlan:
    """What a voice run may keep, or :class:`ConfigError` (exit 3) before anything is sent.

    A voice can be biometric data, so audio is only ever stored encrypted (ADR-0053):

    - ``OPSEVAL_RECORD_AUDIO=hashes``: no bytes are written (a key set is still checked, so
      a malformed one is found now rather than on the first run that needs it);
    - ``files`` with the key: every file sealed with AES-256-GCM (the ``opseval[voice]``
      extra; without it, refused naming the install command);
    - ``files`` without a key: refused, unless the voices are the demo's own synthetic ones.
    """
    secret = settings.audio_encryption_key
    raw = secret.get_secret_value() if secret is not None else None
    mode: Mode = settings.record_audio
    if mode == "hashes":
        if raw is not None and raw.strip():
            parse_key(raw)
        return AudioPlan("hashes")
    key = load_key(raw)
    if key is None and not synthetic:
        raise ConfigError(
            "a voice run records the caller's and the agent's audio, which is only ever stored "
            "encrypted (ADR-0053): set OPSEVAL_AUDIO_ENCRYPTION_KEY (32 random bytes, base64 "
            "or hex: `opseval audio keygen`) with the voice extra installed, or set "
            "OPSEVAL_RECORD_AUDIO=hashes to keep only each turn's hash and metadata"
        )
    return AudioPlan("files", key)


@dataclass
class AudioRecorder:
    """Keeps one attempt's audio. ``run_dir`` is where the run keeps it: the store's
    :class:`~opseval.storage.RunAudio`, or a run folder; ``None`` keeps hashes only (a run
    with nowhere to write, such as a test)."""

    run_dir: RunAudio | Path | None
    test_id: str
    attempt: int
    mode: Mode = "hashes"
    key: AudioKey | None = None
    """Seals every file (AES-256-GCM). ``None`` writes plain files: only the demo's
    synthetic voices get there (:func:`check_audio_privacy`)."""
    run_id: str | None = None
    """Bound into each sealed file. Default: the run folder's name."""

    def _relative(self, turn: int, speaker: str, suffix: str) -> str:
        # '@' is kept (storage.SAFE_ID): an expansion's folder (ADR-0060) is never an
        # authored id's.
        test = SAFE_ID.sub("_", self.test_id).strip(".") or "scenario"
        if self.key is not None:
            suffix += SUFFIX
        return f"audio/{test}/attempt{self.attempt}/{turn:03d}-{speaker}{suffix}"

    def _sealed(self, path: str | None) -> tuple[Literal["none", "aes-256-gcm"], str | None]:
        """How a file was written: ``(encryption, key_id)`` for its ``AudioRef``."""
        if path is None or self.key is None:
            return "none", None
        return "aes-256-gcm", self.key.key_id

    def _write(
        self, relative: str, data: bytes, turn: int, speaker: Literal["caller", "agent"]
    ) -> str | None:
        if self.mode != "files" or self.run_dir is None:
            return None
        audio = FileRunAudio(self.run_dir) if isinstance(self.run_dir, Path) else self.run_dir
        if self.key is not None:
            run_id = self.run_id if self.run_id is not None else audio.run_id
            place = FilePlace(run_id, self.test_id, self.attempt, turn, speaker)
            data = self.key.seal(data, place)
        audio.write(relative, data)  # never outside audio/, whatever the id (ValueError)
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
        path = self._write(self._relative(turn, speaker, ".wav"), wav, turn, speaker)
        encryption, key_id = self._sealed(path)
        return AudioRef(
            encryption=encryption,
            key_id=key_id,
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
        path = self._write(self._relative(turn, "agent", ".raw"), data, turn, "agent")
        width = 2 if codec == "pcm16" else 1
        encryption, key_id = self._sealed(path)
        return AudioRef(
            encryption=encryption,
            key_id=key_id,
            path=path,
            speaker="agent",
            start_ms=max(0.0, start_ms),
            end_ms=max(0.0, start_ms) + len(data) / width / rate * 1000,
            sha256=hashlib.sha256(data).hexdigest(),
            codec=codec,
            rate=rate,
            bytes=len(data),
        )


__all__ = ["AudioPlan", "AudioRecorder", "Mode", "check_audio_privacy"]
