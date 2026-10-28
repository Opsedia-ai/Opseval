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

Recorded audio has a shorter retention of its own (``OPSEVAL_AUDIO_RETENTION_DAYS``, 30 by
default; ADR-0053): a voice can be biometric data, so a run's ``audio/`` is emptied long
before the run goes. Records are never rewritten (ADR-0005), so the run keeps every turn's
hash and transcript, and ``audio/PRUNED.json`` says when the audio went: a reader reports
"audio pruned on <date>", never missing evidence. The marker is written before any file is
removed, so a prune stopped part-way still reads as pruned.
"""

from __future__ import annotations

import json
import os
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


AUDIO_MARKER = "PRUNED.json"
"""Shared with ``voice.playback.PRUNED_MARKER`` (a test keeps them equal); retention does
not import the voice package."""


@dataclass(frozen=True)
class AudioPruneItem:
    run_id: str
    files: int
    started_at: datetime


def _audio_files(folder: Path) -> list[Path] | None:
    """The files under a run's ``audio/``, marker aside; ``None`` when ``audio/`` is not a
    plain folder of ours (absent, or a link: never followed)."""
    audio = folder / "audio"
    if audio.is_symlink() or not audio.is_dir():
        return None
    found: list[Path] = []
    for root, dirs, names in os.walk(audio, followlinks=False):
        here = Path(root)
        found.extend(here / n for n in names if not (here == audio and n == AUDIO_MARKER))
        dirs[:] = [d for d in dirs if not (here / d).is_symlink()]
    return found


def plan_audio(
    store: FileStore,
    *,
    older_than_days: int,
    now: datetime | None = None,
    skip: frozenset[str] = frozenset(),
) -> list[AudioPruneItem]:
    """Finished runs whose recorded audio is older than the audio retention, oldest first.
    ``skip`` names runs the run prune deletes anyway. Deletes nothing."""
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    items: list[AudioPruneItem] = []
    if not store.root.is_dir():
        return items
    for folder in sorted(store.root.iterdir()):
        name = folder.name
        if name in skip or folder.is_symlink() or not folder.is_dir():
            continue
        if not RUN_FOLDER.match(name) or not (folder / "manifest.json").is_file():
            continue
        started = _started(store, name)
        if started is None or started > cutoff or not store.layout_known(name):
            continue
        if _ended(store, folder) is None:
            continue  # may still be recording
        files = _audio_files(folder)
        if files:
            items.append(AudioPruneItem(name, len(files), started))
    return sorted(items, key=lambda i: (i.started_at, i.run_id))


def apply_audio(
    store: FileStore,
    items: list[AudioPruneItem],
    *,
    older_than_days: int,
    now: datetime | None = None,
) -> PruneOutcome:
    """Empty each planned run's ``audio/``: the marker first, then the files. Each run is
    checked again just before (ended, a plain folder directly inside the reports folder)."""
    at = now or datetime.now(UTC)
    done: list[str] = []
    failed: list[tuple[str, str]] = []
    root = store.root.resolve()
    for item in items:
        folder = store.root / item.run_id
        if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != root:
            continue
        if _ended(store, folder) is None or not store.layout_known(item.run_id):
            continue
        files = _audio_files(folder)
        if not files:
            continue
        audio = folder / "audio"
        try:
            marker = audio / AUDIO_MARKER
            earlier: list[object] = []
            try:
                previous = json.loads(marker.read_text("utf-8"))
                if isinstance(previous, dict) and isinstance(previous.get("history"), list):
                    earlier = previous["history"]
            except (OSError, ValueError):
                pass
            body = {
                "pruned_at": at.isoformat(),
                "older_than_days": older_than_days,
                "files": len(files),
                "history": [*earlier, {"pruned_at": at.isoformat(), "files": len(files)}],
            }
            tmp = marker.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
            os.replace(tmp, marker)
            for path in files:
                path.unlink(missing_ok=True)
            for sub in sorted((p for p in audio.rglob("*") if p.is_dir()), reverse=True):
                if not sub.is_symlink() and not any(sub.iterdir()):
                    sub.rmdir()
        except OSError as exc:
            failed.append((item.run_id, f"{type(exc).__name__}: {exc}"))
            continue
        done.append(item.run_id)
    return PruneOutcome(deleted=done, failed=failed)


__all__ = [
    "AUDIO_MARKER",
    "AudioPruneItem",
    "PruneItem",
    "PruneOutcome",
    "apply",
    "apply_audio",
    "plan",
    "plan_audio",
]
