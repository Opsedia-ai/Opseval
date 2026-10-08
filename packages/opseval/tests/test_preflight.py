# SPDX-License-Identifier: Apache-2.0
"""Preflight health checks.

Fault-injection view of the core rule:
- harness half: a dependency outage skips the scenarios that would have nothing
  measurable without it (``skipped_precondition``), instead of failing them, and a check
  that cannot prove a credential says ``unverified`` rather than implying it is good;
- agent half: an unreachable **agent** is NOT a skip. A skip is only a gate warning, so
  skipping there would report "safe to release" on a suite that tested nothing; it is a
  configuration error (exit 3). And a scenario that still has a measurable scorer keeps
  running, because skipping it would throw away the checks that can run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from opseval.cli import app
from opseval.config import Settings
from opseval.demo.refund_agent import run_agent
from opseval.errors import ConfigError
from opseval.models.preflight import CheckStatus, HealthCheck, PreflightReport
from opseval.models.profile import SutProfile, TokenConfig, TraceConfig
from opseval.models.results import VerdictStatus
from opseval.preflight import (
    check_llm,
    check_token,
    check_traces,
    preflight,
    probe_target,
)
from opseval.runner import RunOptions, launch

from .conftest import make_scenario


def report(**statuses: CheckStatus) -> PreflightReport:
    return PreflightReport(
        checks=[
            HealthCheck(name=name, status=status, detail="because")
            for name, status in statuses.items()
        ]
    )


# --- which scenarios depend on what --------------------------------------------------


class TestRequirements:
    """Only a dependency that would leave *nothing* measurable counts."""

    async def probe(self, scenario: Any, **kw: Any) -> PreflightReport:
        return await preflight(
            [scenario],
            SutProfile(name="p"),
            Settings(),
            target="python:x",
            transport="python",
            probe_target_host=False,
            **kw,
        )

    async def test_a_judge_only_scenario_needs_the_judge(self) -> None:
        out = await self.probe(make_scenario(scorers=[{"id": "judge.goal_achievement"}]))
        judge = out.get("judge")
        assert judge is not None
        assert judge.needed_by == 1
        assert judge.status is CheckStatus.NOT_CONFIGURED

    async def test_a_mixed_scenario_does_not_need_the_judge(self) -> None:
        """It still produces a real verdict from its deterministic scorer."""
        out = await self.probe(
            make_scenario(
                scorers=[{"id": "output.terminal_state"}, {"id": "judge.goal_achievement"}]
            )
        )
        judge = out.get("judge")
        assert judge is not None
        assert judge.needed_by == 0
        assert judge.status is CheckStatus.NOT_NEEDED

    async def test_a_deterministic_scenario_needs_neither(self) -> None:
        out = await self.probe(make_scenario(scorers=[{"id": "output.terminal_state"}]))
        for name in ("judge", "simulator", "traces"):
            check = out.get(name)
            assert check is not None and check.status is CheckStatus.NOT_NEEDED, name

    async def test_a_scripted_scenario_does_not_need_a_simulator(self) -> None:
        out = await self.probe(make_scenario())
        sim = out.get("simulator")
        assert sim is not None and sim.status is CheckStatus.NOT_NEEDED

    async def test_an_llm_persona_needs_a_simulator(self) -> None:
        out = await self.probe(
            make_scenario(conversation={"scripted_turns": [], "flow": "ask for a refund"})
        )
        sim = out.get("simulator")
        assert sim is not None and sim.needed_by == 1

    async def test_a_blocked_scenario_needs_nothing(self) -> None:
        out = await self.probe(
            make_scenario(tags=["blocked"], scorers=[{"id": "judge.goal_achievement"}])
        )
        judge = out.get("judge")
        assert judge is not None and judge.needed_by == 0


# --- probing the agent ---------------------------------------------------------------


class TestProbeTarget:
    async def test_a_live_websocket_agent_is_reachable(self) -> None:
        async with run_agent() as url:
            check = await probe_target(url, "websocket")
        assert check.status is CheckStatus.OK

    async def test_a_refused_connection_is_unavailable(self) -> None:
        check = await probe_target("ws://127.0.0.1:9", "websocket")
        assert check.status is CheckStatus.UNAVAILABLE
        assert "ConnectionRefusedError" in check.detail

    async def test_an_http_answer_of_any_status_proves_reachability(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 401 or 404 still means the host is up, which is all this check claims."""

        async def fake_get(self: Any, url: str) -> httpx.Response:
            return httpx.Response(404)

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        check = await probe_target("https://agent.example.com/x", "http-json")
        assert check.status is CheckStatus.OK
        assert "404" in check.detail

    async def test_an_http_connection_error_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(self: Any, url: str) -> httpx.Response:
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx.AsyncClient, "get", boom)
        check = await probe_target("https://agent.example.com", "http-json")
        assert check.status is CheckStatus.UNAVAILABLE

    async def test_an_in_process_agent_needs_no_network(self) -> None:
        check = await probe_target("python:factory", "python")
        assert check.status is CheckStatus.OK

    async def test_an_unsupported_scheme_is_unavailable(self) -> None:
        check = await probe_target("gopher://agent", "websocket")
        assert check.status is CheckStatus.UNAVAILABLE
        assert "scheme" in check.detail


# --- credentials and dependencies ----------------------------------------------------


class TestToken:
    def test_a_profile_without_tokens_needs_no_check(self, profile: SutProfile) -> None:
        assert check_token(profile, Settings()).status is CheckStatus.NOT_NEEDED

    def test_a_token_profile_without_a_key_is_not_configured(self, profile: SutProfile) -> None:
        p = profile.model_copy(update={"token": TokenConfig(ttl_seconds=300)})
        check = check_token(p, Settings())
        assert check.status is CheckStatus.NOT_CONFIGURED
        assert "OPSEVAL_TOKEN_SIGNING_KEY" in check.detail

    def test_a_mintable_token_is_ok(self, profile: SutProfile) -> None:
        p = profile.model_copy(update={"token": TokenConfig(ttl_seconds=300)})
        check = check_token(p, Settings(token_signing_key=SecretStr("k" * 32)))
        assert check.status is CheckStatus.OK
        assert "minted" in check.detail


class TestLlm:
    async def test_not_needed_when_no_scenario_depends_on_it(self) -> None:
        check = await check_llm("judge", Settings(), 0)
        assert check.status is CheckStatus.NOT_NEEDED

    async def test_needed_but_unset_is_not_configured(self) -> None:
        check = await check_llm("judge", Settings(), 3)
        assert check.status is CheckStatus.NOT_CONFIGURED
        assert "OPSEVAL_JUDGE_BASE_URL" in check.detail
        assert check.needed_by == 3

    async def test_a_reachable_endpoint_is_unverified_not_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reachability is not proof the key works, and must not be reported as if it were."""

        async def fake_get(self: Any, url: str) -> httpx.Response:
            return httpx.Response(200)

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        check = await check_llm(
            "judge",
            Settings(
                judge_base_url="https://api.example.com/v1",
                judge_model="m",
                allowed_hosts="api.example.com",
            ),
            2,
        )
        assert check.status is CheckStatus.UNVERIFIED
        assert "not tested" in check.detail

    async def test_an_unreachable_endpoint_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(self: Any, url: str) -> httpx.Response:
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(httpx.AsyncClient, "get", boom)
        check = await check_llm(
            "judge",
            Settings(
                judge_base_url="https://api.example.com/v1",
                judge_model="m",
                allowed_hosts="api.example.com",
            ),
            1,
        )
        assert check.status is CheckStatus.UNAVAILABLE

    async def test_a_host_that_is_not_allowlisted_is_never_probed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The allowlist covers every endpoint, keyed or not, and even a health probe."""
        probed: list[str] = []

        async def spy(self: Any, url: str) -> httpx.Response:
            probed.append(url)
            return httpx.Response(200)

        monkeypatch.setattr(httpx.AsyncClient, "get", spy)
        check = await check_llm(
            "judge", Settings(judge_base_url="http://gpu-box.lan:11434/v1", judge_model="m"), 1
        )
        assert check.status is CheckStatus.UNAVAILABLE and "OPSEVAL_ALLOWED_HOSTS" in check.detail
        assert probed == []


class TestTraces:
    async def test_not_needed_without_a_trace_source(self, profile: SutProfile) -> None:
        check = await check_traces(profile, Settings(), 1, enabled=True)
        assert check.status is CheckStatus.NOT_NEEDED

    async def test_disabled_is_not_needed(self, profile: SutProfile) -> None:
        p = profile.model_copy(update={"traces": TraceConfig()})
        check = await check_traces(p, Settings(), 1, enabled=False)
        assert check.status is CheckStatus.NOT_NEEDED

    async def test_missing_credentials_are_not_configured(self, profile: SutProfile) -> None:
        p = profile.model_copy(update={"traces": TraceConfig()})
        check = await check_traces(p, Settings(), 1, enabled=True)
        assert check.status is CheckStatus.NOT_CONFIGURED
        assert "--no-traces" in check.detail


# --- the report ----------------------------------------------------------------------


class TestReport:
    def test_only_unavailable_and_unset_are_blocking(self) -> None:
        out = report(
            sut=CheckStatus.OK,
            judge=CheckStatus.UNAVAILABLE,
            traces=CheckStatus.NOT_CONFIGURED,
            simulator=CheckStatus.NOT_NEEDED,
            token=CheckStatus.UNVERIFIED,
        )
        assert [c.name for c in out.blocking()] == ["judge", "traces"]

    def test_unverified_is_not_blocking(self) -> None:
        """An unproven key is a caveat, not a reason to refuse to run."""
        assert report(judge=CheckStatus.UNVERIFIED).blocking() == []

    def test_a_reason_is_given_only_for_a_blocking_check(self) -> None:
        out = report(judge=CheckStatus.UNAVAILABLE, traces=CheckStatus.OK)
        assert "judge unavailable" in (out.reason("judge") or "")
        assert out.reason("traces") is None
        assert out.reason("absent") is None

    def test_the_summary_says_what_will_be_skipped(self) -> None:
        text = report(judge=CheckStatus.UNAVAILABLE).summary()
        assert "skipped_precondition" in text
        assert "not failed" in text

    def test_the_summary_says_so_when_all_is_well(self) -> None:
        assert "available" in report(sut=CheckStatus.OK).summary()


# --- how the runner uses it ----------------------------------------------------------


class TestRunnerIntegration:
    async def test_an_unreachable_agent_is_a_config_error_not_a_skip(
        self, cat_dir: Path, settings: Settings
    ) -> None:
        """The decision this feature turns on.

        A skipped scenario is only a gate *warning*, so skipping here would report
        "safe to release" on a suite that never reached the agent. It must be exit 3.
        """
        with pytest.raises(ConfigError, match="not reachable"):
            await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(
                    target="ws://127.0.0.1:9",
                    profile=cat_dir / "profile.yaml",
                    repeats=1,
                ),
                settings,
            )

    async def test_no_preflight_lets_the_run_proceed(
        self, cat_dir: Path, settings: Settings
    ) -> None:
        """The escape hatch the error message names."""
        result = await launch(
            cat_dir / "suites" / "smoke.yaml",
            RunOptions(
                target="ws://127.0.0.1:9",
                profile=cat_dir / "profile.yaml",
                repeats=1,
                preflight=False,
            ),
            settings,
        )
        assert result.exit_code == 3, "every attempt was a transport failure"
        assert result.preflight is None

    async def test_a_judge_only_scenario_is_skipped_not_failed(
        self, cat_dir: Path, settings: Settings
    ) -> None:
        async with run_agent() as url:
            result = await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1),
                settings,
            )
        assert result.preflight is not None
        judge = result.preflight.get("judge")
        assert judge is not None
        # The demo suite is deterministic, so no scenario depends on a judge.
        assert judge.status is CheckStatus.NOT_NEEDED
        assert all(t.status is not VerdictStatus.SKIPPED_PRECONDITION for t in result.tests)

    async def test_the_report_is_recorded_on_the_result(
        self, cat_dir: Path, settings: Settings
    ) -> None:
        async with run_agent() as url:
            result = await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1),
                settings,
            )
        assert result.preflight is not None
        assert {c.name for c in result.preflight.checks} == {
            "sut",
            "token",
            "simulator",
            "judge",
            "traces",
        }


class TestCli:
    runner = CliRunner()

    def test_an_unreachable_agent_is_exit_2_and_names_the_check(self, cat_dir: Path) -> None:
        """Standalone preflight reports; it is `run` that refuses. Exit 2 = not judged."""
        res = self.runner.invoke(
            app,
            [
                "preflight",
                str(cat_dir / "suites" / "smoke.yaml"),
                "--profile",
                str(cat_dir / "profile.yaml"),
                "--target",
                "ws://127.0.0.1:9",
            ],
        )
        assert res.exit_code == 2, res.output
        assert "UNAVAILABLE" in res.output
        assert "sut" in res.output

    def test_a_bad_profile_is_exit_3(self, tmp_path: Path) -> None:
        res = self.runner.invoke(
            app,
            [
                "preflight",
                str(tmp_path),
                "--profile",
                str(tmp_path / "nope.yaml"),
                "--target",
                "ws://127.0.0.1:9",
            ],
        )
        assert res.exit_code == 3
