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
    # A request may leave out what has a default; a response always carries every field.
    assert "export interface LaunchIn {\n  suite?: string | null;" in out
    assert "export interface LaunchOut {\n  accepted: boolean;" in out
    assert '  presets: Record<"strict" | "standard" | "lenient", Record<string, string>>;' in out
    # The core's enums are generated, and the fields use them by name.
    assert 'export type Axis = "output" | "flow" | "tools" | "security" | "harness";' in out
    assert "  axis: Axis;" in out and "  status: VerdictStatus;" in out


def test_every_enum_member_reaches_the_console(gen: ModuleType) -> None:
    """The API uses the core enums themselves, so a new member cannot 500 the server."""
    from opseval.models.results import Axis, VerdictStatus

    out = gen.render()
    assert all(f'"{a.value}"' in out for a in Axis)
    assert all(f'"{v.value}"' in out for v in VerdictStatus)


def test_the_generator_fails_closed(gen: ModuleType) -> None:
    """A schema shape it cannot type is an error, never a silent `unknown`."""
    with pytest.raises(ValueError, match="prefixItems"):
        gen.ts({"type": "array", "prefixItems": [{"type": "integer"}]}, "X.pair")
    with pytest.raises(ValueError, match="oneOf"):
        gen.ts({"oneOf": [{"type": "string"}]}, "X.choice")
    assert gen.ts({}, "X.anything") == "unknown"  # a declared Any stays unknown


def test_requests_and_responses_cannot_be_nested(
    gen: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opseval.server import schemas

    class Bad(schemas.ApiIn):
        summary: schemas.RunSummary

    kinds = {"RunSummary": False, "Bad": True}
    with pytest.raises(ValueError, match="keep requests and responses apart"):
        gen.interface(Bad, kinds)


def test_a_docstring_cannot_end_the_comment(gen: ModuleType) -> None:
    assert gen._doc("Ends */ here.\n\nSecond paragraph.") == ["/** Ends *\\/ here. */"]
