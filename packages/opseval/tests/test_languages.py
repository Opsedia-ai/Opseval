# SPDX-License-Identifier: Apache-2.0
"""Languages and dialects (ADR-0059, step 1): a persona language Opseval has no pack for is a
load error, never English; a dialect falls back only where a pack declares it, with a warning;
every name that resolved before still resolves to the same pack."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from opseval import langpacks
from opseval.advice import scenario_warnings
from opseval.catalogue import check_scenario_text, load_catalogue
from opseval.cli import app
from opseval.errors import ConfigError
from opseval.langpacks import (
    PACKS,
    LanguagePack,
    UnknownLanguage,
    effective_language,
    lookup,
    register,
    resolve,
)
from opseval.models.profile import SutProfile
from opseval.models.scenario import Scenario
from opseval.scoring import INVARIANTS, REGISTRY, ScoringContext
from opseval.scoring.judges import LanguageConsistency, PersonaConsistency
from opseval.simulator import LLMSimulator

from .conftest import make_scenario

ENGLISH = resolve("en", None)


def _tag(language: str, dialect: str | None = None) -> str:
    return resolve(language, dialect).tag


@pytest.mark.parametrize(
    ("language", "dialect", "tag"),
    [
        ("en", None, "en"),
        ("EN", "", "en"),
        ("ar", None, "ar"),
        ("ar", "msa", "ar"),
        # every alias from before ADR-0059 still names the pack it named
        ("ar", "egyptian", "ar-EG"),
        ("ar", "eg", "ar-EG"),
        ("ar", "egypt", "ar-EG"),
        ("ar", "masri", "ar-EG"),
        ("ar", "gulf", "ar-x-gulf"),
        ("ar", "khaleeji", "ar-x-gulf"),
        ("ar", "saudi", "ar-x-gulf"),
        ("ar", "emirati", "ar-x-gulf"),
        # BCP-47 tags, in `language` alone or split across both fields; case and _ ignored
        ("ar-EG", None, "ar-EG"),
        ("ar_eg", None, "ar-EG"),
        ("ar", "EG", "ar-EG"),
        ("ar-EG", "egyptian", "ar-EG"),
        ("ar-x-gulf", None, "ar-x-gulf"),
    ],
)
def test_names_and_tags_resolve_to_the_same_packs(
    language: str, dialect: str | None, tag: str
) -> None:
    found = lookup(language, dialect)
    assert found.pack.tag == tag and found.fallback_from is None


def test_the_old_pack_names_are_unchanged() -> None:
    """The guided form and saved scenarios write `language` + `dialect`; those stay."""
    assert {(p.code, p.dialect) for p in PACKS} >= {
        ("en", None),
        ("ar", None),
        ("ar", "egyptian"),
        ("ar", "gulf"),
    }


@pytest.mark.parametrize("language", ["fr", "de", "xx", "zh-HK", "", "english"])
def test_an_unknown_language_raises_never_english(language: str) -> None:
    with pytest.raises(UnknownLanguage) as exc:
        resolve(language, None)
    assert isinstance(exc.value, ConfigError) and exc.value.field == "language"


@pytest.mark.parametrize(
    ("language", "dialect"), [("ar", "klingon"), ("en", "pirate"), ("ar-ZZ", None)]
)
def test_an_undeclared_dialect_is_unknown(language: str, dialect: str | None) -> None:
    with pytest.raises(UnknownLanguage) as exc:
        resolve(language, dialect)
    assert exc.value.field == ("dialect" if dialect else "language")


def test_two_fields_naming_different_packs_are_refused() -> None:
    with pytest.raises(UnknownLanguage, match="different packs"):
        resolve("ar-EG", "gulf")


@pytest.mark.parametrize(
    ("language", "dialect", "tag"),
    [
        ("ar", "yemeni", "ar"),  # MSA, as before ADR-0059, now with a warning
        ("ar-LY", None, "ar"),
        ("ar", "maghrebi", "ar"),
        ("ar-QA", None, "ar-x-gulf"),
        ("ar", "omani", "ar-x-gulf"),
        ("ar-SY", None, "ar-x-levant"),
        ("ar", "palestinian", "ar-x-levant"),
        ("en-US", None, "en"),
        ("en", "GB", "en"),
        # English dialect names load as they did before ADR-0059, now with a warning
        ("en", "american", "en"),
        ("en", "us", "en"),
        ("en", "British", "en"),
        ("en", "uk", "en"),
    ],
)
def test_a_declared_fallback_names_what_it_stands_in_for(
    language: str, dialect: str | None, tag: str
) -> None:
    found = lookup(language, dialect)
    assert found.pack.tag == tag
    assert found.fallback_from == (dialect or language)


def test_an_exact_name_beside_a_fallback_tag_is_exact() -> None:
    assert lookup("ar-QA", "gulf").fallback_from is None
    # ar-SA has a pack of its own now; the pair keeps the Gulf pack it named before.
    assert (lookup("ar-SA", "gulf").pack.tag, lookup("ar-SA", "gulf").fallback_from) == (
        "ar-x-gulf",
        None,
    )


def _system(**persona: Any) -> str:
    sc = make_scenario(persona={"role": "customer", **persona})
    return LLMSimulator(sc, 1, client=None)._system()  # type: ignore[arg-type]


def test_the_simulator_never_writes_english_for_an_unknown_code() -> None:
    assert ENGLISH.instruction in _system(language="en")
    assert resolve("ar", "egyptian").instruction in _system(language="ar-EG")
    with pytest.raises(UnknownLanguage):
        _system(language="fr")
    with pytest.raises(UnknownLanguage):
        _system(language="ar", dialect="klingon")


# --- every place a scenario is loaded ------------------------------------------------------

FRENCH = (
    "id: fr-001\nname: n\ngoal: g\npersona: {role: r, language: fr}\n"
    "conversation: {scripted_turns: [bonjour]}\n"
)
FRENCH_LLM = FRENCH.replace("scripted_turns: [bonjour]", "flow: ask for a refund")
LLM = {"scripted_turns": [], "flow": "ask for a refund"}


def test_an_llm_persona_is_refused_at_persona_language(tmp_path: Path, profile: SutProfile) -> None:
    (tmp_path / "fr.yaml").write_text(FRENCH_LLM, encoding="utf-8")
    cat, _ = load_catalogue(
        tmp_path, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
    )
    assert cat.scenarios == []
    assert [(p.field, "'fr'" in p.message) for p in cat.problems] == [("persona.language", True)]


def test_scripted_turns_need_no_pack_and_are_warned_about(
    tmp_path: Path, profile: SutProfile
) -> None:
    """Scripted turns are sent verbatim, so no pack is used: refusing them would block a
    scenario that measures exactly what it says."""
    (tmp_path / "fr.yaml").write_text(FRENCH, encoding="utf-8")
    cat, _ = load_catalogue(
        tmp_path, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
    )
    assert cat.problems == [] and [sc.id for sc in cat.scenarios] == ["fr-001"]
    warnings = scenario_warnings(cat.scenarios[0], "fr.yaml", profile)
    lang = [w for w in warnings if w.field.startswith("persona.")]
    assert [w.field for w in lang] == ["persona.language"]
    assert "scripted turns" in lang[0].message and "would be refused" in lang[0].message


def test_an_unknown_dialect_is_reported_at_persona_dialect(profile: SutProfile) -> None:
    text = FRENCH_LLM.replace("language: fr", "language: ar, dialect: klingon")
    sc, problems = check_scenario_text(
        text, "x.yaml", profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
    )
    assert sc is not None and [p.field for p in problems] == ["persona.dialect"]


def test_validate_exits_3(tmp_path: Path, cat_dir: Path) -> None:
    (tmp_path / "fr.yaml").write_text(FRENCH_LLM, encoding="utf-8")
    res = CliRunner().invoke(
        app, ["validate", str(tmp_path), "--profile", str(cat_dir / "profile.yaml")]
    )
    assert res.exit_code == 3, res.output
    assert "persona.language" in res.output


def test_a_declared_fallback_loads_with_a_warning(profile: SutProfile) -> None:
    sc = make_scenario(
        persona={"role": "r", "language": "ar", "dialect": "yemeni"},
        conversation=LLM,
        execution={"repeats": 3},
    )
    warnings = scenario_warnings(sc, "x.yaml", profile)
    assert [w.field for w in warnings] == ["persona.dialect"]
    assert "yemeni" in warnings[0].message and "ar pack" in warnings[0].message
    by_tag = make_scenario(
        persona={"role": "r", "language": "ar-QA"}, conversation=LLM, execution={"repeats": 3}
    )
    assert [w.field for w in scenario_warnings(by_tag, "x.yaml", profile)] == ["persona.language"]
    american = make_scenario(
        persona={"role": "r", "language": "en", "dialect": "american"},
        conversation=LLM,
        execution={"repeats": 3},
    )
    assert [w.field for w in scenario_warnings(american, "x.yaml", profile)] == ["persona.dialect"]


def test_scripted_turns_in_a_fallback_dialect_are_not_warned_about(profile: SutProfile) -> None:
    """The author wrote the turns in the dialect itself; no pack stands in for anything."""
    sc = make_scenario(
        persona={"role": "r", "language": "ar", "dialect": "yemeni"}, execution={"repeats": 3}
    )
    assert scenario_warnings(sc, "x.yaml", profile) == []


def test_the_demo_and_scaffold_scenarios_need_no_fallback(profile: SutProfile) -> None:
    from opseval.demo import catalogue_dir

    roots = [catalogue_dir(), Path(__file__).parents[1] / "src" / "opseval" / "scaffold"]
    for root in roots:
        for f in root.rglob("*.yaml"):
            if "scenarios" in f.parts:
                text = f.read_text("utf-8")
                sc, problems = check_scenario_text(
                    text, str(f), profile, known_scorers=REGISTRY, known_invariants=set()
                )
                if sc is None:
                    continue
                assert lookup(sc.persona.language, sc.persona.dialect).fallback_from is None


def test_the_console_editor_and_import_refuse_it(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from .test_server import RF1, app_for, connect

    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    scripted = (cat / RF1).read_text("utf-8").replace("language: en", "language: fr")
    out = c.post("/api/v1/scenarios/validate", json={"text": scripted, "file": RF1}).json()
    assert out["ok"] is True
    assert "persona.language" in {w["field"] for w in out["warnings"]}
    text = re.sub(r"scripted_turns:\n\s+- .*\n", "flow: ask for a refund\n", scripted)
    assert "language: fr" in text and "scripted_turns" not in text
    out = c.post("/api/v1/scenarios/validate", json={"text": text, "file": RF1}).json()
    assert out["ok"] is False
    assert "persona.language" in {p["field"] for p in out["problems"]}
    new = text.replace("id: rf-001", "id: rf-980")
    imp = c.post(
        "/api/v1/scenarios/import", json={"family": "f", "files": [{"name": "a.yaml", "text": new}]}
    )
    assert imp.status_code == 422 and "persona.language" in imp.text


def test_the_guided_form_offers_pack_names(tmp_path: Path) -> None:
    from opseval.server.forms import languages

    offered = {lang.code: lang.dialects for lang in languages()}
    assert offered == {
        "ar": [
            "algerian",
            "codeswitch",
            "egyptian",
            "gulf",
            "iraqi",
            "jordanian",
            "ksa",
            "kuwaiti",
            "lebanese",
            "levantine",
            "moroccan",
            "tunisian",
            "uae",
        ],
        "en": [],
    }
    # Arabic is offered as a preview: no Arabic pack has had its native-speaker review.
    assert {lang.code: lang.stable for lang in languages()} == {"ar": False, "en": True}


def test_a_copied_demo_catalogue_still_loads(tmp_path: Path, profile: SutProfile) -> None:
    from opseval.demo import catalogue_dir

    shutil.copytree(catalogue_dir(), tmp_path / "c")
    cat, _ = load_catalogue(
        tmp_path / "c" / "scenarios",
        profile,
        known_scorers=REGISTRY,
        known_invariants=set(INVARIANTS),
    )
    assert cat.problems == []


# --- registering packs ---------------------------------------------------------------------


@pytest.fixture
def registry() -> Iterator[None]:
    """Restore the pack registry after a test registers packs of its own."""
    packs, exact, fallback = list(PACKS), dict(langpacks._EXACT), dict(langpacks._FALLBACK)
    yield
    PACKS[:] = packs
    langpacks._EXACT.clear()
    langpacks._EXACT.update(exact)
    langpacks._FALLBACK.clear()
    langpacks._FALLBACK.update(fallback)


@pytest.mark.usefixtures("registry")
def test_a_regional_pack_needs_no_base_pack() -> None:
    canadian = LanguagePack(
        tag="fr-CA",
        code="fr",
        dialect="canadian",
        instruction="i",
        aliases=("quebecois",),
        scripts=("latin",),
    )
    register(canadian)
    for language, dialect in [("fr-CA", None), ("FR_ca", None), ("fr", "canadian"), ("fr", "CA")]:
        assert lookup(language, dialect).pack is canadian
    assert lookup("fr", "quebecois").pack is canadian
    with pytest.raises(UnknownLanguage, match="no base pack") as exc:
        resolve("fr", None)
    assert exc.value.field == "language"
    with pytest.raises(UnknownLanguage) as exc:
        resolve("fr", "belgian")
    assert exc.value.field == "dialect"


def _new(**kw: Any) -> LanguagePack:
    kw.setdefault("scripts", ("arabic",))
    return LanguagePack(tag="ar-x-new", code="ar", dialect="newish", instruction="i", **kw)


@pytest.mark.usefixtures("registry")
@pytest.mark.parametrize(
    "pack",
    [
        # new keys first, then one already taken: nothing may be written
        _new(aliases=("eg",)),
        _new(fallback_for=("levantine",)),
        _new(fallback_for=("newish",)),
    ],
)
def test_a_colliding_registration_writes_nothing(pack: LanguagePack) -> None:
    before = (list(PACKS), dict(langpacks._EXACT), dict(langpacks._FALLBACK))
    with pytest.raises(ValueError, match="already taken"):
        register(pack)
    assert (list(PACKS), dict(langpacks._EXACT), dict(langpacks._FALLBACK)) == before
    with pytest.raises(UnknownLanguage):
        resolve("ar", "newish")


# --- what the judges are told --------------------------------------------------------------


def _llm(**persona: Any) -> Scenario:
    return make_scenario(persona={"role": "customer", **persona}, conversation=LLM)


def _scripted(**persona: Any) -> Scenario:
    return make_scenario(persona={"role": "customer", **persona})


def _told(scenario: Scenario, profile: SutProfile) -> tuple[str, str, str, str]:
    ctx = ScoringContext(scenario=scenario, profile=profile)
    record: Any = None  # neither context() reads the record
    lang = LanguageConsistency().context(record, ctx)[0]
    persona = PersonaConsistency().context(record, ctx)[-1]
    return (
        lang,
        persona,
        LanguageConsistency().rubric_hash(ctx) or "",
        PersonaConsistency().rubric_hash(ctx) or "",
    )


@pytest.mark.parametrize("make", [_llm, _scripted])
@pytest.mark.parametrize(
    ("language", "dialect", "label"),
    [
        ("en", None, "en"),
        ("ar", None, "ar"),
        ("ar", "egyptian", "ar (egyptian)"),
        ("ar", "gulf", "ar (gulf)"),
    ],
)
def test_canonical_spellings_tell_the_judges_what_they_did(
    make: Any, language: str, dialect: str | None, label: str, profile: SutProfile
) -> None:
    """Byte for byte the pre-ADR-0059 label and rubric, so no saved grade is invalidated."""
    sc = make(language=language, dialect=dialect)
    lang, persona, _, _ = _told(sc, profile)
    assert lang == f"Expected customer language: {label}"
    assert persona == f"Persona language: {label}"
    assert LanguageConsistency().rubric(ScoringContext(sc, profile)) == (language, dialect or "")


@pytest.mark.parametrize("make", [_llm, _scripted])
@pytest.mark.parametrize(
    ("spellings", "label"),
    [
        (
            [("ar", "egyptian"), ("ar-EG", None), ("AR_eg", None), ("ar", "eg"), ("ar", "masri")],
            "ar (egyptian)",
        ),
        ([("ar", "gulf"), ("ar-x-gulf", None), ("ar", "saudi"), ("ar", "khaleeji")], "ar (gulf)"),
        ([("ar", None), ("ar", "msa"), ("AR", None)], "ar"),
        ([("en", None), ("EN", "")], "en"),
    ],
)
def test_every_spelling_of_one_pack_grades_alike(
    make: Any, spellings: list[tuple[str, str | None]], label: str, profile: SutProfile
) -> None:
    told = {_told(make(language=lang, dialect=d), profile) for lang, d in spellings}
    assert len(told) == 1
    assert next(iter(told))[0] == f"Expected customer language: {label}"


def test_an_llm_persona_is_graded_against_the_pack_it_was_told_to_write(
    profile: SutProfile,
) -> None:
    """ar + yemeni: the simulator writes MSA, so the persona judge must not invalidate the
    attempt for MSA, and the language judge expects what the customer actually wrote."""
    lang, persona, *_ = _told(_llm(language="ar", dialect="yemeni"), profile)
    assert lang == "Expected customer language: ar"
    assert persona == "Persona language: ar"
    assert _told(_llm(language="ar", dialect="yemeni"), profile) == _told(
        _llm(language="ar"), profile
    )
    lang, persona, *_ = _told(_llm(language="ar-QA"), profile)
    assert persona == "Persona language: ar (gulf)"
    assert resolve("ar", "gulf").instruction in _system(language="ar-QA")


def test_scripted_turns_are_graded_against_the_declared_language(profile: SutProfile) -> None:
    """Nothing stands in for scripted turns: the author wrote them in the declared dialect."""
    lang, persona, *_ = _told(_scripted(language="ar", dialect="yemeni"), profile)
    assert lang == "Expected customer language: ar (yemeni)"
    assert persona == "Persona language: ar (yemeni)"
    lang, persona, *_ = _told(_scripted(language="fr"), profile)
    assert lang == "Expected customer language: fr"


def test_effective_language_tags() -> None:
    def tag(sc: Scenario) -> str | None:
        return effective_language(sc.persona, scripted=not sc.conversation.llm_persona).tag

    assert tag(_llm(language="ar", dialect="masri")) == "ar-EG"
    assert tag(_llm(language="ar", dialect="yemeni")) == "ar"
    assert tag(_scripted(language="ar", dialect="yemeni")) is None
    assert tag(_scripted(language="fr")) is None
    assert tag(_scripted(language="ar-EG")) == "ar-EG"


def test_the_language_judges_were_bumped() -> None:
    # 3: the resolved pack (ADR-0059); 4: code-switching in context and rubric (ADR-0055).
    assert LanguageConsistency.version == "4" and PersonaConsistency.version == "4"
