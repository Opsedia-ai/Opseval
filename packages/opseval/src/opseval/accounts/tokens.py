# SPDX-License-Identifier: Apache-2.0
"""Random tokens, their hashes, API key strings and CSRF tokens (ADR-0050). Standard library
only.

Session tokens and key secrets are 256-bit random values, so a plain sha256 is the right
hash for them (a slow hash protects guessable secrets, which these are not). Only hashes are
stored. Every comparison of a secret is constant-time.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

PUBLIC_PREFIX = "pk-oe-"
SECRET_PREFIX = "sk-oe-"  # noqa: S105 - a prefix, not a secret
SHOWN_PREFIX = 10
"""How much of a secret a key list shows (``sk-oe-abcd``), to tell keys apart."""


def new_token() -> str:
    return secrets.token_urlsafe(32)


def digest(token: str) -> str:
    """The stored form of a session token or a key secret."""
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def mint_key() -> tuple[str, str]:
    """A new key: ``(public, secret)``. The secret is shown once and stored as its digest."""
    return PUBLIC_PREFIX + secrets.token_hex(12), SECRET_PREFIX + secrets.token_urlsafe(32)


def parse_key(header: str) -> tuple[str, str] | None:
    """The ``(public, secret)`` an ``Authorization`` header carries, or ``None``.

    Two forms: ``Bearer pk-oe-…:sk-oe-…`` and HTTP basic auth with the public key as the user
    and the secret as the password (as Langfuse's SDKs send it).
    """
    scheme, _, value = header.strip().partition(" ")
    value = value.strip()
    if scheme.lower() == "basic":
        try:
            value = base64.b64decode(value, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return None
    elif scheme.lower() != "bearer":
        return None
    public, sep, secret = value.partition(":")
    if not sep or not public.startswith(PUBLIC_PREFIX) or not secret.startswith(SECRET_PREFIX):
        return None
    if len(public) > 64 or len(secret) > 128:
        return None
    return public, secret


def csrf_token(csrf_secret: str, session_id: str) -> str:
    """The synchronizer token a session's writes must carry: bound to the session's own
    secret and id, so one session's token is worthless for another."""
    return hmac.new(csrf_secret.encode(), session_id.encode(), hashlib.sha256).hexdigest()


__all__ = [
    "PUBLIC_PREFIX",
    "SECRET_PREFIX",
    "SHOWN_PREFIX",
    "csrf_token",
    "digest",
    "mint_key",
    "new_token",
    "parse_key",
    "same",
]
