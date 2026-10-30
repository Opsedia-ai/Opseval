# SPDX-License-Identifier: Apache-2.0
"""Review of the MENA language packs (ADR-0059): nothing that loaded before stops loading, a
saved run keeps the language it was graded against, and ``output.language_script`` never
guesses a script, either way round."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from opseval import langpacks
from opseval.advice import scenario_warnings
from opseval.catalogue import check_references
from opseval.langpacks import (
    PACK_KEY,
    PACKS,
    SCRIPT_PREFIXES,
    LanguagePack,
    UnknownLanguage,
    graded_language,
    legacy_lookup,
    lookup,
    provider_language,
    register,
    script_expectation,
)
from opseval.models.profile import AudioConfig, FrameKind, SutProfile
from opseval.models.record import FrameRecord, RunRecord, TerminalState
from opseval.models.results import Axis, ScoreResult, ScoreStatus
from opseval.models.scenario import Scenario, ScorerSpec
from opseval.rescore import _Rescorer
from opseval.scoring import INVARIANTS, REGISTRY, ScoringContext, ScoringEngine
from opseval.scoring.deterministic import LanguageScript
from opseval.scoring.judges import LanguageConsistency
from opseval.speech import Audio, SpeechConfig, SpeechToText, TextToSpeech, Transcript
from opseval.speech.guard import GuardedSpeechToText, GuardedTextToSpeech
from opseval.voice.bridge import SCRIPTS, in_script

from .conftest import make_scenario

LLM = {"scripted_turns": [], "flow": "ask for a refund"}

# --- 1. a pair that loaded before still loads ------------------------------------------------

# language + dialect pairs that loaded before the MENA step and now name two packs: each keeps
# the pack it resolved to then, and names the packs it now also names.
KEPT_PAIRS = [
    ("ar-SA", "saudi", "ar-x-gulf", ("ar-SA",)),
    ("ar-SA", "gulf", "ar-x-gulf", ("ar-SA",)),
    ("ar-SA", "khaleeji", "ar-x-gulf", ("ar-SA",)),
    ("ar-SA", "emirati", "ar-x-gulf", ("ar-SA",)),
    ("ar-AE", "emirati", "ar-x-gulf", ("ar-AE",)),
    ("ar-AE", "gulf", "ar-x-gulf", ("ar-AE",)),
    ("ar-KW", "gulf", "ar-x-gulf", ("ar-KW",)),
    ("ar-KW", "saudi", "ar-x-gulf", ("ar-KW",)),
    ("ar-LB", "levantine", "ar", ("ar-LB", "ar-x-levant")),
    ("ar-JO", "levantine", "ar", ("ar-JO", "ar-x-levant")),
    ("ar-LB", "jordanian", "ar", ("ar-LB", "ar-JO")),
    ("ar-SY", "lebanese", "ar", ("ar-x-levant", "ar-LB")),
    ("ar-MA", "maghrebi", "ar", ("ar-MA",)),
    ("ar-MA", "algerian", "ar", ("ar-MA", "ar-DZ")),
    ("ar-IQ", "levantine", "ar", ("ar-IQ", "ar-x-levant")),
    ("ar-YE", "iraqi", "ar", ("ar-IQ",)),
]


@pytest.mark.parametrize(("language", "dialect", "before", "named"), KEPT_PAIRS)
def test_a_pair_that_loaded_before_keeps_its_pack(
    language: str, dialect: str, before: str, named: tuple[str, ...]
) -> None:
    assert legacy_lookup(language, dialect)[0] == before
    found = lookup(language, dialect)
    assert found.pack.tag == before
    assert set(found.kept_pair) == set(named)


def test_an_exact_name_beside_a_regional_tag_is_still_exact() -> None:
    """``ar-SA`` + ``gulf`` was the Gulf pack, exactly (``gulf`` named it); it still is."""
    found = lookup("ar-SA", "gulf")
    assert (found.pack.tag, found.fallback_from) == ("ar-x-gulf", None)


def _legacy_names() -> tuple[list[str], list[str | None]]:
    subs = {sub for code, sub in (*langpacks._LEGACY_EXACT, *langpacks._LEGACY_FALLBACK)}
    subs.discard(None)
    languages = ["ar", "en", *(f"ar-{s}" for s in subs), *(f"en-{s}" for s in subs)]
    return languages, [None, *sorted(s for s in subs if s)]


def test_nothing_that_loaded_before_is_refused_now() -> None:
    """Every language + dialect pair over every name the old packs knew: if it resolved
    before, it resolves now, and where the two fields now name different packs, to the pack
    it had."""
    languages, dialects = _legacy_names()
    checked = 0
    for language in languages:
        for dialect in dialects:
            try:
                before, _ = legacy_lookup(language, dialect)
            except UnknownLanguage:
                continue
            found = lookup(language, dialect)  # never raises
            if found.kept_pair:
                assert found.pack.tag == before, (language, dialect)
            checked += 1
    assert checked > 500


def test_a_pair_that_never_loaded_is_still_refused() -> None:
    with pytest.raises(UnknownLanguage, match="different packs"):
        lookup("ar-SA", "egyptian")
    with pytest.raises(UnknownLanguage, match="different packs"):
        lookup("ar-EG", "ksa")  # ksa is new: there is no old pack to keep


def test_a_kept_pair_is_named_in_a_warning(profile: SutProfile) -> None:
    sc = make_scenario(
        persona={"role": "c", "language": "ar-SA", "dialect": "gulf"},
        conversation=LLM,
        execution={"repeats": 3},
    )
    warnings = scenario_warnings(sc, "x.yaml", profile)
    assert [w.field for w in warnings] == ["persona.dialect"]
    assert "ar-SA" in warnings[0].message and "ar-x-gulf" in warnings[0].message
    levant = make_scenario(
        persona={"role": "c", "language": "ar-LB", "dialect": "levantine"},
        conversation=LLM,
        execution={"repeats": 3},
    )
    (only,) = scenario_warnings(levant, "x.yaml", profile)
    assert "ar-LB" in only.message and "the ar pack" in only.message
    assert "no pack for" not in only.message  # not mistaken for a plain fallback


# --- 2. a saved run keeps the language it was graded against --------------------------------


def _llm(**persona: Any) -> Scenario:
    return make_scenario(
        persona={"role": "customer", **persona},
        conversation=LLM,
        scorers=[{"id": "judge.language_consistency"}],
    )


def _record(persona: dict[str, Any], *texts: str) -> RunRecord:
    frames = tuple(
        FrameRecord.model_construct(seq=i, direction="received", kind=FrameKind.MESSAGE, text=t)
        for i, t in enumerate(texts)
    )
    return RunRecord.model_construct(
        run_id="r",
        test_id="t-001",
        attempt_index=0,
        terminal_state=TerminalState.GOAL_REACHED,
        frames=frames,
        persona=persona,
        voice=None,
        probes=(),
        infra_errors=(),
        notes=(),
    )


def test_an_old_record_is_graded_as_it_was_then() -> None:
    sc = _llm(language="ar", dialect="lebanese")
    old = {"role": "customer", "language": "ar", "dialect": "lebanese"}
    spoken = graded_language(sc.persona, scripted=False, recorded=old)
    assert (spoken.label, spoken.tag) == ("ar", "ar")  # MSA, what the simulator wrote then
    new = graded_language(sc.persona, scripted=False, recorded={**old, PACK_KEY: "ar-LB"})
    assert new.label == "Arabic (Lebanese), ar-LB"
    none = graded_language(sc.persona, scripted=True, recorded={**old, PACK_KEY: None})
    assert (none.label, none.tag) == ("ar (lebanese)", None)
    # A record with no persona at all says nothing: the packs as they are now.
    assert graded_language(sc.persona, scripted=False, recorded={}).tag == "ar-LB"
    # The scripted rules of the time: a fallback dialect was graded as declared.
    scripted = graded_language(sc.persona, scripted=True, recorded=old)
    assert (scripted.label, scripted.tag) == ("ar (lebanese)", None)


async def test_rescoring_an_old_record_keeps_its_rubric(profile: SutProfile) -> None:
    """What ``opseval rescore`` does with a run recorded before the pack was: the judge
    grades the attempt against MSA (what the simulator was told then), with the rubric digest
    of then, so a saved grade is reused rather than reported as a changed scenario."""
    sc = _llm(language="ar", dialect="lebanese")
    msa_then = LanguageConsistency().rubric_hash(
        ScoringContext(scenario=_llm(language="ar"), profile=profile)
    )
    old = _record({"role": "customer", "language": "ar", "dialect": "lebanese"}, "تمام")
    (fresh,) = [
        s
        for s in await ScoringEngine().score(old, sc, profile)
        if s.scorer_id == LanguageConsistency.id
    ]
    assert fresh.details["rubric_hash"] == msa_then
    saved = ScoreResult(
        scorer_id=LanguageConsistency.id,
        axis=Axis.OUTPUT,
        status=ScoreStatus.PASSED,
        score=1.0,
        threshold=1.0,
        details={"prompt_hash": LanguageConsistency.prompt_hash(), "rubric_hash": msa_then},
        scorer_version=LanguageConsistency.version,
    )
    spec = ScorerSpec(id=LanguageConsistency.id)
    reused = _Rescorer(ScoringEngine(), judge_configured=False)._reuse(fresh, saved, spec)
    assert reused.status is ScoreStatus.PASSED and reused.details["reused"] is True
    # The same attempt recorded now names its pack, and is graded against it.
    new = _record({**old.persona, PACK_KEY: "ar-LB"}, "تمام")
    (now,) = [
        s
        for s in await ScoringEngine().score(new, sc, profile)
        if s.scorer_id == LanguageConsistency.id
    ]
    assert now.details["rubric_hash"] != msa_then


async def test_the_driver_records_the_resolved_pack() -> None:
    from opseval.driver import Driver
    from opseval.simulator import ScriptedSimulator
    from opseval.transport import PythonTransport

    chat = SutProfile.model_validate(
        {"name": "chat", "kinds": {"message": "message"}, "phase_field": None}
    )
    for persona, tag in [
        ({"language": "ar", "dialect": "ksa"}, "ar-SA"),
        ({"language": "ar", "dialect": "yemeni"}, None),  # scripted: no pack stands in
        ({"language": "fr"}, None),
    ]:
        sc = Scenario.model_validate(
            {
                "id": "s",
                "name": "s",
                "goal": "g",
                "persona": {"role": "c", **persona},
                "conversation": {"scripted_turns": ["hi"]},
                "execution": {"repeats": 1},
            }
        )
        record = await Driver(
            scenario=sc,
            profile=chat,
            transport=PythonTransport(lambda: lambda _f: {"type": "message", "text": "ok"}, chat),
            simulator=ScriptedSimulator(sc, 1),
            run_id="r",
            attempt_index=0,
            seed=1,
        ).run()
        assert record.persona[PACK_KEY] == tag
        assert record.persona["language"] == persona["language"]


# --- 3. a code-switching pack and voice.code_switching must agree ----------------------------


def _problems(sc: Scenario, profile: SutProfile) -> list[str]:
    return check_references(sc, profile, REGISTRY, set(INVARIANTS))


def test_a_codeswitch_pack_and_a_different_voice_mix_are_refused(profile: SutProfile) -> None:
    def scenario(mix: list[str]) -> Scenario:
        return make_scenario(
            persona={"role": "c", "language": "ar-x-codeswitch"},
            conversation=LLM,
            voice={"code_switching": mix, "caller_voice": "v"},
        )

    errors = _problems(scenario(["ar", "fr"]), profile)
    assert any(e.startswith("voice.code_switching:") and "ar-x-codeswitch" in e for e in errors)
    assert _problems(scenario(["en", "ar"]), profile) == []  # the same languages
    plain = make_scenario(
        persona={"role": "c", "language": "ar-EG"},
        conversation=LLM,
        voice={"code_switching": ["ar", "fr"], "caller_voice": "v"},
    )
    assert _problems(plain, profile) == []  # no pack mix to disagree with


# --- 4. output.language_script without script ------------------------------------------------


def _scripted(scorer: dict[str, Any] | None = None, **persona: Any) -> Scenario:
    return make_scenario(
        persona={"role": "c", **persona},
        scorers=[scorer or {"id": "output.language_script"}],
        execution={"repeats": 3},
    )


@pytest.mark.parametrize(
    ("persona", "scripts", "source"),
    [
        ({"language": "ar", "dialect": "yemeni"}, ("arabic",), "stand-in"),  # a fallback
        ({"language": "ar-OM"}, ("arabic",), "stand-in"),
        ({"language": "ar", "dialect": "hassaniya"}, ("arabic",), "stand-in"),  # no pack: base
        ({"language": "en-ZA"}, ("latin",), "stand-in"),
        ({"language": "ar-EG"}, ("arabic",), "pack"),
    ],
)
def test_scripted_turns_take_the_script_of_the_nearest_pack(
    persona: dict[str, str], scripts: tuple[str, ...], source: str, profile: SutProfile
) -> None:
    sc = _scripted(**persona)
    found = script_expectation(sc.persona, scripted=True)
    assert found is not None and (found.scripts, found.source) == (scripts, source)
    assert _problems(sc, profile) == []
    warned = [w for w in scenario_warnings(sc, "x.yaml", profile) if w.field.startswith("scor")]
    if source == "stand-in":
        assert warned and "stands in" in warned[0].message
    else:
        assert warned == []


def test_no_script_and_no_pack_is_a_load_error(profile: SutProfile) -> None:
    errors = _problems(_scripted(language="fr"), profile)
    assert any(e.startswith("scorers.0.params.script: output.language_script:") for e in errors)
    given = _scripted(
        {"id": "output.language_script", "params": {"script": "latin"}}, language="fr"
    )
    assert _problems(given, profile) == []
    listed = _scripted(
        {"id": "output.language_script", "params": {"scripts": ["latin"]}}, language="fr"
    )
    assert _problems(listed, profile) == []


# --- 5. speech providers get BCP-47 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "given"),
    [
        ("ar-x-gulf", "ar"),
        ("ar-x-codeswitch", "ar"),
        ("ar-x-levant", "ar"),
        ("AR-X-GULF", "ar"),
        ("ar-SA", "ar-SA"),
        ("ar", "ar"),
        ("en", "en"),
        ("fr-CA", "fr-CA"),
    ],
)
def test_a_provider_never_sees_a_private_use_tag(language: str, given: str) -> None:
    assert provider_language(language) == given


AUDIO = Audio(pcm16=b"\x01\x00" * 1600, rate=16_000)


async def test_the_voice_map_is_keyed_by_the_pack_tag_and_the_provider_gets_bcp47() -> None:
    seen: dict[str, Any] = {}

    class Tts(TextToSpeech):
        name = "fake"

        async def synthesize(self, text: str, *, voice: str | None, language: str) -> Audio:
            seen["tts"] = (voice, language)
            return AUDIO

    class Stt(SpeechToText):
        name = "fake"

        async def transcribe(
            self, audio: Audio, *, language: str, hint: str | None = None
        ) -> Transcript:
            seen["stt"] = language
            return Transcript(text="تمام", provider="fake", model=None)

    tts_cfg = SpeechConfig(
        role="tts",
        provider="fake",
        url=None,
        model="m",
        timeout_seconds=5,
        voice_map={"ar-x-gulf": "gulf-voice", "ar": "msa-voice"},
    )
    await GuardedTextToSpeech(Tts(tts_cfg), tts_cfg).synthesize(
        "x", voice=None, language="ar-x-gulf"
    )
    assert seen["tts"] == ("gulf-voice", "ar")
    stt_cfg = SpeechConfig(role="stt", provider="fake", url=None, model="m", timeout_seconds=5)
    await GuardedSpeechToText(Stt(stt_cfg), stt_cfg).transcribe(AUDIO, language="ar-x-codeswitch")
    assert seen["stt"] == "ar"


def test_preflight_speaks_the_language_an_attempt_speaks(profile: SutProfile) -> None:
    from opseval.runner import voice_language_for

    ksa = make_scenario(persona={"role": "c", "language": "ar", "dialect": "ksa"}, conversation=LLM)
    assert voice_language_for([ksa], profile) == "ar-SA"
    assert voice_language_for([], profile) == "en"
    # The profile's audio.language wins, for an attempt (VoiceBridge) and for preflight alike.
    heard_as = profile.model_copy(update={"audio": AudioConfig(language="ar-EG")})
    assert voice_language_for([ksa], heard_as) == "ar-EG"


# --- 6. Japanese: kanji and kana together ---------------------------------------------------


def _replies(*texts: str) -> RunRecord:
    return _record({}, *texts)


async def _score(*texts: str, scenario: Scenario | None = None, **params: Any) -> Any:
    ctx: Any = ScoringContext(scenario=scenario, profile=None) if scenario else None  # type: ignore[arg-type]
    spec = ScorerSpec(id="output.language_script", params=params)
    return await LanguageScript().score(_replies(*texts), spec, ctx)


JAPANESE = ("ご注文は明日届きます", "返金の手続きを開始しました")


@pytest.mark.parametrize("params", [{"script": "japanese"}, {"scripts": ["han", "kana"]}])
async def test_japanese_replies_pass_as_han_and_kana_together(params: dict[str, Any]) -> None:
    out = await _score(*JAPANESE, **params)
    assert out.status is ScoreStatus.PASSED and out.details["scripts"] == ["han", "kana"]
    assert (await _score("Your order arrives tomorrow", **params)).status is ScoreStatus.FAILED


async def test_script_and_scripts_together_are_refused(profile: SutProfile) -> None:
    both = {"id": "output.language_script", "params": {"script": "han", "scripts": ["kana"]}}
    assert any("not both" in e for e in _problems(_scripted(both, language="en"), profile))
    out = await _score("x", script="han", scripts=["kana"])
    assert out.status is ScoreStatus.INFRA_ERROR and "BAD_PARAMS" in (out.reason or "")


# --- 7. code-switching is mostly Arabic -----------------------------------------------------


async def test_a_codeswitch_reply_must_be_mostly_arabic() -> None:
    sc = make_scenario(persona={"role": "c", "language": "ar-x-codeswitch"}, conversation=LLM)
    mixed = "الـ order حقك وصل، والـ refund بيوصلك خلال يومين"
    assert (await _score(mixed, scenario=sc)).status is ScoreStatus.PASSED
    english = await _score("Your refund is on its way, thanks", scenario=sc)
    assert english.status is ScoreStatus.FAILED
    assert english.details["primary"] == {"script": "arabic", "min_share": 0.5}
    # The floor is a param: 0 turns it off, a higher one asks for more Arabic.
    off = await _score("Your refund is on its way", scenario=sc, min_primary_share=0)
    assert off.status is ScoreStatus.PASSED
    strict = await _score("الـ refund تمام", scenario=sc, min_primary_share=0.9)
    assert strict.status is ScoreStatus.FAILED
    # Given scripts carry no floor unless one is asked for.
    given = await _score("ok تمام thanks", script="arabic")
    assert given.status is ScoreStatus.FAILED  # Latin outnumbers Arabic: not in the script
    listed = await _score("ok تمام thanks", scripts=["arabic", "latin"])
    assert listed.status is ScoreStatus.PASSED
    floored = await _score("ok تمام thanks", scripts=["arabic", "latin"], min_primary_share=0.5)
    assert floored.status is ScoreStatus.FAILED


# --- 8. a pack must say its scripts ---------------------------------------------------------


@pytest.fixture
def registry() -> Iterator[None]:
    packs, exact, fallback = list(PACKS), dict(langpacks._EXACT), dict(langpacks._FALLBACK)
    yield
    PACKS[:] = packs
    langpacks._EXACT.clear()
    langpacks._EXACT.update(exact)
    langpacks._FALLBACK.clear()
    langpacks._FALLBACK.update(fallback)


@pytest.mark.usefixtures("registry")
@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({}, "scripts is required"),
        ({"scripts": ("klingon",)}, "unknown scripts"),
        ({"scripts": ("latin",), "primary_script": "arabic"}, "primary_script"),
        ({"scripts": ("latin",), "primary_share": 0.5}, "primary_share"),
        ({"scripts": ("latin",), "provider_language": "fr-x-qc"}, "private use"),
    ],
)
def test_a_pack_without_its_scripts_is_refused(kw: dict[str, Any], match: str) -> None:
    before = (list(PACKS), dict(langpacks._EXACT))
    pack = LanguagePack(tag="fr", code="fr", dialect=None, instruction="i", **kw)
    with pytest.raises(ValueError, match=match):
        register(pack)
    assert (list(PACKS), dict(langpacks._EXACT)) == before


@pytest.mark.usefixtures("registry")
def test_a_declared_provider_language_is_what_the_provider_gets() -> None:
    register(
        LanguagePack(
            tag="fr-x-qc",
            code="fr",
            dialect="quebec",
            instruction="i",
            scripts=("latin",),
            provider_language="fr-CA",
        )
    )
    assert provider_language("fr-x-qc") == "fr-CA"


def test_every_pack_says_its_scripts() -> None:
    assert all(p.scripts for p in PACKS)


# --- 9. one script table --------------------------------------------------------------------


def test_the_voice_bridge_reads_the_one_script_table() -> None:
    assert {s for scripts in SCRIPTS.values() for s in scripts} <= set(SCRIPT_PREFIXES)
    assert in_script("ありがとう", "ja") and in_script("日本", "ja-JP")
    assert in_script("تمام", "ar-x-gulf") and not in_script("tamam", "ar-x-codeswitch")
