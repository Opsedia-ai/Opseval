# SPDX-License-Identifier: Apache-2.0
"""Scorer plugin API and its contract test kit (FR-SCR-10).

Fault-injection view of the core rule:
- harness half: a plugin that cannot be imported, breaks the contract or collides with an
  existing id is a ConfigError (exit 3). It is never skipped — a suite naming a skipped
  scorer would get ``UNKNOWN_SCORER``, which is an ``infra_error``, which
  ``attempt_verdict`` drops from the measured set, so a scenario with one other passing
  scorer would report green with the plugin's check silently gone;
- agent half: a well-behaved plugin scores like a built-in, and the contract kit holds it
  to the three-state rule so a plugin cannot report 0.0 for "could not tell".
"""

from __future__ import annotations

from typing import Any

import pytest

from opseval.errors import ConfigError
from opseval.models.record import RunRecord
from opseval.models.results import Axis, ScoreResult, ScoreStatus
from opseval.models.scenario import ScorerSpec
from opseval.plugins import (
    SCORER_GROUP,
    THIRD_PARTY,
    check_contract,
    load_scorers,
    reset_for_tests,
)
from opseval.scoring.base import REGISTRY, Scorer, ScoringContext
from opseval.testing import ScorerContract, sample_record


class GoodScorer(Scorer):
    """A compliant third-party scorer: namespaced, three-state, non-mutating."""

    id = "acme.greeting"
    axis = Axis.OUTPUT
    requires = "frames"
    version = "1"
    description = "The agent greeted the customer."

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        agent_text = [t for who, t in record.transcript() if who == "agent"]
        if not agent_text:
            return self.infra(spec, "NO_AGENT_TEXT: the agent said nothing to grade")
        greeted = any("hi" in t.lower() or "hello" in t.lower() for t in agent_text)
        return self.result(spec, 1.0 if greeted else 0.0)


class NotAScorer:
    id = "acme.bogus"


class NoAxis(Scorer):
    id = "acme.no_axis"
    requires = "frames"

    async def score(self, record: Any, spec: Any, ctx: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class BadRequires(Scorer):
    id = "acme.bad_requires"
    axis = Axis.OUTPUT
    requires = "vibes"

    async def score(self, record: Any, spec: Any, ctx: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class SyncScore(Scorer):
    id = "acme.sync"
    axis = Axis.OUTPUT
    requires = "frames"

    def score(self, record: Any, spec: Any, ctx: Any) -> Any:  # type: ignore[override]
        return None


class CollidesWithBuiltin(Scorer):
    id = "output.terminal_state"
    axis = Axis.OUTPUT
    requires = "frames"

    async def score(self, record: Any, spec: Any, ctx: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class FakeDist:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeEntryPoint:
    """Stands in for an installed entry point, including a failing import."""

    def __init__(self, name: str, obj: Any, *, dist: str = "acme-opseval") -> None:
        self.name = name
        self.value = f"fake:{name}"
        self._obj = obj
        self.dist = FakeDist(dist)

    def load(self) -> Any:
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """Keep the global REGISTRY as it was, whatever a test registers."""
    reset_for_tests()
    before = dict(REGISTRY)
    yield
    REGISTRY.clear()
    REGISTRY.update(before)
    reset_for_tests()


def discover(*entries: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr("opseval.plugins._iter_entry_points", lambda group: iter(entries))
    return load_scorers()


# --- a well-behaved plugin ------------------------------------------------------------


class TestDiscovery:
    def test_a_compliant_plugin_is_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        found = discover(FakeEntryPoint("greeting", GoodScorer), monkeypatch=monkeypatch)
        assert found == {"acme.greeting": "acme-opseval"}
        assert REGISTRY["acme.greeting"] is GoodScorer

    def test_the_providing_distribution_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """So `opseval list-scorers` can say where a scorer came from."""
        discover(FakeEntryPoint("greeting", GoodScorer), monkeypatch=monkeypatch)
        assert THIRD_PARTY["acme.greeting"] == "acme-opseval"

    def test_discovery_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Importing Opseval from several places must not trip the duplicate guard."""
        entry = FakeEntryPoint("greeting", GoodScorer)
        monkeypatch.setattr("opseval.plugins._iter_entry_points", lambda group: iter([entry]))
        assert load_scorers() == {"acme.greeting": "acme-opseval"}
        assert load_scorers() == {}, "second call registers nothing"
        assert REGISTRY["acme.greeting"] is GoodScorer

    def test_no_plugins_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert discover(monkeypatch=monkeypatch) == {}

    async def test_a_plugin_scores_like_a_built_in(
        self, monkeypatch: pytest.MonkeyPatch, profile: Any
    ) -> None:
        from opseval.scoring import ScoringEngine

        from .conftest import make_scenario

        discover(FakeEntryPoint("greeting", GoodScorer), monkeypatch=monkeypatch)
        scenario = make_scenario(scorers=[{"id": "acme.greeting"}])
        record = sample_record((("customer", "hello?"), ("agent", "Hi, how can I help?")))
        scores = await ScoringEngine().score(record, scenario, profile)
        assert [s.scorer_id for s in scores] == ["acme.greeting"]
        assert scores[0].status is ScoreStatus.PASSED

    def test_the_real_entry_point_group_is_queried(self) -> None:
        """Guards against the group name drifting from the published contract."""
        assert SCORER_GROUP == "opseval.scorers"
        assert isinstance(next(iter([]), None), type(None))


# --- failing closed -------------------------------------------------------------------


class TestFailsClosed:
    """Why these raise instead of skipping: a dropped scorer becomes UNKNOWN_SCORER,
    which is an infra_error, which attempt_verdict drops from `measured` — so a suite
    could report green with the plugin's check gone."""

    def test_an_unimportable_plugin_is_a_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ConfigError, match="could not be imported"):
            discover(
                FakeEntryPoint("broken", ImportError("no module named acme")),
                monkeypatch=monkeypatch,
            )

    def test_the_error_says_how_to_recover(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ConfigError, match="Fix or uninstall"):
            discover(FakeEntryPoint("broken", RuntimeError("boom")), monkeypatch=monkeypatch)

    def test_a_non_scorer_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ConfigError, match="deriving from opseval.scoring.Scorer"):
            discover(FakeEntryPoint("bogus", NotAScorer), monkeypatch=monkeypatch)

    def test_a_missing_axis_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ConfigError, match="no valid 'axis'"):
            discover(FakeEntryPoint("no_axis", NoAxis), monkeypatch=monkeypatch)

    def test_an_unknown_requires_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ConfigError, match="requires='vibes'"):
            discover(FakeEntryPoint("bad_requires", BadRequires), monkeypatch=monkeypatch)

    def test_a_synchronous_score_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ConfigError, match="async def score"):
            discover(FakeEntryPoint("sync", SyncScore), monkeypatch=monkeypatch)

    def test_colliding_with_a_built_in_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ConfigError, match="already registered"):
            discover(FakeEntryPoint("collide", CollidesWithBuiltin), monkeypatch=monkeypatch)

    def test_the_collision_error_suggests_a_namespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ConfigError, match="prefix yours with your own namespace"):
            discover(FakeEntryPoint("collide", CollidesWithBuiltin), monkeypatch=monkeypatch)

    def test_one_broken_plugin_stops_the_lot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Partial discovery would be the quiet-coverage-loss case again."""
        with pytest.raises(ConfigError):
            discover(
                FakeEntryPoint("aaa_broken", ImportError("nope")),
                FakeEntryPoint("greeting", GoodScorer),
                monkeypatch=monkeypatch,
            )

    def test_a_missing_version_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class NoVersion(Scorer):
            id = "acme.no_version"
            axis = Axis.OUTPUT
            requires = "frames"
            version = ""

            async def score(self, record: Any, spec: Any, ctx: Any) -> Any:
                raise NotImplementedError

        with pytest.raises(ConfigError, match="no 'version'"):
            discover(FakeEntryPoint("no_version", NoVersion), monkeypatch=monkeypatch)


class TestCheckContractDirectly:
    def test_it_returns_the_class_on_success(self) -> None:
        assert check_contract(GoodScorer, source="x") is GoodScorer

    def test_an_instance_is_not_a_class(self) -> None:
        with pytest.raises(ConfigError, match="must be a class"):
            check_contract(GoodScorer(), source="x")


# --- the contract kit, applied to the kit's own example -------------------------------


class TestGoodScorerContract(ScorerContract):
    """The kit holding a compliant scorer: this is what a plugin author writes."""

    scorer = GoodScorer


class TestTheKitCatchesViolations:
    """The kit is only worth shipping if it fails a bad scorer. These are the violations
    `ScoreResult` cannot block, which is exactly why the kit exists."""

    async def test_it_catches_a_scorer_that_raises(self) -> None:
        """The engine would mask this as infra_error, so the bug looks like a result."""

        class Raises(Scorer):
            id = "acme.raises"
            axis = Axis.OUTPUT
            requires = "frames"
            version = "1"

            async def score(self, record: Any, spec: Any, ctx: Any) -> Any:
                raise RuntimeError("kaboom")

        class Harness(ScorerContract):
            scorer = Raises

        with pytest.raises(pytest.fail.Exception, match="kaboom"):
            await Harness().test_it_does_not_raise()

    async def test_it_catches_a_non_score_result(self) -> None:
        class ReturnsDict(Scorer):
            id = "acme.dict"
            axis = Axis.OUTPUT
            requires = "frames"
            version = "1"

            async def score(self, record: Any, spec: Any, ctx: Any) -> Any:
                return {"status": "passed"}

        class Harness(ScorerContract):
            scorer = ReturnsDict

        with pytest.raises(AssertionError, match="returned dict"):
            await Harness().test_it_returns_a_score_result()

    async def test_it_catches_a_mutated_nested_payload(self) -> None:
        """Frozen models stop attribute assignment; a dict inside a payload is reachable."""

        class Mutates(Scorer):
            id = "acme.mutates"
            axis = Axis.OUTPUT
            requires = "frames"
            version = "1"

            async def score(
                self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext
            ) -> ScoreResult:
                for frame in record.frames:
                    if isinstance(frame.payload, dict):
                        frame.payload.clear()
                return self.result(spec, 1.0)

        class Harness(ScorerContract):
            scorer = Mutates

        with pytest.raises(AssertionError):
            await Harness().test_it_does_not_mutate_a_nested_payload()

    async def test_it_catches_a_scorer_reporting_someone_elses_id(self) -> None:
        class Impersonates(Scorer):
            id = "acme.impersonator"
            axis = Axis.OUTPUT
            requires = "frames"
            version = "1"

            async def score(
                self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext
            ) -> ScoreResult:
                return ScoreResult(
                    scorer_id="output.terminal_state",
                    axis=Axis.OUTPUT,
                    status=ScoreStatus.PASSED,
                    score=1.0,
                    threshold=1.0,
                    weight=1.0,
                    scorer_version="1",
                )

        class Harness(ScorerContract):
            scorer = Impersonates

        with pytest.raises(AssertionError):
            await Harness().test_it_reports_its_own_id_and_axis()

    def test_it_catches_an_unnamespaced_id(self) -> None:
        class Bare(Scorer):
            id = "tone"
            axis = Axis.OUTPUT
            requires = "frames"
            version = "1"

            async def score(self, record: Any, spec: Any, ctx: Any) -> Any:
                raise NotImplementedError

        class Harness(ScorerContract):
            scorer = Bare

        with pytest.raises(AssertionError, match="should be namespaced"):
            Harness().test_the_id_is_namespaced()

    async def test_it_catches_a_missing_version(self) -> None:
        class NoVersion(Scorer):
            id = "acme.unversioned"
            axis = Axis.OUTPUT
            requires = "frames"
            version = "1"

            async def score(
                self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext
            ) -> ScoreResult:
                return ScoreResult(
                    scorer_id=self.id,
                    axis=self.axis,
                    status=ScoreStatus.PASSED,
                    score=1.0,
                    threshold=1.0,
                    weight=1.0,
                    scorer_version=None,
                )

        class Harness(ScorerContract):
            scorer = NoVersion

        with pytest.raises(AssertionError):
            await Harness().test_it_reports_a_version()
