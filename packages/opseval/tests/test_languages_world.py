# SPDX-License-Identifier: Apache-2.0
"""The US/EU and Chinese steps of ADR-0059: ``en-US``, ``en-GB``, ``fr-FR``, ``de-DE``,
``es-ES``, ``it-IT``, ``zh-CN`` (Mandarin, Simplified) and ``zh-HK`` (Cantonese, Traditional).

``en`` stays the stable base pack, unchanged. ``fr``, ``de``, ``es`` and ``it`` alone are the
country pack. ``zh`` alone names no pack, and the two Chinese packs never stand in for each
other: a Cantonese customer is never simulated, graded or spoken as Mandarin silently."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opseval import langpacks
from opseval.advice import scenario_warnings
from opseval.catalogue import check_scenario_text
from opseval.langpacks import (
    PACKS,
    UnknownLanguage,
    by_tag,
    effective_language,
    graded_language,
    in_scripts,
    lookup,
    provider_language,
    resolve,
    script_expectation,
    voice_language,
)
from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import FrameRecord, RunRecord
from opseval.models.results import ScoreStatus
from opseval.models.scenario import Scenario, ScorerSpec
from opseval.scoring import INVARIANTS, REGISTRY, ScoringContext
from opseval.scoring.deterministic import LanguageScript
from opseval.scoring.judges import LanguageConsistency, PersonaConsistency
from opseval.simulator import LLMSimulator, ScriptedSimulator
from opseval.simulator.typing_noise import LATIN_NEAR, LAYOUTS, add_typing_noise

from .conftest import make_scenario

LLM = {"scripted_turns": [], "flow": "ask for a refund"}
NEW = ["en-US", "en-GB", "fr-FR", "de-DE", "es-ES", "it-IT", "zh-CN", "zh-HK"]

# --- the packs -----------------------------------------------------------------------------


@pytest.mark.parametrize("tag", NEW)
def test_every_new_pack_is_a_described_preview(tag: str) -> None:
    pack = by_tag(tag)
    assert pack is not None and pack.tag == tag
    assert pack.stable is False  # preview until the promotion steps (docs/guide/languages.md)
    assert pack.name and pack.instruction and len(pack.examples) >= 3
    langpacks._check_pack(pack)  # what register() checks
    for example in pack.examples:
        assert in_scripts(example, pack.scripts), (tag, example)
    # The simulator is told the variant by name, not only the language.
    assert pack.name.split(" (")[0] in pack.instruction or pack.code == "zh"


def test_english_stays_the_stable_base_pack() -> None:
    en = resolve("en", None)
    assert en.tag == "en" and en.stable and en.dialect is None and en.written_form == ""
    assert lookup("en", None).fallback_from is None


@pytest.mark.parametrize(
    ("language", "dialect", "tag"),
    [
        ("en-US", None, "en-US"),
        ("en", "american", "en-US"),
        ("en", "US", "en-US"),
        ("EN_us", None, "en-US"),
        ("en-GB", None, "en-GB"),
        ("en", "british", "en-GB"),
        ("en", "uk", "en-GB"),
        ("fr", None, "fr-FR"),
        ("fr-FR", None, "fr-FR"),
        ("FR_fr", None, "fr-FR"),
        ("de", None, "de-DE"),
        ("de-DE", None, "de-DE"),
        ("es", None, "es-ES"),
        ("es-ES", None, "es-ES"),
        ("it", None, "it-IT"),
        ("it-IT", None, "it-IT"),
        ("zh-CN", None, "zh-CN"),
        ("zh", "mandarin", "zh-CN"),
        ("zh", "CN", "zh-CN"),
        ("zh-Hans-CN", None, "zh-CN"),
        ("zh-HK", None, "zh-HK"),
        ("zh", "cantonese", "zh-HK"),
        ("zh_hk", None, "zh-HK"),
        ("zh-Hant-HK", None, "zh-HK"),
        ("zh-HK", "cantonese", "zh-HK"),
    ],
)
def test_tags_and_aliases_resolve_exactly(language: str, dialect: str | None, tag: str) -> None:
    found = lookup(language, dialect)
    assert (found.pack.tag, found.fallback_from) == (tag, None)


@pytest.mark.parametrize(
    ("language", "dialect"),
    [
        ("zh", None),  # no base: Mandarin or Cantonese must be named
        ("zh-TW", None),  # Taiwan Mandarin is neither pack
        ("zh-Hant", None),
        ("zh-Hans", None),
        ("zh-MO", None),
        ("zh-SG", None),
        ("zh-Hant-TW", None),
        ("zh", "taiwanese"),
        ("es-MX", None),  # another region of a language is not its European pack
        ("es-419", None),
        ("fr-CA", None),
        ("pt-BR", None),
        ("pt", None),
        ("de-AT", None),
        ("sw", None),
    ],
)
def test_what_has_no_pack_is_refused(language: str, dialect: str | None) -> None:
    with pytest.raises(UnknownLanguage):
        resolve(language, dialect)


@pytest.mark.parametrize(
    ("language", "dialect"),
    [("zh-CN", "cantonese"), ("zh-HK", "mandarin"), ("zh-Hans-CN", "cantonese")],
)
def test_mandarin_and_cantonese_named_together_are_refused(language: str, dialect: str) -> None:
    with pytest.raises(UnknownLanguage, match="different packs"):
        resolve(language, dialect)


def test_cantonese_never_falls_back_to_mandarin() -> None:
    """No pack declares a fallback for Chinese, so nothing reaches zh-CN but its own names."""
    assert not by_tag("zh-CN").fallback_for and not by_tag("zh-HK").fallback_for  # type: ignore[union-attr]
    assert not [key for key in langpacks._FALLBACK if key[0] == "zh"]
    cantonese_names = ["zh-HK", "zh-Hant-HK", "zh_HK"]
    for name in cantonese_names:
        assert resolve(name, None).tag == "zh-HK"
    for sub in ("cantonese", "hk", "hant-hk"):
        assert resolve("zh", sub).tag == "zh-HK"
    for key, pack in langpacks._EXACT.items():
        if pack.tag == "zh-CN":
            assert key[1] in {"cn", "mandarin", "hans-cn"}


def test_a_us_and_gb_pair_keeps_the_english_it_had(profile: SutProfile) -> None:
    """``en-GB`` + ``american`` loaded before (both English fallbacks): it keeps ``en``."""
    found = lookup("en-GB", "american")
    assert found.pack.tag == "en" and set(found.kept_pair) == {"en-GB", "en-US"}
    sc = make_scenario(
        persona={"role": "r", "language": "en-GB", "dialect": "american"},
        conversation=LLM,
        execution={"repeats": 3},
    )
    warned = [w for w in scenario_warnings(sc, "x.yaml", profile) if w.field.startswith("pers")]
    assert len(warned) == 1 and "en-GB" in warned[0].message


def test_other_english_regions_stay_english_fallbacks() -> None:
    for name in ("en-AU", "en-NZ", "en-IE", "en-CA"):
        found = lookup(name, None)
        assert found.pack.tag == "en" and found.fallback_from == name


def test_a_new_pack_loads_without_a_language_warning(profile: SutProfile) -> None:
    for tag in NEW:
        sc = make_scenario(
            persona={"role": "r", "language": tag}, conversation=LLM, execution={"repeats": 3}
        )
        assert [w for w in scenario_warnings(sc, "x.yaml", profile) if "persona" in w.field] == []


def test_an_unknown_chinese_persona_is_a_load_error(profile: SutProfile) -> None:
    text = (
        "id: zh-001\nname: n\ngoal: g\npersona: {role: r, language: zh}\n"
        "conversation: {flow: ask for a refund}\n"
    )
    sc, problems = check_scenario_text(
        text, "x.yaml", profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
    )
    assert [p.field for p in problems] == ["persona.language"]
    assert "zh-CN" in problems[0].message and "zh-HK" in problems[0].message


# --- what the simulator is told -----------------------------------------------------------


def _system(**persona: Any) -> str:
    sc = make_scenario(persona={"role": "customer", **persona}, conversation=LLM)
    return LLMSimulator(sc, 1, client=None)._system()  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("language", "dialect", "named"),
    [
        ("zh", "cantonese", "Language: Chinese (Cantonese, Hong Kong) (zh-HK). "),
        ("zh-CN", None, "Language: Chinese (Mandarin, Simplified) (zh-CN). "),
        ("fr", None, "Language: French (France) (fr-FR). "),
        ("en", "american", "Language: English (United States) (en-US). "),
        ("ar-SA", None, "Language: Arabic (Saudi) (ar-SA). "),
    ],
)
def test_the_persona_prompt_names_the_pack(language: str, dialect: str | None, named: str) -> None:
    system = _system(language=language, dialect=dialect)
    pack = resolve(language, dialect)
    assert f"{named}{pack.instruction}" in system
    for example in pack.examples:
        assert f"- {example}" in system


def test_the_older_packs_keep_their_prompt() -> None:
    for language, dialect in [("en", None), ("ar", None), ("ar", "egyptian"), ("ar", "gulf")]:
        pack = resolve(language, dialect)
        assert f"Language: {pack.instruction}" in _system(language=language, dialect=dialect)


def test_the_chinese_packs_state_their_characters() -> None:
    assert "Simplified" in resolve("zh-CN", None).instruction
    hk = resolve("zh-HK", None)
    # The register the simulator writes (ADR-0059): written Cantonese, Traditional characters.
    assert "Traditional" in hk.instruction and "written Cantonese" in hk.instruction
    assert any(word in "".join(hk.examples) for word in ("唔", "咗", "嘅", "喺"))


# --- what the judges are told -------------------------------------------------------------


def _told(scenario: Scenario, profile: SutProfile) -> tuple[list[str], list[str]]:
    ctx = ScoringContext(scenario=scenario, profile=profile)
    record: Any = None  # neither context() reads the record
    return LanguageConsistency().context(record, ctx), PersonaConsistency().context(record, ctx)


def test_the_cantonese_judges_are_told_both_written_forms(profile: SutProfile) -> None:
    sc = make_scenario(persona={"role": "c", "language": "zh-HK"}, conversation=LLM)
    lang, persona = _told(sc, profile)
    assert lang[0] == "Expected customer language: Chinese (Cantonese, Hong Kong), zh-HK"
    assert lang[1].startswith("Expected written form: Hong Kong Chinese in Traditional")
    assert "Standard Written Chinese" in lang[1] and "both acceptable" in lang[1]
    assert "Simplified characters" in lang[1]
    assert persona[3] == "Persona language: Chinese (Cantonese, Hong Kong), zh-HK"
    assert persona[4] == lang[1]
    ctx = ScoringContext(scenario=sc, profile=profile)
    rubric = LanguageConsistency().rubric(ctx)
    assert "zh-HK" in rubric and any(r.startswith("written_form=") for r in rubric)


def test_mandarin_and_cantonese_grade_differently(profile: SutProfile) -> None:
    def rubric(language: str) -> tuple[str, ...]:
        sc = make_scenario(persona={"role": "c", "language": language}, conversation=LLM)
        return LanguageConsistency().rubric(ScoringContext(scenario=sc, profile=profile))

    assert rubric("zh-CN") != rubric("zh-HK")


@pytest.mark.parametrize(
    ("language", "label", "form"),
    [
        ("en-US", "English (United States), en-US", True),
        ("en-GB", "English (United Kingdom), en-GB", True),
        ("fr", "French (France), fr-FR", False),
        ("de-DE", "German (Germany), de-DE", False),
        ("es", "Spanish (Spain), es-ES", False),
        ("it-IT", "Italian (Italy), it-IT", False),
        ("zh-CN", "Chinese (Mandarin, Simplified), zh-CN", True),
    ],
)
def test_the_judges_name_each_pack(
    language: str, label: str, form: bool, profile: SutProfile
) -> None:
    sc = make_scenario(persona={"role": "c", "language": language}, conversation=LLM)
    lang, _ = _told(sc, profile)
    assert lang[0] == f"Expected customer language: {label}"
    assert (len(lang) == 2) is form


def test_english_is_told_as_before(profile: SutProfile) -> None:
    sc = make_scenario(persona={"role": "c", "language": "en"}, conversation=LLM)
    lang, _ = _told(sc, profile)
    assert lang == ["Expected customer language: en"]
    ctx = ScoringContext(scenario=sc, profile=profile)
    assert LanguageConsistency().rubric(ctx) == ("en", "")


def test_a_run_recorded_before_the_step_keeps_english(profile: SutProfile) -> None:
    """``en-US`` was an English fallback: a saved attempt is graded against what it was told."""
    sc = make_scenario(persona={"role": "c", "language": "en-US"}, conversation=LLM)
    assert graded_language(sc.persona, scripted=False, recorded={"pack": "en"}).label == "en"
    legacy = graded_language(sc.persona, scripted=False, recorded={"language": "en-US"})
    assert legacy.tag == "en"
    assert effective_language(sc.persona, scripted=False).tag == "en-US"


# --- output.language_script ---------------------------------------------------------------


def _record(*texts: str) -> RunRecord:
    frames = [
        FrameRecord.model_construct(seq=i, direction="received", kind=FrameKind.MESSAGE, text=t)
        for i, t in enumerate(texts)
    ]
    return RunRecord.model_construct(frames=frames)


async def _score(scenario: Scenario, *texts: str, **params: Any) -> Any:
    spec = ScorerSpec(id="output.language_script", params=params)
    ctx = ScoringContext(scenario=scenario, profile=None)  # type: ignore[arg-type]
    return await LanguageScript().score(_record(*texts), spec, ctx)


@pytest.mark.parametrize(
    ("tag", "scripts"),
    [(t, ("latin",)) for t in NEW[:6]] + [("zh-CN", ("han",)), ("zh-HK", ("han",))],
)
def test_each_pack_expects_its_script(tag: str, scripts: tuple[str, ...]) -> None:
    sc = make_scenario(persona={"role": "c", "language": tag}, conversation=LLM)
    expected = script_expectation(sc.persona, scripted=False)
    assert expected is not None
    assert (expected.scripts, expected.source, expected.pack) == (scripts, "pack", tag)


async def test_han_is_checked_and_the_variant_is_not() -> None:
    """Simplified and Traditional are both Unicode Han: the script check passes either, and
    the written form is the language judge's to grade (documented, warned about)."""
    hk = make_scenario(persona={"role": "c", "language": "zh-HK"}, conversation=LLM)
    assert (await _score(hk, "你嘅訂單已經寄出咗")).status is ScoreStatus.PASSED
    assert (await _score(hk, "您的订单已经发货了")).status is ScoreStatus.PASSED
    assert (await _score(hk, "Your order has shipped")).status is ScoreStatus.FAILED
    cn = make_scenario(persona={"role": "c", "language": "zh-CN"}, conversation=LLM)
    assert (await _score(cn, "您的订单已经发货了，order 号码在短信里")).status is ScoreStatus.PASSED
    fr = make_scenario(persona={"role": "c", "language": "fr"}, conversation=LLM)
    assert (await _score(fr, "Votre commande est en route")).status is ScoreStatus.PASSED
    assert (await _score(fr, "您的订单")).status is ScoreStatus.FAILED


def test_the_script_check_without_the_judge_is_warned_about(profile: SutProfile) -> None:
    def warned(scorers: list[dict[str, Any]]) -> list[str]:
        sc = make_scenario(
            persona={"role": "c", "language": "zh-HK"},
            conversation=LLM,
            execution={"repeats": 3},
            scorers=scorers,
        )
        return [w.message for w in scenario_warnings(sc, "x.yaml", profile)]

    alone = [m for m in warned([{"id": "output.language_script"}]) if "written form" in m]
    assert len(alone) == 1 and "zh-HK" in alone[0] and "not measured" in alone[0]
    both = warned([{"id": "output.language_script"}, {"id": "judge.language_consistency"}])
    assert not [m for m in both if "written form" in m]


# --- typing noise ------------------------------------------------------------------------


def test_each_pack_names_its_keyboard() -> None:
    keyboards = {tag: by_tag(tag).keyboard for tag in NEW}  # type: ignore[union-attr]
    assert keyboards == {
        "en-US": "qwerty",
        "en-GB": "qwerty",
        "fr-FR": "azerty",
        "de-DE": "qwertz",
        "es-ES": "qwerty",
        "it-IT": "qwerty",
        "zh-CN": "ime",
        "zh-HK": "ime",
    }
    assert {p.keyboard for p in PACKS if p.code == "ar"} == {"qwerty"}  # unchanged


def test_the_layouts_differ_where_the_keyboards_do() -> None:
    assert set(LAYOUTS["qwerty"]["a"]) == {"s", "q", "z"}
    assert set(LAYOUTS["azerty"]["a"]) == {"z", "q"}
    assert "w" in LAYOUTS["azerty"]["q"]
    assert set(LAYOUTS["qwertz"]["z"]) == {"t", "u", "h"}
    assert "y" not in LAYOUTS["qwertz"]["t"]
    assert "ö" in LAYOUTS["qwertz"]["l"] and "ä" in LAYOUTS["qwertz"]["ö"]
    assert LAYOUTS["ime"] is LATIN_NEAR


SENTENCES = {
    "azerty": "Bonjour, ma commande numéro 10482 n'est toujours pas arrivée",
    "qwertz": "Hallo, meine Bestellung 10482 ist immer noch nicht angekommen",
    "qwerty": "hi, my package 10482 still hasn't shown up",
}


@pytest.mark.parametrize("keyboard", ["qwerty", "azerty", "qwertz"])
def test_noise_per_layout_is_deterministic(keyboard: str) -> None:
    text = SENTENCES[keyboard]
    once = add_typing_noise(text, 0.6, 11, keyboard)
    assert once == add_typing_noise(text, 0.6, 11, keyboard)
    assert once != text and "10482" in once
    assert any(add_typing_noise(text, 0.6, s, keyboard) != once for s in range(12, 20))


def test_qwerty_is_the_noise_from_before() -> None:
    text = "please refund order 10482 to my card"
    for seed in range(30):
        assert add_typing_noise(text, 0.7, seed) == add_typing_noise(text, 0.7, seed, "qwerty")


def test_azerty_slips_hit_azerty_neighbours() -> None:
    """Over many seeds, a substituted letter is always an AZERTY neighbour of the original."""
    word = "aaaaaa"
    for seed in range(200):
        noisy = add_typing_noise(word, 1.0, seed, "azerty")
        if len(noisy) == len(word) and noisy != word:
            subs = {c for c in noisy if c != "a"}
            assert subs <= set(LAYOUTS["azerty"]["a"]), (seed, noisy)


@pytest.mark.parametrize("seed", range(40))
def test_ime_noise_never_changes_chinese_characters(seed: int) -> None:
    text = "你好，我張單到而家都未到 可唔可以幫我 refund 返落張卡度？ 订单号 10482 HKD"
    noisy = add_typing_noise(text, 1.0, seed, "ime")
    han = [ch for ch in text if langpacks.script_of(ch) == "han"]
    assert [ch for ch in noisy if langpacks.script_of(ch) == "han"] == han
    assert "10482" in noisy and "HKD" in noisy


def test_ime_noise_still_mistypes_latin_words() -> None:
    text = "幫我 refund 返落張卡度 please"
    assert any(add_typing_noise(text, 1.0, s, "ime") != text for s in range(10))


def test_an_unknown_keyboard_is_refused() -> None:
    with pytest.raises(ValueError, match="keyboard"):
        add_typing_noise("hi there friend", 0.5, 1, "dvorak")
    with pytest.raises(ValueError, match="keyboard"):
        langpacks._check_pack(
            langpacks.LanguagePack(
                tag="xx",
                code="xx",
                dialect=None,
                instruction="i",
                scripts=("latin",),
                keyboard="dvorak",  # type: ignore[arg-type]
            )
        )


def test_typing_noise_on_a_chinese_pack_is_warned_about(profile: SutProfile) -> None:
    sc = make_scenario(
        persona={"role": "c", "language": "zh-CN", "typing_noise": 0.2},
        conversation=LLM,
        execution={"repeats": 3},
    )
    warned = [w for w in scenario_warnings(sc, "x.yaml", profile) if w.field.endswith("noise")]
    assert len(warned) == 1 and "input method" in warned[0].message


# --- scripted personas (offline; the demo agent speaks English) ----------------------------


async def test_a_scripted_french_customer_types_on_azerty() -> None:
    turn = "Bonjour, je voudrais un remboursement pour la commande 10482"
    sc = make_scenario(
        persona={"role": "c", "language": "fr-FR", "typing_noise": 0.8},
        conversation={"scripted_turns": [turn]},
    )
    sim = ScriptedSimulator(sc, seed=5)
    assert sim._keyboard() == "azerty"
    out = await sim.opening()
    assert out.text == add_typing_noise(turn, 0.8, 5 + 1, "azerty")
    assert "10482" in out.text


async def test_a_scripted_mandarin_customer_keeps_every_character() -> None:
    turns = ["您好，我的订单 10482 怎么还没到？", "可以退款吗"]
    sc = make_scenario(
        persona={"role": "c", "language": "zh-CN", "typing_noise": 1.0},
        conversation={"scripted_turns": turns},
    )
    sim = ScriptedSimulator(sc, seed=3)
    assert (await sim.opening()).text == turns[0]
    assert (await sim.next_turn([])).text == turns[1]


async def test_a_scripted_persona_with_no_pack_keeps_qwerty() -> None:
    sc = make_scenario(
        persona={"role": "c", "language": "sw", "typing_noise": 0.5},
        conversation={"scripted_turns": ["habari, agizo langu halijafika"]},
    )
    assert ScriptedSimulator(sc, seed=1)._keyboard() == "qwerty"


# --- voice ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("persona", "spoken", "given"),
    [
        ({"language": "en", "dialect": "american"}, "en-US", "en-US"),
        ({"language": "fr"}, "fr-FR", "fr-FR"),
        ({"language": "zh", "dialect": "cantonese"}, "zh-HK", "zh-HK"),
        ({"language": "zh-Hant-HK"}, "zh-HK", "zh-HK"),
        ({"language": "zh", "dialect": "mandarin"}, "zh-CN", "zh-CN"),
    ],
)
def test_voice_runs_speak_the_pack_tag(persona: dict[str, str], spoken: str, given: str) -> None:
    sc = make_scenario(persona={"role": "r", **persona}, conversation=LLM)
    assert voice_language(sc.persona, scripted=False) == spoken
    assert provider_language(spoken) == given


def test_the_voice_map_never_speaks_cantonese_with_a_mandarin_voice() -> None:
    from opseval.speech.guard import resolve_voice

    mapping = {"zh-CN": "mandarin-voice", "fr-FR": "french-voice", "default": "any"}
    assert resolve_voice(mapping, None, "zh-CN") == "mandarin-voice"
    assert resolve_voice(mapping, None, "zh-HK") == "any"  # never the zh-CN entry
    assert resolve_voice(mapping, None, "fr-FR") == "french-voice"
    assert resolve_voice({"en": "en-voice"}, None, "en-GB") == "en-voice"
    assert resolve_voice({"zh-HK": "yue-voice"}, None, "zh-HK") == "yue-voice"


# --- matrix --------------------------------------------------------------------------------


def test_a_language_matrix_resolves_a_pack_per_cell(tmp_path: Path) -> None:
    from .test_datasets import catalogue, load, mine, problems_of

    scenario: dict[str, Any] = {
        "id": "world",
        "name": "Refund in {{language}}",
        "goal": "Get a refund.",
        "matrix": {"language": ["en-US", "fr-FR", "zh-HK"]},
        "persona": {"role": "customer", "language": "en"},
        "conversation": {
            "flow": "Ask for a refund in {{language}}.",
            "max_turns": 4,
            "stop_at": "goal_reached",
        },
        "scorers": [{"id": "conversation.no_errors"}],
    }
    cat = load(catalogue(tmp_path, scenario, {}))
    assert not problems_of(cat)
    found = mine(cat, "world")
    tags = sorted(lookup(s.persona.language, s.persona.dialect).pack.tag for s in found)
    assert tags == ["en-US", "fr-FR", "zh-HK"]
    for s in found:
        assert effective_language(s.persona, scripted=False).tag == s.persona.language
