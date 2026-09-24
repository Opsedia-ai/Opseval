# SPDX-License-Identifier: Apache-2.0
"""Credentials for the system under test: a static token or short-lived HS256 tokens.

HS256 tokens are signed with ``OPSEVAL_TOKEN_SIGNING_KEY`` (standard JWT, no extra
dependency), minted per attempt and refreshed ``refresh_before_seconds`` before they
expire. Tokens never appear in run records: frames are redacted and transport events
only say that a refresh happened.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator
from typing import Any

import httpx


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_hs256(key: str, claims: dict[str, Any]) -> str:
    """A compact JWS (JWT) signed with HMAC-SHA256."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(key.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


class TokenSource(ABC):
    """Where a transport gets its bearer token."""

    @abstractmethod
    def current(self) -> str:
        """A token that is valid now (refreshed first if it is due)."""

    def seconds_until_refresh(self) -> float | None:
        """Seconds until the token should be refreshed; ``None`` if it never expires."""
        return None

    def refresh_due(self) -> bool:
        due = self.seconds_until_refresh()
        return due is not None and due <= 0


class StaticToken(TokenSource):
    def __init__(self, token: str) -> None:
        self._token = token

    def current(self) -> str:
        return self._token


class HS256Tokens(TokenSource):
    """Short-lived HS256 tokens, re-minted before they expire."""

    def __init__(
        self,
        key: str,
        *,
        ttl_seconds: int = 300,
        refresh_before_seconds: int = 30,
        claims: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not key:
            raise ValueError("an HS256 signing key is required")
        self._key = key
        self._ttl = ttl_seconds
        self._before = min(refresh_before_seconds, max(ttl_seconds - 1, 0))
        self._claims = dict(claims or {})
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self.minted = 0

    def _mint(self) -> str:
        now = int(self._clock())
        self._expires_at = now + self._ttl
        self.minted += 1
        claims = {
            "sub": "opseval",
            **self._claims,
            "iat": now,
            "exp": now + self._ttl,
            "jti": uuid.uuid4().hex,
        }
        self._token = mint_hs256(self._key, claims)
        return self._token

    def seconds_until_refresh(self) -> float:
        if self._token is None:
            return 0.0
        return self._expires_at - self._before - self._clock()

    def current(self) -> str:
        if self._token is None or self.refresh_due():
            return self._mint()
        return self._token


class BearerAuth(httpx.Auth):
    """Sets ``Authorization: Bearer`` on every request, refreshing the token when due."""

    def __init__(self, tokens: TokenSource) -> None:
        self._tokens = tokens

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._tokens.current()}"
        yield request


def as_token_source(token: str | TokenSource | None) -> TokenSource | None:
    if token is None or isinstance(token, TokenSource):
        return token
    return StaticToken(token)


__all__ = [
    "BearerAuth",
    "HS256Tokens",
    "StaticToken",
    "TokenSource",
    "as_token_source",
    "mint_hs256",
]
