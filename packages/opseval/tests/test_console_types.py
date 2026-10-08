# SPDX-License-Identifier: Apache-2.0
"""The console's API types are generated from the server models and must not drift."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "console_types.py"


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("console_types", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_types_match_the_models(gen: ModuleType) -> None:
    assert gen.OUT.read_text("utf-8") == gen.render(), "run scripts/console_types.py"
    assert gen.main(["--check"]) == 0


def test_a_drifted_file_fails_the_check(
    gen: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / "api.gen.ts"
    stale.write_text(gen.render().replace("  vacuous: boolean;\n", ""), "utf-8")
    monkeypatch.setattr(gen, "OUT", stale)
    assert gen.main(["--check"]) == 1


def test_wire_shapes(gen: ModuleType) -> None:
    out = gen.render()
    # camelCase, as on the wire; null kept as "not measured"; literals, not loose strings.
    assert "  passRate: number | null;" in out
    assert '  axis: "output" | "flow" | "tools" | "security" | "harness";' in out
    # A request may leave out what has a default; a response always carries every field.
    assert "export interface LaunchIn {\n  suite?: string | null;" in out
    assert "export interface LaunchOut {\n  accepted: boolean;" in out
    assert "  presets: Record<string, Record<string, string>>;" in out
