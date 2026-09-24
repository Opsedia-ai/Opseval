# SPDX-License-Identifier: Apache-2.0
"""Repository rules that are cheap to check on every run."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPDX = "# SPDX-License-Identifier: Apache-2.0"


def _python_files() -> list[Path]:
    return sorted(p for p in (ROOT / "packages").rglob("*.py") if ".venv" not in p.parts)


def test_python_files_are_found() -> None:
    assert len(_python_files()) > 10


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_python_file_has_an_spdx_header(path: Path) -> None:
    head = path.read_text(encoding="utf-8").splitlines()[:2]
    assert SPDX in head, f"{path.relative_to(ROOT)} must start with {SPDX!r}"
