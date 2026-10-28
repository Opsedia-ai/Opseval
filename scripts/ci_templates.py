# SPDX-License-Identifier: Apache-2.0
"""Copy the shared CI script into every template that carries it (ADR-0058, E3).

``templates/shared/opseval-ci.sh`` is the source. GitLab, CircleCI and Azure DevOps inline it
in YAML, the Jenkins library ships it as a resource and the Bitbucket pipe as its entrypoint.
Each copy sits between the ``# --- begin opseval-ci`` and ``# --- end opseval-ci`` lines, at
the indentation of the begin line. Run this after changing the source; with ``--check`` it
only reports drift (``tests/test_ci_templates.py`` checks the same).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "templates" / "shared" / "opseval-ci.sh"
COPIES = (
    ROOT / "templates" / "gitlab" / "opseval.yml",
    ROOT / "templates" / "circleci" / "orb" / "orb.yml",
    ROOT / "templates" / "azure" / "opseval.yml",
    ROOT / "templates" / "jenkins" / "resources" / "opseval" / "opseval-ci.sh",
    ROOT / "templates" / "bitbucket" / "pipe" / "pipe.sh",
)
BEGIN = "# --- begin opseval-ci"
END = "# --- end opseval-ci"


def block(text: str) -> tuple[int, int, str, list[str]]:
    """(start, end) line indexes of the block, its indentation, and its dedented lines."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip().startswith(BEGIN)]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == END]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise ValueError("expected exactly one begin and one end marker, in that order")
    start, end = starts[0], ends[0]
    indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
    body = []
    for ln in lines[start : end + 1]:
        if ln.strip() and not ln.startswith(indent):
            raise ValueError(f"a line in the block is less indented than the marker: {ln!r}")
        body.append(ln[len(indent) :] if ln.strip() else "")
    return start, end, indent, body


def sync(path: Path, source: list[str]) -> str:
    text = path.read_text(encoding="utf-8")
    start, end, indent, _ = block(text)
    lines = text.splitlines()
    copied = [indent + ln if ln else "" for ln in source]
    return "\n".join(lines[:start] + copied + lines[end + 1 :]) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    _, _, _, source = block(SOURCE.read_text(encoding="utf-8"))
    stale = []
    for path in COPIES:
        new = sync(path, source)
        if new != path.read_text(encoding="utf-8"):
            stale.append(path.relative_to(ROOT))
            if not check:
                path.write_text(new, encoding="utf-8")
    for path in stale:
        print(f"{'stale' if check else 'updated'}: {path}")
    return 1 if check and stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
