# SPDX-License-Identifier: Apache-2.0
"""OIDC sign-in: authorization code with PKCE, ``state`` and ``nonce`` (ADR-0050).

- The issuer is the one configured (``OPSEVAL_OIDC_ISSUER``). Its host, and the host of every
  endpoint its discovery document names, must be on ``OPSEVAL_ALLOWED_HOSTS`` and use https
  (loopback aside, for a local test IdP); anything else is refused before any redirect.
- Discovery and the JWKS are fetched over verified TLS (httpx's default), never following a
  redirect. The discovery document's ``issuer``, and every ID token's ``iss``, must equal the
  configured issuer exactly (OIDC Discovery 4.3): ``https://idp/`` and ``https://idp`` differ.
- The ID token is checked: signature against the JWKS (asymmetric algorithms only, never
  ``none`` or an HMAC the client secret could forge), ``iss``, ``aud`` (and ``azp`` when there
  are several audiences), ``exp``, ``iat`` and the ``nonce`` of this sign-in.
- The JWKS is cached for an hour and fetched again at most once a minute for an unknown
  ``kid`` (a key rotation), so a stream of forged ``kid`` values cannot hammer the IdP.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKSet

from opseval.accounts import tokens
from opseval.config import check_url_allowed
from opseval.errors import AllowedHostError, ConfigError

ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "EdDSA")
"""Asymmetric only: an ID token signed with ``none`` or an HMAC is refused."""
JWKS_TTL = 3600.0
JWKS_REFETCH = 60.0
FLOW_TTL = 600.0
"""A sign-in must come back from the IdP within ten minutes."""
LEEWAY = 60
"""Seconds of clock skew allowed on ``exp`` and ``iat``."""


class OidcError(Exception):
    """A sign-in the IdP or its token did not support. The message names the check, never a
    token."""


@dataclass(frozen=True)
class Flow:
    """What one sign-in remembers between the redirect and the callback."""

    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    started: float


@dataclass(frozen=True)
class Claims:
    issuer: str
    subject: str
    email: str | None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class OidcClient:
    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        allowlist: set[str],
        scopes: str = "openid email profile",
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.issuer = issuer.strip()
        """Exactly as configured: discovery's ``issuer`` and every token's ``iss`` must equal
        it character for character (OIDC Discovery 4.3), trailing slash and all."""
        self.client_id = client_id
        self._secret = client_secret
        self.allowlist = allowlist
        self.scopes = scopes
        self._http = http or httpx.Client(timeout=10.0, follow_redirects=False)
        self._clock = clock
        self._discovery: dict[str, Any] | None = None
        self._jwks: PyJWKSet | None = None
        self._jwks_at = 0.0
        self._https(self.issuer, "OPSEVAL_OIDC_ISSUER")

    def _https(self, url: str, what: str) -> None:
        try:
            check_url_allowed(url, self.allowlist, secure_schemes=("https",))
        except AllowedHostError as exc:
            raise ConfigError(f"{what}: {exc}") from exc

    def _get(self, url: str) -> dict[str, Any]:
        try:
            response = self._http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OidcError(f"the identity provider did not answer ({type(exc).__name__})") from exc
        if response.status_code != 200:
            raise OidcError(f"the identity provider answered {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise OidcError("the identity provider's answer is not JSON") from exc
        if not isinstance(body, dict):
            raise OidcError("the identity provider's answer is not a JSON object")
        return body

    def discovery(self) -> dict[str, Any]:
        if self._discovery is None:
            # Discovery 4: a trailing "/" is removed from the issuer before the well-known path
            # is added; the issuer itself is compared exactly.
            base = self.issuer.rstrip("/")
            doc = self._get(f"{base}/.well-known/openid-configuration")
            if doc.get("issuer") != self.issuer:
                raise OidcError("the discovery document names another issuer")
            for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                url = doc.get(name)
                if not isinstance(url, str):
                    raise OidcError(f"the discovery document has no {name}")
                self._https(url, f"the issuer's {name}")
            self._discovery = doc
        return self._discovery

    def _algorithms(self) -> list[str]:
        offered = self.discovery().get("id_token_signing_alg_values_supported") or ["RS256"]
        algorithms = [a for a in offered if a in ALGORITHMS]
        if not algorithms:
            raise OidcError("the issuer signs ID tokens with no algorithm Opseval accepts")
        return algorithms

    def _key(self, kid: str | None) -> Any:
        now = self._clock()
        jwks = self._jwks
        if jwks is None or now - self._jwks_at > JWKS_TTL:
            jwks = self._load_jwks(now)
        found = [k for k in jwks.keys if kid is None or k.key_id == kid]
        if not found and now - self._jwks_at > JWKS_REFETCH:
            jwks = self._load_jwks(now)  # a rotation: fetch once more, at most once a minute
            found = [k for k in jwks.keys if kid is None or k.key_id == kid]
        if len(found) != 1:
            raise OidcError("no single signing key matches the token's kid")
        return found[0]

    def _load_jwks(self, now: float) -> PyJWKSet:
        body = self._get(str(self.discovery()["jwks_uri"]))
        try:
            jwks = PyJWKSet.from_dict(body)
        except jwt.PyJWTError as exc:
            raise OidcError("the issuer's JWKS cannot be read") from exc
        self._jwks, self._jwks_at = jwks, now
        return jwks

    def start(self, redirect_uri: str) -> tuple[str, Flow]:
        """The IdP's URL to send the browser to, and what to remember for the callback."""
        doc = self.discovery()
        flow = Flow(
            state=tokens.new_token(),
            nonce=tokens.new_token(),
            verifier=secrets.token_urlsafe(48),
            redirect_uri=redirect_uri,
            started=self._clock(),
        )
        challenge = _b64(hashlib.sha256(flow.verifier.encode()).digest())
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": self.scopes,
                "state": flow.state,
                "nonce": flow.nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        endpoint = str(doc["authorization_endpoint"])
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}", flow

    def finish(self, flow: Flow, *, code: str, state: str) -> Claims:
        """Exchange the code and check the ID token. Raises :class:`OidcError` naming the
        check that failed."""
        if self._clock() - flow.started > FLOW_TTL:
            raise OidcError("the sign-in took too long; start again")
        if not tokens.same(state, flow.state):
            raise OidcError("the state does not match this sign-in")
        doc = self.discovery()
        auth = (
            httpx.BasicAuth(self.client_id, self._secret)
            if self._secret
            else httpx.USE_CLIENT_DEFAULT
        )
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": flow.redirect_uri,
            "code_verifier": flow.verifier,
            "client_id": self.client_id,
        }
        try:
            response = self._http.post(
                str(doc["token_endpoint"]),
                data=form,
                auth=auth,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OidcError(f"the token endpoint did not answer ({type(exc).__name__})") from exc
        if response.status_code != 200:
            raise OidcError(f"the token endpoint answered {response.status_code}")
        try:
            id_token = response.json().get("id_token")
        except (ValueError, AttributeError) as exc:
            raise OidcError("the token endpoint's answer is not JSON") from exc
        if not isinstance(id_token, str):
            raise OidcError("the token endpoint returned no ID token")
        return self.validate(id_token, nonce=flow.nonce)

    def validate(self, id_token: str, *, nonce: str) -> Claims:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise OidcError("the ID token is malformed") from exc
        algorithms = self._algorithms()
        if header.get("alg") not in algorithms:
            raise OidcError(f"the ID token's algorithm {header.get('alg')!r} is not accepted")
        key = self._key(header.get("kid"))
        try:
            claims = jwt.decode(
                id_token,
                key=key.key,
                algorithms=algorithms,
                audience=self.client_id,
                issuer=self.issuer,
                leeway=LEEWAY,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise OidcError("the ID token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise OidcError("the ID token is for another client (aud)") from exc
        except jwt.InvalidIssuerError as exc:
            raise OidcError("the ID token is from another issuer (iss)") from exc
        except jwt.InvalidSignatureError as exc:
            raise OidcError("the ID token's signature does not verify") from exc
        except jwt.PyJWTError as exc:
            raise OidcError(f"the ID token is invalid ({type(exc).__name__})") from exc
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        if len(audiences) > 1 and claims.get("azp") != self.client_id:
            raise OidcError("the ID token has several audiences and is not authorised for us")
        if not isinstance(claims.get("nonce"), str) or not tokens.same(claims["nonce"], nonce):
            raise OidcError("the ID token's nonce does not match this sign-in")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise OidcError("the ID token has no usable subject")
        email = claims.get("email")
        return Claims(self.issuer, subject, email if isinstance(email, str) else None)


__all__ = ["ALGORITHMS", "Claims", "Flow", "OidcClient", "OidcError"]
