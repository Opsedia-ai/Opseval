# SPDX-License-Identifier: Apache-2.0
"""Passwords: argon2id (argon2-cffi), parameters kept in the hash so they can be raised later
(ADR-0050). A hash made with older parameters is replaced at the next successful sign-in."""

from __future__ import annotations

from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 1024
"""Long enough for any passphrase, short enough that hashing it is not a denial of service."""

_HASHER = PasswordHasher()  # argon2id, RFC 9106 low-memory profile


def check_password(password: str) -> str:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"a password needs at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise ValueError(f"a password has at most {MAX_LENGTH} characters")
    return password


def hash_password(password: str) -> str:
    return _HASHER.hash(check_password(password))


@lru_cache(maxsize=1)
def _dummy() -> str:
    return _HASHER.hash("opseval-no-such-user-dummy")


def verify_password(stored: str | None, password: str) -> bool:
    """Whether ``password`` matches. With no stored hash (no such user, an OIDC-only user) a
    dummy hash is still checked, so the answer takes as long either way."""
    if len(password) > MAX_LENGTH:
        return False
    try:
        return bool(_HASHER.verify(stored or _dummy(), password)) and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    try:
        return _HASHER.check_needs_rehash(stored)
    except InvalidHashError:
        return True


__all__ = [
    "MAX_LENGTH",
    "MIN_LENGTH",
    "check_password",
    "hash_password",
    "needs_rehash",
    "verify_password",
]
