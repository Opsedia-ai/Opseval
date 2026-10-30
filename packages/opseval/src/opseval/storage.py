# SPDX-License-Identifier: Apache-2.0
"""FileStore: the on-disk layout contract (ADR-0005).

<root>/<run_id>/manifest.json
<root>/<run_id>/profile.json                     (SUT profile as run, for offline re-scoring)
<root>/<run_id>/scenarios/<test_id>.json         (each scenario as run, suite merges applied)
<root>/<run_id>/records/<test_id>__attempt<N>.json
<root>/<run_id>/result.json        (written last: marks completion)
<root>/<run_id>/error.json         (instead of result.json: the run could not be carried out)
<root>/<run_id>/progress.json      (while it runs: the runner's own account of how far it got)
<root>/<run_id>/cancel.json        (a request to stop; the runner honours it before each attempt)
<root>/<run_id>/report.html
<root>/<run_id>/junit.xml
<root>/<run_id>/rescore-<UTC time>/{result.json, report.html, junit.xml}   (opseval rescore)

Records are written once and never modified; re-scoring writes a new folder.

The layout is versioned (``manifest.json`` ``"layout"``, ADR-0005 amendment). A folder without
the field predates versioning and has layout 1. A reader refuses a layout it does not know
(:class:`UnknownLayout`) instead of guessing at a newer one.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from opseval.errors import ConfigError
from opseval.models.profile import SutProfile
from opseval.models.progress import RunProgress
from opseval.models.record import RunRecord
from opseval.models.results import SuiteResult
from opseval.models.scenario import Scenario

SAFE = re.compile(r"[^A-Za-z0-9_.-]")
SAFE_ID = re.compile(r"[^A-Za-z0-9_.@-]")
"""What a file name keeps of a scenario id. ``@`` stays: ``a@1a2b3c4d`` (an expansion,
ADR-0060) must never share a file with an authored ``a_1a2b3c4d``."""


def _unreadable(exc: BaseException) -> str:
    """Why a run's result could not be read, in one line."""
    text = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        return str(first.get("msg", exc))
    return f"{type(exc).__name__}: {exc}"


LEGACY_LAYOUT = "opseval-runs/1"
"""A folder whose manifest has no layout field predates versioning: layout 1."""
LAYOUT = "opseval-runs/8"
"""The layout this version writes. Bump it (and keep reading the old) on any change a reader
of an older folder, or an older reader of a new folder, could misread or reject. 2: run
records may carry created_artifacts. 3: frames may carry spoken audio and its transcript,
records a voice timeline, and the run folder an ``audio/`` folder (ADR-0053). 4: a result's
usage rows may carry speech counts (audio seconds, characters) and ``null`` tokens for a
speech role (ADR-0054); an older reader (``extra="forbid"``, integer tokens) would reject it.
5: the profile snapshot may carry an ``audio:`` block, and a spoken reply's payload the voice
bridge's evidence (``_opseval_voice``: transcripts tried, the STT canary, ADR-0053), with the
audio files themselves under ``audio/``. 6: a record carries its usage events (one per LLM,
TTS or STT call, ADR-0066), a result the calls made outside any attempt, and a usage row
reasoning and cached tokens, failed calls and how many calls reported each count, with token
totals ``null`` when a call did not report them. 7: a voice timeline may carry its mode
(``turns`` or ``duplex``), its speech spans, the planted noise and the per-turn voice metrics
(ADR-0055); an older reader (``extra="forbid"``) would reject them. 8: a voice timeline may
carry the metric inputs (``inputs``: caller texts and times, the agent's transcripts with their
arrival, barge-in outcomes), the STT confidence bound, and harness spans say whether they held
the line (``holds_line``), so ``opseval rescore`` can recompute the metrics (review of #91)."""
KNOWN_LAYOUTS = frozenset(
    {
        LEGACY_LAYOUT,
        "opseval-runs/2",
        "opseval-runs/3",
        "opseval-runs/4",
        "opseval-runs/5",
        "opseval-runs/6",
        "opseval-runs/7",
        LAYOUT,
    }
)

PRE_USAGE_LAYOUTS = frozenset(
    {LEGACY_LAYOUT, "opseval-runs/2", "opseval-runs/3", "opseval-runs/4", "opseval-runs/5"}
)
"""Layouts written before per-call usage (ADR-0066): their records carry no usage events, and
their usage rows' totals are as written (a count the provider did not report was added as 0).

The rule: this set is literal and closed. It is never derived from ``LAYOUT`` or
``KNOWN_LAYOUTS``: when ``LAYOUT`` moves to 7, a layout-6 run still has per-call usage and must
not become "written before per-call usage". Nothing is ever added to it."""


class UnknownLayout(ConfigError):
    """A run folder in a layout this version cannot read: written by a newer Opseval."""


def layout_at(folder: Path) -> str:
    """The layout of the run folder at ``folder``; no field means layout 1."""
    try:
        data = json.loads((folder / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    found = data.get("layout", LEGACY_LAYOUT) if isinstance(data, dict) else LEGACY_LAYOUT
    return found if isinstance(found, str) else repr(found)


def load_result_at(folder: Path) -> SuiteResult:
    """The finished run at exactly ``folder`` (any name, ``.`` included), layout checked."""
    found = layout_at(folder)
    if found not in KNOWN_LAYOUTS:
        raise UnknownLayout(
            f"the run in {folder.name or folder} was written in layout {found!r}, which this "
            f"version of Opseval cannot read (it reads {', '.join(sorted(KNOWN_LAYOUTS))}); "
            "upgrade Opseval to read it"
        )
    return SuiteResult.model_validate_json((folder / "result.json").read_text("utf-8"))


AUDIO_MARKER = "PRUNED.json"
"""``audio/PRUNED.json``: written by retention before it removes a run's audio (ADR-0053).
The one name every reader uses; retention does not import the voice package."""


@dataclass(frozen=True)
class PrunedMarker:
    """What retention left in place of a run's audio."""

    pruned_at: datetime | None
    readable: bool = True
    """``False``: the marker is there but cannot be read. The audio still counts as pruned
    (a reader never plays around a marker, and never calls a pruned run's audio missing)."""

    @property
    def note(self) -> str:
        """In words, the same in the player, the CLI and the report."""
        if not self.readable:
            return "audio pruned (marker unreadable)"
        if self.pruned_at is not None:
            return f"audio pruned on {self.pruned_at.date().isoformat()}"
        return "audio pruned by retention"


def read_pruned_marker(run_dir: Path) -> PrunedMarker | None:
    """The run folder's audio prune marker, or ``None`` when there is none. The one parser
    the player, the CLI and the report share, so they never disagree on a run."""
    path = run_dir / "audio" / AUDIO_MARKER
    try:
        text = path.read_text("utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return PrunedMarker(None, readable=False) if os.path.lexists(path) else None
    except OSError:
        return PrunedMarker(None, readable=False)
    return parse_pruned_marker(text)


def parse_pruned_marker(text: str) -> PrunedMarker:
    """A marker that exists, from its text: unreadable text still means pruned."""
    try:
        data = json.loads(text)
    except ValueError:
        return PrunedMarker(None, readable=False)
    if not isinstance(data, dict):
        return PrunedMarker(None, readable=False)
    at = data.get("pruned_at")
    if isinstance(at, str):
        try:
            return PrunedMarker(datetime.fromisoformat(at))
        except ValueError:
            pass
    return PrunedMarker(None)


def marker_text(previous: str | None, *, at: datetime, older_than_days: int, files: int) -> str:
    """The prune marker to write, carrying the history of an earlier one forward."""
    earlier: list[object] = []
    try:
        found = json.loads(previous) if previous is not None else None
        if isinstance(found, dict) and isinstance(found.get("history"), list):
            earlier = found["history"]
    except ValueError:
        pass
    body = {
        "pruned_at": at.isoformat(),
        "older_than_days": older_than_days,
        "files": files,
        "history": [*earlier, {"pruned_at": at.isoformat(), "files": files}],
    }
    return json.dumps(body, indent=2)


def check_audio_path(relative: str) -> str:
    """A path a record may name for audio: under ``audio/``, no ``..``, no absolute path."""
    parts = relative.split("/")
    if parts[0] != "audio" or len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
        raise AudioPathRefused(f"{relative} leaves the run's audio/")
    return relative


class AudioPathRefused(ValueError):
    """An audio path that leaves the run's ``audio/`` (a tampered record, or a hostile id)."""


@runtime_checkable
class RunAudio(Protocol):
    """One run's recorded audio (ADR-0053), addressed by the relative path an
    :class:`~opseval.models.record.AudioRef` names (``audio/<test>/attempt<N>/...``).

    A path outside ``audio/`` raises :class:`AudioPathRefused`; nothing is ever read or
    written there. A missing file is ``None`` from :meth:`size` and ``FileNotFoundError``
    from :meth:`read`."""

    run_id: str

    def write(self, relative: str, data: bytes) -> None: ...
    def size(self, relative: str) -> int | None: ...
    def read(self, relative: str) -> bytes: ...
    def pruned(self) -> PrunedMarker | None: ...
    def count(self) -> int | None: ...
    def prune(self, *, at: datetime, older_than_days: int) -> int: ...
    def location(self) -> str: ...
    def export_args(self) -> str: ...


@dataclass(frozen=True)
class RunCounts:
    """A finished run's headline as an index holds it (the database's ``runs`` row), so a
    listing need not read every result."""

    suite: str
    started_at: datetime
    gate: str
    passed: int
    failed: int
    unjudged: int
    flaky: int


@dataclass(frozen=True)
class StoredRun:
    """One run as a listing needs it. ``counts`` is ``None`` when the store keeps no index
    (run folders): the reader then reads the result, and names a run it cannot read."""

    run_id: str
    layout: str
    manifest: dict[str, Any]
    finished: bool
    error: dict[str, Any] | None
    progress: RunProgress | None
    cancel_requested: bool
    changed_at: float | None
    counts: RunCounts | None = None


@dataclass(frozen=True)
class UnreadableRun:
    """A finished run whose result could not be read: named, never silently left out."""

    run_id: str
    reason: str


@dataclass(frozen=True)
class FinishedRuns:
    results: list[SuiteResult]
    """Every finished run this version can read, newest first."""
    unreadable: list[UnreadableRun]
    """Finished runs that could not be read (a broken result, a storage failure)."""


@runtime_checkable
class RunStore(Protocol):
    """What the runner, rescore, calibrate, prune and the console need from storage (ADR-0005,
    ADR-0051).

    Path-free: every method names a run by id, writes return nothing (or an opaque ref), and
    reads return models, text or bytes. :class:`FileStore` (run folders on disk) and the
    server-mode database store implement it, so nothing above it knows which one it has. A
    reader refuses a layout it does not know (:class:`UnknownLayout`).
    """

    def where(self) -> str: ...
    def run_ids(self) -> list[str]: ...
    def exists(self, run_id: str) -> bool: ...
    def has_result(self, run_id: str) -> bool: ...
    def has_error(self, run_id: str) -> bool: ...
    def deletable(self, run_id: str) -> bool: ...
    def delete_run(self, run_id: str) -> bool: ...
    def layout(self, run_id: str) -> str: ...
    def layout_known(self, run_id: str) -> bool: ...
    def check_layout(self, run_id: str) -> None: ...
    def write_manifest(self, run_id: str, manifest: dict[str, Any]) -> None: ...
    def load_manifest(self, run_id: str) -> dict[str, Any]: ...
    def save_profile(self, run_id: str, profile: SutProfile) -> None: ...
    def save_scenario(self, run_id: str, scenario: Scenario) -> None: ...
    def load_snapshot(self, run_id: str) -> tuple[SutProfile, dict[str, Scenario]] | None: ...
    def save_record(self, record: RunRecord) -> str: ...
    def load_record(self, run_id: str, test_id: str, attempt: int) -> RunRecord | None: ...
    def load_records(self, run_id: str) -> list[RunRecord]: ...
    def save_progress(self, run_id: str, progress: RunProgress) -> None: ...
    def load_progress(self, run_id: str) -> RunProgress | None: ...
    def request_cancel(self, run_id: str, *, by: str) -> bool: ...
    def cancel_requested(self, run_id: str) -> bool: ...
    def save_error(self, run_id: str, error: dict[str, Any]) -> None: ...
    def load_error(self, run_id: str) -> dict[str, Any] | None: ...
    def save_result(self, result: SuiteResult) -> None: ...
    def load_result(self, run_id: str) -> SuiteResult: ...
    def read_finished(self) -> FinishedRuns: ...
    def listing(self) -> list[StoredRun]: ...
    def changed_at(self, run_id: str, rescore: str | None = None) -> float | None: ...
    def save_report(self, run_id: str, html: str) -> None: ...
    def load_report(self, run_id: str) -> str | None: ...
    def save_junit(self, run_id: str, xml: str) -> None: ...
    def load_junit(self, run_id: str) -> str | None: ...
    def save_rescore(
        self, run_id: str, stamp: str, result: SuiteResult, html: str, xml: str
    ) -> str: ...
    def rescores(self, run_id: str) -> list[str]: ...
    def load_rescore(self, run_id: str, stamp: str) -> SuiteResult: ...
    def audio(self, run_id: str) -> RunAudio: ...


def process_gone(manifest: dict[str, Any]) -> bool:
    """True only when we can tell: same host, and that process no longer exists.

    A run from another machine, or an older manifest without a pid, stays "running":
    guessing it dead could hide a run that is in fact still going.
    """
    pid, host = manifest.get("pid"), manifest.get("host")
    if os.name == "nt" or not isinstance(pid, int) or host != socket.gethostname():
        # On Windows os.kill(pid, 0) is TerminateProcess: the probe would kill the run.
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:  # exists but not ours (EPERM): alive
        return False
    return False


RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
"""The run ids a store deletes: the same names the console accepts as run ids."""
RESCORE_PREFIX = "rescore-"


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


class FileRunAudio:
    """A run folder's ``audio/`` (ADR-0053). Nothing is read or written outside it, links
    included."""

    def __init__(self, folder: Path, run_id: str | None = None) -> None:
        self.folder = folder
        self.run_id = run_id if run_id is not None else folder.name

    def _path(self, relative: str) -> Path:
        path = self.folder / relative
        root = (self.folder / "audio").resolve()
        if root not in path.resolve().parents:
            raise AudioPathRefused(f"{relative} leaves the run folder's audio/")
        return path

    def write(self, relative: str, data: bytes) -> None:
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def size(self, relative: str) -> int | None:
        path = self._path(relative)
        try:
            return path.stat().st_size if path.is_file() else None
        except OSError:
            return None

    def read(self, relative: str) -> bytes:
        return self._path(relative).read_bytes()

    def pruned(self) -> PrunedMarker | None:
        return read_pruned_marker(self.folder)

    def count(self) -> int | None:
        """How many audio files the run keeps (the marker aside); ``None`` when there is no
        plain ``audio/`` folder."""
        files = _audio_files(self.folder)
        return None if files is None else len(files)

    def prune(self, *, at: datetime, older_than_days: int) -> int:
        """Empty ``audio/``: the marker first, then the files, so a prune stopped part-way
        still reads as pruned. Returns how many files went; raises ``OSError``."""
        files = _audio_files(self.folder)
        if not files:
            return 0
        audio = self.folder / "audio"
        marker = audio / AUDIO_MARKER
        try:
            previous: str | None = marker.read_text("utf-8")
        except OSError:
            previous = None
        body = marker_text(previous, at=at, older_than_days=older_than_days, files=len(files))
        tmp = marker.with_suffix(".json.tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, marker)
        for path in files:
            path.unlink(missing_ok=True)
        for sub in sorted((p for p in audio.rglob("*") if p.is_dir()), reverse=True):
            if not sub.is_symlink() and not any(sub.iterdir()):
                sub.rmdir()
        return len(files)

    def location(self) -> str:
        return str(self.folder)

    def export_args(self) -> str:
        """What ``opseval audio export`` takes to find this run: its folder."""
        return shlex.quote(str(self.folder))


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


class FileStore:
    """Run folders on disk (ADR-0005). The only class that knows a run is a folder: nothing
    outside this module builds a path into it (``tests/test_store_contract.py``)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def run_dir(self, run_id: str) -> Path:
        name = SAFE.sub("_", run_id)
        if name.strip(".") == "":
            # "", "." and ".." would name the reports folder itself or its parent.
            raise ValueError(f"not a run id: {run_id!r}")
        return self.root / name

    def where(self) -> str:
        return str(self.root)

    def run_ids(self) -> list[str]:
        """Every folder in the reports folder, sorted: a reader decides which are runs."""
        if not self.root.is_dir():
            return []
        return sorted(f.name for f in self.root.iterdir() if f.is_dir())

    def exists(self, run_id: str) -> bool:
        """A run folder: it has a manifest."""
        return (self.run_dir(run_id) / "manifest.json").is_file()

    def has_result(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "result.json").is_file()

    def has_error(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "error.json").is_file()

    def deletable(self, run_id: str) -> bool:
        """A plain folder directly inside the reports folder, named as a run: never a link,
        never anything a prune would have to guess about."""
        if not RUN_NAME.match(run_id):
            return False
        folder = self.root / run_id
        return (
            not folder.is_symlink()
            and folder.is_dir()
            and folder.resolve().parent == self.root.resolve()
        )

    def delete_run(self, run_id: str) -> bool:
        """Delete a run's folder; ``False`` when it is not :meth:`deletable`. Raises
        ``OSError`` when it could not be (fully) removed."""
        if not self.deletable(run_id):
            return False
        _remove_run(self.root / run_id)
        return True

    def save_error(self, run_id: str, error: dict[str, Any]) -> None:
        """Record that a run could not be carried out (a harness or configuration error).

        It takes the place of ``result.json``, so a run that failed to start or crashed is
        a visible harness error rather than a run that is "running" forever or never
        appears at all.
        """
        path = self.run_dir(run_id) / "error.json"
        _atomic_write(path, json.dumps(error, indent=2, ensure_ascii=False, default=str))

    def load_error(self, run_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads((self.run_dir(run_id) / "error.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def layout(self, run_id: str) -> str:
        """The run folder's layout. A manifest without one predates versioning: layout 1."""
        return layout_at(self.run_dir(run_id))

    def layout_known(self, run_id: str) -> bool:
        return self.layout(run_id) in KNOWN_LAYOUTS

    def check_layout(self, run_id: str) -> None:
        """Raise :class:`UnknownLayout` rather than read a folder this version may misread."""
        found = self.layout(run_id)
        if found not in KNOWN_LAYOUTS:
            raise UnknownLayout(
                f"run {run_id!r} was written in layout {found!r}, which this version of "
                f"Opseval cannot read (it reads {', '.join(sorted(KNOWN_LAYOUTS))}); "
                "upgrade Opseval to read it"
            )

    def write_manifest(self, run_id: str, manifest: dict[str, Any]) -> None:
        path = self.run_dir(run_id) / "manifest.json"
        body = {"layout": LAYOUT, **manifest}
        _atomic_write(path, json.dumps(body, indent=2, ensure_ascii=False, default=str))

    def load_manifest(self, run_id: str) -> dict[str, Any]:
        """The run's manifest, or ``{}`` if it is missing or unreadable.

        Re-scoring an older run must not fail just because its manifest is absent.
        """
        try:
            data = json.loads((self.run_dir(run_id) / "manifest.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_profile(self, run_id: str, profile: SutProfile) -> None:
        _atomic_write(self.run_dir(run_id) / "profile.json", profile.model_dump_json(indent=2))

    def save_scenario(self, run_id: str, scenario: Scenario) -> None:
        path = self.run_dir(run_id) / "scenarios" / f"{SAFE_ID.sub('_', scenario.id)}.json"
        _atomic_write(path, scenario.model_dump_json(indent=2, by_alias=True))

    def load_snapshot(self, run_id: str) -> tuple[SutProfile, dict[str, Scenario]] | None:
        """The profile and scenarios exactly as run, or ``None`` for runs without one.

        Raises :class:`ConfigError` when the snapshot no longer loads: a rule added since the
        run (say, a profile check) can reject what was valid then. That is not a crash, and
        the run can still be scored against current files.
        """
        self.check_layout(run_id)
        folder = self.run_dir(run_id)
        profile_path = folder / "profile.json"
        if not profile_path.is_file():
            return None
        current = profile_path
        try:
            texts = [(profile_path.name, profile_path.read_text("utf-8"))]
            for current in sorted((folder / "scenarios").glob("*.json")):
                texts.append((current.name, current.read_text("utf-8")))
        except OSError as exc:
            raise ConfigError(
                f"the scenarios and profile saved with run {run_id!r} no longer load "
                f"({current.name}: {_first_error(exc)}); pass --catalogue and --profile to "
                "score it against the current files"
            ) from exc
        return parse_snapshot(run_id, texts)

    def load_result(self, run_id: str) -> SuiteResult:
        return load_result_at(self.run_dir(run_id))

    def read_finished(self) -> FinishedRuns:
        """Every finished run this version can read, newest first, and those it cannot (named,
        never dropped). A run without ``result.json`` is still in flight; a re-score lives
        inside its run's folder and is not a run of its own; a run in a newer layout is listed
        by the console as an error row of its own."""
        out: list[SuiteResult] = []
        unreadable: list[UnreadableRun] = []
        for folder in self.root.glob("*/result.json"):
            if not self.layout_known(folder.parent.name):
                continue  # listed by the console as an error row: shown, never misread
            try:
                out.append(self.load_result(folder.parent.name))
            except (ValueError, OSError) as exc:
                unreadable.append(UnreadableRun(folder.parent.name, _unreadable(exc)))
        return FinishedRuns(
            sorted(out, key=lambda r: r.started_at, reverse=True),
            sorted(unreadable, key=lambda u: u.run_id),
        )

    def listing(self) -> list[StoredRun]:
        """Every run folder, as a listing needs it (no index: ``counts`` is ``None``)."""
        out: list[StoredRun] = []
        for run_id in self.run_ids():
            finished = self.has_result(run_id)
            out.append(
                StoredRun(
                    run_id=run_id,
                    layout=self.layout(run_id),
                    manifest=self.load_manifest(run_id),
                    finished=finished,
                    error=None if finished else self.load_error(run_id),
                    progress=None if finished else self.load_progress(run_id),
                    cancel_requested=self.cancel_requested(run_id),
                    changed_at=self.changed_at(run_id),
                )
            )
        return out

    def changed_at(self, run_id: str, rescore: str | None = None) -> float | None:
        """When the run's result (or one re-score of it) was last written; ``None`` when there
        is none. A cache keyed on it sees every rewrite."""
        folder = self.run_dir(run_id)
        if rescore is not None:
            folder = folder / f"{RESCORE_PREFIX}{rescore}"
        try:
            return (folder / "result.json").stat().st_mtime
        except OSError:
            return None

    def save_record(self, record: RunRecord) -> str:
        """Write one attempt's record; returns its ref (here, the file's path as text)."""
        name = f"{SAFE_ID.sub('_', record.test_id)}__attempt{record.attempt_index}.json"
        path = self.run_dir(record.run_id) / "records" / name
        _atomic_write(path, record.model_dump_json(indent=2))
        return str(path)

    def request_cancel(self, run_id: str, *, by: str) -> bool:
        """Ask a running run to stop (ADR-0035). ``False`` if it was already asked.

        A file rather than a signal, so it works across processes: a run the CLI started
        can be cancelled from the console, and the reverse. The runner checks it before
        each attempt; what it has not run is recorded as cancelled, never dropped.
        """
        path = self.run_dir(run_id) / "cancel.json"
        if path.exists():
            return False
        body = {"requested_at": datetime.now(UTC).isoformat(), "by": by}
        _atomic_write(path, json.dumps(body))
        return True

    def cancel_requested(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "cancel.json").is_file()

    def save_progress(self, run_id: str, progress: RunProgress) -> None:
        _atomic_write(self.run_dir(run_id) / "progress.json", progress.model_dump_json(indent=2))

    def load_progress(self, run_id: str) -> RunProgress | None:
        """The runner's progress, or ``None`` if it wrote none (or it cannot be read)."""
        try:
            return RunProgress.model_validate_json(
                (self.run_dir(run_id) / "progress.json").read_text("utf-8")
            )
        except (OSError, ValueError):
            return None

    def load_record(self, run_id: str, test_id: str, attempt: int) -> RunRecord | None:
        """One attempt's record, or ``None`` if it was not (or not yet) written. A record that
        exists but cannot be read raises (``ValueError``/``OSError``): that is not absence."""
        self.check_layout(run_id)
        name = f"{SAFE_ID.sub('_', test_id)}__attempt{attempt}.json"
        path = self.run_dir(run_id) / "records" / name
        try:
            return RunRecord.model_validate_json(path.read_text("utf-8"))
        except FileNotFoundError:
            return None

    def load_records(self, run_id: str) -> list[RunRecord]:
        self.check_layout(run_id)
        folder = self.run_dir(run_id) / "records"
        records = [
            RunRecord.model_validate_json(p.read_text("utf-8")) for p in folder.glob("*.json")
        ]
        return sorted(records, key=lambda r: (r.test_id, r.attempt_index))

    def save_result(self, result: SuiteResult) -> None:
        _atomic_write(self.run_dir(result.run_id) / "result.json", result.model_dump_json(indent=2))

    def _text(self, run_id: str, name: str) -> str | None:
        try:
            return (self.run_dir(run_id) / name).read_text("utf-8")
        except OSError:
            return None

    def save_report(self, run_id: str, html: str) -> None:
        _atomic_write(self.run_dir(run_id) / "report.html", html)

    def load_report(self, run_id: str) -> str | None:
        return self._text(run_id, "report.html")

    def save_junit(self, run_id: str, xml: str) -> None:
        _atomic_write(self.run_dir(run_id) / "junit.xml", xml)

    def load_junit(self, run_id: str) -> str | None:
        return self._text(run_id, "junit.xml")

    def save_rescore(
        self, run_id: str, stamp: str, result: SuiteResult, html: str, xml: str
    ) -> str:
        """Write a re-scored result next to the original run; never over it. Returns the
        re-score's stamp, its name in :meth:`rescores`."""
        folder = self.run_dir(run_id) / f"{RESCORE_PREFIX}{stamp}"
        if folder.exists():
            raise FileExistsError(folder)
        _atomic_write(folder / "report.html", html)
        _atomic_write(folder / "junit.xml", xml)
        _atomic_write(folder / "result.json", result.model_dump_json(indent=2))  # last
        return stamp

    def rescores(self, run_id: str) -> list[str]:
        """The stamps of the run's finished re-scores, oldest first."""
        found = sorted(self.run_dir(run_id).glob(f"{RESCORE_PREFIX}*/result.json"))
        return [p.parent.name[len(RESCORE_PREFIX) :] for p in found]

    def load_rescore(self, run_id: str, stamp: str) -> SuiteResult:
        return load_result_at(self.run_dir(run_id) / f"{RESCORE_PREFIX}{stamp}")

    def rescore_files(self, run_id: str, stamp: str) -> tuple[str | None, str | None]:
        """A re-score's report and JUnit, as written (``None`` when missing)."""
        name = f"{RESCORE_PREFIX}{stamp}"
        return self._text(run_id, f"{name}/report.html"), self._text(run_id, f"{name}/junit.xml")

    def audio(self, run_id: str) -> FileRunAudio:
        return FileRunAudio(self.run_dir(run_id), run_id)

    def audio_marker_text(self, run_id: str) -> str | None:
        """The run's audio prune marker as written, or ``None``."""
        return self._text(run_id, f"audio/{AUDIO_MARKER}")


def parse_snapshot(
    run_id: str, texts: list[tuple[str, str]]
) -> tuple[SutProfile, dict[str, Scenario]]:
    """A run's snapshot from its texts: the profile first, then each scenario, each with the
    name an error names. Raises :class:`ConfigError` when one no longer loads."""
    current = texts[0][0]
    try:
        profile = SutProfile.model_validate_json(texts[0][1])
        scenarios = []
        for name, text in texts[1:]:
            current = name
            scenarios.append(Scenario.model_validate_json(text))
    except ValidationError as exc:
        raise ConfigError(
            f"the scenarios and profile saved with run {run_id!r} no longer load "
            f"({current}: {_first_error(exc)}); pass --catalogue and --profile to "
            "score it against the current files"
        ) from exc
    return profile, {s.id: s for s in scenarios}
