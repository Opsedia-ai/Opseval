# SPDX-License-Identifier: Apache-2.0
"""Languages and dialects (ADR-0059, step 1): a persona language Opseval has no pack for is a
load error, never English; a dialect falls back only where a pack declares it, with a warning;
every name that resolved before still resolves to the same pack."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from opseval.advice import scenario_warnings
from opseval.catalogue import check_scenario_text, load_catalogue
from opseval.cli import app
from opseval.errors import ConfigError
from opseval.langpacks import PACKS, UnknownLanguage, lookup, resolve
from opseval.models.profile import SutProfile
from opseval.scoring import INVARIANTS, REGISTRY
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
    assert {(p.code, p.dialect) for p in PACKS} == {
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
    ("language", "dialect"), [("ar", "klingon"), ("en", "american"), ("ar-ZZ", None)]
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
        ("ar", "levantine", "ar"),  # MSA, as before ADR-0059, now with a warning
        ("ar-LB", None, "ar"),
        ("ar-SA", None, "ar-x-gulf"),
        ("ar", "kuwaiti", "ar-x-gulf"),
        ("en-US", None, "en"),
        ("en", "GB", "en"),
    ],
)
def test_a_declared_fallback_names_what_it_stands_in_for(
    language: str, dialect: str | None, tag: str
) -> None:
    found = lookup(language, dialect)
    assert found.pack.tag == tag
    assert found.fallback_from == (dialect or language)


def test_an_exact_name_beside_a_fallback_tag_is_exact() -> None:
    assert lookup("ar-SA", "gulf").fallback_from is None


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


def test_the_loader_reports_it_at_persona_language(tmp_path: Path, profile: SutProfile) -> None:
    (tmp_path / "fr.yaml").write_text(FRENCH, encoding="utf-8")
    cat, _ = load_catalogue(
        tmp_path, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
    )
    assert cat.scenarios == []
    assert [(p.field, "'fr'" in p.message) for p in cat.problems] == [("persona.language", True)]


def test_an_unknown_dialect_is_reported_at_persona_dialect(profile: SutProfile) -> None:
    text = FRENCH.replace("language: fr", "language: ar, dialect: klingon")
    sc, problems = check_scenario_text(
        text, "x.yaml", profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
    )
    assert sc is not None and [p.field for p in problems] == ["persona.dialect"]


def test_validate_exits_3(tmp_path: Path, cat_dir: Path) -> None:
    (tmp_path / "fr.yaml").write_text(FRENCH, encoding="utf-8")
    res = CliRunner().invoke(
        app, ["validate", str(tmp_path), "--profile", str(cat_dir / "profile.yaml")]
    )
    assert res.exit_code == 3, res.output
    assert "persona.language" in res.output


def test_a_declared_fallback_loads_with_a_warning(profile: SutProfile) -> None:
    sc = make_scenario(
        persona={"role": "r", "language": "ar", "dialect": "levantine"},
        execution={"repeats": 3},
    )
    warnings = scenario_warnings(sc, "x.yaml", profile)
    assert [w.field for w in warnings] == ["persona.dialect"]
    assert "levantine" in warnings[0].message and "ar pack" in warnings[0].message
    by_tag = make_scenario(persona={"role": "r", "language": "ar-SA"}, execution={"repeats": 3})
    assert [w.field for w in scenario_warnings(by_tag, "x.yaml", profile)] == ["persona.language"]


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
    text = (cat / RF1).read_text("utf-8").replace("language: en", "language: fr")
    assert "language: fr" in text
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
    assert offered == {"ar": ["egyptian", "gulf"], "en": []}


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
