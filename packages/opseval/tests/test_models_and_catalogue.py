# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from opseval.catalogue import check_references, load_catalogue
from opseval.config import Settings, check_url_allowed, config_digest
from opseval.errors import AllowedHostError, ConfigError
from opseval.models.profile import FrameKind, SutProfile, render_template
from opseval.models.results import Axis, ScoreResult, ScoreStatus
from opseval.scoring import INVARIANTS, REGISTRY

from .conftest import make_scenario


def load(path: Path, profile: SutProfile, **kw: object):  # type: ignore[no-untyped-def]
    return load_catalogue(
        path, profile, known_scorers=REGISTRY.keys(), known_invariants=set(INVARIANTS), **kw
    )


class TestScenarioSchema:
    def test_minimal_is_valid(self) -> None:
        s = make_scenario()
        assert s.execution.repeats == 1 and s.criticality.value == "normal"

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            make_scenario(colour="blue")

    def test_needs_flow_or_script(self) -> None:
        with pytest.raises(ValidationError, match="flow"):
            make_scenario(conversation={"scripted_turns": [], "flow": ""})

    def test_outcome_committed_needs_explicit_opt_in(self) -> None:
        with pytest.raises(ValidationError, match="allow_commit"):
            make_scenario(conversation={"stop_at": "outcome_committed"})

    def test_approve_via_chat_rejected(self) -> None:
        with pytest.raises(ValidationError, match="via: resume"):
            make_scenario(
                conversation={
                    "checkpoint_policy": {
                        "proposal_approval": {"action": "approve", "via": "user_message"}
                    }
                }
            )

    def test_sequence_rules(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            make_scenario(
                conversation={"checkpoint_policy": {"proposal_approval": {"action": "sequence"}}}
            )

    def test_bounds(self) -> None:
        with pytest.raises(ValidationError):
            make_scenario(persona={"typing_noise": 1.5})
        with pytest.raises(ValidationError):
            make_scenario(execution={"repeats": 0})

    def test_empty_scorer_list_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one scorer"):
            make_scenario(scorers=[])


class TestScoreContract:
    def test_infra_error_has_no_score(self) -> None:
        with pytest.raises(ValidationError):
            ScoreResult(
                scorer_id="x",
                axis=Axis.FLOW,
                status=ScoreStatus.INFRA_ERROR,
                score=0.0,
                threshold=1,
                reason="r",
            )

    def test_measured_score_required(self) -> None:
        with pytest.raises(ValidationError):
            ScoreResult(
                scorer_id="x", axis=Axis.FLOW, status=ScoreStatus.FAILED, score=None, threshold=1
            )

    def test_infra_error_needs_reason(self) -> None:
        with pytest.raises(ValidationError):
            ScoreResult(
                scorer_id="x",
                axis=Axis.FLOW,
                status=ScoreStatus.INFRA_ERROR,
                score=None,
                threshold=1,
            )


class TestProfile:
    def test_classify_never_raises(self, profile: SutProfile) -> None:
        assert profile.classify("garbage") is FrameKind.UNKNOWN
        assert profile.classify({"type": 3}) is FrameKind.UNKNOWN
        assert profile.classify({"type": "needs_human"}) is FrameKind.NEEDS_HUMAN
        assert profile.classify({"_opseval_kind": "error"}) is FrameKind.ERROR

    def test_render_template_drops_none(self) -> None:
        out = render_template(
            {"a": "{x}", "b": "{y}", "c": "lit", "n": {"d": "{x}"}}, {"x": 1, "y": None}
        )
        assert out == {"a": 1, "c": "lit", "n": {"d": 1}}

    def test_commit_gate_must_exist(self) -> None:
        with pytest.raises(ValidationError, match="commit_gate"):
            SutProfile(name="p", commit_gate="nope")

    def test_a_committed_outcome_needs_a_commit_gate(self) -> None:
        """Without a gate the commit guard has nowhere to stop (ADR-0008): a load error."""
        with pytest.raises(ValidationError, match="needs a commit_gate"):
            SutProfile(name="p", committed_outcome="refund_issued")


class TestCatalogue:
    def test_demo_catalogue_is_clean(self, cat_dir: Path, profile: SutProfile) -> None:
        cat, name = load(cat_dir / "suites" / "full.yaml", profile)
        assert name == "full"
        assert cat.problems == []
        assert {s.id for s in cat.scenarios} == {"rf-001", "rf-002", "rf-003", "rf-101"}

    def test_smoke_suite_excludes_llm_dir(self, cat_dir: Path, profile: SutProfile) -> None:
        cat, _ = load(cat_dir / "suites" / "smoke.yaml", profile)
        assert {s.id for s in cat.scenarios} == {"rf-001", "rf-002", "rf-003"}

    def test_filters(self, cat_dir: Path, profile: SutProfile) -> None:
        cat, _ = load(cat_dir / "scenarios", profile, filters=["tag=arabic"])
        assert {s.id for s in cat.scenarios} == {"rf-003", "rf-101"}
        with pytest.raises(ConfigError):
            load(cat_dir / "scenarios", profile, filters=["colour=red"])

    def test_reference_errors(self, profile: SutProfile) -> None:
        s = make_scenario(
            conversation={"stop_at": "nope", "checkpoint_policy": {"ghost": {"action": "approve"}}},
            expectations={
                "expected_tools": {
                    "must_call": ["teleport"],
                    "ordering": [{"before": "lookup_order", "after": "warp"}],
                },
                "invariants": ["magic"],
            },
            scorers=[{"id": "does.not.exist"}],
        )
        errors = check_references(s, profile, REGISTRY.keys(), set(INVARIANTS))
        joined = "\n".join(errors)
        for needle in ("stop_at", "ghost", "teleport", "warp", "magic", "does.not.exist"):
            assert needle in joined

    def test_broken_file_is_reported_not_run(self, tmp_path: Path, profile: SutProfile) -> None:
        (tmp_path / "bad.yaml").write_text("id: bad\nname: x\n", encoding="utf-8")
        (tmp_path / "worse.yaml").write_text(": : :\n", encoding="utf-8")
        cat, _ = load(tmp_path, profile)
        assert cat.scenarios == []
        assert len(cat.problems) >= 2


class TestConfig:
    def test_allowlist(self) -> None:
        allow = {"agent.example.com", "127.0.0.1"}
        check_url_allowed("wss://agent.example.com/ws", allow, secure_schemes=("wss",))
        check_url_allowed("ws://127.0.0.1:8765", allow, secure_schemes=("wss",))
        with pytest.raises(AllowedHostError, match="not in OPSEVAL_ALLOWED_HOSTS"):
            check_url_allowed("wss://evil.example.com", allow, secure_schemes=("wss",))
        with pytest.raises(AllowedHostError, match="must use"):
            check_url_allowed("ws://agent.example.com", allow, secure_schemes=("wss",))

    def test_digest_redacts_secrets(self) -> None:
        digest = config_digest(Settings(simulator_api_key="sk-secret", allowed_hosts="a"))  # type: ignore[arg-type]
        assert digest["simulator_api_key"] == "***redacted***"
        assert "sk-secret" not in str(digest)
        assert digest["judge_api_key"] is None
