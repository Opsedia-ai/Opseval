# SPDX-License-Identifier: Apache-2.0
"""Credentials for the system under test: a static token, short-lived HS256 tokens, or a
credential provider plugin (``opseval.credentials``, ADR-0046).

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
from dataclasses import dataclass
from typing import Any

import httpx

from opseval.errors import CredentialsError
from opseval.models.profile import SutProfile, TokenConfig


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


@dataclass(frozen=True)
class CredentialContext:
    """What a credential provider is told about the attempt it is supplying."""

    profile: str
    """The SUT profile's name."""
    target: str
    """Where the attempt connects."""
    tenant: str | None
    """The test tenant's id when the run uses a pool (ADR-0041), else ``None``. A provider
    used with a pool must return a different identity per tenant, or lanes share state."""
    value: Callable[[str], str | None]
    """Reads a named variable from the environment or the ``.env`` file Opseval was started
    with. Use it for the provider's own settings and secrets, rather than ``os.environ``,
    which never sees ``.env``."""


class CredentialProvider(ABC):
    """A plugin that supplies the agent's bearer tokens (``opseval.credentials``, ADR-0046).

    Declared in the plugin's own metadata::

        [project.entry-points."opseval.credentials"]
        acme_sso = "acme_opseval.auth:AcmeSso"

    and used only when named in ``OPSEVAL_CREDENTIALS``. ``token_source`` is called once per
    attempt; the source's ``current()`` is called whenever a token is needed, on the event
    loop, so it must return quickly (cache it: see :class:`FetchedTokens`). Whatever either
    raises is a harness error, never the agent's.

    HTTP transports ask for a token on every request. On WebSocket the token is sent when a
    connection opens; to replace it on an open connection, the profile declares a ``token:``
    block with only a ``refresh_frame``, sent when ``seconds_until_refresh`` runs out.
    """

    name: str = "credentials"

    @abstractmethod
    def token_source(self, context: CredentialContext) -> TokenSource:
        """A token source for one attempt."""


class FetchedTokens(TokenSource):
    """A helper for providers that fetch tokens from somewhere: implement :meth:`fetch` and
    the token is cached, and fetched again ``refresh_before_seconds`` before it expires
    (at most half its lifetime early, so a short-lived token is not fetched on every use)."""

    def __init__(
        self, *, refresh_before_seconds: float = 30.0, clock: Callable[[], float] = time.time
    ) -> None:
        self._before = refresh_before_seconds
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float | None = None
        self._early = refresh_before_seconds

    @abstractmethod
    def fetch(self) -> tuple[str, float | None]:
        """A fresh token and its lifetime in seconds (``None``: it does not expire)."""

    def seconds_until_refresh(self) -> float | None:
        if self._token is None:
            return 0.0
        if self._expires_at is None:
            return None
        return self._expires_at - self._early - self._clock()

    def current(self) -> str:
        if self._token is None or self.refresh_due():
            token, ttl = self.fetch()
            self._token = token
            self._expires_at = None if ttl is None else self._clock() + ttl
            self._early = self._before if ttl is None else min(self._before, ttl / 2)
        return self._token


class GuardedTokens(TokenSource):
    """A plugin's token source, held to the core rule.

    Anything the provider raises becomes a :class:`CredentialsError`, a harness error. Left
    alone, a provider's own ``TimeoutError`` (its token endpoint was slow) would reach a
    transport that reads ``TimeoutError`` as *the agent* not answering, and be scored as an
    agent failure. The message names the provider and the exception type only: a provider's
    error text can quote the credential it was handling.

    The provider's source is built on first use, so a failure lands inside the attempt (not
    judged) rather than stopping the run between attempts.
    """

    def __init__(self, provider: CredentialProvider, context: CredentialContext) -> None:
        self._provider = provider
        self._context = context
        self._inner: TokenSource | None = None

    def _fail(self, what: str, exc: BaseException | None = None) -> CredentialsError:
        cause = f": {type(exc).__name__}" if exc is not None else ""
        return CredentialsError(f"credential provider {self._provider.name!r} {what}{cause}")

    def _source(self) -> TokenSource:
        if self._inner is None:
            try:
                inner = self._provider.token_source(self._context)
            except Exception as exc:  # noqa: BLE001 - any plugin failure is a harness error
                raise self._fail("could not start", exc) from None
            if not isinstance(inner, TokenSource):
                raise self._fail(f"returned {type(inner).__name__}, not a TokenSource")
            self._inner = inner
        return self._inner

    def current(self) -> str:
        source = self._source()
        try:
            token = source.current()
        except Exception as exc:  # noqa: BLE001 - any plugin failure is a harness error
            raise self._fail("failed to supply a token", exc) from None
        if not isinstance(token, str) or not token.strip():
            raise self._fail("supplied an empty token")
        return token

    def seconds_until_refresh(self) -> float | None:
        if self._inner is None:
            return 0.0
        try:
            return self._inner.seconds_until_refresh()
        except Exception as exc:  # noqa: BLE001 - any plugin failure is a harness error
            raise self._fail("failed to say when to refresh", exc) from None


def token_block_problem(profile: SutProfile, provider: str) -> str | None:
    """Why the profile's ``token:`` block cannot be used with a credential provider, if it
    cannot. With a provider, the block may declare only ``refresh_frame`` (how a new token is
    sent on an open WebSocket connection): the token and its lifetime are the provider's, so
    a TTL or claims there would be silently ignored."""
    block = profile.token
    if block is None:
        return None
    if block == TokenConfig(refresh_frame=block.refresh_frame):
        return None
    return (
        f"profile {profile.name!r} has a token: block that mints HS256 tokens (ttl, claims or "
        f"refresh_before_seconds), and OPSEVAL_CREDENTIALS names {provider!r}: with a provider, "
        "the token: block may only declare refresh_frame. Choose one, so it is clear which "
        "identity the agent sees"
    )


def as_token_source(token: str | TokenSource | None) -> TokenSource | None:
    if token is None or isinstance(token, TokenSource):
        return token
    return StaticToken(token)


__all__ = [
    "BearerAuth",
    "CredentialContext",
    "CredentialProvider",
    "FetchedTokens",
    "GuardedTokens",
    "HS256Tokens",
    "StaticToken",
    "TokenSource",
    "as_token_source",
    "token_block_problem",
    "mint_hs256",
]
