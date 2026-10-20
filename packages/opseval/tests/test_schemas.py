# SPDX-License-Identifier: Apache-2.0
"""Published JSON Schemas (FR-RPT-02, FR-CAT-11).

The models are the source of truth and the schemas are generated from them, so the thing
worth testing is that the committed files have not drifted: a schema that disagrees with
the loader is worse than none, because an editor would accept a file `opseval validate`
rejects, or flag a field that is perfectly valid.

So this suite proves three things: the committed schemas match the models, they accept
the project's own real files, and they actually reject bad ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from opseval.cli import app
from opseval.demo import catalogue_dir
from opseval.schemas import (
    BASE_URI,
    PUBLISHED,
    SCHEMA_DIALECT,
    editor_hint,
    render,
    schema_for,
    stale,
    write_all,
)

REPO = Path(__file__).resolve().parents[3]
COMMITTED = REPO / "schemas" / "v1"


def committed(name: str) -> dict[str, Any]:
    return json.loads((COMMITTED / f"{name}.json").read_text("utf-8"))


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text("utf-8"))


# --- the files on disk match the models -----------------------------------------------


class TestNotStale:
    def test_the_committed_schemas_match_the_models(self) -> None:
        """The one that matters. If this fails, run `opseval schemas` and commit."""
        assert stale(COMMITTED) == []

    def test_every_published_contract_has_a_file(self) -> None:
        for name in PUBLISHED:
            assert (COMMITTED / f"{name}.json").is_file(), name

    def test_the_expected_contracts_are_published(self) -> None:
        assert set(PUBLISHED) == {"scenario", "suite", "profile", "record", "result", "tenants"}

    def test_rendering_is_deterministic(self) -> None:
        """Sorted keys, so regenerating never produces a spurious diff."""
        assert render("scenario") == render("scenario")

    def test_stale_reports_a_missing_file(self, tmp_path: Path) -> None:
        assert any("missing" in p for p in stale(tmp_path))

    def test_stale_reports_a_changed_file(self, tmp_path: Path) -> None:
        write_all(tmp_path)
        (tmp_path / "scenario.json").write_text('{"changed": true}', encoding="utf-8")
        assert any("no longer matches" in p for p in stale(tmp_path))

    def test_write_all_then_check_is_clean(self, tmp_path: Path) -> None:
        write_all(tmp_path)
        assert stale(tmp_path) == []


class TestShape:
    @pytest.mark.parametrize("name", sorted(PUBLISHED))
    def test_each_declares_its_dialect_and_id(self, name: str) -> None:
        schema = committed(name)
        assert schema["$schema"] == SCHEMA_DIALECT
        assert schema["$id"] == f"{BASE_URI}/{name}.json"

    @pytest.mark.parametrize("name", sorted(PUBLISHED))
    def test_each_says_it_is_generated(self, name: str) -> None:
        assert "do not edit by hand" in committed(name)["description"]

    def test_a_yaml_author_sees_the_aliased_field_name(self) -> None:
        """`Scenario.schema_` is spelled `schema` in a file; the schema must say so."""
        assert "schema" in committed("scenario")["properties"]
        assert "schema_" not in committed("scenario")["properties"]

    def test_unknown_fields_are_forbidden(self) -> None:
        """What makes editor autocompletion useful: a typo is flagged."""
        assert committed("scenario")["additionalProperties"] is False


# --- they accept the project's own real files ------------------------------------------


class TestAcceptsRealFiles:
    def test_every_demo_scenario_validates(self) -> None:
        schema = committed("scenario")
        files = sorted(catalogue_dir().glob("scenarios/**/*.yaml"))
        assert files, "the demo catalogue should have scenarios"
        for path in files:
            jsonschema.validate(load_yaml(path), schema)

    def test_the_demo_profile_validates(self) -> None:
        jsonschema.validate(load_yaml(catalogue_dir() / "profile.yaml"), committed("profile"))

    def test_every_demo_suite_validates(self) -> None:
        schema = committed("suite")
        for path in sorted(catalogue_dir().glob("suites/*.yaml")):
            jsonschema.validate(load_yaml(path), schema)

    def test_every_scaffolded_file_validates(self, tmp_path: Path) -> None:
        """Including the editor hint the scaffold now writes."""
        from opseval.scaffold import TEMPLATES, scaffold

        for template in TEMPLATES:
            dest = tmp_path / template
            scaffold(dest, template)
            for path in sorted(dest.glob("scenarios/**/*.yaml")):
                jsonschema.validate(load_yaml(path), committed("scenario"))
            for path in sorted(dest.glob("suites/*.yaml")):
                jsonschema.validate(load_yaml(path), committed("suite"))
            jsonschema.validate(load_yaml(dest / "profile.yaml"), committed("profile"))

    def test_a_real_result_and_record_validate(self, tmp_path: Path) -> None:
        import asyncio

        from opseval.config import Settings
        from opseval.demo.refund_agent import run_agent
        from opseval.runner import RunOptions, launch

        async def go() -> Any:
            cat = catalogue_dir()
            settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path))
            async with run_agent() as url:
                return await launch(
                    cat / "suites" / "smoke.yaml",
                    RunOptions(target=url, profile=cat / "profile.yaml", repeats=1),
                    settings,
                )

        result = asyncio.run(go())
        run_dir = tmp_path / result.run_id
        jsonschema.validate(
            json.loads((run_dir / "result.json").read_text("utf-8")), committed("result")
        )
        records = sorted(run_dir.glob("records/*.json"))
        assert records
        for path in records:
            jsonschema.validate(json.loads(path.read_text("utf-8")), committed("record"))


# --- and they reject bad ones ----------------------------------------------------------


class TestRejectsBadFiles:
    def minimal(self, **extra: Any) -> dict[str, Any]:
        return {
            "schema": "opseval/v1",
            "id": "t-001",
            "name": "a scenario",
            "goal": "do the thing",
            "persona": {"role": "customer"},
            "conversation": {"scripted_turns": ["hi"]},
            **extra,
        }

    def test_the_minimal_scenario_is_accepted(self) -> None:
        jsonschema.validate(self.minimal(), committed("scenario"))

    def test_a_typo_is_rejected(self) -> None:
        with pytest.raises(jsonschema.ValidationError, match="tyop"):
            jsonschema.validate(self.minimal(tyop=1), committed("scenario"))

    def test_a_missing_required_field_is_rejected(self) -> None:
        bad = self.minimal()
        del bad["goal"]
        with pytest.raises(jsonschema.ValidationError, match="goal"):
            jsonschema.validate(bad, committed("scenario"))

    def test_a_wrong_type_is_rejected(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(self.minimal(tags="not-a-list"), committed("scenario"))

    def test_an_out_of_range_value_is_rejected(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(self.minimal(execution={"repeats": 999}), committed("scenario"))

    def test_an_unknown_criticality_is_rejected(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(self.minimal(criticality="catastrophic"), committed("scenario"))


# --- the editor hint and the CLI -------------------------------------------------------


class TestEditorHint:
    def test_it_points_at_the_published_uri(self) -> None:
        assert editor_hint("scenario") == (
            f"# yaml-language-server: $schema={BASE_URI}/scenario.json"
        )

    def test_scaffolded_files_carry_the_right_one(self, tmp_path: Path) -> None:
        from opseval.scaffold import scaffold

        scaffold(tmp_path, "openai-chat")
        pairs = [
            (tmp_path / "profile.yaml", "profile"),
            (tmp_path / "suites" / "smoke.yaml", "suite"),
        ]
        for path, name in pairs:
            assert path.read_text("utf-8").startswith(editor_hint(name)), path
        for path in sorted((tmp_path / "scenarios").glob("**/*.yaml")):
            assert path.read_text("utf-8").startswith(editor_hint("scenario")), path

    def test_the_hint_does_not_break_the_loader(self, tmp_path: Path) -> None:
        """A comment is a comment, but worth proving on the real loader."""
        from opseval.catalogue import load_profile
        from opseval.scaffold import scaffold

        scaffold(tmp_path, "openai-chat")
        assert load_profile(tmp_path / "profile.yaml").name


class TestCli:
    runner = CliRunner()

    def test_check_passes_against_the_committed_schemas(self) -> None:
        res = self.runner.invoke(app, ["schemas", "--check", "--out", str(COMMITTED)])
        assert res.exit_code == 0, res.output
        assert "match the models" in res.output

    def test_check_fails_and_says_what_to_do(self, tmp_path: Path) -> None:
        res = self.runner.invoke(app, ["schemas", "--check", "--out", str(tmp_path)])
        assert res.exit_code == 3
        assert "out of date" in res.output
        assert "opseval schemas" in res.output

    def test_it_writes_every_schema(self, tmp_path: Path) -> None:
        res = self.runner.invoke(app, ["schemas", "--out", str(tmp_path)])
        assert res.exit_code == 0
        assert sorted(p.name for p in tmp_path.glob("*.json")) == sorted(
            f"{n}.json" for n in PUBLISHED
        )

    def test_schema_for_an_unknown_name_raises(self) -> None:
        with pytest.raises(KeyError):
            schema_for("nope")
