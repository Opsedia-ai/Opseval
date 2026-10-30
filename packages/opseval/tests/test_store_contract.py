# SPDX-License-Identifier: Apache-2.0
"""The run store is path-free (ADR-0051): nothing outside ``storage.py`` builds a path into a
run, so a second store (the database) changes nothing above it."""

from __future__ import annotations

import re
from pathlib import Path

from opseval.storage import FileRunAudio, FileStore, RunAudio, RunStore

SRC = Path(__file__).resolve().parents[1] / "src" / "opseval"
AROUND_THE_STORE = re.compile(r"\bstore\.root\b|\.run_dir\(|\bstore\.root\.")


def test_nothing_outside_storage_reaches_around_the_store() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "storage.py" and path.parent == SRC:
            continue
        for n, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if AROUND_THE_STORE.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert offenders == [], "read and write runs through the RunStore:\n" + "\n".join(offenders)


def test_the_guard_catches_what_it_is_for() -> None:
    for line in ("store.run_dir(run_id) / 'x'", "for p in self.store.root.glob('*')"):
        assert AROUND_THE_STORE.search(line)
    assert not AROUND_THE_STORE.search("turns(run_dir, record)")


def test_the_file_store_keeps_the_contract(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    assert isinstance(store, RunStore)
    assert isinstance(store.audio("r1"), RunAudio)
    assert isinstance(FileRunAudio(tmp_path / "r1"), RunAudio)


def test_no_write_method_returns_a_path() -> None:
    import inspect
    import typing

    hints_of = typing.get_type_hints
    for name, fn in inspect.getmembers(RunStore, inspect.isfunction):
        if name.startswith(("save_", "write_")):
            assert hints_of(fn)["return"] in (type(None), str), name
