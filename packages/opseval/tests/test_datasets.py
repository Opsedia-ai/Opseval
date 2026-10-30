# SPDX-License-Identifier: Apache-2.0
"""Datasets, placeholders and matrices (ADR-0060, flexibility level L1).

One scenario stands for many, and none of the rules that make a scenario trustworthy bend:
mistakes fail at load (with the file, row and field), a value can never change a file's
structure or pull in other data, the cap is a load error and never a truncation, ids are
stable under reordering, and a saved run is graded from exactly what ran.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from opseval.advice import expansion_warnings
from opseval.catalogue import (
    Catalogue,
    check_scenario_source,
    check_scenario_text,
    load_catalogue,
    load_profile,
)
from opseval.cli import app
from opseval.config import Settings
from opseval.datasets import (
    DatasetError,
    Limits,
    TemplateError,
    parse_dataset,
    persona_label,
    placeholders,
    render,
)
from opseval.demo import catalogue_dir
from opseval.demo.refund_agent import run_agent
from opseval.models.profile import SutProfile
from opseval.models.results import VerdictStatus
from opseval.models.scenario import Matrix, PersonaVariant, Scenario
from opseval.rescore import rescore_run
from opseval.runner import RunOptions, launch
from opseval.scoring import INVARIANTS, REGISTRY
from opseval.storage import FileStore

runner = CliRunner()

SCRIPTED: dict[str, Any] = {
    "id": "refund",
    "name": "Refund for order {{order_id}}",
    "goal": "Get a refund for order {{order_id}} ({{kind}}).",
    "tags": ["refund", "dataset"],
    "criticality": "critical",
    "dataset": "datasets/orders.csv",
    "persona": {"role": "customer", "language": "en"},
    "conversation": {
        "scripted_turns": [
            "Order {{order_id}} arrived {{kind}}, refund to my original card please."
        ],
        "stop_at": "supervisor_review",
        "checkpoint_policy": {"proposal_approval": {"action": "approve"}},
    },
    "expectations": {"expected_tools": {"must_call": ["lookup_order", "check_eligibility"]}},
    "scorers": [{"id": "output.terminal_state"}, {"id": "tools.expected_called"}],
    "execution": {"repeats": 1, "turn_timeout_seconds": 5, "attempt_timeout_seconds": 20},
}
ORDERS = "order_id,kind\n10482,damaged\n10483,late\n"


def catalogue(tmp_path: Path, scenario: dict[str, Any], files: dict[str, str | bytes]) -> Path:
    """A copy of the demo catalogue with ``scenario`` under scenarios/ and ``files`` written."""
    cat = tmp_path / "cat"
    if not cat.exists():
        shutil.copytree(catalogue_dir(), cat)
    for rel, body in files.items():
        path = cat / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")
    (cat / "scenarios" / "ds.yaml").write_text(
        yaml.safe_dump(scenario, allow_unicode=True), encoding="utf-8"
    )
    return cat


def load(
    target: Path, *, limits: Limits | None = None, filters: list[str] | None = None
) -> Catalogue:
    root = target if target.is_dir() else target.parent.parent
    if target.is_dir():
        target = target / "scenarios"  # a folder load would read profile.yaml as a scenario
    profile = load_profile(root / "profile.yaml")
    cat, _ = load_catalogue(
        target,
        profile,
        known_scorers=REGISTRY,
        known_invariants=set(INVARIANTS),
        filters=filters,
        limits=limits or Limits(),
    )
    return cat


def mine(cat: Catalogue, base: str = "refund") -> list[Scenario]:
    return [s for s in cat.scenarios if s.base_id == base]


def problems_of(cat: Catalogue) -> list[str]:
    return [f"{p.field}: {p.message}" for p in cat.problems if p.path.endswith("ds.yaml")]


def with_(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    out = json.loads(json.dumps(base))
    for key, value in changes.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


# --- placeholders ----------------------------------------------------------------------


class TestPlaceholders:
    def test_names_spaces_and_escapes(self) -> None:
        assert placeholders("a {{x}} b {{ y_1 }} \\{{z}}") == ["x", "y_1"]
        assert render("{{ x }}-\\{{x}}", {"x": "1"}) == "1-{{x}}"

    @pytest.mark.parametrize(
        "text", ["{{x-y}}", "{{}}", "{{x", "{{ x | upper }}", "{{1x}}", "{{x.y}}"]
    )
    def test_anything_but_a_name_is_a_typo(self, text: str) -> None:
        with pytest.raises(TemplateError, match="malformed placeholder"):
            placeholders(text)

    def test_a_value_is_never_scanned_again(self) -> None:
        assert render("{{a}}", {"a": "{{b}}", "b": "secret"}) == "{{b}}"
        assert render("{{a}}", {"a": "\\{{b}}"}) == "\\{{b}}"

    def test_a_missing_name_is_a_key_error(self) -> None:
        with pytest.raises(KeyError):
            render("{{nope}}", {})


# --- reading datasets ------------------------------------------------------------------


class TestCsv:
    def test_bom_quotes_newlines_arabic_and_blank_lines(self) -> None:
        data = (
            '﻿order_id,note\n10482,"says ""hi"", then: {{x}}"\n\n10483,مرحبا بكم\n10484,"a: b\nc"\n'
        ).encode()
        columns, rows = parse_dataset("datasets/x.csv", data)
        assert columns == ("order_id", "note")  # the byte-order mark is not part of a name
        assert rows == [
            (2, {"order_id": "10482", "note": 'says "hi", then: {{x}}'}),
            (4, {"order_id": "10483", "note": "مرحبا بكم"}),
            (5, {"order_id": "10484", "note": "a: b\nc"}),
        ]

    def test_crlf_rows(self) -> None:
        _cols, rows = parse_dataset("d.csv", b"a,b\r\n1,2\r\n3,4\r\n")
        assert [r for _n, r in rows] == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]

    @pytest.mark.parametrize(
        ("data", "match"),
        [
            (b"", "empty"),
            (b"a,b\n", "no rows"),
            (b"a,a\n1,2\n", "'a' appears twice"),
            (b"a,b-c\n1,2\n", "not a placeholder name"),
            (b"a,\n1,2\n", "not a placeholder name"),
            (b"a,b\n1\n", "row 2 has 1 cell"),
            (b"a,b\n1,2,3\n", "row 2 has 3 cell"),
            (b"a\n\xff\n", "not UTF-8"),
            (b"a\nx\x00y\n", "NUL"),
        ],
    )
    def test_unusable_files_are_named(self, data: bytes, match: str) -> None:
        with pytest.raises(DatasetError, match=match):
            parse_dataset("datasets/x.csv", data)


class TestJsonl:
    def test_scalars_keep_their_type(self) -> None:
        data = (
            '{"a": "x", "n": 3, "f": 2.5, "ok": true, "z": null}\n\n'
            '{"a": "ي", "n": 0, "f": 1, "ok": false, "z": ""}\n'
        )
        columns, rows = parse_dataset("datasets/x.jsonl", data.encode())
        assert columns == ("a", "n", "f", "ok", "z")
        assert rows[0] == (1, {"a": "x", "n": 3, "f": 2.5, "ok": True, "z": None})
        assert rows[1][0] == 3

    @pytest.mark.parametrize(
        ("data", "match"),
        [
            ('{"a": {"b": 1}}', "nested"),
            ('{"a": [1]}', "nested"),
            ('{"a": 1, "a": 2}', "appears twice"),
            ('{"a": NaN}', "not a value JSON allows"),
            ("[1, 2]", "one object per line"),
            ("not json", "not a JSON object"),
            ('{"a": 1}\n{"b": 1}', "keys differ"),
            ('{"bad-name": 1}', "not a placeholder name"),
            ("\n\n", "empty"),
        ],
    )
    def test_unusable_files_are_named(self, data: str, match: str) -> None:
        with pytest.raises(DatasetError, match=match):
            parse_dataset("datasets/x.jsonl", data.encode())


# --- the model -------------------------------------------------------------------------


class TestModel:
    @pytest.mark.parametrize(
        "path",
        [
            "orders.csv",
            "../datasets/x.csv",
            "datasets/../x.csv",
            "/datasets/x.csv",
            "datasets/x.yaml",
            "datasets\\x.csv",
            "datasets/",
            "scenarios/x.csv",
        ],
    )
    def test_a_dataset_lives_under_datasets(self, path: str) -> None:
        with pytest.raises(ValidationError, match="under datasets/"):
            Scenario.model_validate(with_(SCRIPTED, dataset=path))

    def test_the_short_and_long_forms(self) -> None:
        short = Scenario.model_validate(SCRIPTED)
        long = Scenario.model_validate(
            with_(SCRIPTED, dataset={"path": "datasets/orders.csv", "allow_blank": ["kind"]})
        )
        assert short.dataset_spec is not None and short.dataset_spec.path == "datasets/orders.csv"
        assert long.dataset_spec is not None and long.dataset_spec.allow_blank == ["kind"]

    def test_at_is_reserved_for_expansions(self) -> None:
        with pytest.raises(ValidationError, match="'@' is reserved"):
            Scenario.model_validate(with_(SCRIPTED, id="refund@1a2b3c4d", dataset=None))

    def test_an_expansion_names_its_base(self) -> None:
        wrong = with_(SCRIPTED, id="refund@1a2b3c4d", dataset=None)
        wrong["expanded_from"] = {"base_id": "other", "hash": "1a2b3c4d"}
        with pytest.raises(ValidationError, match="<base_id>@<hash>"):
            Scenario.model_validate(wrong)

    @pytest.mark.parametrize(
        ("matrix", "match"),
        [
            ({}, "at least one axis"),
            ({"language": ["en", "en"]}, "listed twice"),
            ({"language": ["en"], "exclude": [{"dialect": "x"}]}, "not an axis"),
            ({"language": ["en"], "exclude": [{"language": "fr"}]}, "not one of"),
            ({"persona": [{"role": "a"}], "exclude": [{"persona": 1}]}, "no persona 1"),
            ({"persona": [{"role": "a"}], "exclude": [{"persona": "a"}]}, "no persona named"),
            ({"persona": [{"name": "x", "role": "a"}, {"name": "x", "role": "b"}]}, "two variants"),
            ({"persona": [{"name": "only"}]}, "at least one of"),
            ({"persona": [{}]}, "at least one of"),
            ({"language": ["en"], "exclude": [{}]}, "empty"),
        ],
    )
    def test_matrix_mistakes(self, matrix: dict[str, Any], match: str) -> None:
        with pytest.raises(ValidationError, match=match):
            Matrix.model_validate(matrix)


# --- expansion through the loader ------------------------------------------------------


class TestExpansion:
    def test_one_scenario_per_row_with_stable_ids_and_provenance(self, tmp_path: Path) -> None:
        cat = load(catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS}))
        found = mine(cat)
        assert not problems_of(cat)
        assert len(found) == 2 and all(s.id.startswith("refund@") for s in found)
        by_row = {s.expanded_from.row: s for s in found if s.expanded_from}
        first = by_row[2]
        assert first.goal == "Get a refund for order 10482 (damaged)."
        assert first.name == "Refund for order 10482"
        assert first.conversation.scripted_turns == [
            "Order 10482 arrived damaged, refund to my original card please."
        ]
        assert first.criticality.value == "critical" and first.tags == ["refund", "dataset"]
        assert first.dataset is None and first.matrix is None
        ef = first.expanded_from
        assert (
            ef is not None
            and ef.dataset == "datasets/orders.csv"
            and len(ef.dataset_digest or "") == 64
        )
        assert cat.expansions["refund"].ids == [s.id for s in found] or set(
            cat.expansions["refund"].ids
        ) == {s.id for s in found}
        assert cat.sources[first.id].endswith("ds.yaml")

        # Reordering rows keeps every id; changing a value gives that row a new one.
        cat2 = load(
            catalogue(
                tmp_path,
                SCRIPTED,
                {"datasets/orders.csv": "kind,order_id\nlate,10483\ndamaged,10482\n"},
            )
        )
        assert {s.id for s in mine(cat2)} == {s.id for s in found}
        cat3 = load(
            catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS.replace("late", "lost")})
        )
        ids3 = {s.id for s in mine(cat3)}
        assert first.id in ids3 and len(ids3 - {s.id for s in found}) == 1

    def test_values_never_change_structure_or_pull_in_data(self, tmp_path: Path) -> None:
        rows = 'order_id,kind\n"10482\nmax_turns: 1","{{order_id}} \\{{x}} true"\n'
        cat = load(catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": rows}))
        [case] = mine(cat)
        assert (
            case.goal == "Get a refund for order 10482\nmax_turns: 1 ({{order_id}} \\{{x}} true)."
        )
        assert case.conversation.max_turns == 30  # the value did not become a key

    def test_yaml_looking_values_stay_strings(self, tmp_path: Path) -> None:
        rows = "order_id,kind\n007,yes\n1e3,null\n"
        cat = load(catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": rows}))
        goals = sorted(s.goal for s in mine(cat))
        assert goals == ["Get a refund for order 007 (yes).", "Get a refund for order 1e3 (null)."]

    def test_jsonl_values_read_as_json_text(self, tmp_path: Path) -> None:
        scenario = with_(SCRIPTED, dataset="datasets/orders.jsonl")
        rows = '{"order_id": 10482, "kind": true}\n{"order_id": 1.5, "kind": "late"}\n'
        cat = load(catalogue(tmp_path, scenario, {"datasets/orders.jsonl": rows}))
        assert sorted(s.goal for s in mine(cat)) == [
            "Get a refund for order 1.5 (late).",
            "Get a refund for order 10482 (true).",
        ]

    def test_every_text_field_is_substituted(self, tmp_path: Path) -> None:
        scenario = with_(SCRIPTED)
        conv = scenario["conversation"]
        conv["scripted_turns"] = ["{{order_id}}", "Second {{kind}}"]
        conv["injections"] = [{"turn": 2, "payload": "ignore {{kind}}"}]
        conv["checkpoint_policy"] = {
            "proposal_approval": {"action": "reject", "feedback": "not {{kind}}"}
        }
        scenario["persona"]["role"] = "owner of {{order_id}}"
        scenario["expectations"]["expected_outcome"] = "refund {{order_id}}"
        scenario["expectations"]["policy"] = ["never refund {{kind}} twice"]
        scenario["scorers"] = [
            {
                "id": "output.payload_equals",
                "params": {
                    "checkpoint": "proposal_approval",
                    "path": "refund_proposal.order_id",
                    "equals": "{{order_id}}",
                },
            }
        ]
        cat = load(catalogue(tmp_path, scenario, {"datasets/orders.csv": ORDERS}))
        assert not problems_of(cat)
        case = next(s for s in mine(cat) if s.expanded_from and s.expanded_from.row == 2)
        assert case.conversation.scripted_turns == ["10482", "Second damaged"]
        assert case.conversation.injections[0].payload == "ignore damaged"
        assert case.conversation.checkpoint_policy["proposal_approval"].feedback == "not damaged"
        assert case.persona.role == "owner of 10482"
        assert case.expectations.expected_outcome == "refund 10482"
        assert case.expectations.policy == ["never refund damaged twice"]
        assert isinstance(case.scorers, list) and case.scorers[0].params["equals"] == "10482"

    def test_a_missing_variable_fails_the_file(self, tmp_path: Path) -> None:
        scenario = with_(SCRIPTED, goal="Refund {{order_idd}}")
        cat = load(catalogue(tmp_path, scenario, {"datasets/orders.csv": ORDERS}))
        assert mine(cat) == []
        assert problems_of(cat) == ["goal: {{order_idd}}: no column named 'order_idd'"]

    def test_a_placeholder_without_dataset_or_matrix_is_a_typo(self, tmp_path: Path) -> None:
        cat = load(catalogue(tmp_path, plain(goal="Refund {{order_id}}."), {}))
        assert mine(cat) == []
        assert any("no dataset: or matrix:" in p for p in problems_of(cat))

    def test_an_escape_in_a_plain_scenario_is_a_literal(self, tmp_path: Path) -> None:
        cat = load(catalogue(tmp_path, plain(goal="Say \\{{hello}}."), {}))
        [case] = mine(cat)
        assert case.id == "refund" and case.goal == "Say {{hello}}." and case.expanded_from is None
        assert "refund" not in cat.expansions

    @pytest.mark.parametrize(
        ("where", "value"),
        [
            ("tags", ["{{kind}}"]),
            ("expectations", {"expected_tools": {"must_call": ["{{order_id}}"]}}),
        ],
    )
    def test_fields_that_decide_what_runs_hold_no_placeholder(
        self, tmp_path: Path, where: str, value: Any
    ) -> None:
        cat = load(
            catalogue(tmp_path, with_(SCRIPTED, **{where: value}), {"datasets/orders.csv": ORDERS})
        )
        assert mine(cat) == []
        assert any("cannot hold a placeholder" in p for p in problems_of(cat))

    def test_a_blank_cell_needs_allow_blank(self, tmp_path: Path) -> None:
        rows = "order_id,kind\n10482,\n10483,late\n"
        cat = load(catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": rows}))
        assert mine(cat) == []
        assert problems_of(cat) == [
            "dataset: row 2 of datasets/orders.csv: blank 'kind'; fill it, or list the column "
            "in dataset.allow_blank"
        ]
        allowed = with_(SCRIPTED, dataset={"path": "datasets/orders.csv", "allow_blank": ["kind"]})
        cat = load(catalogue(tmp_path, allowed, {"datasets/orders.csv": rows}))
        assert sorted(s.goal for s in mine(cat)) == [
            "Get a refund for order 10482 ().",
            "Get a refund for order 10483 (late).",
        ]

    def test_allow_blank_names_a_real_column(self, tmp_path: Path) -> None:
        scenario = with_(SCRIPTED, dataset={"path": "datasets/orders.csv", "allow_blank": ["knd"]})
        cat = load(catalogue(tmp_path, scenario, {"datasets/orders.csv": ORDERS}))
        assert "dataset.allow_blank: datasets/orders.csv has no column 'knd'" in problems_of(cat)

    def test_duplicate_rows_are_refused(self, tmp_path: Path) -> None:
        rows = ORDERS + "10482,damaged\n"
        cat = load(catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": rows}))
        assert mine(cat) == []
        assert any("row 4" in p and "same values as row 2" in p for p in problems_of(cat))
        # Different only in a column nothing reads: the same conversation twice.
        rows = "order_id,kind,note\n10482,damaged,a\n10482,damaged,b\n"
        cat = load(catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": rows}))
        assert any("expands to the same scenario as row 2" in p for p in problems_of(cat))

    def test_an_expansion_that_fails_validation_names_its_row(self, tmp_path: Path) -> None:
        scenario = with_(SCRIPTED)
        scenario["conversation"]["injections"] = [{"turn": 1, "payload": "{{kind}}"}]
        rows = "order_id,kind\n10482,fine\n10483,[INJECT]x\n"
        cat = load(catalogue(tmp_path, scenario, {"datasets/orders.csv": rows}))
        assert mine(cat) == []
        assert any(
            p.startswith("conversation.injections.0.payload: row 3 of datasets/orders.csv")
            for p in problems_of(cat)
        )

    def test_the_cap_is_a_load_error_never_a_truncation(self, tmp_path: Path) -> None:
        rows = "order_id,kind\n" + "".join(f"{n},damaged\n" for n in range(5))
        cat = load(
            catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": rows}),
            limits=Limits(max_expansion=4),
        )
        assert mine(cat) == []
        [problem] = problems_of(cat)
        assert "expands to 5 scenarios" in problem and "limit of 4" in problem
        assert "OPSEVAL_MAX_EXPANSION" in problem

    def test_the_cap_comes_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPSEVAL_MAX_EXPANSION", "7")
        monkeypatch.setenv("OPSEVAL_MAX_DATASET_BYTES", "99")
        assert Limits.from_env() == Limits(7, 99)
        assert Limits.from_settings(Settings(_env_file=None, max_expansion=3)).max_expansion == 3

    def test_an_oversized_dataset_is_named(self, tmp_path: Path) -> None:
        cat = load(
            catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS}),
            limits=Limits(max_dataset_bytes=10),
        )
        assert any("over the 10-byte limit" in p for p in problems_of(cat))

    def test_a_missing_dataset_is_named(self, tmp_path: Path) -> None:
        cat = load(catalogue(tmp_path, SCRIPTED, {}))
        assert any("datasets/orders.csv: no such dataset" in p for p in problems_of(cat))

    def test_a_link_out_of_datasets_is_refused(self, tmp_path: Path) -> None:
        outside = tmp_path / "secret.csv"
        outside.write_text(ORDERS, "utf-8")
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/keep.csv": ORDERS})
        (cat_dir / "datasets" / "orders.csv").symlink_to(outside)
        cat = load(cat_dir)
        assert any("a link to a file outside datasets/" in p for p in problems_of(cat))
        # A datasets folder that is itself a link out is refused too.
        shutil.rmtree(cat_dir / "datasets")
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / "orders.csv").write_text(ORDERS, "utf-8")
        (cat_dir / "datasets").symlink_to(tmp_path / "elsewhere")
        assert any("folder outside the catalogue" in p for p in problems_of(load(cat_dir)))

    def test_the_path_is_relative_to_the_catalogue_root(self, tmp_path: Path) -> None:
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        nested = cat_dir / "scenarios" / "deep" / "er"
        nested.mkdir(parents=True)
        (cat_dir / "scenarios" / "ds.yaml").rename(nested / "ds.yaml")
        # The same file loads as a suite, a folder, the scenarios folder, or on its own.
        for target in (
            cat_dir / "suites" / "full.yaml",
            cat_dir,
            cat_dir / "scenarios",
            nested / "ds.yaml",
        ):
            profile = load_profile(cat_dir / "profile.yaml")
            loaded, _ = load_catalogue(
                target,
                profile,
                known_scorers=REGISTRY,
                known_invariants=set(INVARIANTS),
                limits=Limits(),
            )
            assert len(mine(loaded)) == 2, target

    def test_expanded_from_cannot_be_authored(self, tmp_path: Path) -> None:
        scenario = with_(SCRIPTED)
        scenario["expanded_from"] = {"base_id": "refund", "hash": "1a2b3c4d"}
        cat = load(catalogue(tmp_path, scenario, {"datasets/orders.csv": ORDERS}))
        assert problems_of(cat) == [
            "expanded_from: set by the loader on the scenarios a dataset or matrix expands to; "
            "remove it"
        ]

    def test_two_files_with_one_base_id_clash(self, tmp_path: Path) -> None:
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        shutil.copy(cat_dir / "scenarios" / "ds.yaml", cat_dir / "scenarios" / "ds2.yaml")
        cat = load(cat_dir)
        assert any("duplicate id 'refund'" in p.message for p in cat.problems)


def plain(**changes: Any) -> dict[str, Any]:
    """SCRIPTED without its dataset or any placeholder."""
    out = with_(SCRIPTED, **{"dataset": None, "name": "Refund", "goal": "Get a refund.", **changes})
    out["conversation"]["scripted_turns"] = ["Order 10482 arrived damaged."]
    return out


class TestMatrix:
    LLM: dict[str, Any] = {
        "id": "multi",
        "name": "Refund in {{language}} as persona {{persona}}",
        "goal": "Get a refund for order {{order_id}}.",
        "dataset": "datasets/orders.csv",
        "matrix": {
            "language": ["ar-EG", "ar-x-gulf", "ar", "en"],
            "persona": [
                {"role": "an impatient customer with order {{order_id}}", "typing_noise": 0.2},
                {"name": "first-timer", "role": "a first-time customer"},
            ],
            "exclude": [{"language": "en", "persona": 0}],
        },
        "persona": {"role": "x", "language": "fr", "dialect": "quebec"},
        "conversation": {
            "flow": "Ask for a refund on {{ order_id }} in {{language}}.",
            "max_turns": 4,
            "stop_at": "goal_reached",
        },
        "scorers": [{"id": "conversation.no_errors"}],
    }

    def test_rows_times_cells_less_exclude(self, tmp_path: Path) -> None:
        cat = load(catalogue(tmp_path, self.LLM, {"datasets/orders.csv": ORDERS}))
        assert not problems_of(cat)
        found = mine(cat, "multi")
        assert len(found) == 2 * (4 * 2 - 1)
        assert cat.expansions["multi"].count == 14
        langs = {s.persona.language for s in found}
        assert langs == {"ar-EG", "ar-x-gulf", "ar", "en"}
        # A language axis names the whole language: the template's dialect is not kept.
        assert all(s.persona.dialect is None for s in found)
        egyptian = [s for s in found if s.persona.language == "ar-EG" and s.persona.typing_noise]
        assert {s.persona.role for s in egyptian} == {
            "an impatient customer with order 10482",
            "an impatient customer with order 10483",
        }
        one = egyptian[0]
        # {{persona}} is the variant's name, else a hash of what it says: never its index.
        impatient = persona_label(
            PersonaVariant(role="an impatient customer with order {{order_id}}", typing_noise=0.2)
        )
        assert re.fullmatch(r"[0-9a-f]{8}", impatient)
        assert one.name == f"Refund in ar-EG as persona {impatient}"
        assert one.expanded_from is not None and one.expanded_from.matrix == {
            "language": "ar-EG",
            "persona": impatient,
        }
        named = [s for s in found if s.persona.role == "a first-time customer"]
        assert named and all(s.name.endswith("as persona first-timer") for s in named)
        assert not any(s.persona.language == "en" and s.persona.typing_noise == 0.2 for s in found)

    def test_reordering_personas_keeps_ids(self, tmp_path: Path) -> None:
        before = {
            s.id
            for s in mine(
                load(catalogue(tmp_path, self.LLM, {"datasets/orders.csv": ORDERS})), "multi"
            )
        }
        swapped = json.loads(json.dumps(self.LLM))
        swapped["matrix"]["persona"].reverse()
        swapped["matrix"]["exclude"] = [{"language": "en", "persona": 1}]
        after = {
            s.id
            for s in mine(
                load(catalogue(tmp_path, swapped, {"datasets/orders.csv": ORDERS})), "multi"
            )
        }
        assert before == after

    def test_an_unknown_language_is_a_load_error_for_an_llm_persona(self, tmp_path: Path) -> None:
        scenario = with_(
            self.LLM,
            matrix={"language": ["en", "xx-YY"]},
            dataset=None,
            goal="Get a refund.",
            name="Refund",
        )
        scenario["conversation"]["flow"] = "Ask for a refund in {{language}}."
        cat = load(catalogue(tmp_path, scenario, {}))
        assert mine(cat, "multi") == []
        assert any(
            p.startswith("persona.language: matrix language=xx-YY: no language pack")
            for p in problems_of(cat)
        )

    def test_scripted_turns_need_no_pack(self, tmp_path: Path) -> None:
        scenario = plain(matrix={"language": ["en", "fr"]}, goal="Refund, in {{language}}.")
        cat = load(catalogue(tmp_path, scenario, {}))
        assert len(mine(cat)) == 2 and not problems_of(cat)

    def test_dialect_axis(self, tmp_path: Path) -> None:
        scenario = with_(
            self.LLM,
            dataset=None,
            goal="Refund.",
            name="Refund",
            matrix={"dialect": ["egyptian", "gulf"]},
        )
        scenario["persona"] = {"role": "customer", "language": "ar"}
        scenario["conversation"]["flow"] = "Ask in {{dialect}}."
        found = mine(load(catalogue(tmp_path, scenario, {})), "multi")
        assert sorted((s.persona.language, s.persona.dialect) for s in found) == [
            ("ar", "egyptian"),
            ("ar", "gulf"),
        ]

    def test_a_column_named_like_an_axis_is_refused(self, tmp_path: Path) -> None:
        rows = "order_id,language\n1,en\n"
        cat = load(catalogue(tmp_path, self.LLM, {"datasets/orders.csv": rows}))
        assert "dataset: column 'language' is also a matrix axis; rename the column" in problems_of(
            cat
        )

    def test_excluding_everything_runs_nothing_and_says_so(self, tmp_path: Path) -> None:
        scenario = with_(
            self.LLM,
            dataset=None,
            goal="Refund.",
            name="Refund",
            matrix={"language": ["en"], "exclude": [{"language": "en"}]},
        )
        scenario["conversation"]["flow"] = "Ask."
        assert any(
            "excludes every cell" in p for p in problems_of(load(catalogue(tmp_path, scenario, {})))
        )

    def test_a_persona_role_placeholder_must_exist(self, tmp_path: Path) -> None:
        scenario = json.loads(json.dumps(self.LLM))
        scenario["matrix"]["persona"][1]["role"] = "a {{mood}} customer"
        cat = load(catalogue(tmp_path, scenario, {"datasets/orders.csv": ORDERS}))
        assert any(p.startswith("matrix.persona.1.role: {{mood}}") for p in problems_of(cat))


# --- selection, warnings, validate, the editor's check --------------------------------


class TestSelection:
    def test_filters_by_base_id_expanded_id_and_tag(self, tmp_path: Path) -> None:
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        every = mine(load(cat_dir))
        assert [s.id for s in load(cat_dir, filters=["id=refund"]).scenarios] == [
            s.id for s in every
        ]
        one = every[0].id
        assert [s.id for s in load(cat_dir, filters=[f"id={one}"]).scenarios] == [one]
        assert len(load(cat_dir, filters=["tag=dataset"]).scenarios) == 2
        assert len(load(cat_dir, filters=["criticality=critical", "tag=dataset"]).scenarios) == 2

    def test_a_suite_selects_by_id(self, tmp_path: Path) -> None:
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        (cat_dir / "suites" / "rows.yaml").write_text(
            "name: rows\ninclude: ['scenarios/*.yaml']\nfilters: ['id=refund']\n", "utf-8"
        )
        assert len(load(cat_dir / "suites" / "rows.yaml").scenarios) == 2


class TestWarningsAndValidate:
    def test_unused_columns_and_near_the_cap(self, tmp_path: Path) -> None:
        rows = "order_id,kind,unused\n" + "".join(f"{n},damaged,x\n" for n in range(4))
        cat = load(
            catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": rows}),
            limits=Limits(max_expansion=5),
        )
        messages = [w.message for w in expansion_warnings(cat.expansions["refund"])]
        assert any("'unused'" in m and "used by no placeholder" in m for m in messages)
        assert any("expands to 4 scenarios, near the limit of 5" in m for m in messages)

    def test_validate_names_counts_and_attempts(self, tmp_path: Path) -> None:
        scenario = with_(SCRIPTED, execution={"repeats": 3})
        cat_dir = catalogue(tmp_path, scenario, {"datasets/orders.csv": ORDERS})
        out = runner.invoke(
            app,
            ["validate", str(cat_dir / "scenarios"), "--profile", str(cat_dir / "profile.yaml")],
        )
        assert out.exit_code == 0, out.output
        assert "refund: expands to 2 scenario(s), 6 attempt(s) (2 x 3 repeats)" in out.output
        # A warning shared by every expansion is said once.
        assert out.output.count("ds.yaml: tags:") <= 1

        broken = with_(SCRIPTED, goal="{{nope}}")
        cat_dir = catalogue(tmp_path, broken, {"datasets/orders.csv": ORDERS})
        out = runner.invoke(
            app,
            ["validate", str(cat_dir / "scenarios"), "--profile", str(cat_dir / "profile.yaml")],
        )
        assert out.exit_code == 3 and "{{nope}}: no column named 'nope'" in out.output

    def test_check_scenario_text_returns_the_template_and_its_expansion(
        self, tmp_path: Path, profile: SutProfile
    ) -> None:
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        text = (cat_dir / "scenarios" / "ds.yaml").read_text("utf-8")
        sc, problems = check_scenario_text(
            text,
            "scenarios/ds.yaml",
            profile,
            known_scorers=REGISTRY,
            known_invariants=set(INVARIANTS),
            root=cat_dir,
            limits=Limits(),
        )
        assert sc is not None and sc.id == "refund" and not problems
        found = check_scenario_source(
            text,
            "scenarios/ds.yaml",
            profile,
            known_scorers=REGISTRY,
            known_invariants=set(INVARIANTS),
            root=cat_dir,
            limits=Limits(),
        )
        assert len(found.expanded) == 2 and found.expansion is not None
        # Without the catalogue folder there is nothing to read the rows from.
        _sc, problems = check_scenario_text(
            text,
            "x.yaml",
            profile,
            known_scorers=REGISTRY,
            known_invariants=set(INVARIANTS),
            limits=Limits(),
        )
        assert [p.message for p in problems] == ["a dataset needs the catalogue folder it sits in"]


# --- a run, its snapshot and rescore ---------------------------------------------------


class TestRunAndRescore:
    async def test_each_row_is_its_own_verdict_and_rescore_reads_the_snapshot(
        self, tmp_path: Path
    ) -> None:
        scenario = with_(SCRIPTED)
        scenario["scorers"] = [
            {"id": "output.terminal_state"},
            {
                "id": "output.payload_equals",
                "params": {
                    "checkpoint": "proposal_approval",
                    "path": "refund_proposal.method",
                    "equals": "{{method}}",
                },
            },
        ]
        rows = "order_id,kind,method\n10482,damaged,original_card\n10483,damaged,store_credit\n"
        cat_dir = catalogue(tmp_path, scenario, {"datasets/orders.csv": rows})
        (cat_dir / "suites" / "rows.yaml").write_text(
            "name: rows\ninclude: ['scenarios/ds.yaml']\n", "utf-8"
        )
        settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "reports"))
        async with run_agent() as url:
            result = await launch(
                cat_dir / "suites" / "rows.yaml",
                RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1),
                settings,
            )
        verdicts = {t.test_id: t.status for t in result.tests}
        assert len(verdicts) == 2 and all(i.startswith("refund@") for i in verdicts)
        assert sorted(verdicts.values()) == [VerdictStatus.FAILED, VerdictStatus.PASSED]
        assert result.exit_code == 1  # one failing row fails the gate, never averaged away

        store = FileStore(settings.reports_dir)
        snapshot = store.load_snapshot(result.run_id)
        assert snapshot is not None
        saved = snapshot[1]
        assert set(saved) == set(verdicts)
        assert all(s.expanded_from and s.expanded_from.dataset_digest for s in saved.values())
        names = {p.name for p in (store.run_dir(result.run_id) / "scenarios").iterdir()}
        assert names == {f"{i}.json" for i in verdicts}  # '@' kept in the file name

        # The dataset changes after the run: rescore still grades what ran.
        (cat_dir / "datasets" / "orders.csv").write_text(
            rows.replace("store_credit", "original_card"), "utf-8"
        )
        outcome = await rescore_run(store.run_dir(result.run_id), settings)
        assert outcome.info.scenario_source == "snapshot"
        assert {t.test_id: t.status for t in outcome.result.tests} == verdicts

    async def test_only_ids_takes_a_base_id(self, tmp_path: Path) -> None:
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "reports"))
        async with run_agent() as url:
            result = await launch(
                cat_dir / "suites" / "full.yaml",
                RunOptions(
                    target=url, profile=cat_dir / "profile.yaml", repeats=1, only_ids=["refund"]
                ),
                settings,
            )
        assert [t.test_id.partition("@")[0] for t in result.tests] == ["refund", "refund"]


# --- the console's check ---------------------------------------------------------------


def test_the_editor_check_lists_the_expanded_ids(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from opseval.server import create_app

    cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
    settings = Settings(reports_dir=str(tmp_path / "r"))
    api = create_app(
        settings=settings, catalogue=cat_dir, profile=cat_dir / "profile.yaml", token="t"
    )
    c = TestClient(api, base_url="http://127.0.0.1:8000", headers={"Authorization": "Bearer t"})
    text = (cat_dir / "scenarios" / "ds.yaml").read_text("utf-8")
    out = c.post("/api/v1/scenarios/validate", json={"text": text, "file": "scenarios/ds.yaml"})
    body = out.json()
    assert body["ok"], body
    assert body["id"] == "refund" and len(body["expanded"]) == 2
    assert all(i.startswith("refund@") for i in body["expanded"])
    # The base id is taken: a new file may not reuse it.
    clash = c.post("/api/v1/scenarios/validate", json={"text": text}).json()
    assert any("already used" in p["message"] for p in clash["problems"])
    # An expansion opens its file by id, and the scenario list shows each expansion.
    assert c.get(f"/api/v1/scenarios/{body['expanded'][0]}/source").status_code == 200
    broken = c.post(
        "/api/v1/scenarios/validate", json={"text": text.replace("{{kind}}", "{{knd}}")}
    ).json()
    assert not broken["ok"] and broken["expanded"] == []


# --- server mode: push and pull (ADR-0047) ---------------------------------------------


class TestCatalogueStore:
    """Datasets are catalogue items of kind ``dataset``, named by their path."""

    @staticmethod
    def read(cat_dir: Path, limits: Limits | None = None) -> list[Any]:
        from opseval.db.catalogue_store import read_folder

        return read_folder(
            cat_dir,
            known_scorers=REGISTRY,
            known_invariants=set(INVARIANTS),
            limits=limits or Limits(),
        )

    def test_push_reads_datasets_beside_the_yaml(self, tmp_path: Path) -> None:
        crlf = ORDERS.replace("\n", "\r\n")
        cat_dir = catalogue(
            tmp_path,
            SCRIPTED,
            {"datasets/orders.csv": crlf, "datasets/more/extra.jsonl": '{"a": 1}\n'},
        )
        files = {f.path: f for f in self.read(cat_dir)}
        orders = files["datasets/orders.csv"]
        assert orders.kind == "dataset" and orders.name == "datasets/orders.csv"
        assert orders.source == crlf  # exactly as written, line endings too
        assert orders.content == {"format": "csv", "columns": ["order_id", "kind"], "rows": 2}
        assert files["datasets/more/extra.jsonl"].content["rows"] == 1

    def test_a_broken_or_oversized_dataset_refuses_the_push(self, tmp_path: Path) -> None:
        from opseval.errors import ConfigError

        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        (cat_dir / "datasets" / "unused.jsonl").write_text('{"a": [1]}\n', "utf-8")
        with pytest.raises(ConfigError, match="nested"):
            self.read(cat_dir)
        (cat_dir / "datasets" / "unused.jsonl").unlink()
        with pytest.raises(ConfigError, match="OPSEVAL_MAX_DATASET_BYTES"):
            self.read(cat_dir, Limits(max_dataset_bytes=10))
        # A scenario whose rows are wrong refuses the whole folder, as any problem does.
        (cat_dir / "datasets" / "orders.csv").write_text("order_id\n1\n", "utf-8")
        with pytest.raises(ConfigError, match="no column named 'kind'"):
            self.read(cat_dir)

    def test_a_link_out_of_datasets_refuses_the_push(self, tmp_path: Path) -> None:
        from opseval.errors import ConfigError

        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        (cat_dir / "x.csv").write_text(ORDERS, "utf-8")
        (cat_dir / "datasets" / "out.csv").symlink_to(cat_dir / "x.csv")
        with pytest.raises(ConfigError, match="outside datasets/"):
            self.read(cat_dir)

    def test_a_dangling_or_unreadable_dataset_is_named(self, tmp_path: Path) -> None:
        import os

        from opseval.errors import ConfigError

        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        gone = cat_dir / "datasets" / "gone.csv"
        gone.symlink_to(cat_dir / "datasets" / "missing.csv")
        with pytest.raises(ConfigError, match=r"gone\.csv: cannot be read \(a link to nothing\)"):
            self.read(cat_dir)
        gone.unlink()
        locked = cat_dir / "datasets" / "orders.csv"
        locked.chmod(0)
        try:
            if os.access(locked, os.R_OK):  # pragma: no cover - running as root
                pytest.skip("permissions are not enforced for this user")
            with pytest.raises(ConfigError, match=r"orders\.csv: cannot be read"):
                self.read(cat_dir)
            # The loader names it too, as a problem at the scenario's dataset.
            assert any("cannot be read" in p for p in problems_of(load(cat_dir)))
        finally:
            locked.chmod(0o644)

    def test_a_name_too_long_to_store_is_refused_before_the_push(self, tmp_path: Path) -> None:
        from opseval.errors import ConfigError

        long = "datasets/" + "n" * 195 + ".csv"
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS, long: ORDERS})
        with pytest.raises(ConfigError, match="dataset name is 208 characters, over the 200"):
            self.read(cat_dir)

    async def test_a_refused_folder_writes_nothing(self, pg_url: str, tmp_path: Path) -> None:
        from sqlalchemy import func, select

        from opseval.db import migrate
        from opseval.db.engine import make_engine, sessions
        from opseval.db.schema import CatalogueItem
        from opseval.errors import ConfigError

        engine = make_engine(
            Settings(_env_file=None, allowed_hosts="127.0.0.1", database_url=pg_url)
        )
        await migrate.upgrade(engine)
        try:
            long = "datasets/" + "n" * 195 + ".csv"
            cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS, long: ORDERS})
            (cat_dir / "datasets" / "gone.jsonl").symlink_to(cat_dir / "datasets" / "nowhere.jsonl")
            with pytest.raises(ConfigError, match="gone.jsonl: cannot be read"):
                self.read(cat_dir)
            (cat_dir / "datasets" / "gone.jsonl").unlink()
            with pytest.raises(ConfigError, match="over the 200"):
                self.read(cat_dir)
            async with sessions(engine)() as s:
                assert await s.scalar(select(func.count()).select_from(CatalogueItem)) == 0
        finally:
            await engine.dispose()

    @pytest.mark.parametrize(
        ("path", "ok"),
        [
            ("datasets/orders.csv", True),
            ("datasets/a/b.jsonl", True),
            ("datasets/x.yaml", False),
            ("datasets/x.txt", False),
            ("scenarios/x.csv", False),
            ("orders.csv", False),
            ("datasets/../x.csv", False),
            ("suites/x.yaml", True),
            ("profile.yaml", True),
        ],
    )
    def test_pull_writes_datasets_only_under_datasets(
        self, tmp_path: Path, path: str, ok: bool
    ) -> None:
        from opseval.db.catalogue_store import _safe_target
        from opseval.errors import ConfigError

        if ok:
            assert _safe_target(tmp_path, path).is_relative_to(tmp_path)
        else:
            with pytest.raises(ConfigError, match="refusing to write"):
                _safe_target(tmp_path, path)

    async def test_push_and_pull_round_trip(self, pg_url: str, tmp_path: Path) -> None:
        from opseval.db import migrate
        from opseval.db.catalogue_store import (
            ensure_project,
            get_task,
            list_items,
            materialize,
            push,
        )
        from opseval.db.engine import make_engine, sessions

        engine = make_engine(
            Settings(_env_file=None, allowed_hosts="127.0.0.1", database_url=pg_url)
        )
        await migrate.upgrade(engine)
        try:
            crlf = ORDERS.replace("\n", "\r\n")
            cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": crlf})
            async with sessions(engine)() as s, s.begin():
                await ensure_project(s, "acme/bot")
                task = await get_task(s, "acme/bot")
                first = await push(s, task, self.read(cat_dir), label="production")
            assert ("dataset", "datasets/orders.csv", 1) in first.added

            # A changed row is a new version of the dataset, like any other item.
            (cat_dir / "datasets" / "orders.csv").write_text(
                crlf.replace("late", "lost"), "utf-8", newline=""
            )
            async with sessions(engine)() as s, s.begin():
                task = await get_task(s, "acme/bot")
                second = await push(s, task, self.read(cat_dir))
                items = {(i.kind, i.name): i for i in await list_items(s, task)}
            assert second.added == [("dataset", "datasets/orders.csv", 2)]
            assert items[("dataset", "datasets/orders.csv")].labels == {
                "latest": 2,
                "production": 1,
            }

            out = tmp_path / "pulled"
            async with sessions(engine)() as s, s.begin():
                await materialize(s, await get_task(s, "acme/bot"), "production", out)
            assert (out / "datasets" / "orders.csv").read_bytes() == crlf.encode()
            pulled = load(out)
            assert {s.id for s in mine(pulled)} == {
                s.id
                for s in mine(
                    load(catalogue(tmp_path / "again", SCRIPTED, {"datasets/orders.csv": ORDERS}))
                )
            }
        finally:
            await engine.dispose()

    async def test_the_downgrade_refuses_while_datasets_are_stored(
        self, pg_url: str, tmp_path: Path
    ) -> None:
        from alembic import command
        from sqlalchemy import text

        from opseval.db import migrate
        from opseval.db.catalogue_store import ensure_project, get_task, push
        from opseval.db.engine import make_engine, sessions

        engine = make_engine(
            Settings(_env_file=None, allowed_hosts="127.0.0.1", database_url=pg_url)
        )
        await migrate.upgrade(engine)
        try:
            async with engine.connect() as conn:
                definition = await conn.scalar(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'ck_catalogue_items_kind'"
                    )
                )
            assert "'dataset'" in str(definition)
            cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
            async with sessions(engine)() as s, s.begin():
                await ensure_project(s, "acme/bot")
                await push(s, await get_task(s, "acme/bot"), self.read(cat_dir))

            def down(conn: Any) -> None:
                command.downgrade(migrate.config(conn), "0003")

            with pytest.raises(RuntimeError, match="1 dataset item"):
                async with engine.begin() as conn:
                    await conn.run_sync(down)
            assert await migrate.current(engine) == migrate.head()
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM catalogue_items WHERE kind = 'dataset'"))
                await conn.run_sync(down)
            assert await migrate.current(engine) == "0003"
            async with engine.connect() as conn:
                names = await conn.scalars(
                    text(
                        "SELECT conname FROM pg_constraint WHERE contype = 'c' "
                        "AND conrelid = CAST('catalogue_items' AS regclass)"
                    )
                )
                assert list(names) == ["ck_catalogue_items_ck_catalogue_items_kind"]
            await migrate.upgrade(engine)
            assert await migrate.current(engine) == migrate.head()
        finally:
            await engine.dispose()


# --- the editor's identity of a file, import clashes, the index cache ---------------------


def console(cat_dir: Path, tmp_path: Path, profile: Path | None = None) -> Any:
    from fastapi.testclient import TestClient

    from opseval.server import create_app

    api = create_app(
        settings=Settings(_env_file=None, reports_dir=str(tmp_path / "r")),
        catalogue=cat_dir,
        profile=profile or cat_dir / "profile.yaml",
        token="t",
    )
    return TestClient(api, base_url="http://127.0.0.1:8000", headers={"Authorization": "Bearer t"})


def suite_with(cat_dir: Path, name: str, flt: str) -> None:
    (cat_dir / "suites" / f"{name}.yaml").write_text(
        f"name: {name}\ninclude: ['scenarios/**/*.yaml']\nfilters: ['{flt}']\n", "utf-8"
    )


class TestEditorIdentity:
    """A dataset scenario's file is known by its authored id, never one of its expansions."""

    def test_an_unchanged_template_is_never_a_rename(self, tmp_path: Path) -> None:
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        one = mine(load(cat_dir))[0].id
        suite_with(cat_dir, "base", "id=refund")
        suite_with(cat_dir, "row", f"id={one}")
        c = console(cat_dir, tmp_path)
        text = (cat_dir / "scenarios" / "ds.yaml").read_text("utf-8")
        body = c.post(
            "/api/v1/scenarios/validate", json={"text": text, "file": "scenarios/ds.yaml"}
        ).json()
        assert body["ok"] and body["problems"] == [], body
        # The file opens with its own id, by path, by its base id and by an expansion.
        assert c.get("/api/v1/scenarios/source?file=scenarios/ds.yaml").json()["id"] == "refund"
        assert c.get("/api/v1/scenarios/refund/source").json()["id"] == "refund"
        assert c.get(f"/api/v1/scenarios/{one}/source").json()["file"] == "scenarios/ds.yaml"

    def test_renaming_the_base_empties_suites_selecting_it_or_an_expansion(
        self, tmp_path: Path
    ) -> None:
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        one = mine(load(cat_dir))[0].id
        suite_with(cat_dir, "base", "id=refund")
        suite_with(cat_dir, "row", f"id={one}")
        suite_with(cat_dir, "other", "id=refund-other")
        c = console(cat_dir, tmp_path)
        text = (cat_dir / "scenarios" / "ds.yaml").read_text("utf-8")
        renamed = text.replace("id: refund", "id: refund2")
        body = c.post(
            "/api/v1/scenarios/validate", json={"text": renamed, "file": "scenarios/ds.yaml"}
        ).json()
        messages = sorted(p["message"] for p in body["problems"])
        assert messages == [
            "suite 'base' selects id=refund: renaming empties it",
            "suite 'row' selects id=refund: renaming empties it",
        ]

    def test_import_refuses_an_authored_id_a_dataset_scenario_holds(self, tmp_path: Path) -> None:
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        c = console(cat_dir, tmp_path)
        text = yaml.safe_dump(plain(id="refund"))
        out = c.post(
            "/api/v1/scenarios/import",
            json={"family": "more", "files": [{"name": "same.yaml", "text": text}]},
        )
        assert out.status_code == 422
        assert any(
            p["message"] == "would replace existing scenario 'refund'" for p in out.json()["detail"]
        )
        assert not (cat_dir / "scenarios" / "more").exists()

    def test_a_new_row_refreshes_the_index(self, tmp_path: Path) -> None:
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        c = console(cat_dir, tmp_path)
        before = {s.id for s in mine(load(cat_dir))}
        for sid in before:  # the index is built and cached
            assert c.get(f"/api/v1/scenarios/{sid}/source").status_code == 200
        (cat_dir / "datasets" / "orders.csv").write_text(ORDERS + "10484,lost\n", "utf-8")
        [new] = {s.id for s in mine(load(cat_dir))} - before
        assert c.get(f"/api/v1/scenarios/{new}/source").status_code == 200


# --- one dataset root per load -----------------------------------------------------------


def _family_named_scenarios(tmp_path: Path) -> Path:
    cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
    family = cat_dir / "scenarios" / "scenarios"
    family.mkdir()
    (cat_dir / "scenarios" / "ds.yaml").rename(family / "ds.yaml")
    return cat_dir


class TestDatasetRoot:
    def test_a_family_named_scenarios(self, tmp_path: Path) -> None:
        cat_dir = _family_named_scenarios(tmp_path)
        profile = load_profile(cat_dir / "profile.yaml")
        for target in (
            cat_dir / "suites" / "full.yaml",
            cat_dir / "scenarios",
            cat_dir / "scenarios" / "scenarios",
            cat_dir / "scenarios" / "scenarios" / "ds.yaml",
        ):
            loaded, _ = load_catalogue(
                target, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
            )
            assert len(mine(loaded)) == 2, (target, loaded.problems)

    def test_a_flat_catalogue_in_a_folder_named_scenarios(self, tmp_path: Path) -> None:
        flat = tmp_path / "x" / "scenarios"
        (flat / "datasets").mkdir(parents=True)
        (flat / "datasets" / "orders.csv").write_text(ORDERS, "utf-8")
        (flat / "ds.yaml").write_text(yaml.safe_dump(SCRIPTED), "utf-8")
        profile_path = catalogue_dir() / "profile.yaml"
        loaded, _ = load_catalogue(
            flat,
            load_profile(profile_path),
            known_scorers=REGISTRY,
            known_invariants=set(INVARIANTS),
        )
        assert len(mine(loaded)) == 2, loaded.problems
        # The server (a flat catalogue: scenarios at its top) reads the same datasets.
        c = console(flat, tmp_path, profile_path)
        ids = {s["id"] for s in c.get("/api/v1/scenarios").json()["scenarios"]}
        assert {s.id for s in mine(loaded)} <= ids
        text = (flat / "ds.yaml").read_text("utf-8")
        body = c.post("/api/v1/scenarios/validate", json={"text": text, "file": "ds.yaml"}).json()
        assert body["ok"] and len(body["expanded"]) == 2, body

    def test_validate_from_inside_scenarios_with_a_relative_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        monkeypatch.chdir(cat_dir / "scenarios")
        for target in (".", "ds.yaml"):
            out = runner.invoke(app, ["validate", target, "--profile", "../profile.yaml"])
            assert "refund: expands to 2 scenario(s)" in out.output, (target, out.output)
        monkeypatch.chdir(cat_dir / "suites")
        out = runner.invoke(app, ["validate", "full.yaml", "--profile", "../profile.yaml"])
        assert "refund: expands to 2 scenario(s)" in out.output, out.output


# --- the cap before anything is built; the limits alone from settings -------------------


class TestCapAndLimits:
    BIG: dict[str, Any] = {
        "language": [f"l{i}" for i in range(300)],
        "dialect": [f"d{i}" for i in range(300)],
        "persona": [{"role": f"customer {i}"} for i in range(300)],
    }

    def test_a_huge_matrix_is_refused_before_it_is_built(self, tmp_path: Path) -> None:
        scenario = plain(matrix=self.BIG)
        cat_dir = catalogue(tmp_path, scenario, {})
        started = time.perf_counter()
        cat = load(cat_dir)
        assert time.perf_counter() - started < 0.5
        [problem] = problems_of(cat)
        assert "expands to 27000000 scenarios" in problem and "OPSEVAL_MAX_EXPANSION" in problem
        # With an exclude, a space too large to scan is refused up front too.
        excluded = plain(matrix={**self.BIG, "exclude": [{"language": "l0"}]})
        started = time.perf_counter()
        cat = load(catalogue(tmp_path, excluded, {}))
        assert time.perf_counter() - started < 0.5
        assert any("27000000 cells before exclude" in p for p in problems_of(cat))

    def test_excludes_are_counted_lazily_up_to_the_cap(self, tmp_path: Path) -> None:
        matrix = {
            "language": [f"l{i}" for i in range(50)],
            "persona": [{"role": f"customer {i}"} for i in range(50)],
            "exclude": [{"language": "l0"}],
        }
        cat = load(catalogue(tmp_path, plain(matrix=matrix), {}), limits=Limits(max_expansion=10))
        [problem] = problems_of(cat)
        assert "expands to more than 10 scenarios" in problem
        assert "at least 11 matrix cell(s)" in problem
        # Under the cap, the exclude is exact.
        small = {"language": ["en", "ar"], "dialect": ["egyptian", "gulf"], "exclude": []}
        small["exclude"] = [{"language": "en", "dialect": "gulf"}]
        cat = load(catalogue(tmp_path, plain(matrix=small), {}), limits=Limits(max_expansion=3))
        assert len(mine(cat)) == 3, problems_of(cat)

    def test_a_broken_unrelated_setting_never_stops_a_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPSEVAL_DOTENV", "false")
        monkeypatch.setenv("OPSEVAL_RETENTION_DAYS", "not a number")
        with pytest.raises(ValueError):
            Settings()
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        profile = load_profile(cat_dir / "profile.yaml")
        loaded, _ = load_catalogue(
            cat_dir / "scenarios", profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
        )
        assert len(mine(loaded)) == 2
        out = runner.invoke(
            app,
            ["validate", str(cat_dir / "scenarios"), "--profile", str(cat_dir / "profile.yaml")],
        )
        assert out.exit_code == 0, out.output
        assert "refund: expands to 2 scenario(s)" in out.output

    def test_a_broken_limit_is_a_load_error_naming_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPSEVAL_DOTENV", "false")
        monkeypatch.setenv("OPSEVAL_MAX_EXPANSION", "0")
        cat_dir = catalogue(tmp_path, SCRIPTED, {"datasets/orders.csv": ORDERS})
        profile = load_profile(cat_dir / "profile.yaml")
        loaded, _ = load_catalogue(
            cat_dir / "scenarios", profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
        )
        assert mine(loaded) == []
        assert any("invalid setting OPSEVAL_MAX_EXPANSION" in p for p in problems_of(loaded))
        # A catalogue with nothing to expand never reads it.
        assert any(s.id == "rf-001" for s in loaded.scenarios)


# --- persona variants, field paths, audio folders ----------------------------------------


class TestPersonaVariants:
    def test_a_variants_non_text_fields_hold_no_placeholder(self, tmp_path: Path) -> None:
        matrix = {"persona": [{"role": "a {{kind}} customer", "timezone": "{{order_id}}"}]}
        cat = load(
            catalogue(tmp_path, with_(SCRIPTED, matrix=matrix), {"datasets/orders.csv": ORDERS})
        )
        assert problems_of(cat) == [
            "matrix.persona.0.timezone: this field cannot hold a placeholder (only text fields can)"
        ]

    def test_a_variants_role_is_substituted_and_its_name_is_not_a_persona_field(
        self, tmp_path: Path
    ) -> None:
        matrix = {"persona": [{"name": "rushed", "role": "a {{kind}} customer"}]}
        scenario = with_(SCRIPTED, matrix=matrix, name="Refund {{order_id}} as {{persona}}")
        cat = load(catalogue(tmp_path, scenario, {"datasets/orders.csv": ORDERS}))
        found = sorted(mine(cat), key=lambda s: s.name)
        assert [s.persona.role for s in found] == ["a damaged customer", "a late customer"]
        assert [s.name for s in found] == ["Refund 10482 as rushed", "Refund 10483 as rushed"]
        assert all(
            s.expanded_from and s.expanded_from.matrix == {"persona": "rushed"} for s in found
        )
        # An exclude may name a variant by its name.
        two = {
            "persona": [*matrix["persona"], {"role": "calm"}],
            "exclude": [{"persona": "rushed"}],
        }
        cat = load(
            catalogue(tmp_path, with_(SCRIPTED, matrix=two), {"datasets/orders.csv": ORDERS})
        )
        assert {s.persona.role for s in mine(cat)} == {"calm"}


class TestFieldPaths:
    def test_a_reserved_at_is_reported_at_id(self, tmp_path: Path) -> None:
        cat = load(catalogue(tmp_path, plain(id="refund@1a2b3c4d"), {}))
        assert problems_of(cat) == [
            "id: '@' is reserved for the scenarios a dataset or matrix expands to"
        ]

    @pytest.mark.parametrize("dataset", ["orders.csv", {"path": "orders.csv"}])
    def test_a_bad_dataset_is_one_problem_at_its_path(
        self, tmp_path: Path, dataset: str | dict[str, str]
    ) -> None:
        cat = load(catalogue(tmp_path, with_(SCRIPTED, dataset=dataset), {}))
        [problem] = problems_of(cat)
        assert problem.startswith("dataset.path: ") and "under datasets/" in problem


def test_an_expansion_never_shares_an_audio_folder_with_an_authored_id() -> None:
    from opseval.voice.recording import AudioRecorder

    expanded = AudioRecorder(None, "a@1a2b3c4d", 0)._relative(1, "caller", ".wav")
    authored = AudioRecorder(None, "a_1a2b3c4d", 0)._relative(1, "caller", ".wav")
    assert expanded == "audio/a@1a2b3c4d/attempt0/001-caller.wav"
    assert expanded != authored
