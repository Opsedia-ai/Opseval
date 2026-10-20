# SPDX-License-Identifier: Apache-2.0
"""Credential provider plugins (FR-RUN-03, ADR-0046).

Both halves of the rule: a provider that fails is a harness error (never the agent timing
out, never a verdict), and a provider that works supplies every attempt's token. Discovery
fails closed, like every other plugin group.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from opseval.auth import (
    CredentialContext,
    CredentialProvider,
    FetchedTokens,
    GuardedTokens,
    StaticToken,
    TokenSource,
)
from opseval.config import Settings
from opseval.demo.refund_agent import run_agent
from opseval.errors import ConfigError, CredentialsError, HarnessError
from opseval.models.profile import SutProfile
from opseval.models.results import VerdictStatus
from opseval.plugins import CREDENTIALS_GROUP, load_credential_provider, reset_for_tests
from opseval.preflight import check_token
from opseval.runner import RunOptions, launch

from .test_plugins import FakeEntryPoint

SECRET = "s3cr3t-token-value"
SEEN: list[CredentialContext] = []


@pytest.fixture(autouse=True)
def _clean() -> Any:
    reset_for_tests()
    SEEN.clear()
    yield
    reset_for_tests()


class Acme(CredentialProvider):
    name = "acme"

    def token_source(self, context: CredentialContext) -> TokenSource:
        SEEN.append(context)
        return StaticToken(f"tok-{context.tenant or 'default'}")


class SlowEndpoint(CredentialProvider):
    """Its own token endpoint timed out: a TimeoutError the agent had nothing to do with."""

    name = "slow"

    def token_source(self, context: CredentialContext) -> TokenSource:
        class Source(TokenSource):
            def current(self) -> str:
                raise TimeoutError(f"token endpoint slow; last token was {SECRET}")

        return Source()


def install(monkeypatch: pytest.MonkeyPatch, *entries: FakeEntryPoint) -> None:
    monkeypatch.setattr(
        "opseval.plugins._iter_entry_points",
        lambda group: iter(entries if group == CREDENTIALS_GROUP else ()),
    )


def ctx(**kw: Any) -> CredentialContext:
    base: dict[str, Any] = {
        "profile": "p",
        "target": "ws://127.0.0.1:1",
        "tenant": None,
        "value": lambda _n: None,
    }
    return CredentialContext(**{**base, **kw})


class TestDiscoveryFailsClosed:
    def test_the_named_provider_is_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install(monkeypatch, FakeEntryPoint("acme", Acme))
        assert isinstance(load_credential_provider("acme"), Acme)

    def test_only_the_named_plugin_is_imported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install(
            monkeypatch,
            FakeEntryPoint("acme", Acme),
            FakeEntryPoint("other", RuntimeError("must not be imported")),
        )
        assert isinstance(load_credential_provider("acme"), Acme)

    def test_a_name_nobody_provides_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install(monkeypatch, FakeEntryPoint("acme", Acme))
        with pytest.raises(ConfigError, match=r"names 'okta'.*installed: acme"):
            load_credential_provider("okta")

    def test_an_unimportable_plugin_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install(monkeypatch, FakeEntryPoint("acme", ImportError("no module acme")))
        with pytest.raises(ConfigError, match="could not be imported"):
            load_credential_provider("acme")

    class Unnamed(CredentialProvider):
        def token_source(self, context: CredentialContext) -> TokenSource:
            return StaticToken("x")

    class Async(CredentialProvider):
        name = "async"

        async def token_source(self, context: CredentialContext) -> TokenSource:  # type: ignore[override]
            return StaticToken("x")

    class Abstract(CredentialProvider):
        name = "abstract"

    class Exploding(CredentialProvider):
        name = "exploding"

        def __init__(self) -> None:
            raise RuntimeError(f"bad config {SECRET}")

        def token_source(self, context: CredentialContext) -> TokenSource:
            raise NotImplementedError

    @pytest.mark.parametrize(
        ("obj", "match"),
        [
            (object, "deriving from opseval.auth.CredentialProvider"),
            (Unnamed, "its own 'name'"),
            (Async, "plain 'def token_source"),
            (Abstract, "abstract"),
            (Exploding, "could not be created: RuntimeError"),
        ],
    )
    def test_a_broken_contract_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, obj: Any, match: str
    ) -> None:
        install(monkeypatch, FakeEntryPoint("acme", obj))
        with pytest.raises(ConfigError, match=match) as info:
            load_credential_provider("acme")
        assert SECRET not in str(info.value)


class TestTheGuard:
    def test_a_providers_timeout_is_a_harness_error_not_the_agents(self) -> None:
        guarded = GuardedTokens(SlowEndpoint(), ctx())
        with pytest.raises(CredentialsError) as info:
            guarded.current()
        assert isinstance(info.value, HarnessError)
        assert not isinstance(info.value, TimeoutError), "a transport would blame the agent"
        assert "TimeoutError" in str(info.value) and SECRET not in str(info.value)

    @pytest.mark.parametrize("bad", ["", "   ", None, 42])
    def test_an_empty_or_odd_token_is_refused(self, bad: Any) -> None:
        class Odd(CredentialProvider):
            name = "odd"

            def token_source(self, context: CredentialContext) -> TokenSource:
                class Source(TokenSource):
                    def current(self) -> str:
                        return bad  # type: ignore[no-any-return]

                return Source()

        with pytest.raises(CredentialsError, match="empty token"):
            GuardedTokens(Odd(), ctx()).current()

    def test_a_source_that_is_not_one_is_refused(self) -> None:
        class Wrong(CredentialProvider):
            name = "wrong"

            def token_source(self, context: CredentialContext) -> TokenSource:
                return "a-token"  # type: ignore[return-value]

        with pytest.raises(CredentialsError, match="not a TokenSource"):
            GuardedTokens(Wrong(), ctx()).current()

    def test_the_source_is_built_on_first_use(self) -> None:
        guarded = GuardedTokens(Acme(), ctx(tenant="t-1"))
        assert SEEN == [] and guarded.seconds_until_refresh() == 0.0
        assert guarded.current() == "tok-t-1" and len(SEEN) == 1
        assert guarded.seconds_until_refresh() is None


class TestFetchedTokens:
    def test_cached_then_fetched_again_before_expiry(self) -> None:
        now = [1000.0]

        class Endpoint(FetchedTokens):
            calls = 0

            def fetch(self) -> tuple[str, float | None]:
                Endpoint.calls += 1
                return f"t{Endpoint.calls}", 100.0

        tokens = Endpoint(refresh_before_seconds=10, clock=lambda: now[0])
        assert tokens.current() == "t1"
        now[0] += 80
        assert tokens.current() == "t1", "still well inside its lifetime"
        now[0] += 15
        assert tokens.current() == "t2", "re-fetched ahead of expiry"

    def test_a_token_without_expiry_is_fetched_once(self) -> None:
        class Forever(FetchedTokens):
            calls = 0

            def fetch(self) -> tuple[str, float | None]:
                Forever.calls += 1
                return "t", None

        tokens = Forever()
        assert [tokens.current() for _ in range(3)] == ["t"] * 3 and Forever.calls == 1


def authed(cat_dir: Path, tmp_path: Path) -> Path:
    """The demo profile, sending its token in an auth frame: a profile that never asks for a
    token never consults a provider."""
    data = yaml.safe_load((cat_dir / "profile.yaml").read_text(encoding="utf-8"))
    data["auth_frame"] = {"type": "auth", "token": "{token}"}
    path = tmp_path / "authed.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def settings_for(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(reports_dir=str(tmp_path / "r"), allowed_hosts="127.0.0.1", **kw)


class TestARun:
    async def test_every_attempt_takes_its_token_from_the_provider(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeEntryPoint("acme", Acme))
        async with run_agent() as url:
            result = await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target=url, profile=authed(cat_dir, tmp_path), repeats=1),
                settings_for(tmp_path, credentials="acme"),
            )
        assert result.exit_code == 0
        attempts = sum(len(t.attempts) for t in result.tests)
        asked = [c for c in SEEN if c.target == url]
        assert len(asked) >= attempts, "a token source per attempt"
        assert {c.profile for c in asked} == {"refund-agent"} or len(
            {c.profile for c in asked}
        ) == 1

    async def test_a_failing_provider_is_never_an_agent_failure(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeEntryPoint("slow", SlowEndpoint))
        async with run_agent() as url:
            result = await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(
                    target=url, profile=authed(cat_dir, tmp_path), repeats=1, preflight=False
                ),
                settings_for(tmp_path, credentials="slow"),
            )
        assert all(t.status is not VerdictStatus.FAILED for t in result.tests)
        assert all(a.passed is None for t in result.tests for a in t.attempts), "not judged"
        assert result.exit_code != 1
        assert SECRET not in (tmp_path / "r").joinpath(result.run_id, "result.json").read_text()

    async def test_a_minting_profile_and_a_provider_are_refused(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeEntryPoint("acme", Acme))
        prof = (cat_dir / "profile.yaml").read_text(encoding="utf-8")
        minted = tmp_path / "profile.yaml"
        minted.write_text(prof + "\ntoken:\n  ttl_seconds: 120\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="Choose one"):
            await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target="ws://127.0.0.1:9", profile=minted, repeats=1),
                settings_for(tmp_path, credentials="acme"),
            )

    async def test_a_missing_provider_stops_the_run_before_it_starts(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch)
        with pytest.raises(ConfigError, match="no installed plugin"):
            await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target="ws://127.0.0.1:9", profile=cat_dir / "profile.yaml"),
                settings_for(tmp_path, credentials="acme"),
            )
        assert not (tmp_path / "r").exists() or not any((tmp_path / "r").iterdir())


class TestWithAPool:
    def write_pool(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "tenants.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    async def test_the_provider_is_told_each_tenant(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a provider, a tenant needs neither token_env nor target to be distinct."""
        install(monkeypatch, FakeEntryPoint("acme", Acme))
        pool = self.write_pool(tmp_path, "tenants:\n  - id: a\n  - id: b\n")
        async with run_agent() as url:
            result = await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target=url, profile=authed(cat_dir, tmp_path), repeats=1),
                settings_for(
                    tmp_path,
                    credentials="acme",
                    tenants_file=str(pool),
                    lanes=2,
                    tenant_lock_dir=str(tmp_path / "locks"),
                ),
            )
        assert result.exit_code == 0
        assert {c.tenant for c in SEEN if c.tenant} == {"a", "b"}

    async def test_a_tenants_own_token_wins(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeEntryPoint("acme", Acme))
        monkeypatch.setenv("T_A", "own-token")
        pool = self.write_pool(tmp_path, "tenants:\n  - id: a\n    token_env: T_A\n")
        async with run_agent() as url:
            await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target=url, profile=authed(cat_dir, tmp_path), repeats=1),
                settings_for(
                    tmp_path,
                    credentials="acme",
                    tenants_file=str(pool),
                    tenant_lock_dir=str(tmp_path / "locks"),
                ),
            )
        assert [c for c in SEEN if c.tenant == "a"] == [], "the provider was not asked for a"

    async def test_without_a_provider_unnamed_tenants_are_still_refused(
        self, cat_dir: Path, tmp_path: Path
    ) -> None:
        pool = self.write_pool(tmp_path, "tenants:\n  - id: a\n  - id: b\n")
        with pytest.raises(ConfigError, match="neither token_env nor target"):
            await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target="ws://127.0.0.1:9", profile=cat_dir / "profile.yaml"),
                settings_for(tmp_path, tenants_file=str(pool), tenant_lock_dir=str(tmp_path / "l")),
            )


class TestPreflight:
    def test_a_working_provider_is_ok(
        self, profile: SutProfile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeEntryPoint("acme", Acme))
        check = check_token(profile, settings_for(tmp_path, credentials="acme"), target="ws://x")
        assert check.status.value == "ok" and "acme" in check.detail
        assert SEEN[0].target == "ws://x" and SEEN[0].tenant is None

    def test_a_failing_provider_is_not_configured(
        self, profile: SutProfile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeEntryPoint("slow", SlowEndpoint))
        check = check_token(profile, settings_for(tmp_path, credentials="slow"))
        assert check.status.value == "not_configured"
        assert "TimeoutError" in check.detail and SECRET not in check.detail

    def test_the_provider_reads_settings_through_opseval(
        self, profile: SutProfile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = tmp_path / ".env"
        env.write_text("ACME_CLIENT_ID=from-dotenv\n", encoding="utf-8")
        got: list[str | None] = []

        class Reads(CredentialProvider):
            name = "reads"

            def token_source(self, context: CredentialContext) -> TokenSource:
                got.append(context.value("ACME_CLIENT_ID"))
                return StaticToken("t")

        install(monkeypatch, FakeEntryPoint("reads", Reads))
        settings = Settings(_env_file=str(env), credentials="reads")  # type: ignore[call-arg]
        check_token(profile, settings)
        assert got == ["from-dotenv"], ".env is read, though it is never in os.environ"


class TestReviewOf67:
    def test_a_refresh_only_token_block_refreshes_the_providers_token_on_websocket(
        self, profile: SutProfile, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With a provider, `token: {refresh_frame}` is how a new token reaches an open
        connection; without it an expired token would be rejected in-band (review of #67)."""
        from opseval.runner import build_transport_factory

        class Short(CredentialProvider):
            name = "short"

            def token_source(self, context: CredentialContext) -> TokenSource:
                class Tokens(FetchedTokens):
                    def fetch(self) -> tuple[str, float | None]:
                        return "t", 60.0

                return Tokens(refresh_before_seconds=10)

        install(monkeypatch, FakeEntryPoint("short", Short))
        data = profile.model_dump()
        data["auth_frame"] = {"type": "auth", "token": "{token}"}
        data["token"] = {"refresh_frame": {"type": "auth_refresh", "token": "{token}"}}
        refreshing = SutProfile.model_validate(data)
        factory = build_transport_factory(
            RunOptions(target="ws://127.0.0.1:9", profile=tmp_path / "p.yaml"),
            settings_for(tmp_path, credentials="short"),
        )
        transport = factory(refreshing, None)
        assert transport.token_or_none() == "t"  # type: ignore[attr-defined]
        due = transport.seconds_until_refresh()  # type: ignore[attr-defined]
        assert due is not None and 45 < due <= 50, "the provider's lifetime drives refresh"

    @pytest.mark.parametrize(
        "block", [{"ttl_seconds": 120}, {"claims": {"aud": "x"}}, {"refresh_before_seconds": 5}]
    )
    def test_a_minting_token_block_with_a_provider_is_refused(
        self, profile: SutProfile, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, block: Any
    ) -> None:
        install(monkeypatch, FakeEntryPoint("acme", Acme))
        data = profile.model_dump()
        data["token"] = block
        check = check_token(
            SutProfile.model_validate(data), settings_for(tmp_path, credentials="acme")
        )
        assert (
            check.status.value == "not_configured" and "only declare refresh_frame" in check.detail
        )

    def test_an_injected_transport_with_a_provider_is_refused(self, tmp_path: Path) -> None:
        from opseval.runner import build_transport_factory

        with pytest.raises(ConfigError, match="injected transport"):
            build_transport_factory(
                RunOptions(profile=tmp_path / "p.yaml", transport_factory=lambda p: None),  # type: ignore[arg-type,return-value]
                settings_for(tmp_path, credentials="acme"),
            )

    async def test_a_provider_failing_preflight_stops_the_run(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeEntryPoint("slow", SlowEndpoint))
        async with run_agent() as url:
            with pytest.raises(ConfigError, match="Nothing was run"):
                await launch(
                    cat_dir / "suites" / "smoke.yaml",
                    RunOptions(target=url, profile=authed(cat_dir, tmp_path), repeats=1),
                    settings_for(tmp_path, credentials="slow"),
                )

    def test_preflight_asks_about_a_tenant_the_provider_will_serve(
        self, profile: SutProfile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.models.tenant import TenantPool
        from opseval.runner import _credentials_tenant

        install(monkeypatch, FakeEntryPoint("acme", Acme))
        settings = settings_for(tmp_path, credentials="acme")
        pool = TenantPool.model_validate(
            {"tenants": [{"id": "own", "token_env": "X"}, {"id": "served"}]}
        )
        assert _credentials_tenant(pool, settings) == "served"
        check_token(profile, settings, credentials_tenant="served")
        assert SEEN[-1].tenant == "served"
        everyone_own = TenantPool.model_validate({"tenants": [{"id": "a", "token_env": "X"}]})
        assert _credentials_tenant(everyone_own, settings) is False
        unused = check_token(profile, settings, credentials_tenant=False)
        assert unused.status.value == "not_needed"

    def test_one_provider_instance_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        made: list[int] = []

        class Counted(Acme):
            name = "counted"

            def __init__(self) -> None:
                made.append(1)

        install(monkeypatch, FakeEntryPoint("counted", Counted))
        assert load_credential_provider("counted") is load_credential_provider("counted")
        assert len(made) == 1

    def test_a_short_lived_token_is_not_fetched_on_every_use(self) -> None:
        now = [0.0]

        class Brief(FetchedTokens):
            calls = 0

            def fetch(self) -> tuple[str, float | None]:
                Brief.calls += 1
                return "t", 20.0  # shorter than the default 30s refresh margin

        tokens = Brief(clock=lambda: now[0])
        for _ in range(5):
            tokens.current()
        assert Brief.calls == 1
        now[0] += 11
        tokens.current()
        assert Brief.calls == 2, "re-fetched at half its lifetime"

    def test_whitespace_is_not_a_provider_name(self, tmp_path: Path) -> None:
        assert settings_for(tmp_path, credentials="  ").credentials == ""
        assert settings_for(tmp_path, credentials=" acme ").credentials == "acme"
