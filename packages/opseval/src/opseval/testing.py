# SPDX-License-Identifier: Apache-2.0
"""Contract test kit for plugins (FR-SCR-10, ADR-0022).

A third-party scorer proves compliance in its own CI with one line::

    from opseval.testing import ScorerContract

    class TestToneScorer(ScorerContract):
        scorer = ToneScorer

The kit runs a scorer against realistic records and holds it to the boundary the harness
depends on. Be clear about the division of labour: ``ScoreResult`` already *validates* the
three-state invariants at construction — ``score is None`` exactly when the status is
``infra_error``, a score within ``[0, 1]``, and a reason on every ``infra_error``. A
scorer cannot return a ``ScoreResult`` that breaks those.

So what the kit actually finds is what the model cannot see:

- the scorer **raises** instead of reporting. The engine converts an exception into
  ``infra_error``, so the bug still produces a plausible-looking result and nobody
  investigates;
- it returns something that is **not** a ``ScoreResult`` at all;
- it **mutates** the record. Frozen models block attribute assignment, but a nested dict
  inside a frame payload is still reachable, and a later scorer, the report and
  ``opseval rescore`` all read that same evidence;
- it reports **another scorer's** id or axis;
- it declares no ``version``, so ``opseval rescore`` cannot attribute a moved verdict;
- its id is not namespaced, so it will eventually collide with a built-in.

The state assertions are kept as regression guards — they would catch the model loosening,
or a scorer bypassing it — not because a compliant scorer can fail them today.

What the kit cannot do is tell whether a scorer's *judgement* is right. That is what
``opseval calibrate`` is for (ADR-0018).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import FrameRecord, RunRecord, TerminalState
from opseval.models.results import Axis, ScoreResult, ScoreStatus
from opseval.models.scenario import Scenario, ScorerSpec
from opseval.plugins import check_contract
from opseval.scoring.base import Scorer, ScoringContext

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def sample_record(turns: tuple[tuple[str, str], ...] = (), **kw: Any) -> RunRecord:
    """A minimal frozen record a contract test can hand to any scorer."""
    frames = tuple(
        FrameRecord(
            seq=i,
            at=T0,
            direction="sent" if who == "customer" else "received",
            kind=FrameKind.USER_MESSAGE if who == "customer" else FrameKind.MESSAGE,
            payload={},
            text=text,
        )
        for i, (who, text) in enumerate(turns)
    )
    base: dict[str, Any] = {
        "run_id": "contract",
        "test_id": "t-001",
        "attempt_index": 0,
        "seed": 1,
        "target": "python:contract",
        "started_at": T0,
        "ended_at": T0,
        "terminal_state": TerminalState.GOAL_REACHED,
        "frames": frames,
    }
    return RunRecord(**{**base, **kw})


def sample_scenario(**overrides: Any) -> Scenario:
    base: dict[str, Any] = {
        "id": "t-001",
        "name": "contract scenario",
        "goal": "Prove the scorer honours the contract.",
        "persona": {"role": "customer", "language": "en"},
        "conversation": {"scripted_turns": ["hello"], "stop_at": "goal_reached"},
    }
    return Scenario.model_validate({**base, **overrides})


class ScorerContract:
    """Subclass in your own test suite and set ``scorer``.

    Override :meth:`records` to add cases of your own; every one is held to the same
    boundary. Override :meth:`context` if your scorer needs a particular profile.
    """

    scorer: ClassVar[type[Scorer]]

    # --- hooks a plugin author may override ------------------------------------------

    def spec(self) -> ScorerSpec:
        return ScorerSpec(id=self.scorer.id)

    def context(self) -> ScoringContext:
        return ScoringContext(scenario=sample_scenario(), profile=SutProfile(name="contract"))

    def records(self) -> list[RunRecord]:
        """Records to run the contract against. Empty and non-empty by default."""
        return [
            sample_record(),
            sample_record((("customer", "hello"), ("agent", "hi, how can I help?"))),
            sample_record(terminal_state=TerminalState.TRANSPORT_FAILURE, stop_reason="dropped"),
        ]

    # --- the contract ----------------------------------------------------------------

    def test_it_satisfies_the_plugin_contract(self) -> None:
        check_contract(self.scorer, source="contract kit")

    def test_the_id_is_namespaced(self) -> None:
        """``vendor.thing`` keeps third-party ids from colliding with ours."""
        assert "." in self.scorer.id, (
            f"scorer id {self.scorer.id!r} should be namespaced, e.g. 'acme.tone', so it "
            "cannot collide with a built-in or another plugin"
        )

    def test_the_axis_is_known(self) -> None:
        assert isinstance(self.scorer.axis, Axis)

    async def test_it_returns_a_score_result(self) -> None:
        """Not a dict, not None. The engine would treat either as a scorer crash."""
        for record in self.records():
            result = await self._score(record)
            assert isinstance(result, ScoreResult), (
                f"{self.scorer.id} returned {type(result).__name__}; return "
                "self.result(...) or self.infra(...)"
            )
            assert result.status in (
                ScoreStatus.PASSED,
                ScoreStatus.FAILED,
                ScoreStatus.INFRA_ERROR,
            )

    async def test_a_score_is_none_exactly_when_not_measured(self) -> None:
        """ADR-0006. Enforced by ScoreResult itself; kept as a regression guard."""
        for record in self.records():
            result = await self._score(record)
            if result.status is ScoreStatus.INFRA_ERROR:
                assert result.score is None, (
                    f"{self.scorer.id} reported infra_error with score={result.score!r}; "
                    "an unmeasured value must be None, never a number"
                )
            else:
                assert result.score is not None, (
                    f"{self.scorer.id} reported {result.status.value} with score=None"
                )

    async def test_it_does_not_mutate_a_nested_payload(self) -> None:
        """Frozen models stop attribute assignment, but a dict inside a payload is
        still reachable, and every later reader sees the same evidence."""
        record = sample_record(
            frames=(
                FrameRecord(
                    seq=0,
                    at=T0,
                    direction="received",
                    kind=FrameKind.MESSAGE,
                    payload={"data": {"keep": "me"}},
                    text="hello",
                ),
            )
        )
        await self._score(record)
        assert record.frames[0].payload == {"data": {"keep": "me"}}

    async def test_an_unmeasured_result_says_why(self) -> None:
        for record in self.records():
            result = await self._score(record)
            if result.status is ScoreStatus.INFRA_ERROR:
                assert result.reason, (
                    f"{self.scorer.id} reported infra_error with no reason; "
                    "whoever reads the report needs to know what was not measured"
                )

    async def test_it_reports_its_own_id_and_axis(self) -> None:
        for record in self.records():
            result = await self._score(record)
            assert result.scorer_id == self.scorer.id
            assert result.axis == self.scorer.axis

    async def test_it_reports_a_version(self) -> None:
        """``opseval rescore`` attributes a moved verdict to a version change."""
        result = await self._score(self.records()[0])
        assert result.scorer_version == self.scorer.version

    async def test_it_does_not_mutate_the_record(self) -> None:
        for record in self.records():
            before = record.model_dump_json()
            await self._score(record)
            assert record.model_dump_json() == before, (
                f"{self.scorer.id} changed the record; records are frozen so a later "
                "scorer, the report and `opseval rescore` all see the same evidence"
            )

    async def test_it_does_not_raise(self) -> None:
        """The engine converts an exception to infra_error, but a scorer that reports
        its own problem can say something useful about it."""
        for record in self.records():
            try:
                await self._score(record)
            except Exception as exc:  # noqa: BLE001 - that is what is being tested
                pytest.fail(
                    f"{self.scorer.id} raised {type(exc).__name__}: {exc}. Return "
                    "self.infra(spec, 'REASON_CODE: explanation') instead."
                )

    # --- plumbing --------------------------------------------------------------------

    async def _score(self, record: RunRecord) -> ScoreResult:
        return await self.scorer().score(record, self.spec(), self.context())


__all__ = ["ScorerContract", "sample_record", "sample_scenario"]
