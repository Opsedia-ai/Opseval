# SPDX-License-Identifier: Apache-2.0
"""Accounts and access (ADR-0050), the parts that need no database: roles and the one
authorization decision, tokens, passwords, rate limiting, OIDC against a fake identity
provider, the bind rule, forwarded headers, and the route table.

The database half (sessions, keys, roles on real rows, audit events) is test_accounts_db.py.
"""

from __future__ import annotations

import base64
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from typer.testing import CliRunner

from opseval.accounts import tokens
from opseval.accounts.oidc import OidcClient, OidcError
from opseval.accounts.passwords import hash_password, needs_rehash, verify_password
from opseval.accounts.ratelimit import ACCOUNT_FREE, MAX_WAIT, LoginLimiter
from opseval.accounts.roles import (
    MEMBER,
    OPERATOR,
    READER,
    SYSTEM_MAX,
    Need,
    Principal,
    decide,
    effective_role,
    grantable,
    key_scopes,
)
from opseval.accounts.service import Accounts, Served
from opseval.config import Settings
from opseval.demo import catalogue_dir
from opseval.errors import ConfigError
from opseval.server import create_app
from opseval.server.access import AccessConfig, PublicUrl, check_bind, parse_proxies
from opseval.server.app import API, ROUTE_NEEDS, check_policies

# --- roles and the one decision ------------------------------------------------------------


class TestRoles:
    @pytest.mark.parametrize(
        ("org", "project", "want"),
        [
            ("owner", None, "owner"),
            ("admin", "viewer", "viewer"),
            ("member", "admin", "member"),  # a raise is ignored whatever the row says
            ("viewer", "owner", "viewer"),
            (None, "admin", None),  # no org role is no role at all
        ],
    )
    def test_a_project_role_only_lowers(
        self, org: str | None, project: str | None, want: str | None
    ) -> None:
        assert effective_role(org, project) == want

    def test_a_key_never_exceeds_its_creators_role_at_use(self) -> None:
        granted = ["runs:read", "runs:start", "label:protected"]
        assert key_scopes(granted, "admin", system=False) == frozenset(granted)
        assert key_scopes(granted, "member", system=False) == {"runs:read", "runs:start"}
        assert key_scopes(granted, "viewer", system=False) == {"runs:read"}
        assert key_scopes(granted, None, system=False) == frozenset()  # creator gone

    def test_a_system_key_whose_creator_is_gone_holds_nothing(self) -> None:
        granted = ["runs:read", "runs:start"]
        assert key_scopes(granted, None, system=True) == frozenset()
        assert key_scopes(granted, None, system=True, creator_kind="user") == frozenset()
        # Only a key the CLI minted with nobody behind it has the system cap.
        assert key_scopes(granted, None, system=True, creator_kind="cli") == frozenset(granted)
        assert key_scopes(["catalogue:write"], "viewer", system=True) == frozenset()
        assert key_scopes(granted, None, system=False, creator_kind="cli") == frozenset()

    def test_a_system_key_never_holds_label_protected(self) -> None:
        assert "label:protected" not in SYSTEM_MAX
        assert key_scopes(["label:protected"], "owner", system=True) == frozenset()
        assert "label:protected" not in grantable("owner", system=True)
        assert "label:protected" in grantable("admin", system=False)
        assert "label:protected" not in grantable("member", system=False)

    def test_no_key_ever_manages(self) -> None:
        assert key_scopes(["manage"], "owner", system=False) == frozenset()
        assert "manage" not in grantable("owner", system=False)

    def test_deny_by_default(self) -> None:
        viewer = Principal(kind="user", id="u", name="v", role="viewer", scopes=READER)
        member = Principal(kind="user", id="u", name="m", role="member", scopes=MEMBER)
        hidden = Principal(kind="key", id="k", name="k", scopes=MEMBER, visible=False)
        assert decide(None, Need.PUBLIC) is None
        assert decide(None, Need.READ).status == 401  # type: ignore[union-attr]
        assert decide(viewer, Need.READ) is None
        assert decide(viewer, Need.START).status == 403  # type: ignore[union-attr]
        assert decide(viewer, Need.CATALOGUE).status == 403  # type: ignore[union-attr]
        assert decide(member, Need.START) is None
        assert decide(member, Need.MANAGE).status == 403  # type: ignore[union-attr]
        assert decide(hidden, Need.READ).status == 404  # type: ignore[union-attr]
        assert decide(hidden, Need.SIGNED_IN) is None
        assert decide(OPERATOR, Need.MANAGE) is None
        assert hidden.capabilities() == []
        assert member.capabilities() == [
            "runs:read",
            "runs:start",
            "catalogue:write",
            "catalogue:label",
        ]


# --- tokens and passwords -------------------------------------------------------------------


class TestTokens:
    def test_both_key_forms_parse(self) -> None:
        public, secret = tokens.mint_key()
        assert tokens.parse_key(f"Bearer {public}:{secret}") == (public, secret)
        basic = base64.b64encode(f"{public}:{secret}".encode()).decode()
        assert tokens.parse_key(f"Basic {basic}") == (public, secret)

    @pytest.mark.parametrize(
        "header",
        [
            "Bearer test-token",  # the shared token is not a key
            "Bearer pk-oe-x",
            "Bearer sk-oe-x:pk-oe-y",
            "Basic !!!",
            "Token pk-oe-a:sk-oe-b",
            "Bearer pk-oe-" + "a" * 80 + ":sk-oe-b",
        ],
    )
    def test_anything_else_is_not_a_key(self, header: str) -> None:
        assert tokens.parse_key(header) is None

    def test_a_csrf_token_is_bound_to_its_session(self) -> None:
        assert tokens.csrf_token("s", "a") != tokens.csrf_token("s", "b")
        assert tokens.csrf_token("s", "a") == tokens.csrf_token("s", "a")


class TestPasswords:
    def test_argon2id_with_its_parameters_in_the_hash(self) -> None:
        stored = hash_password("correct horse battery")
        assert stored.startswith("$argon2id$v=19$m=")
        assert verify_password(stored, "correct horse battery")
        assert not verify_password(stored, "wrong horse battery")
        assert not needs_rehash(stored)
        assert needs_rehash("not-a-hash")

    def test_no_user_is_checked_against_a_dummy_and_fails(self) -> None:
        assert not verify_password(None, "opseval-no-such-user-dummy")
        assert not verify_password("garbage", "x")

    def test_short_passwords_are_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 12"):
            hash_password("short")


class TestRateLimit:
    def test_failures_past_the_allowance_back_off(self) -> None:
        now = [1000.0]
        limiter = LoginLimiter(clock=lambda: now[0])
        for _ in range(ACCOUNT_FREE):
            assert limiter.begin("1.2.3.4", "a@x") == 0  # reserved, counted as a failure
        assert limiter.begin("1.2.3.4", "a@x") == pytest.approx(1.0)  # the allowance is spent
        now[0] += 1.0
        assert limiter.begin("1.2.3.4", "a@x") == 0
        assert limiter.wait("1.2.3.4", "a@x") == pytest.approx(2.0)
        assert limiter.wait("5.6.7.8", "a@x") == pytest.approx(2.0)  # per account too
        assert limiter.wait("1.2.3.4", "b@x") == 0  # the address is below its own allowance
        for _ in range(30):
            now[0] += MAX_WAIT
            limiter.begin("1.2.3.4", "a@x")
        assert limiter.wait("1.2.3.4", "a@x") == pytest.approx(MAX_WAIT)
        now[0] += MAX_WAIT + 1
        assert limiter.wait("9.9.9.9", "a@x") == 0

    def test_a_success_gives_the_attempt_back(self) -> None:
        limiter = LoginLimiter(clock=lambda: 0.0)
        for _ in range(ACCOUNT_FREE * 3):
            assert limiter.begin("1.2.3.4", "a@x") == 0
            limiter.succeeded("1.2.3.4", "a@x")

    def test_a_success_clears_the_account_not_the_address(self) -> None:
        from opseval.accounts.ratelimit import ADDRESS_FREE

        limiter = LoginLimiter(clock=lambda: 0.0)
        assert limiter.begin("1.2.3.4", "user0@x") == 0
        for i in range(1, ADDRESS_FREE):
            assert limiter.begin("1.2.3.4", f"user{i}@x") == 0
        assert limiter.wait("1.2.3.4", "anyone@x") > 0
        limiter.succeeded("1.2.3.4", "user0@x")  # only its own attempt comes back
        assert limiter.wait("1.2.3.4", "user0@x") == 0
        assert limiter.begin("1.2.3.4", "user99@x") == 0
        assert limiter.wait("1.2.3.4", "user0@x") > 0  # the address keeps its other failures

    def test_a_parallel_burst_never_exceeds_the_allowance(self) -> None:
        """Check and count are one step: 50 threads at once, at most the allowance passes."""
        import threading

        limiter = LoginLimiter()
        barrier = threading.Barrier(50)
        passed: list[int] = []

        def attempt() -> None:
            barrier.wait()
            if limiter.begin("1.2.3.4", "a@x") == 0:
                passed.append(1)

        threads = [threading.Thread(target=attempt) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(passed) == ACCOUNT_FREE


# --- OIDC against a fake identity provider ----------------------------------------------------

ISSUER = "https://idp.example.com"
CLIENT = "opseval-console"


class FakeIdp:
    """Discovery, JWKS and a token endpoint, over httpx.MockTransport."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.codes: dict[str, str] = {}
        self.issuer = ISSUER
        self.issuer_in_discovery = ISSUER
        self.token_requests: list[dict[str, list[str]]] = []

    def jwks(self) -> dict[str, Any]:
        jwk = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        return {"keys": [jwk]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer_in_discovery,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json=self.jwks())
        if path == "/token":
            form = parse_qs(request.content.decode())
            self.token_requests.append(form)
            code = form["code"][0]
            if code not in self.codes:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.codes.pop(code)})
        return httpx.Response(404)

    def token(self, nonce: str, *, key: Any = None, alg: str = "RS256", **claims: Any) -> str:
        now = int(time.time())
        body = {
            "iss": self.issuer,
            "aud": CLIENT,
            "sub": "user-123",
            "email": "ada@example.com",
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
        }
        body.update(claims)
        body = {k: v for k, v in body.items() if v is not None}
        signer = key or self.key
        if alg.startswith("HS"):
            signer = "shared-secret-guessable-by-a-client"
        return jwt.encode(body, signer, algorithm=alg, headers={"kid": self.kid})

    def issue(self, nonce: str, **kw: Any) -> str:
        code = tokens.new_token()
        self.codes[code] = self.token(nonce, **kw)
        return code

    def client(self, issuer: str = ISSUER, **kw: Any) -> OidcClient:
        return OidcClient(
            issuer=issuer,
            client_id=CLIENT,
            client_secret="s3cret",
            allowlist={"idp.example.com"},
            http=httpx.Client(transport=httpx.MockTransport(self.handler)),
            **kw,
        )


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


def _started(client: OidcClient) -> tuple[dict[str, str], Any]:
    url, flow = client.start("https://opseval.example.com/api/v1/auth/oidc/callback")
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    return query, flow


class TestOidc:
    def test_the_happy_path_with_pkce_state_and_nonce(self, idp: FakeIdp) -> None:
        client = idp.client()
        query, flow = _started(client)
        assert query["code_challenge_method"] == "S256" and query["response_type"] == "code"
        assert query["state"] == flow.state and query["nonce"] == flow.nonce
        code = idp.issue(flow.nonce)
        claims = client.finish(flow, code=code, state=flow.state)
        assert (claims.issuer, claims.subject, claims.email) == (
            ISSUER,
            "user-123",
            "ada@example.com",
        )
        sent = idp.token_requests[-1]
        assert sent["code_verifier"] == [flow.verifier]

    def test_a_state_that_does_not_match_is_refused(self, idp: FakeIdp) -> None:
        client = idp.client()
        _, flow = _started(client)
        with pytest.raises(OidcError, match="state"):
            client.finish(flow, code=idp.issue(flow.nonce), state="forged")

    @pytest.mark.parametrize(
        ("claims", "message"),
        [
            ({"nonce": "replayed"}, "nonce"),
            ({"aud": "someone-else"}, "another client"),
            ({"iss": "https://evil.example.com"}, "another issuer"),
            ({"exp": int(time.time()) - 3600}, "expired"),
            ({"aud": [CLIENT, "other"]}, "several audiences"),
            ({"sub": None}, "invalid"),
        ],
    )
    def test_a_bad_id_token_is_refused(
        self, idp: FakeIdp, claims: dict[str, Any], message: str
    ) -> None:
        client = idp.client()
        _, flow = _started(client)
        nonce = claims.pop("nonce", flow.nonce)
        with pytest.raises(OidcError, match=message):
            client.finish(flow, code=idp.issue(nonce, **claims), state=flow.state)

    def test_a_token_signed_by_another_key_is_refused(self, idp: FakeIdp) -> None:
        client = idp.client()
        _, flow = _started(client)
        code = idp.issue(flow.nonce, key=idp.other)
        with pytest.raises(OidcError, match="signature"):
            client.finish(flow, code=code, state=flow.state)

    @pytest.mark.parametrize("alg", ["HS256", "none"])
    def test_symmetric_or_unsigned_tokens_are_refused(self, idp: FakeIdp, alg: str) -> None:
        client = idp.client()
        _, flow = _started(client)
        if alg == "none":
            token = jwt.encode({"sub": "x", "nonce": flow.nonce}, None, algorithm="none")
        else:
            token = idp.token(flow.nonce, alg=alg)
        with pytest.raises(OidcError, match="not accepted"):
            client.validate(token, nonce=flow.nonce)

    def test_a_slow_sign_in_expires(self, idp: FakeIdp) -> None:
        now = [time.time()]
        client = idp.client(clock=lambda: now[0])
        _, flow = _started(client)
        now[0] += 3600
        with pytest.raises(OidcError, match="too long"):
            client.finish(flow, code=idp.issue(flow.nonce), state=flow.state)

    def test_discovery_must_name_the_configured_issuer(self, idp: FakeIdp) -> None:
        idp.issuer_in_discovery = "https://evil.example.com"
        with pytest.raises(OidcError, match="another issuer"):
            idp.client().start("https://x/cb")

    def test_a_trailing_slash_issuer_is_compared_exactly(self, idp: FakeIdp) -> None:
        """OIDC Discovery 4.3: the issuer is compared exactly, slash and all."""
        idp.issuer = idp.issuer_in_discovery = ISSUER + "/"
        client = idp.client(issuer=ISSUER + "/")  # configured as the IdP names itself
        _, flow = _started(client)
        assert client.finish(flow, code=idp.issue(flow.nonce), state=flow.state).issuer == (
            ISSUER + "/"
        )
        with pytest.raises(OidcError, match="another issuer"):
            idp.client(issuer=ISSUER).start("https://x/cb")  # the slash left off

    def test_a_token_whose_iss_differs_by_a_slash_is_refused(self, idp: FakeIdp) -> None:
        client = idp.client()
        _, flow = _started(client)
        code = idp.issue(flow.nonce, iss=ISSUER + "/")
        with pytest.raises(OidcError, match="another issuer"):
            client.finish(flow, code=code, state=flow.state)

    def test_an_issuer_off_the_allowlist_is_refused_before_any_redirect(self) -> None:
        with pytest.raises(ConfigError, match="OPSEVAL_ALLOWED_HOSTS"):
            OidcClient(issuer=ISSUER, client_id=CLIENT, client_secret=None, allowlist=set())
        with pytest.raises(ConfigError, match="https"):
            OidcClient(
                issuer="http://idp.example.com",
                client_id=CLIENT,
                client_secret=None,
                allowlist={"idp.example.com"},
            )

    def test_a_key_rotation_is_fetched_again(self, idp: FakeIdp) -> None:
        now = [time.time()]
        client = idp.client(clock=lambda: now[0])
        _, flow = _started(client)
        client.finish(flow, code=idp.issue(flow.nonce), state=flow.state)
        idp.key, idp.kid = idp.other, "k2"
        now[0] += 120
        _, flow = _started(client)
        assert client.finish(flow, code=idp.issue(flow.nonce), state=flow.state).subject


# --- serving: the bind rule, the public URL, forwarded headers ---------------------------------


class TestBindRule:
    def test_token_mode_is_loopback_only(self) -> None:
        check_bind("127.0.0.1", accounts=False, public_url=None, tls=False, proxies=[])
        with pytest.raises(ConfigError, match="loopback only"):
            check_bind("0.0.0.0", accounts=False, public_url=None, tls=False, proxies=[])  # nosec B104

    def test_a_public_bind_needs_https_and_tls_or_a_proxy(self) -> None:
        url = PublicUrl.parse("https://opseval.example.com")
        with pytest.raises(ConfigError, match="public-url"):
            check_bind("0.0.0.0", accounts=True, public_url=None, tls=False, proxies=[])  # nosec B104
        with pytest.raises(ConfigError, match="tls-cert"):
            check_bind("0.0.0.0", accounts=True, public_url=url, tls=False, proxies=[])  # nosec B104
        check_bind("0.0.0.0", accounts=True, public_url=url, tls=True, proxies=[])  # nosec B104
        check_bind(
            "0.0.0.0",  # nosec B104
            accounts=True,
            public_url=url,
            tls=False,
            proxies=parse_proxies("10.0.0.0/8"),
        )

    @pytest.mark.parametrize(
        "url",
        ["http://opseval.example.com", "https://x.example.com/console", "ftp://x", "https://"],
    )
    def test_a_public_url_is_https_and_bare(self, url: str) -> None:
        with pytest.raises(ConfigError):
            PublicUrl.parse(url)

    def test_plain_http_is_allowed_only_on_loopback(self) -> None:
        assert PublicUrl.parse("http://localhost:8000").origin == "http://localhost:8000"
        assert PublicUrl.parse("https://[::1]:8443").origin == "https://[::1]:8443"

    def test_a_bad_proxy_is_named(self) -> None:
        with pytest.raises(ConfigError, match="not-an-ip"):
            parse_proxies("10.0.0.1, not-an-ip")


class _NoDatabase:
    """Accounts never reach a database for an anonymous request without a cookie."""

    def run_in_session(self, fn: Any, *, write: bool = False) -> Any:
        raise AssertionError("no database call expected")


def _served() -> Served:
    return Served(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "acme/bot", "default")


def _accounts_app(tmp_path: Path, **access: Any) -> Any:
    accounts = Accounts(
        _NoDatabase(), _served(), idle=timedelta(minutes=5), lifetime=timedelta(hours=1)
    )
    cfg = AccessConfig(accounts=accounts, **access)
    cat = catalogue_dir()
    return create_app(
        settings=Settings(_env_file=None, reports_dir=str(tmp_path / "r")),
        catalogue=cat,
        profile=cat / "profile.yaml",
        read_only=True,
        access=cfg,
        console_dir=None,
    )


SESSION = f"{API}/auth/session"


class TestForwardedHeaders:
    def proxied(self, tmp_path: Path, peer: str = "10.0.0.5") -> TestClient:
        app = _accounts_app(
            tmp_path,
            public_url=PublicUrl.parse("https://opseval.example.com"),
            trusted_proxies=parse_proxies("10.0.0.0/8"),
        )
        return TestClient(app, base_url="http://opseval.example.com", client=(peer, 1234))

    def test_a_trusted_proxy_saying_https_and_the_public_host_is_believed(
        self, tmp_path: Path
    ) -> None:
        c = self.proxied(tmp_path)
        r = c.get(
            SESSION,
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "opseval.example.com",
                "X-Forwarded-For": "1.1.1.1, 203.0.113.9",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "accounts" and r.json()["user"] is None
        assert "max-age=" in r.headers["strict-transport-security"]
        assert r.headers["cache-control"] == "no-store"

    def test_the_rfc_7239_header_works_too(self, tmp_path: Path) -> None:
        c = self.proxied(tmp_path)
        r = c.get(
            SESSION, headers={"Forwarded": 'for=1.2.3.4;proto=https;host="opseval.example.com"'}
        )
        assert r.status_code == 200

    def test_forwarded_headers_from_anyone_else_are_ignored(self, tmp_path: Path) -> None:
        c = self.proxied(tmp_path, peer="203.0.113.7")
        r = c.get(
            SESSION,
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "opseval.example.com"},
        )
        assert r.status_code == 403 and r.json()["code"] == "HOST_NOT_ALLOWED"

    @pytest.mark.parametrize(
        "headers",
        [
            {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "attacker.example"},
            {"X-Forwarded-Proto": "http", "X-Forwarded-Host": "opseval.example.com"},
            {"X-Forwarded-Host": "opseval.example.com"},
        ],
    )
    def test_a_trusted_proxy_must_say_https_and_the_public_host(
        self, tmp_path: Path, headers: dict[str, str]
    ) -> None:
        r = self.proxied(tmp_path).get(SESSION, headers=headers)
        assert r.status_code == 403 and r.json()["code"] == "HOST_NOT_ALLOWED"

    def test_with_tls_the_host_must_be_the_public_one(self, tmp_path: Path) -> None:
        app = _accounts_app(tmp_path, public_url=PublicUrl.parse("https://opseval.example.com"))
        ok = TestClient(app, base_url="https://opseval.example.com").get(SESSION)
        assert ok.status_code == 200
        evil = TestClient(app, base_url="https://attacker.example").get(SESSION)
        assert evil.status_code == 403
        plain = TestClient(app, base_url="http://opseval.example.com").get(SESSION)
        assert plain.status_code == 403  # never plain http
        page = TestClient(app, base_url="https://opseval.example.com").get("/nothing")
        assert "max-age=" in page.headers["strict-transport-security"]


class TestClientAddress:
    """The client is the first hop, from the right, that is not one of our proxies."""

    proxies = parse_proxies("10.0.0.0/8, 192.168.1.1, 2001:db8:ffff::/48")

    @pytest.mark.parametrize(
        ("hops", "want"),
        [
            (["203.0.113.9"], "203.0.113.9"),
            # The client wrote the leftmost entry itself: it proves nothing.
            (["6.6.6.6", "203.0.113.9", "10.0.0.7"], "203.0.113.9"),
            # Two trusted proxies in a row are both skipped.
            (["203.0.113.9", "192.168.1.1", "10.0.0.7"], "203.0.113.9"),
            # Ports, brackets and quotes are stripped.
            (['"[2001:db8::1]:443"'], "2001:db8::1"),
            (["1.2.3.4:5", "10.0.0.7:8080"], "1.2.3.4"),
            (["2001:db8::2"], "2001:db8::2"),
            # An unreadable hop: never guess, the peer it is.
            (["unknown", "10.0.0.7"], "10.0.0.5"),
            (["_hidden"], "10.0.0.5"),
            # Every hop ours: the leftmost.
            (["10.0.0.9", "10.0.0.7"], "10.0.0.9"),
            ([], "10.0.0.5"),
        ],
    )
    def test_the_walk(self, hops: list[str], want: str) -> None:
        from opseval.server.access import _client

        assert _client("10.0.0.5", hops, self.proxies) == want

    def test_forwarded_lists_every_for(self) -> None:
        from opseval.server.access import _forwarded_for

        header = 'for=1.2.3.4;proto=https, for="[2001:db8::1]:443";host=x, by=10.0.0.1'
        assert _forwarded_for(header) == ["1.2.3.4", '"[2001:db8::1]:443"']


class TestOidcFlowsInFlight:
    def accounts(self) -> Accounts:
        from .test_accounts import FakeIdp as Idp

        acc = Accounts(
            _NoDatabase(),
            _served(),
            idle=timedelta(minutes=5),
            lifetime=timedelta(hours=1),
            oidc=Idp().client(),
        )
        return acc

    def test_a_flow_expires(self) -> None:
        from opseval.accounts.service import FLOW_TTL, LoginRefused

        acc = self.accounts()
        now = [0.0]
        acc.clock = lambda: now[0]
        _, handle = acc.oidc_start("https://x/cb", address="1.1.1.1")
        now[0] += FLOW_TTL + 1
        acc.oidc_start("https://x/cb", address="2.2.2.2")  # prunes on the way
        assert len(acc._flows) == 1
        with pytest.raises(LoginRefused) as refused:
            acc.oidc_finish(handle, code="c", state="s", address="1.1.1.1")
        assert refused.value.code == "OIDC_NO_FLOW"

    def test_one_address_cannot_flood_the_flows(self) -> None:
        from opseval.accounts.service import FLOW_STARTS, FLOW_WINDOW, LoginRefused

        acc = self.accounts()
        now = [0.0]
        acc.clock = lambda: now[0]
        _, real = acc.oidc_start("https://x/cb", address="5.5.5.5")
        for _ in range(FLOW_STARTS):
            acc.oidc_start("https://x/cb", address="6.6.6.6")
        with pytest.raises(LoginRefused) as refused:
            acc.oidc_start("https://x/cb", address="6.6.6.6")
        assert refused.value.status == 429 and refused.value.retry_after
        assert tokens.digest(real) in acc._flows  # the real sign-in is still there
        now[0] += FLOW_WINDOW
        acc.oidc_start("https://x/cb", address="6.6.6.6")  # a minute later it may start again


class TestCookieNames:
    def test_secure_host_cookie_only_behind_https(self) -> None:
        acc = Accounts(_NoDatabase(), _served(), idle=timedelta(1), lifetime=timedelta(1))
        loopback = AccessConfig(accounts=acc)
        assert (loopback.session_cookie, loopback.flow_cookie) == (
            "opseval-session",
            "opseval-oidc",
        )
        local = AccessConfig(accounts=acc, public_url=PublicUrl.parse("http://localhost:8000"))
        assert local.session_cookie == "opseval-session" and not local.secure
        public = AccessConfig(accounts=acc, public_url=PublicUrl.parse("https://o.example.com"))
        assert public.session_cookie == "__Host-opseval-session" and public.secure
        assert public.flow_cookie == "__Host-opseval-oidc"


class TestAccountsModeWithoutAnyone:
    def test_every_route_but_the_sign_in_ones_needs_someone(self, tmp_path: Path) -> None:
        """No route bypasses authorization: anonymous gets 401 on all but the public ones."""
        app = _accounts_app(tmp_path)
        c = TestClient(app, base_url="https://127.0.0.1")
        checked = 0
        for (method, path), need in ROUTE_NEEDS.items():
            if need is Need.PUBLIC:
                continue
            url = API + path.replace("{index}", "0").replace("{seq}", "0")
            for name in ("run_id", "test_id", "scenario_id", "public_key"):
                url = url.replace("{" + name + "}", "x")
            r = c.request(method, url, headers={"Origin": "https://127.0.0.1"}, json={})
            assert r.status_code == 401, (method, path, r.status_code)
            checked += 1
        assert checked > 40

    def test_the_shared_token_does_not_work_in_accounts_mode(self, tmp_path: Path) -> None:
        app = _accounts_app(tmp_path)
        assert app.state.token is None
        c = TestClient(app, base_url="https://127.0.0.1", headers={"Authorization": "Bearer x"})
        r = c.get(f"{API}/runs")
        assert r.status_code == 401 and r.json()["code"] == "INVALID_KEY"

    def test_an_anonymous_write_needs_the_same_origin(self, tmp_path: Path) -> None:
        c = TestClient(_accounts_app(tmp_path), base_url="https://127.0.0.1")
        r = c.post(f"{API}/auth/login", json={"email": "a@b.c", "password": "x"})
        assert r.status_code == 403 and r.json()["code"] == "ORIGIN_NOT_ALLOWED"
        r = c.post(
            f"{API}/auth/login",
            json={"email": "a@b.c", "password": "x"},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403

    def test_on_loopback_the_host_check_holds(self, tmp_path: Path) -> None:
        app = _accounts_app(tmp_path)
        assert TestClient(app, base_url="https://attacker.example").get(SESSION).status_code == 403


class TestRouteTable:
    def test_a_route_without_a_policy_fails_the_start(self, tmp_path: Path) -> None:
        cat = catalogue_dir()
        app = create_app(
            settings=Settings(_env_file=None, reports_dir=str(tmp_path)),
            catalogue=cat,
            profile=cat / "profile.yaml",
            token="t",
        )

        @app.get(f"{API}/unguarded")
        def unguarded() -> dict[str, str]:
            return {"secret": "x"}

        with pytest.raises(RuntimeError, match="unguarded"):
            check_policies(app)
        # And should one slip through anyway, the request is refused, not answered.
        r = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer t"})
        got = r.get(f"{API}/unguarded")
        assert got.status_code == 403 and got.json()["code"] == "NO_POLICY"

    def test_token_mode_answers_the_session_as_the_operator(self, tmp_path: Path) -> None:
        cat = catalogue_dir()
        app = create_app(
            settings=Settings(_env_file=None, reports_dir=str(tmp_path)),
            catalogue=cat,
            profile=cat / "profile.yaml",
            token="t",
        )
        c = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer t"})
        body = c.get(SESSION).json()
        assert body["mode"] == "token" and body["user"]["kind"] == "operator"
        assert "manage" in body["capabilities"] and body["csrfToken"] is None
        r = c.get(f"{API}/auth/keys")
        assert r.status_code == 409 and r.json()["code"] == "ACCOUNTS_MODE_ONLY"
        assert TestClient(app, base_url="http://127.0.0.1").get(SESSION).status_code == 401
        assert c.get(f"{API}/openapi.json").status_code == 200


# --- serve: the mode comes from configuration ---------------------------------------------------


class TestServeMode:
    def run(self, monkeypatch: pytest.MonkeyPatch, *args: str, **env: str) -> Any:
        import uvicorn

        from opseval.cli import app as cli

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        for name in ("OPSEVAL_DATABASE_URL", "OPSEVAL_PUBLIC_URL", "OPSEVAL_TRUSTED_PROXIES"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("OPSEVAL_DOTENV", "false")
        return CliRunner().invoke(cli, ["serve", *args])

    def test_a_database_on_loopback_is_accounts_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        res = self.run(monkeypatch, OPSEVAL_DATABASE_URL="postgresql://u:p@127.0.0.1/x")
        assert res.exit_code == 3 and "accounts mode" in res.output and "--project" in res.output

    def test_a_public_url_without_a_database_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = self.run(
            monkeypatch, "--public-url", "https://o.example.com", "--trusted-proxy", "10.0.0.1"
        )
        assert res.exit_code == 3 and "OPSEVAL_DATABASE_URL" in res.output

    def test_a_public_bind_without_tls_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        res = self.run(
            monkeypatch,
            "--host",
            "0.0.0.0",  # nosec B104 - the test
            OPSEVAL_DATABASE_URL="postgresql://u:p@127.0.0.1/x",
        )
        assert res.exit_code == 3 and "--public-url https" in res.output
        res = self.run(
            monkeypatch,
            "--host",
            "0.0.0.0",  # nosec B104 - the test
            "--public-url",
            "https://o.example.com",
            OPSEVAL_DATABASE_URL="postgresql://u:p@127.0.0.1/x",
        )
        assert res.exit_code == 3 and "--tls-cert" in res.output

    def test_half_a_tls_pair_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        res = self.run(monkeypatch, "--tls-cert", "/x.pem")
        assert res.exit_code == 3 and "go together" in res.output


def test_a_session_out_never_carries_a_token(tmp_path: Path) -> None:
    body = TestClient(_accounts_app(tmp_path), base_url="https://127.0.0.1").get(SESSION).json()
    assert body == {
        "mode": "accounts",
        "user": None,
        "capabilities": [],
        "hasAccess": False,
        "csrfToken": None,
        "project": "acme/bot",
        "task": "default",
        "oidc": False,
    }
