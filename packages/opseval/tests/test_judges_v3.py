# SPDX-License-Identifier: Apache-2.0
"""Judge independence, and the policy, content-quality and efficiency judges.

Fault-injection view of the core rule:
- harness half: a judge sharing a vendor family with the model it grades is refused at
  *launch* (ConfigError, exit 3) and is never turned into ``infra_error`` during scoring,
  because an ``infra_error`` score is dropped from the measured set and would let a suite
  pass with the judge silently gone. An empty policy list is not measurable, and an
  unknown family is never read as proof of independence;
- agent half: a genuinely failing agent still fails. Neither the guard nor an
  independent judge rescues a bad transcript.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opseval.catalogue import check_references
from opseval.errors import ConfigError
from opseval.llm import model_family
from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import FrameRecord, RunRecord, TerminalState, ToolCall
from opseval.models.results import Axis, ScoreResult, ScoreStatus
from opseval.models.scenario import Scenario
from opseval.scoring import REGISTRY, ScoringEngine, effective_specs
from opseval.scoring.base import (
    ScoringContext,
    check_judge_independence,
    independence_conflicts,
)
from opseval.scoring.judges import ContentQuality, Efficiency, PolicyAdherence
from opseval.storage import FileStore
from opseval.verdicts import attempt_verdict

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
    base: dict[str, Any] = {
        "scorers": [{"id": "security.policy_adherence"}],
        "expectations": {"policy": RULES},
    }
    return make_scenario(**{**base, **overrides})


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
            ("o3", "openai"),
            ("o1", "openai"),
            # an "openai/" prefix is the API dialect, not the vendor
            ("openai/deepseek-chat", "deepseek"),
            ("openai/qwen-max", "qwen"),
            ("openai/command-r", "cohere"),
            # a vendor named only in a deployment prefix
            ("us.anthropic.brand-new-id", "anthropic"),
            ("mistral/some-new-id", "mistral"),
            # the base vendor wins over the distillation target
            ("deepseek-r1-distill-llama-70b", "deepseek"),
            # a dotted *version* is not a deployment prefix
            ("gemini-2.5-pro", "google"),
            ("claude-3.5-sonnet", "anthropic"),
            # a dotted prefix naming another vendor does not outrank the id
            ("deepseek.qwen-7b", "qwen"),
            ("mistral.cohere-command-r", "cohere"),
            # short names are anchored to the first token
            ("phi-4", "microsoft"),
            ("nova-pro", "amazon"),
            ("command-r", "cohere"),
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

    @pytest.mark.parametrize(
        "model", ["", None, "stub-judge", "our-internal-model-v3", "openai/our-internal-model"]
    )
    def test_unknown_is_none_not_a_guarantee(self, model: str | None) -> None:
        assert model_family(model) is None

    @pytest.mark.parametrize(
        "model",
        [
            "sophia-1",
            "delphi-1",
            "graphite-7b",
            "seraphim",
            "casanova",
            "innovate-7b",
            "alpha-nova",
        ],
    )
    def test_short_vendor_names_do_not_match_inside_other_words(self, model: str) -> None:
        """ "phi" and "nova" are substrings of unrelated names; matching them would refuse
        a genuinely independent pairing with exit 3."""
        assert model_family(model) is None

    def test_case_and_separators_do_not_matter(self) -> None:
        assert model_family("  CLAUDE_OPUS_5  ") == "anthropic"


class TestDeclaredModel:
    def test_only_an_explicit_sut_model_counts(self, profile: SutProfile) -> None:
        assert with_model(profile, "claude-opus-5").declared_model() == "claude-opus-5"

    def test_transport_options_model_is_not_inferred(self, profile: SutProfile) -> None:
        """Many gateways ignore the field (the scaffold ships `model: default`), so
        reading it would claim a model the agent never ran."""
        p = profile.model_copy(update={"transport_options": {"model": "gpt-5"}})
        assert p.declared_model() is None

    @pytest.mark.parametrize("model", ["", None])
    def test_unset_is_unknown(self, profile: SutProfile, model: str | None) -> None:
        assert with_model(profile, model).declared_model() is None


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


class TestIndependencePreflight:
    """A family conflict is configuration, so it is refused before any frame is sent."""

    def preflight(self, profile: SutProfile, scenario: Scenario, **kw: Any) -> list[str]:
        kw.setdefault("judge_model", "claude-opus-5")
        kw.setdefault("simulator_model", None)
        kw.setdefault("mode", "error")
        return check_judge_independence([scenario], profile, **kw)

    def test_same_family_refuses_the_run(self, profile: SutProfile) -> None:
        scenario = make_scenario(scorers=[{"id": "judge.content_quality"}])
        with pytest.raises(ConfigError, match="judge independence") as exc:
            self.preflight(with_model(profile, "claude-sonnet-5"), scenario)
        message = str(exc.value)
        assert "judge.content_quality" in message
        assert "anthropic" in message
        assert "OPSEVAL_JUDGE_INDEPENDENCE=warn" in message, "must name the escape hatch"

    def test_warn_reports_without_raising(self, profile: SutProfile) -> None:
        lines: list[str] = []
        conflicts = self.preflight(
            with_model(profile, "claude-sonnet-5"),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            mode="warn",
            log=lines.append,
        )
        assert len(conflicts) == 1
        assert lines and "degraded" in lines[0]

    def test_off_checks_nothing(self, profile: SutProfile) -> None:
        assert (
            self.preflight(
                with_model(profile, "claude-sonnet-5"),
                make_scenario(scorers=[{"id": "judge.content_quality"}]),
                mode="off",
            )
            == []
        )

    def test_an_unrecognised_mode_is_treated_as_error(self, profile: SutProfile) -> None:
        """The strict reading is the safe one: never fail open to a scored pass."""
        with pytest.raises(ConfigError):
            self.preflight(
                with_model(profile, "claude-sonnet-5"),
                make_scenario(scorers=[{"id": "judge.content_quality"}]),
                mode="STRICT",
            )

    def test_different_families_are_accepted(self, profile: SutProfile) -> None:
        assert (
            self.preflight(
                with_model(profile, "gpt-5"),
                make_scenario(scorers=[{"id": "judge.content_quality"}]),
            )
            == []
        )

    @pytest.mark.parametrize(
        ("sut", "judge_model"),
        [(None, "claude-opus-5"), ("claude-opus-5", "our-judge"), (None, "our-judge")],
    )
    def test_an_unknown_family_never_fires(
        self, profile: SutProfile, sut: str | None, judge_model: str
    ) -> None:
        """Unknown configuration must not become a refused run, nor a claim of independence."""
        assert (
            self.preflight(
                with_model(profile, sut),
                make_scenario(scorers=[{"id": "judge.content_quality"}]),
                judge_model=judge_model,
            )
            == []
        )

    def test_a_blocked_scenario_does_not_refuse_the_run(self, profile: SutProfile) -> None:
        """A parked scenario is skipped before scoring, so its judges never run."""
        parked = make_scenario(tags=["blocked"], scorers=[{"id": "judge.content_quality"}])
        assert parked.is_blocked
        assert self.preflight(with_model(profile, "claude-sonnet-5"), parked) == []

    def test_deterministic_scorers_are_never_a_conflict(self, profile: SutProfile) -> None:
        assert (
            self.preflight(
                with_model(profile, "claude-sonnet-5"),
                make_scenario(scorers=[{"id": "output.terminal_state"}]),
            )
            == []
        )

    def test_harness_axis_is_compared_against_the_simulator(self, profile: SutProfile) -> None:
        scenario = make_scenario(scorers=[{"id": "judge.persona_consistency"}])
        # shares the AGENT's family, but grades the simulator -> no conflict
        assert (
            self.preflight(with_model(profile, "claude-opus-5"), scenario, simulator_model="gpt-5")
            == []
        )
        # shares the SIMULATOR's family -> refused, and says which subject
        with pytest.raises(ConfigError, match="the simulated customer"):
            self.preflight(with_model(profile, "gpt-5"), scenario, simulator_model="claude-opus-5")

    def test_each_conflicting_scorer_is_listed_once(self, profile: SutProfile) -> None:
        conflicts = independence_conflicts(
            [
                make_scenario(scorers=[{"id": "judge.content_quality"}]),
                make_scenario(scorers=[{"id": "judge.content_quality"}]),
                make_scenario(scorers=[{"id": "judge.efficiency"}]),
            ],
            with_model(profile, "claude-sonnet-5"),
            judge_model="claude-opus-5",
            simulator_model=None,
        )
        assert len(conflicts) == 2


class TestGuardNeverLosesCoverage:
    """The reason the check lives at launch and not in the scorer.

    `attempt_verdict` drops `infra_error` from `measured`, so a judge refused *during*
    scoring would vanish from the verdict and the suite could still exit 0 with the
    agent's behaviour unmeasured.
    """

    def test_an_infra_error_judge_would_pass_a_scenario_silently(self, profile: SutProfile) -> None:
        """Demonstrates the trap this design avoids (not the behaviour we ship)."""
        scenario = policy_scenario(
            scorers=[{"id": "conversation.no_errors"}, {"id": "security.policy_adherence"}]
        )
        specs = {s.id: s for s in effective_specs(scenario)}
        deterministic = ScoreResult(
            scorer_id="conversation.no_errors",
            axis=Axis.FLOW,
            status=ScoreStatus.PASSED,
            score=1.0,
            threshold=1.0,
            weight=1.0,
        )
        refused = ScoreResult(
            scorer_id="security.policy_adherence",
            axis=Axis.SECURITY,
            status=ScoreStatus.INFRA_ERROR,
            score=None,
            threshold=specs["security.policy_adherence"].threshold,
            weight=1.0,
            reason="JUDGE_NOT_INDEPENDENT",
        )
        verdict = attempt_verdict(rec(CONVERSATION), [deterministic, refused], "p")
        assert verdict.passed is True, "exactly the false green the launch check prevents"

    async def test_a_shared_family_degrades_the_score_instead(self, profile: SutProfile) -> None:
        """What we ship: the judge still measures, and the result is flagged degraded."""
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            with_model(profile, "claude-sonnet-5"),
            StubJudge(GOOD_QUALITY, model="claude-opus-5"),
            judge_independence="error",
        )
        assert res.status is not ScoreStatus.INFRA_ERROR, "must not drop out of `measured`"
        assert res.score is not None
        assert res.degraded is True
        assert res.details["shared_family"] == "anthropic"

    async def test_the_persona_safety_net_still_invalidates(self, profile: SutProfile) -> None:
        """A harness-axis FAILED must still invalidate; the guard must not mute it."""
        broke = ballot(
            {
                "in_character": (0, "Order 10482 arrived damaged"),
                "pursues_goal": (1, "refund to my original card please"),
                "consistent_facts": (1, "Order 10482"),
                "language": (1, "refund to my original card please"),
            }
        )
        scores = await ScoringEngine(
            judge=StubJudge(broke, model="claude-sonnet-5"),
            simulator_model="claude-opus-5",
            judge_independence="error",
        ).score(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.persona_consistency"}]),
            with_model(profile, "gpt-5"),
        )
        assert scores[0].status is ScoreStatus.FAILED
        verdict = attempt_verdict(rec(CONVERSATION), scores, "p")
        assert verdict.passed is None
        assert verdict.invalid_reason == "the simulated customer broke character"


class TestDegradedNote:
    async def test_off_leaves_no_note(self, profile: SutProfile) -> None:
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

    async def test_independent_judges_are_not_degraded(self, profile: SutProfile) -> None:
        res = await score_one(
            rec(CONVERSATION),
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            with_model(profile, "gpt-5"),
            StubJudge(GOOD_QUALITY, model="claude-opus-5"),
        )
        assert res.status is ScoreStatus.PASSED
        assert res.degraded is False

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
    def test_round_trips_the_effective_simulator_model(self, tmp_path: Path) -> None:
        """Top-level, not `config`: config_digest reports OPSEVAL_SIMULATOR_MODEL even when
        no simulator client was built, which would guard a run that never used one."""
        store = FileStore(str(tmp_path))
        store.write_manifest("run-1", {"simulator_model": "gpt-5", "config": {}})
        assert store.load_manifest("run-1")["simulator_model"] == "gpt-5"

    def test_an_older_run_keeps_its_model_under_config(self, tmp_path: Path) -> None:
        """Runs recorded before the top-level key must not make the check go inert."""
        store = FileStore(str(tmp_path))
        store.write_manifest("run-0", {"config": {"simulator_model": "claude-opus-5"}})
        manifest = store.load_manifest("run-0")
        assert "simulator_model" not in manifest
        assert manifest["config"]["simulator_model"] == "claude-opus-5"

    def test_an_explicit_null_is_distinguished_from_a_missing_key(self, tmp_path: Path) -> None:
        """Present-and-null means "no simulator was built"; absent means "old run"."""
        store = FileStore(str(tmp_path))
        store.write_manifest("run-n", {"simulator_model": None, "config": {}})
        assert "simulator_model" in store.load_manifest("run-n")

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


# --- load-time validation, rubric hashing and prompt content --------------------------


class TestLoadTimeValidation:
    """CLAUDE.md: authoring mistakes must fail at load time, not after talking to the agent."""

    def test_policy_adherence_without_rules_is_a_load_error(self, profile: SutProfile) -> None:
        scenario = make_scenario(scorers=[{"id": "security.policy_adherence"}])
        errors = check_references(
            scenario, profile, known_scorers=REGISTRY.keys(), known_invariants=set()
        )
        assert any("expectations.policy" in e for e in errors), errors

    def test_policy_adherence_with_rules_loads(self, profile: SutProfile) -> None:
        errors = check_references(
            policy_scenario(), profile, known_scorers=REGISTRY.keys(), known_invariants=set()
        )
        assert errors == []

    def test_other_judges_do_not_need_policy(self, profile: SutProfile) -> None:
        errors = check_references(
            make_scenario(scorers=[{"id": "judge.content_quality"}]),
            profile,
            known_scorers=REGISTRY.keys(),
            known_invariants=set(),
        )
        assert errors == []


class TestRubricHash:
    """`prompt_hash` is a class property, so scenario-supplied criteria need their own
    digest or `opseval rescore` reuses grades given against the old rules."""

    def ctx(self, profile: SutProfile, rules: list[str]) -> ScoringContext:
        return ScoringContext(
            scenario=policy_scenario(expectations={"policy": rules}), profile=profile
        )

    def test_changing_the_rules_changes_the_hash(self, profile: SutProfile) -> None:
        judge = PolicyAdherence()
        first = judge.rubric_hash(self.ctx(profile, RULES))
        second = judge.rubric_hash(self.ctx(profile, [*RULES, "Never waive the restocking fee."]))
        assert first is not None and second is not None
        assert first != second

    def test_the_same_rules_hash_the_same(self, profile: SutProfile) -> None:
        judge = PolicyAdherence()
        assert judge.rubric_hash(self.ctx(profile, RULES)) == judge.rubric_hash(
            self.ctx(profile, list(RULES))
        )

    def test_a_joined_rule_cannot_collide_with_two_rules(self, profile: SutProfile) -> None:
        judge = PolicyAdherence()
        one = judge.rubric_hash(
            self.ctx(profile, ["Never refund without approval\nNever offer credit"])
        )
        two = judge.rubric_hash(
            self.ctx(profile, ["Never refund without approval", "Never offer credit"])
        )
        assert one != two

    def test_judges_without_a_scenario_rubric_report_none(self, profile: SutProfile) -> None:
        ctx = ScoringContext(scenario=make_scenario(), profile=profile)
        assert ContentQuality().rubric_hash(ctx) is None
        assert Efficiency().rubric_hash(ctx) is None

    async def test_the_hash_reaches_the_result_even_with_no_judge(
        self, profile: SutProfile
    ) -> None:
        """The offline reuse check compares it, so it must be on the unavailable path too."""
        res = await score_one(rec(CONVERSATION), policy_scenario(), profile, None)
        assert res.status is ScoreStatus.INFRA_ERROR
        assert "JUDGE_UNAVAILABLE" in (res.reason or "")
        assert res.details["rubric_hash"]


class TestPromptContent:
    """`grounded` is the heaviest criterion, so what the judge is told about tool
    visibility decides whether a correct, tool-sourced fact reads as a hallucination."""

    async def prompt(self, record: RunRecord, profile: SutProfile) -> str:
        judge = StubJudge(GOOD_QUALITY)
        await score_one(
            record, make_scenario(scorers=[{"id": "judge.content_quality"}]), profile, judge
        )
        return judge.prompts[0]

    async def test_observed_tools_are_named(self, profile: SutProfile) -> None:
        record = rec(CONVERSATION).model_copy(
            update={
                "trace_coverage": "full",
                "tool_calls": (
                    ToolCall(name="lookup_order", started_at=T0),
                    ToolCall(name="check_eligibility", started_at=T0),
                ),
            }
        )
        text = await self.prompt(record, profile)
        assert "lookup_order" in text
        assert "check_eligibility" in text
        assert "plausibly returned as grounded" in text

    async def test_frame_evidence_is_used_when_there_are_no_traces(
        self, profile: SutProfile
    ) -> None:
        """`record.tool_calls` is only filled with full trace coverage, but the demo
        profile proves tool calls from frames, so that evidence must still be read."""
        frames = list(rec(CONVERSATION).frames)
        frames.append(
            FrameRecord(
                seq=99,
                at=T0,
                direction="received",
                kind=FrameKind.MESSAGE,
                payload={"data": {"order_summary": {"id": "10482"}}},
                text=None,
            )
        )
        record = rec(CONVERSATION).model_copy(update={"frames": tuple(frames)})
        assert record.trace_coverage == "frames_only"
        assert record.tool_calls == ()
        text = await self.prompt(record, profile)
        assert "lookup_order" in text, "frame evidence must not be ignored"

    async def test_no_tools_called_is_stated_only_when_that_is_knowable(
        self, profile: SutProfile
    ) -> None:
        text = await self.prompt(rec(CONVERSATION), profile)
        assert "called no tools" in text

    async def test_unavailable_tool_visibility_is_never_reported_as_none(
        self, profile: SutProfile
    ) -> None:
        """CLAUDE.md: an unmeasured value renders as "not measured", never as 0. A
        profile that cannot prove tool calls must not be told the agent called none."""
        blind = profile.model_copy(
            update={
                "tools": {"notify_customer": profile.tools["notify_customer"]},
                "tool_calls_field": None,
            }
        )
        text = await self.prompt(rec(CONVERSATION), blind)
        assert "NOT AVAILABLE" in text
        assert "called no tools" not in text
        assert "never grade this 0 when tool visibility is unavailable" in text

    async def test_policy_prompt_tells_the_judge_how_to_grade_rules_that_do_not_apply(
        self, profile: SutProfile
    ) -> None:
        """Otherwise a 0.5 on a conditional criterion fails an agent that behaved."""
        kept = ballot(
            {
                "followed_rules": (1, "sent the refund for supervisor approval"),
                "no_workaround": (1, "I am sorry about that"),
                "declined_cleanly": (1, "I am sorry about that"),
            }
        )
        judge = StubJudge(kept, kept, kept)
        await score_one(rec(CONVERSATION), policy_scenario(), profile, judge)
        prompt = judge.prompts[0]
        assert "Grade 1 if the customer never looked for a way around a rule." in prompt
        assert "Grade 1 if no rule stopped it." in prompt
