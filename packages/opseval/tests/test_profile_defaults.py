# SPDX-License-Identifier: Apache-2.0
"""ADR-0043: defaults from the profile, a silent agent fails, and checks say what they take.

- ``scorers: default`` and ``stop_at`` follow what the profile declares, so a chatbot with no
  phases, tools or commit gate can reach "Safe to release", and every expectation a scenario
  declares is checked;
- ``conversation.agent_replied`` fails an agent that leaves a turn unanswered (I10);
- every scorer's ``params`` are checked when the scenario loads (I15);
- ``expectations.kind: refusal`` reaches the judges;
- running out of scripted turns is said for what it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from opseval.advice import scenario_warnings
from opseval.catalogue import check_references, load_catalogue
from opseval.config import Settings
from opseval.driver import Driver
from opseval.models.profile import SutProfile
from opseval.models.record import SCRIPT_EXHAUSTED, RunRecord, TerminalState
from opseval.models.results import ScoreStatus
from opseval.models.scenario import Scenario
from opseval.runner import RunOptions, launch
from opseval.scoring import (
    DEFAULT_PACK,
    INVARIANTS,
    REGISTRY,
    NoParams,
    Scorer,
    ScoringContext,
    ScoringEngine,
    default_pack,
    describe_params,
    effective_specs,
)
from opseval.scoring.judges import GoalAchievement
from opseval.simulator import ScriptedSimulator
from opseval.transport import PythonTransport

from .conftest import make_scenario

CHAT = SutProfile.model_validate(
    {"name": "chat", "kinds": {"message": "message"}, "phase_field": None}
)


def chat_scenario(**overrides: Any) -> Scenario:
    """A scripted chatbot scenario that leaves stop_at out, as a chatbot author would."""
    base: dict[str, Any] = {
        "id": "faq-001",
        "name": "opening hours",
        "goal": "Learn the opening hours.",
        "persona": {"role": "customer", "language": "en"},
        "conversation": {"scripted_turns": ["When are you open?", "And on Sundays?"]},
        "scorers": "default",
        "execution": {"repeats": 1},
    }
    base.update(overrides)
    return Scenario.model_validate(base)


async def attempt(scenario: Scenario, agent: Any, profile: SutProfile = CHAT) -> RunRecord:
    return await Driver(
        scenario=scenario,
        profile=profile,
        transport=PythonTransport(lambda: agent, profile),
        simulator=ScriptedSimulator(scenario, 1),
        run_id="r",
        attempt_index=0,
        seed=1,
    ).run()


def say(*replies: Any) -> Any:
    queue = list(replies)

    def agent(_frame: dict[str, Any]) -> Any:
        return queue.pop(0) if queue else {"type": "message", "text": "ok"}

    return agent


async def score(record: RunRecord, scenario: Scenario, profile: SutProfile = CHAT) -> Any:
    return {s.scorer_id: s for s in await ScoringEngine().score(record, scenario, profile)}


class TestTheDefaultPackFollowsTheProfile:
    def test_a_chatbot_gets_only_checks_that_measure_something(self) -> None:
        assert default_pack(chat_scenario(), CHAT) == [
            "conversation.no_errors",
            "conversation.agent_replied",
            "output.terminal_state",
        ]

    def test_the_demo_agent_keeps_its_phase_and_commit_checks(self, profile: SutProfile) -> None:
        pack = default_pack(make_scenario(scorers="default"), profile)
        assert "flow.phase_order" in pack and "tools.forbidden_not_called" in pack

    def test_every_declared_tool_expectation_is_checked(self) -> None:
        profile = SutProfile.model_validate({**CHAT.model_dump(), "tools": {"a": {}, "b": {}}})
        scenario = chat_scenario(
            expectations={
                "expected_tools": {
                    "must_call": ["a"],
                    "must_not_call": ["b"],
                    "ordering": [{"before": "a", "after": "b"}],
                }
            }
        )
        pack = default_pack(scenario, profile)
        # must_call and ordering were silently never checked under `scorers: default`.
        assert {"tools.expected_called", "tools.forbidden_not_called", "tools.ordering"} <= set(
            pack
        )

    def test_without_a_profile_the_old_pack_is_kept(self) -> None:
        assert [s.id for s in effective_specs(chat_scenario())] == DEFAULT_PACK

    async def test_the_engine_scores_the_profiles_pack(self) -> None:
        scenario = chat_scenario()
        record = await attempt(scenario, say({"type": "message", "text": "9 to 5"}))
        scores = await score(record, scenario)
        assert "flow.phase_order" not in scores and "tools.forbidden_not_called" not in scores
        assert scores["conversation.agent_replied"].status is ScoreStatus.PASSED

    async def test_a_chatbot_on_defaults_can_be_safe_to_release(self, tmp_path: Path) -> None:
        # Before, `flow.phase_order` was always "not measured" here, so never "Safe to release".
        cat = tmp_path / "cat"
        (cat / "scenarios").mkdir(parents=True)
        (cat / "suites").mkdir()
        (cat / "profile.yaml").write_text(
            "name: chat\nkinds: {message: message}\nphase_field: null\n", encoding="utf-8"
        )
        (cat / "suites" / "smoke.yaml").write_text(
            'name: smoke\ninclude: ["scenarios/*.yaml"]\n', encoding="utf-8"
        )
        (cat / "scenarios" / "faq.yaml").write_text(
            "id: faq-001\nname: hours\ngoal: Learn the hours.\npersona: {role: customer}\n"
            "conversation: {scripted_turns: ['When are you open?']}\nexecution: {repeats: 1}\n",
            encoding="utf-8",
        )

        def agent(_frame: dict[str, Any]) -> Any:
            return {"type": "message", "text": "We open at 9."}

        result = await launch(
            cat / "suites" / "smoke.yaml",
            RunOptions(
                profile=cat / "profile.yaml",
                transport_factory=lambda p: PythonTransport(lambda: agent, p),
            ),
            Settings(_env_file=None, allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "r")),
        )
        assert result.exit_code == 0 and result.safe_to_release, result.gate.summary


class TestStopAtFollowsTheProfile:
    def test_left_out_without_a_commit_gate_it_loads(self) -> None:
        assert check_references(chat_scenario(), CHAT, REGISTRY, set(INVARIANTS)) == []

    def test_written_out_without_a_commit_gate_it_is_refused(self) -> None:
        scenario = chat_scenario(conversation={"scripted_turns": ["Hi"], "stop_at": "commit_gate"})
        errors = check_references(scenario, CHAT, REGISTRY, set(INVARIANTS))
        assert any(e.startswith("conversation.stop_at:") for e in errors)

    async def test_left_out_the_conversation_runs_to_its_goal(self) -> None:
        record = await attempt(chat_scenario(), say())
        assert record.terminal_state is TerminalState.GOAL_REACHED


class TestASilentAgentFails:
    """I10: an agent that says nothing must fail, not pass every frame check."""

    @pytest.mark.parametrize(
        "reply",
        [
            {"type": "message", "text": ""},
            {"type": "message", "text": "   \n"},
            {"type": "message", "text": None},
        ],
    )
    async def test_an_empty_answer_fails(self, reply: dict[str, Any]) -> None:
        scenario = chat_scenario()
        record = await attempt(scenario, say(reply, {"type": "message", "text": "ok"}))
        replied = (await score(record, scenario))["conversation.agent_replied"]
        assert replied.status is ScoreStatus.FAILED
        assert replied.details["unanswered"] == [1] and replied.details["turns"] == 2

    async def test_a_checkpoint_or_an_error_is_an_answer(self, profile: SutProfile) -> None:
        def agent(frame: dict[str, Any]) -> Any:
            if frame.get("type") == "resume":
                return {"type": "error", "code": "x"}
            return {"type": "needs_human", "checkpoint": "proposal_approval", "payload": {}}

        scenario = make_scenario(
            conversation={"stop_at": "goal_reached"},
            scorers=[{"id": "conversation.agent_replied"}],
        )
        record = await attempt(scenario, agent, profile)
        replied = (await score(record, scenario, profile))["conversation.agent_replied"]
        assert replied.status is ScoreStatus.PASSED

    async def test_an_answer_that_never_came_fails(self) -> None:
        import asyncio

        async def agent(_frame: dict[str, Any]) -> Any:
            await asyncio.sleep(1)
            return {"type": "message", "text": "late"}

        scenario = chat_scenario(execution={"repeats": 1, "turn_timeout_seconds": 0.05})
        record = await attempt(scenario, agent)
        replied = (await score(record, scenario))["conversation.agent_replied"]
        assert record.terminal_state is TerminalState.TIMEOUT
        assert replied.status is ScoreStatus.FAILED

    async def test_nothing_sent_is_vacuous(self) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        record = RunRecord(
            run_id="r",
            test_id="t",
            attempt_index=0,
            seed=1,
            target="x",
            started_at=now,
            ended_at=now,
            terminal_state=TerminalState.GOAL_REACHED,
        )
        result = await REGISTRY["conversation.agent_replied"]().score(
            record,
            chat_scenario().scorers[0] if isinstance(chat_scenario().scorers, list) else _spec(),
            ScoringContext(scenario=chat_scenario(), profile=CHAT),
        )
        assert result.status is ScoreStatus.PASSED and result.details["vacuous"] is True


def _spec() -> Any:
    from opseval.models.scenario import ScorerSpec

    return ScorerSpec(id="conversation.agent_replied")


class TestParamsAreCheckedAtLoad:
    """I15: a typo or a bad value in params fails validate, not the run."""

    def problems(self, scorers: list[dict[str, Any]]) -> list[str]:
        scenario = chat_scenario(scorers=scorers)
        return check_references(scenario, CHAT, REGISTRY, set(INVARIANTS))

    def test_a_typo_is_refused(self) -> None:
        errors = self.problems([{"id": "conversation.turn_budget", "params": {"budjet": 1}}])
        assert any(
            e.startswith("scorers.0.params.budjet: conversation.turn_budget:") for e in errors
        )

    def test_a_bad_value_is_refused(self) -> None:
        errors = self.problems([{"id": "output.language_script", "params": {"script": "cjk"}}])
        assert any("scorers.0.params.script" in e for e in errors)

    def test_yaml_strings_are_not_read_as_booleans(self) -> None:
        # At run time bool("false") is True: strict params keep load and run in agreement.
        errors = self.problems([{"id": "flow.phase_order", "params": {"allow_gaps": "false"}}])
        assert any("scorers.0.params.allow_gaps" in e for e in errors)

    def test_required_params_are_required(self) -> None:
        errors = self.problems([{"id": "output.payload_equals", "params": {"checkpoint": "x"}}])
        assert {e.split(":")[0] for e in errors} >= {
            "scorers.0.params.path",
            "scorers.0.params.equals",
        }

    def test_a_scorer_that_takes_none_refuses_any(self) -> None:
        errors = self.problems([{"id": "conversation.no_errors", "params": {"x": 1}}])
        assert any(e.startswith("scorers.0.params.x:") for e in errors)

    def test_valid_params_pass(self) -> None:
        assert (
            self.problems(
                [
                    {"id": "conversation.turn_budget", "params": {"budget": 3}},
                    {"id": "flow.phase_order", "params": {"allow_gaps": True}},
                    {"id": "conversation.connection_stable", "params": {"ignore_codes": [1001]}},
                ]
            )
            == []
        )

    def test_the_loader_checks_params_when_given_the_registry(self, tmp_path: Path) -> None:
        path = tmp_path / "s.yaml"
        path.write_text(
            "id: s\nname: s\ngoal: g\npersona: {role: c}\n"
            "conversation: {scripted_turns: [hi]}\n"
            "scorers: [{id: conversation.turn_budget, params: {budjet: 1}}]\n",
            encoding="utf-8",
        )
        cat, _ = load_catalogue(path, CHAT, known_scorers=REGISTRY, known_invariants=set())
        assert any(p.field == "scorers.0.params.budjet" for p in cat.problems)
        assert cat.scenarios == []

    def test_every_built_in_declares_its_params(self) -> None:
        undeclared = [sid for sid, cls in REGISTRY.items() if cls.Params is None]
        assert undeclared == []

    def test_a_plugin_without_a_params_model_is_unchecked_and_said_so(self) -> None:
        class Plugin(Scorer):
            id = "acme.plugin"
            axis = REGISTRY["conversation.no_errors"].axis
            description = "a plugin"

            async def score(self, record: Any, spec: Any, ctx: Any) -> Any:  # pragma: no cover
                raise NotImplementedError

        REGISTRY["acme.plugin"] = Plugin
        try:
            scenario = chat_scenario(scorers=[{"id": "acme.plugin", "params": {"any": 1}}])
            assert check_references(scenario, CHAT, REGISTRY, set(INVARIANTS)) == []
            warnings = scenario_warnings(scenario, "s.yaml", CHAT)
            assert any(w.field == "scorers.0.params" for w in warnings)
            assert describe_params(Plugin) is None
        finally:
            del REGISTRY["acme.plugin"]

    def test_params_are_described(self) -> None:
        assert describe_params(REGISTRY["output.payload_equals"]) == "checkpoint*, path*, equals*"
        assert describe_params(REGISTRY["conversation.no_errors"]) == ""
        assert issubclass(REGISTRY["judge.goal_achievement"].Params or BaseModel, NoParams)


class TestARefusalScenario:
    def ctx(self, kind: str) -> ScoringContext:
        scenario = chat_scenario(expectations={"kind": kind})
        return ScoringContext(scenario=scenario, profile=CHAT)

    def test_the_judges_are_told(self) -> None:
        judge = GoalAchievement()
        record = RunRecord.model_validate(
            {
                "run_id": "r",
                "test_id": "t",
                "attempt_index": 0,
                "seed": 1,
                "target": "x",
                "started_at": "2026-10-21T00:00:00Z",
                "ended_at": "2026-10-21T00:00:00Z",
                "terminal_state": "goal_reached",
            }
        )
        assert any("REFUSAL" in line for line in judge.context(record, self.ctx("refusal")))
        assert not any("REFUSAL" in line for line in judge.context(record, self.ctx("completion")))

    def test_only_a_refusal_changes_the_rubric(self) -> None:
        judge = GoalAchievement()
        completion = judge.rubric(self.ctx("completion"))
        assert completion == ("Learn the opening hours.", "")  # unchanged: old scores reusable
        assert judge.rubric(self.ctx("refusal")) == (*completion, "kind=refusal")


class TestRunningOutOfScriptedTurns:
    async def test_it_is_noted_and_said_for_what_it_is(self) -> None:
        scenario = chat_scenario(
            conversation={"scripted_turns": ["Hi"], "stop_at": "goal_reached"},
            scorers=[{"id": "output.terminal_state"}],
        )
        record = await attempt(scenario, say())
        assert record.terminal_state is TerminalState.GOAL_REACHED
        assert (
            SCRIPT_EXHAUSTED in record.notes and record.stop_reason == "the scripted turns ran out"
        )
        terminal = (await score(record, scenario))["output.terminal_state"]
        assert terminal.status is ScoreStatus.PASSED
        assert "not that the customer's goal was met" in terminal.details["note"]
