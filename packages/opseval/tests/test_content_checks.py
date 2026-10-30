# SPDX-License-Identifier: Apache-2.0
"""``content.*`` (ADR-0061): deterministic checks on what was said.

Fault-injection view of the core rule:
- agent half: an agent that said nothing did not say what was required (fails); an agent
  message that is not JSON fails ``json_path_equals``; ``true`` is not ``1``;
- harness half: a bad pattern, a nested quantifier or an undeclared checkpoint is a *load*
  error; a turn too long for the regex guard is *not measured*, never cut short to a false
  fail; nothing to read in a not-contains check is a vacuous pass, named as one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from opseval.catalogue import check_references
from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import FrameRecord, InterruptRecord, RunRecord, TerminalState
from opseval.models.results import ScoreResult, ScoreStatus
from opseval.models.scenario import ScorerSpec
from opseval.scoring import REGISTRY
from opseval.scoring.base import ScoringContext
from opseval.scoring.content import (
    MAX_REGEX_INPUT,
    guard_pattern,
    json_equal,
    normalise,
)

from .conftest import make_scenario

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def frame(
    seq: int, who: str, text: str | None, kind: FrameKind | None = None, **payload: Any
) -> FrameRecord:
    return FrameRecord(
        seq=seq,
        at=T0,
        direction="sent" if who == "customer" else "received",
        kind=kind or (FrameKind.USER_MESSAGE if who == "customer" else FrameKind.MESSAGE),
        payload=payload,
        text=text,
    )


def rec(*frames: FrameRecord, interrupts: tuple[InterruptRecord, ...] = ()) -> RunRecord:
    return RunRecord(
        run_id="r",
        test_id="t-001",
        attempt_index=0,
        seed=1,
        target="x",
        started_at=T0,
        ended_at=T0,
        terminal_state=TerminalState.GOAL_REACHED,
        frames=frames,
        interrupts=interrupts,
    )


CHAT = rec(
    frame(0, "customer", "Where is order 10482?"),
    frame(1, "agent", "Order 10482 shipped on Monday."),
    frame(2, "customer", "When does it arrive?"),
    frame(3, "agent", "It arrives Thursday. Anything else?"),
)


async def run(scorer: str, record: RunRecord, profile: SutProfile, **params: Any) -> ScoreResult:
    spec = ScorerSpec(id=scorer, params=params)
    ctx = ScoringContext(scenario=make_scenario(), profile=profile)
    return await REGISTRY[scorer]().score(record, spec, ctx)


def passed(result: ScoreResult) -> bool:
    assert result.status is not ScoreStatus.INFRA_ERROR, result.reason
    return result.status is ScoreStatus.PASSED


class TestContains:
    async def test_all_of_any_of(self, profile: SutProfile) -> None:
        assert passed(await run("content.contains", CHAT, profile, all_of=["10482", "thursday"]))
        assert not passed(await run("content.contains", CHAT, profile, all_of=["10482", "friday"]))
        assert passed(await run("content.contains", CHAT, profile, any_of=["friday", "monday"]))
        miss = await run("content.contains", CHAT, profile, all_of=["10482", "friday"])
        assert miss.details["missing"] == ["friday"] and miss.details["found"] == {"10482": [2]}

    async def test_case_and_whitespace(self, profile: SutProfile) -> None:
        assert passed(await run("content.contains", CHAT, profile, any_of=["ORDER  10482"]))
        assert not passed(
            await run(
                "content.contains", CHAT, profile, any_of=["ORDER 10482"], case_sensitive=True
            )
        )

    async def test_scope(self, profile: SutProfile) -> None:
        last = await run("content.contains", CHAT, profile, any_of=["10482"], scope="last_turn")
        assert not passed(last) and last.details["turns_checked"] == [4]
        assert passed(await run("content.contains", CHAT, profile, any_of=["10482"]))
        every = await run("content.contains", CHAT, profile, any_of=["10482"], scope="every_turn")
        assert not passed(every)
        assert passed(
            await run("content.contains", CHAT, profile, any_of=["."], scope="every_turn")
        )

    async def test_speaker(self, profile: SutProfile) -> None:
        assert passed(
            await run("content.contains", CHAT, profile, any_of=["arrive?"], speaker="customer")
        )
        assert not passed(await run("content.contains", CHAT, profile, any_of=["arrive?"]))

    async def test_arabic_with_and_without_diacritics(self, profile: SutProfile) -> None:
        arabic = rec(
            frame(0, "customer", "أين طلبي؟"),
            frame(1, "agent", "مَرْحَبًا، طلبك رقم ١٠٤٨٢ في الطريق"),
        )
        assert not passed(await run("content.contains", arabic, profile, any_of=["مرحبا"]))
        assert passed(
            await run("content.contains", arabic, profile, any_of=["مرحبا"], ignore_diacritics=True)
        )
        # NFKC does not fold Arabic-Indic digits into ASCII: say them as the agent writes them.
        assert passed(await run("content.contains", arabic, profile, any_of=["١٠٤٨٢"]))

    async def test_an_agent_that_said_nothing_fails(self, profile: SutProfile) -> None:
        silent = rec(frame(0, "customer", "hello?"))
        result = await run("content.contains", silent, profile, any_of=["hi"])
        assert result.status is ScoreStatus.FAILED and "no text" in result.details["note"]

    async def test_one_needle_is_required(self, profile: SutProfile) -> None:
        result = await run("content.contains", CHAT, profile)
        assert result.status is ScoreStatus.INFRA_ERROR and "BAD_PARAMS" in (result.reason or "")


class TestNotContains:
    async def test_is_the_negation_of_contains(self, profile: SutProfile) -> None:
        assert not passed(await run("content.not_contains", CHAT, profile, any_of=["thursday"]))
        assert passed(await run("content.not_contains", CHAT, profile, any_of=["competitor"]))

    async def test_silence_is_a_named_vacuous_pass(self, profile: SutProfile) -> None:
        silent = rec(frame(0, "customer", "hello?"))
        result = await run("content.not_contains", silent, profile, any_of=["x"])
        assert result.status is ScoreStatus.PASSED and result.details["vacuous"] is True

    async def test_a_silent_customer_is_not_measured(self, profile: SutProfile) -> None:
        only_agent = rec(frame(0, "agent", "hello"))
        result = await run(
            "content.not_contains", only_agent, profile, any_of=["x"], speaker="customer"
        )
        assert result.status is ScoreStatus.INFRA_ERROR


class TestRegex:
    async def test_must_match(self, profile: SutProfile) -> None:
        assert passed(await run("content.regex", CHAT, profile, pattern=r"\b\d{5}\b"))
        assert not passed(await run("content.regex", CHAT, profile, pattern=r"\bFriday\b"))
        assert passed(
            await run("content.regex", CHAT, profile, pattern=r"\bFriday\b", must_match=False)
        )
        assert passed(
            await run("content.regex", CHAT, profile, pattern="thursday", case_sensitive=False)
        )

    async def test_scope_every_turn(self, profile: SutProfile) -> None:
        assert passed(await run("content.regex", CHAT, profile, pattern=r"\.", scope="every_turn"))
        assert not passed(
            await run("content.regex", CHAT, profile, pattern=r"\d", scope="every_turn")
        )

    async def test_arabic(self, profile: SutProfile) -> None:
        arabic = rec(frame(0, "customer", "?"), frame(1, "agent", "سيصلك الطلب خلال ٣ أيام"))
        assert passed(await run("content.regex", arabic, profile, pattern=r"خلال\s+\d+\s+أيام"))

    async def test_silence(self, profile: SutProfile) -> None:
        silent = rec(frame(0, "customer", "hello?"))
        assert not passed(await run("content.regex", silent, profile, pattern="x"))
        absent = await run("content.regex", silent, profile, pattern="x", must_match=False)
        assert absent.status is ScoreStatus.PASSED and absent.details["vacuous"] is True

    async def test_a_turn_over_the_guard_is_not_measured_never_cut(
        self, profile: SutProfile
    ) -> None:
        long = rec(frame(0, "customer", "?"), frame(1, "agent", "a" * MAX_REGEX_INPUT + " 10482"))
        result = await run("content.regex", long, profile, pattern="10482")
        assert result.status is ScoreStatus.INFRA_ERROR
        assert (result.reason or "").startswith("REGEX_INPUT_TOO_LONG")

    @pytest.mark.parametrize(
        "pattern",
        [r"(a+)+$", r"(\w*)*x", r"(x+){2,}", r"((ab)*c)+", r"(?:a|b+)*z", r"a*b*c*d*", "a" * 501],
    )
    def test_the_guard_refuses_dangerous_patterns(self, pattern: str) -> None:
        with pytest.raises(ValueError):
            guard_pattern(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [r"\d+-\d+-\d+", r"(?:foo|bar)+", r"(ab){1,3}", r"^order \d{5}$", r"[a-z]+@[a-z]+\.com"],
    )
    def test_the_guard_accepts_ordinary_patterns(self, pattern: str) -> None:
        guard_pattern(pattern)


def load_errors(scorer: str, profile: SutProfile, **params: Any) -> list[str]:
    scenario = make_scenario(scorers=[{"id": scorer, "params": params}])
    return check_references(scenario, profile, known_scorers=REGISTRY, known_invariants=set())


class TestLoad:
    def test_a_bad_regex_is_a_load_error_at_its_field(self, profile: SutProfile) -> None:
        errors = load_errors("content.regex", profile, pattern="(unclosed")
        assert errors and errors[0].startswith("scorers.0.params.pattern: content.regex")
        assert "not a valid regular expression" in errors[0]

    def test_a_nested_quantifier_is_a_load_error(self, profile: SutProfile) -> None:
        errors = load_errors("content.regex", profile, pattern="(a+)+")
        assert errors and "quantifier inside a quantified group" in errors[0]

    def test_typos_fail(self, profile: SutProfile) -> None:
        assert load_errors("content.contains", profile, any_of=["x"], scop="last_turn")
        assert load_errors("content.contains", profile, any_of="x")
        assert load_errors("content.contains", profile, any_of=["x"], speaker="bot")
        assert load_errors("content.contains", profile, any_of=[""])
        assert load_errors("content.contains", profile, any_of=["x"]) == []

    def test_json_path_checkpoint_is_checked_against_the_profile(self, profile: SutProfile) -> None:
        errors = load_errors(
            "content.json_path_equals",
            profile,
            source="checkpoint",
            checkpoint="nope",
            path="a",
            equals=1,
        )
        assert errors and "declares no checkpoint 'nope'" in errors[0]
        assert load_errors(
            "content.json_path_equals", profile, source="checkpoint", path="a", equals=1
        )
        assert load_errors(
            "content.json_path_equals", profile, checkpoint="proposal_approval", path="a", equals=1
        )
        assert load_errors("content.json_path_equals", profile, path="a") != []  # equals missing


class TestJsonPathEquals:
    async def test_result_payload(self, profile: SutProfile) -> None:
        record = rec(
            frame(0, "customer", "refund"),
            frame(1, "agent", None, FrameKind.RESULT, data={"refunds": [{"status": "approved"}]}),
        )
        ok = await run(
            "content.json_path_equals",
            record,
            profile,
            path="data.refunds.0.status",
            equals="approved",
        )
        assert passed(ok) and ok.details["checked"][0]["observed"] == "approved"
        bad = await run(
            "content.json_path_equals",
            record,
            profile,
            path="data.refunds.0.status",
            equals="denied",
        )
        assert not passed(bad)
        missing = await run("content.json_path_equals", record, profile, path="data.x", equals=1)
        assert not passed(missing) and "missing" in missing.details["checked"][0]["problem"]

    async def test_no_result_fails(self, profile: SutProfile) -> None:
        result = await run("content.json_path_equals", CHAT, profile, path="a", equals=1)
        assert result.status is ScoreStatus.FAILED and "no result frame" in result.details["note"]

    async def test_message_json_and_fenced_json(self, profile: SutProfile) -> None:
        record = rec(
            frame(0, "customer", "status?"),
            frame(1, "agent", "Let me check."),
            frame(2, "agent", '```json\n{"status": "approved", "amount": 20}\n```'),
        )
        assert passed(
            await run(
                "content.json_path_equals",
                record,
                profile,
                source="message",
                path="amount",
                equals=20.0,
            )
        )
        anywhere = await run(
            "content.json_path_equals",
            record,
            profile,
            source="message",
            path="status",
            equals="approved",
            scope="every_turn",
        )
        assert not passed(anywhere)  # "Let me check." is not JSON
        assert anywhere.details["checked"][0]["problem"] == "not JSON"

    async def test_a_message_that_is_not_json_fails(self, profile: SutProfile) -> None:
        result = await run(
            "content.json_path_equals", CHAT, profile, source="message", path="a", equals=1
        )
        assert result.status is ScoreStatus.FAILED

    async def test_checkpoint_payload(self, profile: SutProfile) -> None:
        record = rec(
            frame(0, "customer", "refund"),
            interrupts=(
                InterruptRecord(
                    seq=1,
                    checkpoint="proposal_approval",
                    occurrence=1,
                    payload={"proposal": {"amount": 49.5, "method": "original_card"}},
                    action="approve",
                ),
            ),
        )
        ok = await run(
            "content.json_path_equals",
            record,
            profile,
            source="checkpoint",
            checkpoint="proposal_approval",
            path="proposal.method",
            equals="original_card",
        )
        assert passed(ok)
        never = await run(
            "content.json_path_equals",
            rec(frame(0, "customer", "x")),
            profile,
            source="checkpoint",
            checkpoint="proposal_approval",
            path="proposal.method",
            equals="original_card",
        )
        assert never.status is ScoreStatus.FAILED

    def test_json_equality_is_typed(self) -> None:
        assert json_equal(1, 1.0)
        assert not json_equal(True, 1) and not json_equal(1, True) and not json_equal(0, False)
        assert json_equal({"a": [1, None]}, {"a": [1.0, None]})
        assert not json_equal({"a": 1}, {"a": 1, "b": 2})
        assert not json_equal("1", 1) and json_equal(None, None)


def test_normalise() -> None:
    assert normalise("  Café  au\tLAIT ", case_sensitive=False, ignore_diacritics=True) == (
        "cafe au lait"
    )
    assert normalise("مـرحـبـا", case_sensitive=True, ignore_diacritics=True) == "مرحبا"
