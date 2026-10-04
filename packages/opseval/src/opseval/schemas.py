# SPDX-License-Identifier: Apache-2.0
"""Published JSON Schemas for the `opseval/v1` contracts (FR-RPT-02, FR-CAT-11, ADR-0026).

The Pydantic models are the source of truth; these are generated from them, committed
under ``schemas/v1/``, and checked in CI. A committed schema that has drifted from the
model is worse than no schema at all: an editor would accept a file the loader rejects,
or flag a field that is perfectly valid. So ``opseval schemas --check`` fails when the
committed files no longer match, and a test runs it.

Schemas are generated **by alias**, because that is what a YAML author writes:
``Scenario.schema_`` is spelled ``schema`` in a file, and a schema describing the Python
field name would be wrong for every editor that used it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from opseval.models.profile import SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import SuiteResult
from opseval.models.scenario import SCHEMA_VERSION, Scenario, Suite
from opseval.models.tenant import TenantPool

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE_URI = "https://opsedia.ai/schemas/v1"
VERSION_DIR = "v1"

PUBLISHED: dict[str, type[BaseModel]] = {
    "tenants": TenantPool,
    "scenario": Scenario,
    "suite": Suite,
    "profile": SutProfile,
    "record": RunRecord,
    "result": SuiteResult,
}
"""Filename stem → model. Scenario and suite serve editor autocompletion (FR-CAT-11);
result is the documented output contract (FR-RPT-02); record and profile are published
because third parties read and write them; tenants because the pool file is hand-written
and benefits most from autocompletion."""

EDITOR_HINT = "# yaml-language-server: $schema={uri}"
"""What VS Code and friends look for at the top of a YAML file."""


def schema_for(name: str) -> dict[str, Any]:
    """One published schema, with the dialect and a stable ``$id``."""
    model = PUBLISHED[name]
    generated = model.model_json_schema(by_alias=True)
    title = generated.pop("title", name)
    # A model with a docstring already has a description, and spreading `generated` over
    # ours would drop the "generated" marker for exactly those models. Keep both: the
    # docstring is the useful part, the marker is the part that stops hand edits.
    own = generated.pop("description", "").strip()
    note = (
        f"Opseval {SCHEMA_VERSION} {name} contract. Generated from the Pydantic model; "
        "do not edit by hand (run `opseval schemas`)."
    )
    return {
        "$schema": SCHEMA_DIALECT,
        "$id": f"{BASE_URI}/{name}.json",
        "title": title,
        "description": f"{own} {note}".strip() if own else note,
        **generated,
    }


def render(name: str) -> str:
    """The exact file contents, so writing and checking cannot disagree."""
    return json.dumps(schema_for(name), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_all(out_dir: str | Path) -> list[Path]:
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name in sorted(PUBLISHED):
        path = folder / f"{name}.json"
        path.write_text(render(name), encoding="utf-8")
        written.append(path)
    return written


def stale(out_dir: str | Path) -> list[str]:
    """Published schemas that are missing or no longer match the models."""
    folder = Path(out_dir)
    problems = []
    for name in sorted(PUBLISHED):
        path = folder / f"{name}.json"
        try:
            current = path.read_text("utf-8")
        except OSError:
            problems.append(f"{path} is missing")
            continue
        if current != render(name):
            problems.append(f"{path} no longer matches the model")
    return problems


def editor_hint(name: str) -> str:
    return EDITOR_HINT.format(uri=f"{BASE_URI}/{name}.json")


__all__ = [
    "BASE_URI",
    "EDITOR_HINT",
    "PUBLISHED",
    "SCHEMA_DIALECT",
    "VERSION_DIR",
    "editor_hint",
    "render",
    "schema_for",
    "stale",
    "write_all",
]
