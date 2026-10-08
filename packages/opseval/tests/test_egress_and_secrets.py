# SPDX-License-Identifier: Apache-2.0
"""Where conversations and results may go, and which settings are secrets (ATTENTION I2, I3, I5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from opseval.config import Settings, check_url_allowed, config_digest
from opseval.errors import AllowedHostError, ConfigError
from opseval.llm import UsageMeter, client_for_role
from opseval.models.results import Gate, SuiteResult
from opseval.plugins import reset_for_tests
from opseval.sinks import Sink, WebhookSink, build_sinks

from .test_plugins import FakeEntryPoint


class AcmeSink(Sink):
    name = "acme"

    async def publish(self, result: SuiteResult) -> str:
        return "sent"


class TestLlmEndpoints:
    def test_a_keyless_endpoint_must_be_allowlisted_too(self) -> None:
        """A local proxy without a key still receives every transcript."""
        settings = Settings(simulator_base_url="http://gpu-box.lan:11434/v1", simulator_model="m")
        with pytest.raises(AllowedHostError):
            client_for_role(settings, "simulator", UsageMeter())

    def test_loopback_is_allowlisted_like_any_host_but_needs_no_tls(self) -> None:
        """A local model (Ollama on 127.0.0.1) is named in the allowlist like any other host;
        plain HTTP is fine on loopback."""
        settings = Settings(judge_base_url="http://127.0.0.1:11434/v1", judge_model="m")
        with pytest.raises(AllowedHostError):
            client_for_role(settings, "judge", UsageMeter())
        allowed = settings.model_copy(update={"allowed_hosts": "127.0.0.1"})
        assert client_for_role(allowed, "judge", UsageMeter()) is not None

    def test_an_allowlisted_host_still_needs_tls(self) -> None:
        settings = Settings(
            judge_base_url="http://api.example.com/v1",
            judge_model="m",
            allowed_hosts="api.example.com",
        )
        with pytest.raises(ConfigError, match="must use"):
            client_for_role(settings, "judge", UsageMeter())


class TestPluginSinks:
    @pytest.fixture(autouse=True)
    def installed(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """``acme`` is installed and works; ``other`` is installed and must never be imported."""
        reset_for_tests()
        self.entries = [
            FakeEntryPoint("acme", AcmeSink),
            FakeEntryPoint("other", RuntimeError("imported a plugin nobody enabled")),
        ]
        monkeypatch.setattr("opseval.plugins._iter_entry_points", lambda g: iter(self.entries))
        yield
        reset_for_tests()

    def test_installing_a_plugin_publishes_nothing(self) -> None:
        assert build_sinks(Settings()) == []

    def test_a_named_plugin_is_enabled_and_no_other_is_imported(self) -> None:
        assert [s.name for s in build_sinks(Settings(sinks="acme"))] == ["acme"]

    def test_every_run_in_one_process_gets_its_sinks(self) -> None:
        """The console server and the pytest plugin build sinks once per run."""
        for _ in range(2):
            assert [s.name for s in build_sinks(Settings(sinks="acme"))] == ["acme"]

    def test_a_name_given_twice_publishes_once(self) -> None:
        assert [s.name for s in build_sinks(Settings(sinks="acme, acme"))] == ["acme"]

    def test_naming_a_plugin_that_is_not_installed_is_an_error(self) -> None:
        with pytest.raises(ConfigError, match="no installed plugin.*installed: acme, other"):
            build_sinks(Settings(sinks="acme, nope"))

    def test_a_broken_plugin_that_is_named_stops_the_run(self) -> None:
        with pytest.raises(ConfigError, match="could not be imported"):
            build_sinks(Settings(sinks="other"))


class TestWebhookUrlIsASecret:
    URL = "https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXXXXXXXXXX"

    def test_it_is_redacted_in_the_config_digest(self) -> None:
        digest = config_digest(Settings(webhook_url=SecretStr(self.URL)))
        assert self.URL not in str(digest) and digest["webhook_url"] == "***redacted***"

    async def test_its_outcome_names_only_the_host(self) -> None:
        def ok(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        sink = WebhookSink(
            self.URL, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(ok))
        )
        now = datetime(2026, 10, 8, tzinfo=UTC)
        result = SuiteResult(
            opseval_version="0",
            run_id="r",
            suite="s",
            target="ws://x",
            profile="p",
            started_at=now,
            ended_at=now,
            tests=[],
            gate=Gate(status="pass"),
            exit_code=0,
        )
        detail = await sink.publish(result)
        assert "hooks.slack.com" in detail and "XXXX" not in detail and "/services/" not in detail


class TestAllowlistErrorsNeverRepeatTheUrl:
    """A webhook URL's path is its secret; a URL can carry credentials in user-info or query."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://hooks.example.com/services/SECRET-PATH?token=SECRET-QUERY",
            "https://user:SECRET-PASS@other.example.com/SECRET-PATH",
            "https:///SECRET-PATH?token=SECRET-QUERY",
        ],
    )
    def test_only_the_scheme_and_host_are_shown(self, url: str) -> None:
        with pytest.raises(AllowedHostError) as caught:
            check_url_allowed(url, {"hooks.example.com"}, secure_schemes=("https",))
        assert "SECRET" not in str(caught.value)
