# SPDX-License-Identifier: Apache-2.0
"""Refuse a release whose tag, package versions and plugin pin disagree.

    uv run python scripts/check_release.py v0.2.0

``pytest-opseval`` calls into ``opseval``'s runner, so it may only be installed beside the
``opseval`` it was released with. Its requirement on ``opseval`` must admit this version,
and both packages must carry the version the tag names.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

import opseval
import pytest_opseval

ROOT = Path(__file__).resolve().parent.parent


def problems(tag: str) -> list[str]:
    wanted = Version(tag.removeprefix("v"))
    found: list[str] = []
    for name, version in (
        ("opseval", opseval.__version__),
        ("pytest-opseval", pytest_opseval.__version__),
    ):
        if Version(version) != wanted:
            found.append(f"{name} is {version}, but the tag is {tag}")
    project = tomllib.loads((ROOT / "packages/pytest-opseval/pyproject.toml").read_text("utf-8"))
    pins = [Requirement(d) for d in project["project"]["dependencies"]]
    pin = next((r for r in pins if r.name == "opseval"), None)
    if pin is None or not pin.specifier:
        found.append("pytest-opseval must pin the opseval versions it supports")
    elif not pin.specifier.contains(wanted, prereleases=True):
        found.append(f"pytest-opseval requires {pin}, which excludes opseval {wanted}")
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_release.py <tag>")
        return 2
    found = problems(argv[0])
    for problem in found:
        print(f"release refused: {problem}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
