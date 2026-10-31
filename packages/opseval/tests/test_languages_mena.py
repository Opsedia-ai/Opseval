# SPDX-License-Identifier: Apache-2.0
"""The MENA step of ADR-0059: Arabic packs by country and region, Arabic–English
code-switching, ``output.language_script`` v2 by Unicode script, and judges told a
human-readable dialect name. Every name that resolved exactly before still resolves to the
same pack; only declared fallbacks (which warned) moved to a pack of their own."""

from __future__ import annotations

from typing import Any

import pytest

from opseval.advice import scenario_warnings
from opseval.langpacks import (
    LEGACY_LABELS,
    PACKS,
    by_tag,
    effective_language,
    in_scripts,
    letter_scripts,
    lookup,
    resolve,
    voice_language,
)
from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import FrameRecord, RunRecord
from opseval.models.results import ScoreStatus
from opseval.models.scenario import Scenario, ScorerSpec
from opseval.scoring import ScoringContext
from opseval.scoring.deterministic import LanguageScript
from opseval.scoring.judges import LanguageConsistency, PersonaConsistency
from opseval.simulator import LLMSimulator

from .conftest import make_scenario

LLM = {"scripted_turns": [], "flow": "ask for a refund"}

# --- names ---------------------------------------------------------------------------------

# Everything that resolved *exactly* (no warning) before the MENA step, and its pack then.
# Not one of these may change: a saved scenario keeps its meaning and its rubric digest.
BEFORE_EXACT = [
    ("en", None, "en"),
    ("ar", None, "ar"),
    ("ar", "msa", "ar"),
    ("ar", "egyptian", "ar-EG"),
    ("ar", "eg", "ar-EG"),
    ("ar", "egypt", "ar-EG"),
    ("ar", "masri", "ar-EG"),
    ("ar-EG", None, "ar-EG"),
    ("ar", "gulf", "ar-x-gulf"),
    ("ar", "khaleeji", "ar-x-gulf"),
    ("ar", "saudi", "ar-x-gulf"),
    ("ar", "emirati", "ar-x-gulf"),
    ("ar-x-gulf", None, "ar-x-gulf"),
]


@pytest.mark.parametrize(("language", "dialect", "tag"), BEFORE_EXACT)
def test_every_exact_name_from_before_keeps_its_pack(
    language: str, dialect: str | None, tag: str
) -> None:
    found = lookup(language, dialect)
    assert (found.pack.tag, found.fallback_from) == (tag, None)


# Declared fallbacks before the MENA step (each loaded with a "no pack yet" warning), and the
# pack each resolves to now. The ones that moved got a pack of their own, as the warning said.
FALLBACKS = [
    # (language, dialect, pack before, pack now, still a fallback)
    ("ar-SA", None, "ar-x-gulf", "ar-SA", False),
    ("ar-AE", None, "ar-x-gulf", "ar-AE", False),
    ("ar-KW", None, "ar-x-gulf", "ar-KW", False),
    ("ar", "kuwaiti", "ar-x-gulf", "ar-KW", False),
    ("ar-QA", None, "ar-x-gulf", "ar-x-gulf", True),
    ("ar-BH", None, "ar-x-gulf", "ar-x-gulf", True),
    ("ar-OM", None, "ar-x-gulf", "ar-x-gulf", True),
    ("ar", "qatari", "ar-x-gulf", "ar-x-gulf", True),
    ("ar", "bahraini", "ar-x-gulf", "ar-x-gulf", True),
    ("ar", "omani", "ar-x-gulf", "ar-x-gulf", True),
    ("ar", "levantine", "ar", "ar-x-levant", False),
    ("ar", "lebanese", "ar", "ar-LB", False),
    ("ar-LB", None, "ar", "ar-LB", False),
    ("ar", "jordanian", "ar", "ar-JO", False),
    ("ar-JO", None, "ar", "ar-JO", False),
    ("ar", "syrian", "ar", "ar-x-levant", True),
    ("ar-SY", None, "ar", "ar-x-levant", True),
    ("ar", "palestinian", "ar", "ar-x-levant", True),
    ("ar-PS", None, "ar", "ar-x-levant", True),
    ("ar", "iraqi", "ar", "ar-IQ", False),
    ("ar-IQ", None, "ar", "ar-IQ", False),
    ("ar", "moroccan", "ar", "ar-MA", False),
    ("ar-MA", None, "ar", "ar-MA", False),
    ("ar", "algerian", "ar", "ar-DZ", False),
    ("ar-DZ", None, "ar", "ar-DZ", False),
    ("ar", "tunisian", "ar", "ar-TN", False),
    ("ar-TN", None, "ar", "ar-TN", False),
    ("ar", "maghrebi", "ar", "ar", True),
    ("ar", "libyan", "ar", "ar", True),
    ("ar", "sudanese", "ar", "ar", True),
    ("ar", "yemeni", "ar", "ar", True),
    ("ar-LY", None, "ar", "ar", True),
    ("ar-SD", None, "ar", "ar", True),
    ("ar-YE", None, "ar", "ar", True),
    # The US/EU step gave these a pack of their own, as the warning said.
    ("en-US", None, "en", "en-US", False),
    ("en", "british", "en", "en-GB", False),
    ("en-AU", None, "en", "en", True),
]


@pytest.mark.parametrize(("language", "dialect", "before", "now", "fallback"), FALLBACKS)
def test_old_fallbacks_resolve_sensibly(
    language: str, dialect: str | None, before: str, now: str, fallback: bool
) -> None:
    found = lookup(language, dialect)
    assert found.pack.tag == now
    assert (found.fallback_from is not None) is fallback


@pytest.mark.parametrize(
    ("language", "dialect", "tag"),
    [
        ("ar-SA", None, "ar-SA"),
        ("ar", "ksa", "ar-SA"),
        ("ar", "SA", "ar-SA"),
        ("ar", "saudi-arabia", "ar-SA"),
        ("ar_sa", None, "ar-SA"),
        ("ar-AE", None, "ar-AE"),
        ("ar", "uae", "ar-AE"),
        ("ar-KW", "kuwaiti", "ar-KW"),
        ("ar-x-levant", None, "ar-x-levant"),
        ("ar", "shami", "ar-x-levant"),
        ("ar-LB", "lebanese", "ar-LB"),
        ("ar-JO", None, "ar-JO"),
        ("ar", "iraqi", "ar-IQ"),
        ("ar", "darija", "ar-MA"),
        ("AR-ma", None, "ar-MA"),
        ("ar-DZ", None, "ar-DZ"),
        ("ar-TN", None, "ar-TN"),
        ("ar-x-codeswitch", None, "ar-x-codeswitch"),
        ("ar", "codeswitch", "ar-x-codeswitch"),
        ("ar", "code-switching", "ar-x-codeswitch"),
        ("ar", "arabic-english", "ar-x-codeswitch"),
    ],
)
def test_every_new_tag_and_alias_resolves(language: str, dialect: str | None, tag: str) -> None:
    found = lookup(language, dialect)
    assert (found.pack.tag, found.fallback_from) == (tag, None)


def test_every_pack_is_described_and_only_english_is_stable() -> None:
    tags = [p.tag for p in PACKS]
    assert len(tags) == len(set(tags))
    for pack in PACKS:
        assert pack.name and pack.instruction
        assert pack.stable is (pack.tag == "en"), pack.tag
        if pack.code == "ar":
            assert "arabic" in pack.scripts
        for example in pack.examples:
            # an example in the wrong script would teach the simulator the wrong script
            assert in_scripts(example, pack.scripts), (pack.tag, example)
        assert by_tag(pack.tag) is pack
    assert by_tag("AR_sa") is resolve("ar-SA", None)
    assert by_tag("zz") is None and by_tag(None) is None


def test_narrower_names_point_at_real_packs() -> None:
    for pack in PACKS:
        for alias, tag in pack.narrower:
            assert alias in pack.aliases
            assert by_tag(tag) is not None and by_tag(tag) is not pack


@pytest.mark.parametrize(("dialect", "tag"), [("saudi", "ar-SA"), ("emirati", "ar-AE")])
def test_a_kept_alias_names_the_narrower_pack(dialect: str, tag: str, profile: SutProfile) -> None:
    """`saudi` still writes Gulf Arabic (nothing switches silently); advice says ar-SA exists."""
    sc = make_scenario(
        persona={"role": "r", "language": "ar", "dialect": dialect},
        conversation=LLM,
        execution={"repeats": 3},
    )
    assert lookup("ar", dialect).pack.tag == "ar-x-gulf"
    warnings = [w for w in scenario_warnings(sc, "x.yaml", profile) if w.field.startswith("pers")]
    assert [w.field for w in warnings] == ["persona.dialect"]
    assert tag in warnings[0].message and "ar-x-gulf" in warnings[0].message
    scripted = make_scenario(
        persona={"role": "r", "language": "ar", "dialect": dialect}, execution={"repeats": 3}
    )
    assert scenario_warnings(scripted, "x.yaml", profile) == []


def test_a_new_pack_loads_without_a_language_warning(profile: SutProfile) -> None:
    sc = make_scenario(
        persona={"role": "r", "language": "ar-SA"}, conversation=LLM, execution={"repeats": 3}
    )
    assert [w for w in scenario_warnings(sc, "x.yaml", profile) if w.field.startswith("pers")] == []


# --- the simulator ------------------------------------------------------------------------


def _system(**persona: Any) -> str:
    sc = make_scenario(persona={"role": "customer", **persona}, conversation=LLM)
    return LLMSimulator(sc, 1, client=None)._system()  # type: ignore[arg-type]


@pytest.mark.parametrize("tag", [p.tag for p in PACKS])
def test_the_simulator_is_told_each_packs_instruction_and_examples(tag: str) -> None:
    pack = resolve(tag, None) if tag != "en" else resolve("en", None)
    system = _system(language=tag)
    assert pack.instruction in system
    for example in pack.examples:
        assert f"- {example}" in system


def test_the_codeswitch_pack_mixes_english_in_latin_letters() -> None:
    pack = resolve("ar", "codeswitch")
    assert pack.mixes == ("ar", "en") and pack.scripts == ("arabic", "latin")
    assert "Latin letters" in pack.instruction
    assert all(letter_scripts(e)["arabic"] or letter_scripts(e)["latin"] for e in pack.examples)
    assert any(letter_scripts(e)["latin"] and letter_scripts(e)["arabic"] for e in pack.examples)


# --- judges -------------------------------------------------------------------------------


def _told(scenario: Scenario, profile: SutProfile) -> tuple[list[str], list[str]]:
    ctx = ScoringContext(scenario=scenario, profile=profile)
    record: Any = None  # neither context() reads the record
    return LanguageConsistency().context(record, ctx), PersonaConsistency().context(record, ctx)


@pytest.mark.parametrize(
    ("language", "dialect", "label"),
    [
        ("ar-SA", None, "Arabic (Saudi), ar-SA"),
        ("ar", "ksa", "Arabic (Saudi), ar-SA"),
        ("ar", "kuwaiti", "Arabic (Kuwaiti), ar-KW"),
        ("ar", "darija", "Arabic (Moroccan Darija), ar-MA"),
        ("ar-x-levant", None, "Arabic (Levantine), ar-x-levant"),
        ("ar-SY", None, "Arabic (Levantine), ar-x-levant"),  # what the simulator writes
    ],
)
def test_new_packs_are_named_for_the_judges(
    language: str, dialect: str | None, label: str, profile: SutProfile
) -> None:
    sc = make_scenario(
        persona={"role": "customer", "language": language, "dialect": dialect}, conversation=LLM
    )
    lang, persona = _told(sc, profile)
    assert lang[0] == f"Expected customer language: {label}"
    assert persona[3] == f"Persona language: {label}"
    tag = label.rsplit(", ", 1)[1]
    ctx = ScoringContext(scenario=sc, profile=profile)
    assert tag in LanguageConsistency().rubric(ctx)
    assert tag in PersonaConsistency().rubric(ctx)


def test_only_the_four_legacy_packs_keep_the_old_label() -> None:
    assert {"en", "ar", "ar-EG", "ar-x-gulf"} == LEGACY_LABELS
    for pack in PACKS:
        persona = make_scenario(persona={"role": "r", "language": pack.tag}).persona
        spoken = effective_language(persona, scripted=True)
        assert (spoken.name is None) is (pack.tag in LEGACY_LABELS)


def test_a_saudi_alias_and_the_saudi_pack_grade_differently(profile: SutProfile) -> None:
    """`saudi` is the Gulf pack (so the old label and digest); `ar-SA` is graded as itself."""
    alias = make_scenario(
        persona={"role": "c", "language": "ar", "dialect": "saudi"}, conversation=LLM
    )
    tag = make_scenario(persona={"role": "c", "language": "ar-SA"}, conversation=LLM)
    assert _told(alias, profile)[0][0] == "Expected customer language: ar (gulf)"
    a = LanguageConsistency().rubric_hash(ScoringContext(scenario=alias, profile=profile))
    b = LanguageConsistency().rubric_hash(ScoringContext(scenario=tag, profile=profile))
    assert a != b


def test_the_codeswitch_pack_tells_the_judges_the_mix(profile: SutProfile) -> None:
    sc = make_scenario(persona={"role": "c", "language": "ar-x-codeswitch"}, conversation=LLM)
    lang, persona = _told(sc, profile)
    assert "CODE-SWITCHES" in lang[1] and "ar, en" in lang[1]
    assert "CODE-SWITCH" in persona[4] and "ar, en" in persona[4]
    ctx = ScoringContext(scenario=sc, profile=profile)
    assert "code_switching=ar,en" in LanguageConsistency().rubric(ctx)
    plain = make_scenario(persona={"role": "c", "language": "ar-SA"}, conversation=LLM)
    assert len(_told(plain, profile)[0]) == 1


def test_voice_code_switching_still_wins_over_the_pack(profile: SutProfile) -> None:
    """The same languages, as the loader requires (a different mix does not load: see
    test_languages_review): the scenario's own order is what the judges are told."""
    sc = make_scenario(
        persona={"role": "c", "language": "ar-x-codeswitch"},
        conversation=LLM,
        voice={"code_switching": ["en", "ar"], "caller_voice": "v"},
    )
    assert "en, ar" in _told(sc, profile)[0][1]


# --- voice --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("persona", "scripted", "spoken"),
    [
        ({"language": "ar", "dialect": "ksa"}, False, "ar-SA"),
        ({"language": "ar", "dialect": "egyptian"}, True, "ar-EG"),
        ({"language": "ar", "dialect": "yemeni"}, False, "ar"),
        ({"language": "ar", "dialect": "yemeni"}, True, "ar"),
        ({"language": "fr"}, True, "fr-FR"),
        ({"language": "sw"}, True, "sw"),
        ({"language": "en"}, False, "en"),
    ],
)
def test_voice_runs_speak_the_pack_tag(
    persona: dict[str, str], scripted: bool, spoken: str
) -> None:
    sc = make_scenario(
        persona={"role": "r", **persona}, **({} if scripted else {"conversation": LLM})
    )
    assert voice_language(sc.persona, scripted=scripted) == spoken


def test_the_voice_map_finds_a_pack_tag() -> None:
    from opseval.speech.guard import resolve_voice

    mapping = {"ar-SA": "voice-sa", "ar": "voice-ar", "default": "voice-any"}
    assert resolve_voice(mapping, None, "ar-SA") == "voice-sa"
    assert resolve_voice(mapping, None, "ar-x-levant") == "voice-ar"
    assert resolve_voice(mapping, None, "en") == "voice-any"


# --- output.language_script v2 --------------------------------------------------------------


def _record(*texts: str) -> RunRecord:
    frames = [
        FrameRecord.model_construct(seq=i, direction="received", kind=FrameKind.MESSAGE, text=t)
        for i, t in enumerate(texts)
    ]
    return RunRecord.model_construct(frames=frames)


async def _score(scenario: Scenario | None, *texts: str, **params: Any) -> Any:
    spec = ScorerSpec(id="output.language_script", params=params)
    ctx: Any = ScoringContext(scenario=scenario, profile=None) if scenario else None  # type: ignore[arg-type]
    return await LanguageScript().score(_record(*texts), spec, ctx)


@pytest.mark.parametrize(
    ("script", "text"),
    [
        ("arabic", "طلبك في الطريق، يوصل بكرة"),
        ("latin", "Your order is on its way"),
        ("han", "您的订单正在配送中"),
        ("kana", "ありがとうございます"),
        ("hangul", "주문이 배송 중입니다"),
        ("cyrillic", "Ваш заказ в пути"),
        ("devanagari", "आपका ऑर्डर रास्ते में है"),
        ("hebrew", "ההזמנה שלך בדרך"),
        ("greek", "Η παραγγελία σας είναι καθ' οδόν"),
        ("thai", "คำสั่งซื้อของคุณกำลังจัดส่ง"),
    ],
)
async def test_each_script_is_counted(script: str, text: str) -> None:
    passed = await _score(None, text, script=script)
    assert passed.status is ScoreStatus.PASSED, passed
    other = "latin" if script != "latin" else "arabic"
    failed = await _score(None, text, script=other)
    assert failed.status is ScoreStatus.FAILED


def test_every_script_a_pack_can_name_is_counted() -> None:
    from typing import get_args

    from opseval.langpacks import SCRIPT_PREFIXES, Script

    assert set(SCRIPT_PREFIXES) == set(get_args(Script))


def test_the_majority_script_wins_and_digits_never_count() -> None:
    assert in_scripts("طلبك رقم 10482 في الطريق, order", ("arabic",))
    assert not in_scripts("Your order 10482, شكرا", ("arabic",))
    assert not in_scripts("10482 !!", ("arabic",))  # no letters: not an answer in the script
    assert not in_scripts("", ("latin",))
    assert letter_scripts("abc ΑΒΓ")["greek"] == 3
    assert letter_scripts("ⴰⵣⵓⵍ")["other"] == 4  # Tifinagh is a script, just not one we name


async def test_the_default_script_comes_from_the_pack(profile: SutProfile) -> None:
    arabic = make_scenario(persona={"role": "c", "language": "ar-MA"}, conversation=LLM)
    english = make_scenario(persona={"role": "c", "language": "en"}, conversation=LLM)
    mixed = make_scenario(persona={"role": "c", "language": "ar-x-codeswitch"}, conversation=LLM)
    reply = "الطلب ديالك غادي يوصل غدا"
    assert (await _score(arabic, reply)).status is ScoreStatus.PASSED
    assert (await _score(english, reply)).status is ScoreStatus.FAILED
    out = await _score(mixed, reply, "الـ refund حقك approved، بيوصلك خلال يومين")
    assert out.status is ScoreStatus.PASSED and out.details["scripts"] == ["arabic", "latin"]
    # Mostly Arabic is the pack's rule: an all-English reply is not code-switching.
    out = await _score(mixed, reply, "Your refund is approved")
    assert out.status is ScoreStatus.FAILED and out.details["in_script"] == 1
    assert (await _score(mixed, "您的订单")).status is ScoreStatus.FAILED


async def test_no_pack_and_no_script_is_not_measured_never_a_guess() -> None:
    swahili = make_scenario(persona={"role": "c", "language": "sw"})
    out = await _score(swahili, "Agizo lako linakuja")
    assert out.status is ScoreStatus.INFRA_ERROR and "BAD_PARAMS" in (out.reason or "")
    out = await _score(swahili, "Agizo lako linakuja", script="latin")
    assert out.status is ScoreStatus.PASSED


async def test_v1_params_still_score_as_before() -> None:
    assert LanguageScript.version == "2"
    out = await _score(None, "طلبك في الطريق", "Okay", script="arabic", min_fraction=0.5)
    assert out.status is ScoreStatus.PASSED and out.score == 0.5
    out = await _score(None, "x", script="cjk")
    assert out.status is ScoreStatus.INFRA_ERROR
