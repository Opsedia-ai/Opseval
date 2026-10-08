# SPDX-License-Identifier: Apache-2.0
"""Where conversations and results may go, and which settings are secrets (ATTENTION I2, I3, I5)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from opseval.config import Settings, config_digest
from opseval.errors import AllowedHostError, ConfigError
from opseval.llm import UsageMeter, client_for_role
from opseval.models.results import Gate, SuiteResult
from opseval.sinks import WebhookSink, build_sinks


class AcmeSink:
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
    def installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("opseval.plugins.load_sinks", lambda: {"acme": AcmeSink})

    def test_installing_a_plugin_publishes_nothing(self) -> None:
        assert build_sinks(Settings()) == []

    def test_a_named_plugin_is_enabled(self) -> None:
        assert [s.name for s in build_sinks(Settings(sinks="acme"))] == ["acme"]

    def test_naming_a_plugin_that_is_not_installed_is_an_error(self) -> None:
        with pytest.raises(ConfigError, match="no installed plugin provides.*installed: acme"):
            build_sinks(Settings(sinks="acme, nope"))


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
