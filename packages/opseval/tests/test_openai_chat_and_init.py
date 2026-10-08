# SPDX-License-Identifier: Apache-2.0
"""openai-chat transport, built-in profiles, `opseval init` and JUnit output."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import pytest
from typer.testing import CliRunner

from opseval.catalogue import load_profile
from opseval.cli import app
from opseval.config import Settings
from opseval.driver import Driver
from opseval.errors import ConfigError
from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord, TerminalState
from opseval.models.results import SuiteResult, VerdictStatus
from opseval.models.scenario import Scenario
from opseval.reporting.junit import render_junit
from opseval.runner import RunOptions, launch
from opseval.scaffold import scaffold
from opseval.scoring import ScoringEngine
from opseval.simulator import ScriptedSimulator
from opseval.transport import OpenAIChatTransport
from opseval.verdicts import attempt_verdict

from .conftest import make_scenario

runner = CliRunner()
ARABIC_REPLY = "أهلاً بيك! إحنا فاتحين من ٩ الصبح لحد ١٠ بالليل."


def chat_reply(
    content: str | None, tool_calls: list[dict[str, Any]] | None = None
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": message}]})


def fake_agent(request: httpx.Request) -> httpx.Response:
    """A tiny chat agent: answers in the customer's script and 'calls' a tool for orders."""
    body = json.loads(request.content)
    last = body["messages"][-1]["content"]
    if any("؀" <= ch <= "ۿ" for ch in last):
        return chat_reply(ARABIC_REPLY)
    if "order" in last:
        return chat_reply(
            "Let me check that order.",
            [{"type": "function", "function": {"name": "lookup_order", "arguments": '{"id": 1}'}}],
        )
    return chat_reply("We are open 9am to 10pm every day.")


def oc_scenario(**overrides: Any) -> Scenario:
    base: dict[str, Any] = {
        "conversation": {
            "scripted_turns": ["What are your opening hours?"],
            "stop_at": "goal_reached",
            "checkpoint_policy": {},
        },
        "expectations": {},
        "scorers": [{"id": "conversation.no_errors"}, {"id": "output.terminal_state"}],
    }
    base.update(overrides)
    return make_scenario(**base)


def transport(
    profile: SutProfile, handler: Any, sleeps: list[float] | None = None
) -> OpenAIChatTransport:
    async def fake_sleep(s: float) -> None:
        (sleeps if sleeps is not None else []).append(s)

    return OpenAIChatTransport(
        "https://agent.example.com/v1",
        profile,
        token="tok",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=fake_sleep,
    )


async def run_once(scenario: Scenario, profile: SutProfile, tr: OpenAIChatTransport) -> RunRecord:
    return await Driver(
        scenario=scenario,
        profile=profile,
        transport=tr,
        simulator=ScriptedSimulator(scenario, 1),
        run_id="r",
        attempt_index=0,
        seed=1,
    ).run()


async def judged(record: RunRecord, scenario: Scenario, profile: SutProfile) -> bool | None:
    scores = await ScoringEngine().score(record, scenario, profile)
    return attempt_verdict(record, scores, "x").passed


@pytest.fixture
def oc_profile() -> SutProfile:
    return load_profile("builtin:openai-chat")


class TestOpenAIChat:
    async def test_history_options_and_reply(self, oc_profile: SutProfile) -> None:
        profile = oc_profile.model_copy(
            update={
                "transport_options": {
                    "model": "agent-v2",
                    "system_prompt": "You are Acme support.",
                    "temperature": 0,
                    "extra_body": {"user": "opseval"},
                }
            }
        )
        seen: list[dict[str, Any]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/v1/chat/completions"
            assert req.headers["authorization"] == "Bearer tok"
            seen.append(json.loads(req.content))
            return fake_agent(req)

        s = oc_scenario(
            conversation={
                "scripted_turns": ["Hi there", "Where is my order 10482?"],
                "stop_at": "goal_reached",
                "checkpoint_policy": {},
            }
        )
        rec = await run_once(s, profile, transport(profile, handler))
        assert rec.terminal_state is TerminalState.GOAL_REACHED
        assert [m["role"] for m in seen[1]["messages"]] == ["system", "user", "assistant", "user"]
        assert seen[0]["model"] == "agent-v2" and seen[0]["temperature"] == 0
        assert seen[0]["user"] == "opseval"
        replies = rec.received()
        assert replies[-1].payload["data"]["tool_calls"] == [
            {"name": "lookup_order", "arguments": {"id": 1}}
        ]
        assert await judged(rec, s, profile) is True

    async def test_tool_calls_are_frame_evidence(self, oc_profile: SutProfile) -> None:
        profile = SutProfile.model_validate(
            {**oc_profile.model_dump(), "tools": {"lookup_order": {}, "refund": {}}}
        )
        s = oc_scenario(
            conversation={
                "scripted_turns": ["Where is my order 10482?"],
                "stop_at": "goal_reached",
                "checkpoint_policy": {},
            },
            expectations={
                "expected_tools": {"must_call": ["lookup_order"], "must_not_call": ["refund"]}
            },
            scorers=[{"id": "tools.expected_called"}, {"id": "tools.forbidden_not_called"}],
        )
        rec = await run_once(s, profile, transport(profile, fake_agent))
        scores = await ScoringEngine().score(rec, s, profile)
        assert all(x.status.value == "passed" and x.degraded for x in scores), scores

    async def test_5xx_is_an_agent_error(self, oc_profile: SutProfile) -> None:
        s = oc_scenario()
        rec = await run_once(s, oc_profile, transport(oc_profile, lambda r: httpx.Response(502)))
        assert rec.terminal_state is TerminalState.ERROR_FRAME
        assert await judged(rec, s, oc_profile) is False

    async def test_malformed_body_is_an_agent_error(self, oc_profile: SutProfile) -> None:
        s = oc_scenario()
        tr = transport(oc_profile, lambda r: httpx.Response(200, json={"nope": True}))
        rec = await run_once(s, oc_profile, tr)
        assert rec.terminal_state is TerminalState.ERROR_FRAME

    async def test_rate_limit_is_retried(self, oc_profile: SutProfile) -> None:
        responses = iter([httpx.Response(429, headers={"retry-after": "3"}), chat_reply("Hello")])
        sleeps: list[float] = []
        s = oc_scenario()
        rec = await run_once(
            s, oc_profile, transport(oc_profile, lambda r: next(responses), sleeps)
        )
        assert rec.terminal_state is TerminalState.GOAL_REACHED and sleeps == [3.0]

    async def test_persistent_rate_limit_is_not_blamed(self, oc_profile: SutProfile) -> None:
        s = oc_scenario()
        tr = transport(oc_profile, lambda r: httpx.Response(429))
        rec = await run_once(s, oc_profile, tr)
        assert rec.terminal_state is TerminalState.TRANSPORT_FAILURE
        assert await judged(rec, s, oc_profile) is None
        assert "rate_limited" in rec.notes, "stop_on_rate_limit reads this, not error text"

    async def test_an_auth_rejection_is_not_noted_as_a_rate_limit(
        self, oc_profile: SutProfile
    ) -> None:
        s = oc_scenario()
        rec = await run_once(s, oc_profile, transport(oc_profile, lambda r: httpx.Response(401)))
        assert "rate_limited" not in rec.notes

    async def test_auth_rejection_is_not_blamed(self, oc_profile: SutProfile) -> None:
        s = oc_scenario()
        rec = await run_once(s, oc_profile, transport(oc_profile, lambda r: httpx.Response(401)))
        assert rec.terminal_state is TerminalState.TRANSPORT_FAILURE
        assert await judged(rec, s, oc_profile) is None

    async def test_timeout_counts_against_the_agent(self, oc_profile: SutProfile) -> None:
        def slow(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=req)

        s = oc_scenario()
        rec = await run_once(s, oc_profile, transport(oc_profile, slow))
        assert rec.terminal_state is TerminalState.TIMEOUT
        assert await judged(rec, s, oc_profile) is False

    def test_unknown_builtin_profile(self) -> None:
        with pytest.raises(ConfigError, match="unknown built-in profile"):
            load_profile("builtin:nope")

    def test_runner_requires_https_for_remote_targets(self, tmp_path: Path) -> None:
        scaffold(tmp_path / "cat")
        settings = Settings(allowed_hosts="agent.example.com", reports_dir=str(tmp_path / "r"))
        options = RunOptions(
            target="http://agent.example.com/v1",
            transport="openai-chat",
            profile=tmp_path / "cat" / "profile.yaml",
        )
        with pytest.raises(ConfigError, match="must use"):
            asyncio.run(launch(tmp_path / "cat" / "suites" / "smoke.yaml", options, settings))


class TestInit:
    @pytest.mark.parametrize(
        ("template", "suite"), [("openai-chat", "full"), ("refund-demo", "full")]
    )
    def test_generated_catalogue_validates(self, tmp_path: Path, template: str, suite: str) -> None:
        res = runner.invoke(app, ["init", str(tmp_path / "cat"), "--template", template])
        assert res.exit_code == 0, res.output
        cat = tmp_path / "cat"
        assert (cat / "README.md").exists()
        res = runner.invoke(
            app,
            [
                "validate",
                str(cat / "suites" / f"{suite}.yaml"),
                "--profile",
                str(cat / "profile.yaml"),
            ],
        )
        assert res.exit_code == 0, res.output
        if template == "openai-chat":
            assert (cat / ".env.example").exists()

    def test_refuses_to_overwrite(self, tmp_path: Path) -> None:
        assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
        res = runner.invoke(app, ["init", str(tmp_path)])
        assert res.exit_code == 3 and "refusing to overwrite" in res.output
        assert runner.invoke(app, ["init", str(tmp_path), "--force"]).exit_code == 0

    def test_unknown_template(self, tmp_path: Path) -> None:
        res = runner.invoke(app, ["init", str(tmp_path), "--template", "nope"])
        assert res.exit_code == 3

    async def test_scaffold_runs_green_against_a_chat_agent(self, tmp_path: Path) -> None:
        scaffold(tmp_path / "cat")
        profile = load_profile(tmp_path / "cat" / "profile.yaml")
        settings = Settings(allowed_hosts="agent.example.com", reports_dir=str(tmp_path / "r"))
        options = RunOptions(
            target="https://agent.example.com/v1",
            transport="openai-chat",
            profile=tmp_path / "cat" / "profile.yaml",
            transport_factory=lambda p: transport(p, fake_agent),
        )
        result = await launch(tmp_path / "cat" / "suites" / "smoke.yaml", options, settings)
        assert profile.name == "my-agent"
        assert [t.status for t in result.tests] == [VerdictStatus.PASSED, VerdictStatus.PASSED]
        assert result.exit_code == 0
        assert (Path(settings.reports_dir) / result.run_id / "junit.xml").exists()


class TestJUnit:
    def test_mapping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        res = runner.invoke(
            app, ["demo", "--repeats", "1", "--bug", "forget-method", "--junit", "out/j.xml"]
        )
        assert res.exit_code == 1, res.output
        root = ET.parse(tmp_path / "out" / "j.xml").getroot()
        assert root.tag == "testsuite" and root.get("tests") == "3"
        failures = [c for c in root.iter("testcase") if c.find("failure") is not None]
        assert failures and "payload_equals" in (failures[0].find("failure").text or "")  # type: ignore[union-attr]

    def test_inconclusive_is_an_error_and_load_errors_are_visible(self) -> None:
        base = {
            "opseval_version": "x",
            "run_id": "r",
            "suite": "s",
            "target": "t",
            "profile": "p",
            "started_at": "2026-09-23T10:00:00Z",
            "ended_at": "2026-09-23T10:00:05Z",
            "gate": {"status": "block"},
            "exit_code": 2,
            "load_errors": ["bad.yaml: id: field required"],
            "tests": [
                {
                    "test_id": "a",
                    "name": "A",
                    "criticality": "normal",
                    "status": "inconclusive",
                    "reason": "harness",
                },
                {
                    "test_id": "b",
                    "name": "B",
                    "criticality": "normal",
                    "status": "skipped_precondition",
                    "reason": "needs LLM",
                },
                {
                    "test_id": "c",
                    "name": "C & <D>",
                    "criticality": "critical",
                    "status": "passed",
                    "pass_rate": 1.0,
                },
            ],
        }
        xml = render_junit(SuiteResult.model_validate(base))
        root = ET.fromstring(xml)
        assert (
            root.get("tests"),
            root.get("errors"),
            root.get("skipped"),
            root.get("failures"),
        ) == ("4", "2", "1", "0")
        assert root.get("time") == "5.000"
        names = [c.get("name") for c in root.iter("testcase")]
        assert "c: C & <D>" in names and "catalogue load errors" in names
