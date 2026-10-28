# SPDX-License-Identifier: Apache-2.0
"""Audio encrypted at rest (ADR-0053, V0d).

Every audio file a voice run keeps is sealed on its own with AES-256-GCM, under
``OPSEVAL_AUDIO_ENCRYPTION_KEY``. The file is::

    magic "OPSAUDIO" (8) | version 1 (1) | key id (8) | nonce (12) | ciphertext + tag (16)

- **The nonce** is 96 random bits, fresh for every file.
- **The key id** is the first 16 hex of HMAC-SHA256(key, "opseval-audio-key-id"): it says
  which key opens a file after a rotation, and it is never the key, nor a hash an attacker
  could test a guess against without the key.
- **The additional data** is the header and the file's place: run id, test id, attempt, the
  turn number in its name, and the speaker. A file moved to another turn, attempt, speaker
  or run does not authenticate, so a swapped recording is refused, never played as
  another turn's.

The library (``cryptography``) is the optional ``opseval[voice]`` extra. It is imported
only when audio is sealed or opened, and a missing extra is a :class:`ConfigError`
(exit 3) naming the install command, never an unencrypted file.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from opseval.errors import ConfigError

MAGIC = b"OPSAUDIO"
VERSION = 1
KEY_ID_BYTES = 8
NONCE_BYTES = 12
HEADER_BYTES = len(MAGIC) + 1 + KEY_ID_BYTES + NONCE_BYTES
SUFFIX = ".enc"
INSTALL = "pip install 'opseval[voice]'"
KEY_HELP = (
    "32 random bytes as 64 hex characters or as base64 (make one with "
    "`opseval audio keygen` or `openssl rand -base64 32`)"
)

AudioCode = Literal[
    "AUDIO_PRUNED",
    "AUDIO_HASHES_ONLY",
    "AUDIO_KEY_MISSING",
    "AUDIO_KEY_MISMATCH",
    "AUDIO_TAMPERED",
    "AUDIO_MISSING",
    "AUDIO_UNDECODABLE",
    "AUDIO_NOT_FOUND",
]


class AudioUnavailable(ConfigError):
    """A turn's audio cannot be played, and why, as a code the console can act on.

    A configuration-class error (exit 3 from the CLI): never a verdict on the agent."""

    def __init__(self, code: AudioCode, message: str) -> None:
        super().__init__(message)
        self.code: AudioCode = code


_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


def parse_key(raw: str) -> bytes:
    """``OPSEVAL_AUDIO_ENCRYPTION_KEY`` as 32 bytes, or :class:`ConfigError`.

    Two spellings: 64 hex characters, or base64 (standard or URL-safe) of exactly 32 bytes.
    The two never overlap (64 base64 characters are 48 bytes). A key of fewer distinct
    bytes than a random key could plausibly have (a repeated character, a counter) is
    refused as a placeholder, not a key.
    """
    text = raw.strip()
    key: bytes | None = None
    if _HEX.match(text):
        key = bytes.fromhex(text)
    else:
        standard = text.replace("-", "+").replace("_", "/")  # URL-safe, read as standard
        try:
            key = base64.b64decode(standard.encode("ascii"), validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError):
            key = None
    if key is None or len(key) != 32:
        size = "" if key is None else f" (it decodes to {len(key)} bytes)"
        raise ConfigError(f"OPSEVAL_AUDIO_ENCRYPTION_KEY must be {KEY_HELP}{size}")
    if len(set(key)) < 12:
        raise ConfigError(
            "OPSEVAL_AUDIO_ENCRYPTION_KEY looks like a placeholder, not a random key (too few "
            f"distinct bytes): use {KEY_HELP}"
        )
    return key


def key_id(key: bytes) -> str:
    """The key's fingerprint, written beside every file it seals: never the key."""
    return hmac.new(key, b"opseval-audio-key-id", hashlib.sha256).hexdigest()[: 2 * KEY_ID_BYTES]


def new_key() -> str:
    """A fresh key, base64."""
    return base64.b64encode(os.urandom(32)).decode("ascii")


def _aesgcm() -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # the optional extra
        raise ConfigError(
            f"encrypting or opening recorded audio needs the voice extra: `{INSTALL}` (ADR-0053)"
        ) from exc
    return AESGCM


@dataclass(frozen=True)
class FilePlace:
    """Where a file belongs: bound into its additional data."""

    run_id: str
    test_id: str
    attempt: int
    turn: int
    speaker: Literal["caller", "agent"]

    def aad(self, header: bytes) -> bytes:
        place = json.dumps(
            {
                "run": self.run_id,
                "test": self.test_id,
                "attempt": self.attempt,
                "turn": self.turn,
                "speaker": self.speaker,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return header + b"\x00" + place.encode("utf-8")


class AudioKey:
    """A parsed key, ready to seal and open files. Built once per run (or server start)."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ConfigError("an audio key is 32 bytes")
        self._aes = _aesgcm()(key)
        self.key_id = key_id(key)

    @classmethod
    def from_text(cls, raw: str) -> AudioKey:
        return cls(parse_key(raw))

    def seal(self, data: bytes, place: FilePlace) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        header = MAGIC + bytes([VERSION]) + bytes.fromhex(self.key_id) + nonce
        return header + bytes(self._aes.encrypt(nonce, data, place.aad(header)))

    def open(self, blob: bytes, place: FilePlace) -> bytes:
        """The plaintext, or :class:`AudioUnavailable` (wrong key, or a file that was
        modified or does not belong where it was found)."""
        found = file_key_id(blob)
        if found != self.key_id:
            raise AudioUnavailable(
                "AUDIO_KEY_MISMATCH",
                f"this audio was encrypted with key {found}, but the configured "
                f"OPSEVAL_AUDIO_ENCRYPTION_KEY is key {self.key_id}",
            )
        from cryptography.exceptions import InvalidTag

        header, body = blob[:HEADER_BYTES], blob[HEADER_BYTES:]
        nonce = header[-NONCE_BYTES:]
        try:
            return bytes(self._aes.decrypt(nonce, body, place.aad(header)))
        except InvalidTag as exc:
            raise AudioUnavailable(
                "AUDIO_TAMPERED",
                "the audio file does not authenticate: it was modified, or it belongs to "
                "another run, attempt, turn or speaker",
            ) from exc


def file_key_id(blob: bytes) -> str:
    """The key id in a sealed file's header (no key needed), or :class:`AudioUnavailable`."""
    if len(blob) < HEADER_BYTES + 16 or not blob.startswith(MAGIC):
        raise AudioUnavailable("AUDIO_TAMPERED", "not an Opseval encrypted audio file")
    if blob[len(MAGIC)] != VERSION:
        raise AudioUnavailable(
            "AUDIO_TAMPERED",
            f"encrypted audio format version {blob[len(MAGIC)]} is not one this Opseval reads",
        )
    start = len(MAGIC) + 1
    return blob[start : start + KEY_ID_BYTES].hex()


def load_key(raw: str | None) -> AudioKey | None:
    """The configured key, or ``None`` when none is set. A malformed key or a missing
    extra is a :class:`ConfigError` (exit 3)."""
    if raw is None or not raw.strip():
        return None
    return AudioKey(parse_key(raw))


__all__ = [
    "INSTALL",
    "SUFFIX",
    "AudioCode",
    "AudioKey",
    "AudioUnavailable",
    "FilePlace",
    "file_key_id",
    "key_id",
    "load_key",
    "new_key",
    "parse_key",
]
