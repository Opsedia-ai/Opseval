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
from opseval.preflight import llm_roles_needed
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


# --- review of #71 ------------------------------------------------------------------------


class _Streaming:
    """A streaming session (replies_in_send False) whose agent answers late after a typing frame."""

    def __init__(self, script: list[list[tuple[float, dict[str, Any]]]]) -> None:
        import asyncio

        from opseval.transport.base import Session

        self.script = list(script)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outer = self

        class S(Session):
            async def send(self, frame: dict[str, Any], *, timeout: float | None = None) -> None:
                for delay, out in outer.script.pop(0) if outer.script else []:
                    asyncio.get_running_loop().call_later(delay, outer.queue.put_nowait, out)

            async def receive(self, timeout: float) -> Any:
                from opseval.errors import TransportTimeout

                try:
                    return await asyncio.wait_for(outer.queue.get(), timeout)
                except TimeoutError as exc:
                    raise TransportTimeout("quiet") from exc

            async def close(self) -> None:
                return None

        self.session = S()

    @property
    def target(self) -> str:
        return "stream:test"

    async def connect(self) -> Any:
        return self.session


TYPING = SutProfile.model_validate(
    {
        "name": "ws",
        "kinds": {"message": "message", "typing": "notification"},
        "phase_field": None,
        "turn_quiet_ms": 100,
    }
)


async def drive_stream(scenario: Scenario, transport: Any, profile: SutProfile = TYPING) -> Any:
    record = await Driver(
        scenario=scenario,
        profile=profile,
        transport=transport,
        simulator=ScriptedSimulator(scenario, 1),
        run_id="r",
        attempt_index=0,
        seed=1,
    ).run()
    return record, await score(record, scenario, profile)


class TestReviewOf71:
    async def test_a_reply_after_a_typing_frame_is_the_turns_answer(self) -> None:
        # I12: the quiet timer (100 ms) used to end the turn at the typing frame, so the
        # reply 300 ms later was filed under the next turn and agent_replied failed turn 1.
        typing = {"type": "typing", "text": "…"}
        transport = _Streaming(
            [
                [(0.0, typing), (0.3, {"type": "message", "text": "We open at 9."})],
                [(0.0, typing), (0.3, {"type": "message", "text": "Sundays too."})],
            ]
        )
        record, scores = await drive_stream(chat_scenario(), transport)
        assert scores["conversation.agent_replied"].status is ScoreStatus.PASSED
        order = [(f.direction, f.text) for f in record.frames if f.text and f.text != "…"]
        assert order == [
            ("sent", "When are you open?"),
            ("received", "We open at 9."),
            ("sent", "And on Sundays?"),
            ("received", "Sundays too."),
        ]

    async def test_a_typing_frame_alone_is_not_an_answer(self) -> None:
        # Streaming: the driver waits for an answer until the turn's deadline (TIMEOUT).
        transport = _Streaming([[(0.0, {"type": "typing", "text": "Agent is thinking..."})]])
        scenario = chat_scenario(execution={"repeats": 1, "turn_timeout_seconds": 0.3})
        record, scores = await drive_stream(scenario, transport)
        assert record.terminal_state is TerminalState.TIMEOUT
        assert scores["conversation.agent_replied"].status is ScoreStatus.FAILED

    async def test_a_notification_with_text_does_not_answer_on_http_either(self) -> None:
        scenario = chat_scenario()
        record = await attempt(scenario, say({"type": "typing", "text": "thinking..."}), TYPING)
        replied = (await score(record, scenario, TYPING))["conversation.agent_replied"]
        assert replied.status is ScoreStatus.FAILED

    async def test_a_reply_that_is_only_a_credential_was_an_answer(self) -> None:
        scenario = chat_scenario(conversation={"scripted_turns": ["My new key?"]})
        record = await attempt(
            scenario, say({"type": "message", "text": "sk-proj-abcdefghij1234567890"})
        )
        assert record.frames[1].text == "[redacted:openai_key]"
        replied = (await score(record, scenario))["conversation.agent_replied"]
        assert replied.status is ScoreStatus.PASSED

    async def test_a_reply_of_only_tool_calls_was_an_answer(self) -> None:
        profile = SutProfile.model_validate(
            {**CHAT.model_dump(), "tool_calls_field": "tool_calls", "tools": {"lookup": {}}}
        )
        scenario = chat_scenario(conversation={"scripted_turns": ["Find order 1"]})
        reply = {"type": "message", "text": "", "data": {"tool_calls": [{"name": "lookup"}]}}
        record = await attempt(scenario, say(reply), profile)
        replied = (await score(record, scenario, profile))["conversation.agent_replied"]
        assert replied.status is ScoreStatus.PASSED

    def test_a_committing_tool_needs_a_commit_gate(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="commit an irreversible outcome"):
            SutProfile.model_validate({**CHAT.model_dump(), "tools": {"pay": {"commits": True}}})

    async def test_a_null_param_means_left_out_at_run_time_too(self) -> None:
        scenario = chat_scenario(
            scorers=[
                {"id": "conversation.turn_budget", "params": {"budget": None}},
                {
                    "id": "output.language_script",
                    "params": {"script": "latin", "min_fraction": None},
                },
            ]
        )
        assert check_references(scenario, CHAT, REGISTRY, set(INVARIANTS)) == []
        record = await attempt(scenario, say({"type": "message", "text": "We open at nine."}))
        scores = await score(record, scenario)
        assert scores["conversation.turn_budget"].status is ScoreStatus.PASSED
        assert scores["output.language_script"].status is ScoreStatus.PASSED

    def test_a_checkpoint_named_in_params_must_exist(self, profile: SutProfile) -> None:
        scenario = make_scenario(
            scorers=[
                {
                    "id": "output.payload_equals",
                    "params": {"checkpoint": "proposal_aproval", "path": "x", "equals": 1},
                }
            ]
        )
        errors = check_references(scenario, profile, REGISTRY, set(INVARIANTS))
        assert any(
            e.startswith("scorers.0.params.checkpoint: output.payload_equals:") for e in errors
        )

    async def test_a_tool_missing_from_frames_is_not_measured(self) -> None:
        profile = SutProfile.model_validate(
            {
                **CHAT.model_dump(),
                "tool_calls_field": "tool_calls",
                "tools": {"search": {}, "b": {}},
            }
        )
        scenario = chat_scenario(
            expectations={
                "expected_tools": {
                    "must_call": ["search"],
                    "ordering": [{"before": "search", "after": "b"}],
                }
            }
        )
        reply = {"type": "message", "text": "Here.", "data": {"tool_calls": [{"name": "b"}]}}
        record = await attempt(scenario, say(reply, reply), profile)
        scores = await score(record, scenario, profile)
        # The agent may have searched on its own server: absence in frames is not proof.
        assert scores["tools.expected_called"].status is ScoreStatus.INFRA_ERROR
        assert scores["tools.ordering"].status is ScoreStatus.INFRA_ERROR
        complete = SutProfile.model_validate({**profile.model_dump(), "tool_calls_complete": True})
        scores = await score(record, scenario, complete)
        assert scores["tools.expected_called"].status is ScoreStatus.FAILED
        assert scores["tools.ordering"].status is ScoreStatus.FAILED

    def test_a_plugin_params_that_is_not_a_model_is_refused(self) -> None:
        from opseval.errors import ConfigError
        from opseval.plugins import check_contract

        class Odd(Scorer):
            id = "acme.odd"
            axis = REGISTRY["conversation.no_errors"].axis

            class Params:  # not a pydantic model
                pass

            async def score(self, record: Any, spec: Any, ctx: Any) -> Any:  # pragma: no cover
                raise NotImplementedError

        with pytest.raises(ConfigError, match="must be a pydantic model"):
            check_contract(Odd, source="acme")

    def test_a_plugin_judge_keeps_its_own_params(self) -> None:
        from opseval.plugins import check_contract
        from opseval.scoring.judges import Judge

        class MyJudge(Judge):
            id = "acme.judge"
            axis = REGISTRY["judge.goal_achievement"].axis
            instructions = "x"
            criteria = ()

        assert check_contract(MyJudge, source="acme").Params is None  # unchecked, as before

    def test_only_the_goal_judge_reads_refusal(self) -> None:
        from opseval.scoring.judges import Efficiency

        refusal = ScoringContext(
            scenario=chat_scenario(expectations={"kind": "refusal"}), profile=CHAT
        )
        completion = ScoringContext(scenario=chat_scenario(), profile=CHAT)
        assert Efficiency().rubric(refusal) == Efficiency().rubric(completion)
        assert GoalAchievement.version == "3"

    def test_rescore_never_reuses_a_refusal_graded_the_old_way(self) -> None:
        from opseval.models.results import Axis, ScoreResult
        from opseval.models.scenario import ScorerSpec
        from opseval.rescore import _Rescorer

        judge = GoalAchievement()
        spec = ScorerSpec(id=judge.id)

        def graded(rubric: str | None) -> ScoreResult:
            details: dict[str, Any] = {"prompt_hash": judge.prompt_hash()}
            if rubric:
                details["rubric_hash"] = rubric
            return ScoreResult(
                scorer_id=judge.id,
                axis=Axis.OUTPUT,
                status=ScoreStatus.PASSED,
                score=1.0,
                threshold=0.75,
                details=details,
            )

        def fresh(ctx: ScoringContext) -> ScoreResult:
            return ScoreResult(
                scorer_id=judge.id,
                axis=Axis.OUTPUT,
                status=ScoreStatus.INFRA_ERROR,
                score=None,
                threshold=0.75,
                reason="JUDGE_UNAVAILABLE: x",
                details={"rubric_hash": judge.rubric_hash(ctx)},
            )

        refusal = ScoringContext(
            scenario=chat_scenario(expectations={"kind": "refusal"}), profile=CHAT
        )
        completion = ScoringContext(scenario=chat_scenario(), profile=CHAT)
        old_digest = judge.rubric_hash(completion)  # what a refusal scenario got before
        rescorer = _Rescorer(ScoringEngine(), judge_configured=False, scenario_source="snapshot")
        kept = rescorer._reuse(fresh(completion), graded(old_digest), spec)
        refused = rescorer._reuse(fresh(refusal), graded(old_digest), spec)
        assert kept.status is ScoreStatus.PASSED and kept.details.get("reused") is True
        assert refused.status is ScoreStatus.INFRA_ERROR
        assert "how the judge reads this scenario changed" in (refused.reason or "")

    def test_rehearsing_past_the_script_says_it_ran_out(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from opseval.demo import catalogue_dir
        from opseval.server.app import create_app

        from .test_server import AUTH, BASE, TOKEN

        settings = Settings(_env_file=None, reports_dir=str(tmp_path))
        app = create_app(
            settings=settings,
            catalogue=catalogue_dir(),
            profile=catalogue_dir() / "profile.yaml",
            token=TOKEN,
            read_only=True,
        )
        client = TestClient(app, base_url=BASE, headers=AUTH)
        turns = [{"who": "customer", "text": "hi"}]
        for _ in range(10):
            turns.append({"who": "agent", "text": "ok"})
            out = client.post(
                "/api/v1/simulator/rehearse", json={"scenarioId": "rf-001", "transcript": turns}
            ).json()
            if out.get("scriptExhausted"):
                break
            turns.append(out["turn"])
        assert out["scriptExhausted"] is True and out["goalReached"] is False

    async def test_a_wrong_mapping_on_the_default_pack_is_not_judged(self) -> None:
        # Fault injection, the harness half: the profile reads nothing, so agent_replied also
        # fails, but the attempt is invalidated (protocol.replies_read), never failed.
        from opseval.verdicts import attempt_verdict

        scenario = chat_scenario()
        record = await attempt(scenario, say({"reply": "We open at 9."}, {"reply": "Yes."}))
        scores = list((await score(record, scenario)).values())
        verdict = attempt_verdict(record, scores, "x")
        assert {s.scorer_id: s.status for s in scores}["conversation.agent_replied"] is (
            ScoreStatus.FAILED
        )
        assert verdict.passed is None


@pytest.mark.parametrize(
    ("expectations", "judge"),
    [
        ({"kind": "refusal"}, "judge.goal_achievement"),
        ({"policy": ["Never share another customer's data."]}, "security.policy_adherence"),
    ],
)
def test_a_declared_refusal_or_policy_is_graded_under_the_default_pack(
    profile: SutProfile, expectations: dict[str, Any], judge: str
) -> None:
    # ADR-0048: written down and never read, an agent doing the opposite passed.
    sc = make_scenario(scorers="default", expectations=expectations)
    assert judge in default_pack(sc, profile)
    with_profile = {x.id for x in effective_specs(sc, profile)}
    without = {x.id for x in effective_specs(sc)}
    assert judge in with_profile and judge in without, "preflight counts it too"
    assert "judge" in llm_roles_needed([sc]), "so the judge client is built for the run"
    plain = make_scenario(scorers="default")
    assert not {"judge.goal_achievement", "security.policy_adherence"} & set(
        default_pack(plain, profile)
    ), "a completion scenario still runs in CI for free"
