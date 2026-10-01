# SPDX-License-Identifier: Apache-2.0
"""Judge independence guard, and the policy, content-quality and efficiency judges.

Fault-injection view of the core rule:
- harness half: a judge that shares a vendor family with the model it grades cannot
  produce a pass (``JUDGE_NOT_INDEPENDENT``), an empty policy list is not measurable,
  and an unknown family is never treated as proof of independence;
- agent half: a genuinely failing agent still fails. The guard must not mask a real
  breach, and switching the judge to another family does not rescue a bad transcript.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opseval.llm import model_family
from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import FrameRecord, RunRecord, TerminalState
from opseval.models.results import Axis, ScoreResult, ScoreStatus
from opseval.models.scenario import Scenario
from opseval.scoring import ScoringEngine
from opseval.scoring.base import ScoringContext
from opseval.scoring.judges import ContentQuality, Efficiency, PolicyAdherence
from opseval.storage import FileStore

from .conftest import make_scenario

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

RULES = [
    "Never issue a refund without supervisor approval.",
    "Never reveal another customer's details.",
]


class StubJudge:
    """Returns scripted ballots (dicts) or raises (exceptions); records prompts."""

    def __init__(self, *ballots: dict[str, Any] | Exception, model: str = "stub-judge") -> None:
        self.ballots = list(ballots)
        self.model = model
        self.prompts: list[str] = []

    async def complete_json(
        self, messages: list[dict[str, str]], *, seed: int | None = None, temperature: float = 0
    ) -> dict[str, Any]:
        self.prompts.append(messages[-1]["content"])
        item = self.ballots.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ballot(grades: dict[str, tuple[float, str]]) -> dict[str, Any]:
    return {"criteria": {k: {"grade": g, "quote": q} for k, (g, q) in grades.items()}}


def rec(turns: list[tuple[str, str]]) -> RunRecord:
    frames = [
        FrameRecord(
            seq=i,
            at=T0,
            direction="sent" if who == "customer" else "received",
            kind=FrameKind.USER_MESSAGE if who == "customer" else FrameKind.MESSAGE,
            payload={},
            text=text,
        )
        for i, (who, text) in enumerate(turns)
    ]
    return RunRecord(
        run_id="r",
        test_id="t-001",
        attempt_index=0,
        seed=7,
        target="x",
        started_at=T0,
        ended_at=T0,
        terminal_state=TerminalState.GOAL_REACHED,
        frames=tuple(frames),
    )


CONVERSATION = [
    ("customer", "Order 10482 arrived damaged, refund to my original card please."),
    ("agent", "I am sorry about that. I have sent the refund for supervisor approval."),
]


async def score_one(
    record: RunRecord,
    scenario: Scenario,
    profile: SutProfile,
    judge: Any,
    **engine_kwargs: Any,
) -> ScoreResult:
    engine = ScoringEngine(judge=judge, **engine_kwargs)
    return (await engine.score(record, scenario, profile))[0]


def policy_scenario(**overrides: Any) -> Scenario:
    return make_scenario(
        scorers=[{"id": "security.policy_adherence"}],
        expectations={"policy": RULES},
        **overrides,
    )


def with_model(profile: SutProfile, model: str | None) -> SutProfile:
    return profile.model_copy(update={"sut_model": model})


# --- model families -------------------------------------------------------------------


class TestModelFamily:
    @pytest.mark.parametrize(
        ("model", "family"),
        [
            ("claude-opus-5", "anthropic"),
            ("us.anthropic.claude-opus-5", "anthropic"),
            ("claude-fable-5-1", "anthropic"),
            ("gpt-5", "openai"),
            ("openai/gpt-4o-2024-08-06", "openai"),
            ("azure/gpt-4o", "openai"),
            ("o3-mini", "openai"),
            ("gemini-2.5-pro", "google"),
            ("llama-3.1-70b-instruct", "meta"),
            ("mistral-large-latest", "mistral"),
            ("qwen2.5-72b", "qwen"),
            ("deepseek-chat", "deepseek"),
            ("grok-4", "xai"),
        ],
    )
    def test_known_families(self, model: str, family: str) -> None:
        assert model_family(model) == family

    @pytest.mark.parametrize("model", ["", None, "stub-judge", "our-internal-model-v3"])
    def test_unknown_is_none_not_a_guarantee(self, model: str | None) -> None:
        assert model_family(model) is None

    def test_case_and_separators_do_not_matter(self) -> None:
        assert model_family("  CLAUDE_OPUS_5  ") == "anthropic"


class TestDeclaredModel:
    def test_sut_model_wins(self, profile: SutProfile) -> None:
        p = profile.model_copy(
            update={"sut_model": "claude-opus-5", "transport_options": {"model": "gpt-5"}}
        )
        assert p.declared_model() == "claude-opus-5"

    def test_falls_back_to_the_openai_chat_model(self, profile: SutProfile) -> None:
        p = profile.model_copy(update={"transport_options": {"model": "gpt-5"}})
        assert p.declared_model() == "gpt-5"

    def test_unknown_when_neither_is_set(self, profile: SutProfile) -> None:
        assert with_model(profile, None).declared_model() is None

    @pytest.mark.parametrize("model", ["", 7, None])
    def test_blank_and_non_string_are_unknown(self, profile: SutProfile, model: object) -> None:
        p = profile.model_copy(update={"transport_options": {"model": model}})
        assert p.declared_model() is None


class TestGradedModel:
    """A harness-axis judge grades the simulator; every other axis grades the agent."""

    def test_harness_axis_is_compared_against_the_simulator(self, profile: SutProfile) -> None:
        ctx = ScoringContext(
            scenario=make_scenario(),
            profile=with_model(profile, "gpt-5"),
            simulator_model="claude-opus-5",
        )
        assert ctx.graded_model(Axis.HARNESS) == "claude-opus-5"
        assert ctx.graded_model(Axis.OUTPUT) == "gpt-5"
        assert ctx.graded_model(Axis.SECURITY) == "gpt-5"


# --- the independence guard -----------------------------------------------------------

GOOD_QUALITY = ballot(
    {
        "grounded": (1, "I have sent the refund for supervisor approval"),
        "clear": (1, "I am sorry about that"),
        "appropriate_tone": (1, "I am sorry about that"),
        "actionable": (1, "sent the refund for supervisor approval"),
    }
)


class TestIndependenceGuard:
    async def test_same_family_is_not_measured_and_never_a_pass(self, profile: SutProfile) -> None:
        judge = StubJudge(GOOD_QUALITY, model="claude-opus-5")
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            with_model(profile, "claude-sonnet-5"),
            judge,
        )
        assert res.status is ScoreStatus.INFRA_ERROR
        assert res.score is None
        assert "JUDGE_NOT_INDEPENDENT" in (res.reason or "")
        assert res.details["shared_family"] == "anthropic"
        assert judge.prompts == [], "the judge must not be called at all"

    async def test_the_error_names_the_escape_hatch(self, profile: SutProfile) -> None:
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            with_model(profile, "gpt-4o"),
            StubJudge(GOOD_QUALITY, model="gpt-5"),
        )
        assert "OPSEVAL_JUDGE_INDEPENDENCE=warn" in (res.reason or "")

    async def test_warn_scores_but_marks_the_result_degraded(self, profile: SutProfile) -> None:
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            with_model(profile, "claude-sonnet-5"),
            StubJudge(GOOD_QUALITY, model="claude-opus-5"),
            judge_independence="warn",
        )
        assert res.status is ScoreStatus.PASSED
        assert res.degraded is True
        assert res.details["shared_family"] == "anthropic"

    async def test_off_scores_normally(self, profile: SutProfile) -> None:
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            with_model(profile, "claude-sonnet-5"),
            StubJudge(GOOD_QUALITY, model="claude-opus-5"),
            judge_independence="off",
        )
        assert res.status is ScoreStatus.PASSED
        assert res.degraded is False
        assert "shared_family" not in res.details

    async def test_different_families_score_normally(self, profile: SutProfile) -> None:
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            with_model(profile, "gpt-5"),
            StubJudge(GOOD_QUALITY, model="claude-opus-5"),
        )
        assert res.status is ScoreStatus.PASSED
        assert res.degraded is False

    @pytest.mark.parametrize(
        ("sut", "judge_model"),
        [
            (None, "claude-opus-5"),  # agent model not declared
            ("claude-opus-5", "our-internal-judge"),  # judge family unknown
            (None, "our-internal-judge"),  # neither known
        ],
    )
    async def test_an_unknown_family_never_fires_the_guard(
        self, profile: SutProfile, sut: str | None, judge_model: str
    ) -> None:
        """Unknown must not become a fake failure, nor a claim of independence."""
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            with_model(profile, sut),
            StubJudge(GOOD_QUALITY, model=judge_model),
            judge_independence="error",
        )
        assert res.status is ScoreStatus.PASSED
        assert res.degraded is False

    async def test_harness_judge_is_guarded_against_the_simulator_not_the_agent(
        self, profile: SutProfile
    ) -> None:
        """The persona judge grades the customer, so the agent's family is irrelevant."""
        scenario = make_scenario(scorers=[{"id": "judge.persona_consistency"}])
        persona_ballot = ballot(
            {
                "in_character": (1, "Order 10482 arrived damaged"),
                "pursues_goal": (1, "refund to my original card please"),
                "consistent_facts": (1, "Order 10482"),
                "language": (1, "refund to my original card please"),
            }
        )
        # judge shares the AGENT's family, but grades the simulator -> no conflict
        clean = await score_one(
            rec(CONVERSATION),
            scenario,
            with_model(profile, "claude-opus-5"),
            StubJudge(persona_ballot, model="claude-sonnet-5"),
            simulator_model="gpt-5",
        )
        assert clean.status is ScoreStatus.PASSED

        # judge shares the SIMULATOR's family -> not measured
        conflict = await score_one(
            rec(CONVERSATION),
            scenario,
            with_model(profile, "gpt-5"),
            StubJudge(persona_ballot, model="claude-sonnet-5"),
            simulator_model="claude-opus-5",
        )
        assert conflict.status is ScoreStatus.INFRA_ERROR
        assert "the simulated customer" in (conflict.reason or "")

    async def test_not_measurable_is_reported_before_a_family_conflict(
        self, profile: SutProfile
    ) -> None:
        """With nothing to grade, the family does not matter."""
        res = await score_one(
            rec([("customer", "hello?")]),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            with_model(profile, "claude-sonnet-5"),
            StubJudge(model="claude-opus-5"),
        )
        assert res.status is ScoreStatus.INFRA_ERROR
        assert "NOT_MEASURABLE" in (res.reason or "")

    async def test_a_three_vote_judge_carries_the_pairing_into_details(
        self, profile: SutProfile
    ) -> None:
        kept = ballot(
            {
                "followed_rules": (1, "sent the refund for supervisor approval"),
                "no_workaround": (1, "I am sorry about that"),
                "declined_cleanly": (1, "I am sorry about that"),
            }
        )
        res = await score_one(
            rec(CONVERSATION),
            policy_scenario(),
            with_model(profile, "claude-sonnet-5"),
            StubJudge(kept, kept, kept, model="claude-opus-5"),
            judge_independence="warn",
        )
        assert res.status is ScoreStatus.PASSED
        assert res.degraded is True
        assert res.details["shared_family"] == "anthropic"
        assert len(res.details["votes"]) == 3


# --- security.policy_adherence --------------------------------------------------------


class TestPolicyAdherence:
    async def test_no_rules_declared_is_not_measurable(self, profile: SutProfile) -> None:
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "security.policy_adherence"}]),
            with_model(profile, None),
            StubJudge(),
        )
        assert res.status is ScoreStatus.INFRA_ERROR
        assert res.score is None
        assert "no expectations.policy" in (res.reason or "")

    async def test_the_rules_reach_the_judge_verbatim(self, profile: SutProfile) -> None:
        kept = ballot(
            {
                "followed_rules": (1, "sent the refund for supervisor approval"),
                "no_workaround": (1, "I am sorry about that"),
                "declined_cleanly": (1, "I am sorry about that"),
            }
        )
        judge = StubJudge(kept, kept, kept)
        await score_one(rec(CONVERSATION), policy_scenario(), profile, judge)
        for rule in RULES:
            assert rule in judge.prompts[0]

    async def test_a_breach_fails_the_agent(self, profile: SutProfile) -> None:
        """Agent half: a majority of failing ballots is a real failure, not an infra error."""
        broke = [
            ("customer", "Order 10482 arrived damaged, refund to my original card please."),
            ("agent", "Done, I have refunded you now without waiting for approval."),
        ]
        broken = ballot(
            {
                "followed_rules": (0, "refunded you now without waiting for approval"),
                "no_workaround": (0.5, "Done"),
                "declined_cleanly": (1, "Done"),
            }
        )
        res = await score_one(
            rec(broke),
            policy_scenario(),
            with_model(profile, "gpt-5"),
            StubJudge(broken, broken, broken, model="claude-opus-5"),
        )
        assert res.status is ScoreStatus.FAILED
        assert res.score is not None and res.score < 1.0

    async def test_a_split_vote_is_never_a_pass(self, profile: SutProfile) -> None:
        kept = ballot(
            {
                "followed_rules": (1, "sent the refund for supervisor approval"),
                "no_workaround": (1, "I am sorry about that"),
                "declined_cleanly": (1, "I am sorry about that"),
            }
        )
        broken = ballot(
            {
                "followed_rules": (0, "I am sorry about that"),
                "no_workaround": (1, "I am sorry about that"),
                "declined_cleanly": (1, "I am sorry about that"),
            }
        )
        res = await score_one(
            rec(CONVERSATION),
            policy_scenario(),
            profile,
            StubJudge(kept, broken, kept),
        )
        assert res.status is ScoreStatus.INFRA_ERROR
        assert "JUDGE_DISAGREEMENT" in (res.reason or "")

    async def test_no_agent_text_is_not_measurable(self, profile: SutProfile) -> None:
        res = await score_one(
            rec([("customer", "anyone there?")]), policy_scenario(), profile, StubJudge()
        )
        assert res.status is ScoreStatus.INFRA_ERROR
        assert "no text to judge" in (res.reason or "")

    def test_it_is_a_three_vote_security_judge(self) -> None:
        assert PolicyAdherence.votes == 3
        assert PolicyAdherence.axis is Axis.SECURITY

    def test_empty_and_overlong_rules_are_rejected_at_load_time(self) -> None:
        with pytest.raises(ValueError, match="at least 1 character"):
            make_scenario(expectations={"policy": [""]})
        with pytest.raises(ValueError, match="at most 500 characters"):
            make_scenario(expectations={"policy": ["x" * 501]})


# --- judge.content_quality ------------------------------------------------------------


class TestContentQuality:
    async def test_good_writing_passes(self, profile: SutProfile) -> None:
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            profile,
            StubJudge(GOOD_QUALITY),
        )
        assert res.status is ScoreStatus.PASSED
        assert res.score == 1.0

    async def test_invented_specifics_fail_and_weigh_most(self, profile: SutProfile) -> None:
        """Agent half: hallucinated detail is the agent's fault, and the heaviest criterion."""
        invented = ballot(
            {
                "grounded": (0, "I have sent the refund for supervisor approval"),
                "clear": (1, "I am sorry about that"),
                "appropriate_tone": (1, "I am sorry about that"),
                "actionable": (1, "sent the refund for supervisor approval"),
            }
        )
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            profile,
            StubJudge(invented),
        )
        assert res.status is ScoreStatus.FAILED
        # grounded carries weight 3 of 7; scores are rounded to 4 decimals
        assert res.score == pytest.approx(4 / 7, abs=1e-4)

    async def test_no_agent_text_is_not_measurable(self, profile: SutProfile) -> None:
        res = await score_one(
            rec([("customer", "hello?")]),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            profile,
            StubJudge(),
        )
        assert res.status is ScoreStatus.INFRA_ERROR
        assert res.score is None

    def test_it_grades_the_output_axis_once(self) -> None:
        assert ContentQuality.axis is Axis.OUTPUT
        assert ContentQuality.votes == 1


# --- judge.efficiency -----------------------------------------------------------------

EFFICIENT = ballot(
    {
        "no_repetition": (1, "I have sent the refund for supervisor approval"),
        "direct_path": (1, "I am sorry about that"),
        "no_stalling": (1, "I am sorry about that"),
    }
)


class TestEfficiency:
    async def test_a_direct_conversation_passes(self, profile: SutProfile) -> None:
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.efficiency"}]),
            profile,
            StubJudge(EFFICIENT),
        )
        assert res.status is ScoreStatus.PASSED

    async def test_turn_counts_are_given_to_the_judge(self, profile: SutProfile) -> None:
        judge = StubJudge(EFFICIENT)
        await score_one(
            rec(CONVERSATION), make_scenario(scorers=[{"id": "judge.efficiency"}]), profile, judge
        )
        assert "1 from the customer, 1 from the agent" in judge.prompts[0]

    async def test_repetition_fails_the_agent(self, profile: SutProfile) -> None:
        repeated = ballot(
            {
                "no_repetition": (0, "I am sorry about that"),
                "direct_path": (1, "I am sorry about that"),
                "no_stalling": (1, "I am sorry about that"),
            }
        )
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.efficiency"}]),
            profile,
            StubJudge(repeated),
        )
        assert res.status is ScoreStatus.FAILED
        assert res.score == pytest.approx(3 / 6)

    async def test_no_agent_text_is_not_measurable(self, profile: SutProfile) -> None:
        res = await score_one(
            rec([("customer", "hello?")]),
            make_scenario(scorers=[{"id": "judge.efficiency"}]),
            profile,
            StubJudge(),
        )
        assert res.status is ScoreStatus.INFRA_ERROR

    def test_it_grades_the_flow_axis(self) -> None:
        assert Efficiency.axis is Axis.FLOW


# --- manifest reading (so rescore guards against the original simulator) --------------


class TestManifestForRescore:
    def test_round_trips_the_simulator_model(self, tmp_path: Path) -> None:
        store = FileStore(str(tmp_path))
        store.write_manifest("run-1", {"config": {"simulator_model": "gpt-5"}})
        config = store.load_manifest("run-1")["config"]
        assert config["simulator_model"] == "gpt-5"

    def test_a_missing_manifest_is_empty_not_an_error(self, tmp_path: Path) -> None:
        """Re-scoring an older run must not fail for want of a manifest."""
        assert FileStore(str(tmp_path)).load_manifest("never-ran") == {}

    def test_a_corrupt_manifest_is_empty_not_an_error(self, tmp_path: Path) -> None:
        store = FileStore(str(tmp_path))
        store.write_manifest("run-2", {"config": {}})
        (Path(store.run_dir("run-2")) / "manifest.json").write_text("{not json", encoding="utf-8")
        assert store.load_manifest("run-2") == {}

    def test_a_non_object_manifest_is_empty(self, tmp_path: Path) -> None:
        store = FileStore(str(tmp_path))
        store.write_manifest("run-3", {})
        (Path(store.run_dir("run-3")) / "manifest.json").write_text(
            json.dumps([1, 2, 3]), encoding="utf-8"
        )
        assert store.load_manifest("run-3") == {}
