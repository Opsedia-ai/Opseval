# SPDX-License-Identifier: Apache-2.0
"""Writing a pulled catalogue label to a folder (ADR-0047, ADR-0058 E2).

One set of rules for both ways a label leaves the server: ``opseval catalogue pull`` (straight
from the database) and ``opseval run --server`` (over the HTTP API, with neither the database
nor the ``server`` extra installed). The folder must be new or empty, since a file left over
from something else would run as if it were part of the label, and every stored path is checked
again before anything is written: a path is only ever a catalogue file's path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from opseval.errors import ConfigError
from opseval.models.scenario import DATASET_FOLDER, DATASET_SUFFIXES


@dataclass(frozen=True)
class StoredFile:
    """One item's version under a label, as the catalogue stored it."""

    kind: str
    name: str
    path: str
    source: str


def safe_target(dest: Path, path: str) -> Path:
    """Where ``path`` goes inside ``dest`` (resolved), or :class:`ConfigError` when it is not
    a catalogue file's path: ``profile.yaml``, ``suites/*.yaml``, ``scenarios/**.yaml`` or a
    dataset under ``datasets/``. Never absolute, never ``..``."""
    rel = PurePosixPath(path)
    nested = len(rel.parts) > 1
    allowed = (
        rel == PurePosixPath("profile.yaml")
        or (nested and rel.parts[0] in ("suites", "scenarios") and rel.suffix == ".yaml")
        # Datasets (ADR-0060): only their two formats, and only where scenarios look for them.
        or (nested and rel.parts[0] == DATASET_FOLDER and rel.suffix in DATASET_SUFFIXES)
    )
    if rel.is_absolute() or ".." in rel.parts or "\\" in path or not allowed:
        raise ConfigError(f"stored path {path!r} is not a catalogue file path; refusing to write")
    target = (dest / Path(*rel.parts)).resolve()
    if not target.is_relative_to(dest):  # pragma: no cover - the checks above rule it out
        raise ConfigError(f"stored path {path!r} leaves the folder; refusing to write")
    return target


def write_label(dest: Path, files: list[StoredFile], label: str, where: str) -> Path:
    """Write ``files`` (a label's items) into ``dest``, a new or empty folder; returns it
    resolved. Everything is checked before the first file is written. ``where`` names the
    task in a refusal."""
    if not files:
        raise ConfigError(f"nothing in {where} carries the label {label!r}")
    dest = dest.resolve()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise ConfigError(f"{dest}: not an empty folder; pull into a new or empty one")
    targets: dict[Path, str] = {}
    for f in files:
        target = safe_target(dest, f.path)
        if target in targets:
            raise ConfigError(
                f"{f.path}: both {targets[target]} and {f.kind} {f.name!r} under "
                f"{label!r} write it; label only one of them"
            )
        targets[target] = f"{f.kind} {f.name!r}"
    for f in files:
        target = safe_target(dest, f.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # A dataset is written back exactly as pushed (its digest is in every run's snapshot).
        newline = "" if PurePosixPath(f.path).parts[0] == DATASET_FOLDER else None
        target.write_text(f.source, encoding="utf-8", newline=newline)
    return dest


__all__ = ["StoredFile", "safe_target", "write_label"]
