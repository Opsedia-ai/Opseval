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

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from opseval.storage import (
    AUDIO_MARKER,
    RUN_NAME,
    PrunedMarker,
    RunStore,
    process_gone,
    read_pruned_marker,
)

RUN_FOLDER = RUN_NAME
"""The same names the console accepts as run ids."""

Action = Literal["delete", "keep"]


@dataclass(frozen=True)
class PruneItem:
    run_id: str
    action: Action
    reason: str
    started_at: datetime | None = None


def _started(store: RunStore, run_id: str) -> datetime | None:
    raw = store.load_manifest(run_id).get("started_at")
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _ended(store: RunStore, run_id: str) -> str | None:
    """How the run ended, or ``None`` if it may still be going."""
    if store.has_result(run_id):
        return "finished"
    if store.has_error(run_id):
        return "could not be carried out"
    if process_gone(store.load_manifest(run_id)):
        return "interrupted"
    return None


def plan(store: RunStore, *, older_than_days: int, now: datetime | None = None) -> list[PruneItem]:
    """What a prune would do to each run folder, oldest first. Deletes nothing."""
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    items: list[PruneItem] = []
    for name in store.run_ids():
        if not store.deletable(name):
            continue  # not ours to judge (a link, a foreign name): left alone, and not listed
        if not store.exists(name):
            continue  # not a run
        started = _started(store, name)
        ended = _ended(store, name)
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


def apply(store: RunStore, items: list[PruneItem]) -> PruneOutcome:
    """Delete the folders the plan marked ``delete``, carrying on past any that fail.

    Each folder is checked again just before it goes, so a plan cannot delete something
    that changed under it (a run that started writing, a folder replaced by a link).
    """
    deleted: list[str] = []
    failed: list[tuple[str, str]] = []
    for item in items:
        if item.action != "delete":
            continue
        if not store.deletable(item.run_id):
            continue
        if _ended(store, item.run_id) is None or not store.layout_known(item.run_id):
            continue
        try:
            gone = store.delete_run(item.run_id)
        except OSError as exc:
            failed.append((item.run_id, f"{type(exc).__name__}: {exc}"))
            continue
        if gone:
            deleted.append(item.run_id)
    return PruneOutcome(deleted=deleted, failed=failed)


@dataclass(frozen=True)
class AudioPruneItem:
    run_id: str
    files: int
    started_at: datetime


def plan_audio(
    store: RunStore,
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
    for name in store.run_ids():
        if name in skip or not store.deletable(name) or not store.exists(name):
            continue
        started = _started(store, name)
        if started is None or started > cutoff or not store.layout_known(name):
            continue
        if _ended(store, name) is None:
            continue  # may still be recording
        files = store.audio(name).count()
        if files:
            items.append(AudioPruneItem(name, files, started))
    return sorted(items, key=lambda i: (i.started_at, i.run_id))


def apply_audio(
    store: RunStore,
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
    for item in items:
        if not store.deletable(item.run_id):
            continue
        if _ended(store, item.run_id) is None or not store.layout_known(item.run_id):
            continue
        try:
            if not store.audio(item.run_id).prune(at=at, older_than_days=older_than_days):
                continue
        except OSError as exc:
            failed.append((item.run_id, f"{type(exc).__name__}: {exc}"))
            continue
        done.append(item.run_id)
    return PruneOutcome(deleted=done, failed=failed)


__all__ = [
    "AUDIO_MARKER",
    "AudioPruneItem",
    "PruneItem",
    "PrunedMarker",
    "PruneOutcome",
    "apply",
    "apply_audio",
    "plan",
    "plan_audio",
    "read_pruned_marker",
]
