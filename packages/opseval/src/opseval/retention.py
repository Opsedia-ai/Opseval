# SPDX-License-Identifier: Apache-2.0
"""Retention: prune run folders older than N days (FR-SAF-06, ADR-0037).

A run folder holds whole conversations, so keeping them for ever is a liability rather than
a feature. Pruning is the one thing in Opseval that deletes data, so it is built to be
boring:

- it plans first and deletes only when asked (``opseval prune`` is a dry run without
  ``--yes``);
- it deletes only what is clearly a run folder, directly inside the reports folder, and
  never through a symbolic link;
- it never deletes a run that may still be going. A run is old enough to delete only once it
  has an end: a result, an error, or a process on this host that is gone. A run from another
  host, or one whose start cannot be read, is kept and the plan says why.

Age is measured from when the run started, as its manifest records it.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from opseval.storage import FileStore, process_gone

RUN_FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
"""The same names the console accepts as run ids."""

Action = Literal["delete", "keep"]


@dataclass(frozen=True)
class PruneItem:
    run_id: str
    action: Action
    reason: str
    started_at: datetime | None = None


def _started(store: FileStore, run_id: str) -> datetime | None:
    raw = store.load_manifest(run_id).get("started_at")
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _ended(store: FileStore, folder: Path) -> str | None:
    """How the run ended, or ``None`` if it may still be going."""
    if (folder / "result.json").is_file():
        return "finished"
    if (folder / "error.json").is_file():
        return "could not be carried out"
    if process_gone(store.load_manifest(folder.name)):
        return "interrupted"
    return None


def plan(store: FileStore, *, older_than_days: int, now: datetime | None = None) -> list[PruneItem]:
    """What a prune would do to each run folder, oldest first. Deletes nothing."""
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    items: list[PruneItem] = []
    if not store.root.is_dir():
        return items
    for folder in sorted(store.root.iterdir()):
        name = folder.name
        if folder.is_symlink() or not folder.is_dir() or not RUN_FOLDER.match(name):
            continue  # not ours to judge: left alone, and not listed
        if not (folder / "manifest.json").is_file():
            continue  # not a run folder
        started = _started(store, name)
        ended = _ended(store, folder)
        if not store.layout_known(name):
            reason = f"written by a newer Opseval (layout {store.layout(name)!r})"
            items.append(PruneItem(name, "keep", reason, started))
        elif started is None:
            items.append(PruneItem(name, "keep", "its start time cannot be read"))
        elif started > cutoff:
            items.append(PruneItem(name, "keep", f"newer than {older_than_days} days", started))
        elif ended is None:
            items.append(
                PruneItem(name, "keep", "it may still be running (no result yet)", started)
            )
        else:
            items.append(
                PruneItem(name, "delete", f"{ended}, older than {older_than_days} days", started)
            )
    epoch = datetime.min.replace(tzinfo=UTC)
    return sorted(items, key=lambda i: (i.started_at or epoch, i.run_id))


@dataclass(frozen=True)
class PruneOutcome:
    deleted: list[str]
    failed: list[tuple[str, str]]
    """Runs that could not be (fully) removed, with why."""


def _remove_run(folder: Path) -> None:
    """Delete one run folder in an order that is safe to stop at any point.

    Everything else goes first, then the manifest, then the result or error file. If it
    stops part-way, the folder still reads as a finished run, or no longer as a run at
    all; never as one that is still going.
    """
    markers = ("manifest.json", "result.json", "error.json")
    for child in folder.iterdir():
        if child.name in markers:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for name in markers:
        (folder / name).unlink(missing_ok=True)
    folder.rmdir()


def apply(store: FileStore, items: list[PruneItem]) -> PruneOutcome:
    """Delete the folders the plan marked ``delete``, carrying on past any that fail.

    Each folder is checked again just before it goes, so a plan cannot delete something
    that changed under it (a run that started writing, a folder replaced by a link).
    """
    deleted: list[str] = []
    failed: list[tuple[str, str]] = []
    root = store.root.resolve()
    for item in items:
        if item.action != "delete":
            continue
        folder = store.root / item.run_id
        if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != root:
            continue
        if _ended(store, folder) is None or not store.layout_known(item.run_id):
            continue
        try:
            _remove_run(folder)
        except OSError as exc:
            failed.append((item.run_id, f"{type(exc).__name__}: {exc}"))
            continue
        deleted.append(item.run_id)
    return PruneOutcome(deleted=deleted, failed=failed)


__all__ = ["PruneItem", "PruneOutcome", "apply", "plan"]
