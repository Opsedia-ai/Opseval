# SPDX-License-Identifier: Apache-2.0
"""Compute the completion summary in docs/STATUS.md from its requirement table.

    uv run python scripts/project_status.py          # rewrite the summary block
    uv run python scripts/project_status.py --check  # fail if it is out of date (CI)

Each requirement row is ``| ID | what | priority | release | status | evidence |`` with
status ``Done`` (1), ``Partial`` (0.5) or ``Missing`` (0). Percentages are computed, never
typed, so the headline number cannot drift from the rows under it.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

STATUS = Path(__file__).resolve().parent.parent / "docs" / "STATUS.md"
START, END = "<!-- status:summary:start -->", "<!-- status:summary:end -->"
WEIGHT = {"Done": 1.0, "Partial": 0.5, "Missing": 0.0}
ROW = re.compile(
    r"^\|\s*(?P<id>FR-[A-Z]+(?:-\d+)?)\s*\|[^|]*\|\s*(?P<pri>[MSCW])\s*\|"
    r"\s*(?P<rel>v[\d.x]+)\s*\|\s*(?P<status>\w+)\s*\|"
)
RELEASE_ORDER = ["v0.1", "v0.2", "v0.5", "v0.8", "v1.x"]


def rows(text: str) -> list[dict[str, str]]:
    found = [m.groupdict() for line in text.splitlines() if (m := ROW.match(line))]
    bad = [r for r in found if r["status"] not in WEIGHT]
    if bad:
        raise SystemExit(f"unknown status in docs/STATUS.md: {bad[0]['id']} = {bad[0]['status']}")
    return found


def pct(items: list[dict[str, str]]) -> str:
    if not items:
        return "—"
    return f"{100 * sum(WEIGHT[r['status']] for r in items) / len(items):.0f}%"


def counts(items: list[dict[str, str]]) -> str:
    c = {k: sum(r["status"] == k for r in items) for k in WEIGHT}
    return f"{c['Done']} / {c['Partial']} / {c['Missing']}"


def summary(items: list[dict[str, str]]) -> str:
    by_rel: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in items:
        by_rel[r["rel"]].append(r)
    must = [r for r in items if r["pri"] == "M"]
    lines = [
        START,
        f"**Overall: {pct(items)} of {len(items)} functional requirements** "
        f"(Done = 1, Partial = ½, Missing = 0). Must-haves: {pct(must)} of {len(must)}.",
        "",
        "| Release | Requirements | Done / Partial / Missing | Complete |",
        "|---|---|---|---|",
    ]
    for rel in sorted(by_rel, key=lambda r: RELEASE_ORDER.index(r) if r in RELEASE_ORDER else 99):
        group = by_rel[rel]
        lines.append(f"| {rel} | {len(group)} | {counts(group)} | {pct(group)} |")
    lines.append(f"| **All** | {len(items)} | {counts(items)} | **{pct(items)}** |")
    lines.append(END)
    return "\n".join(lines)


def render(text: str) -> str:
    head, _, rest = text.partition(START)
    _, _, tail = rest.partition(END)
    if not rest:
        raise SystemExit("docs/STATUS.md has no summary markers")
    return head + summary(rows(text)) + tail


def main(argv: list[str]) -> int:
    text = STATUS.read_text("utf-8")
    new = render(text)
    if "--check" in argv:
        if new != text:
            print("docs/STATUS.md summary is out of date: run scripts/project_status.py")
            return 1
        return 0
    STATUS.write_text(new, "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
