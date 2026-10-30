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
import socket
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


@runtime_checkable
class RunStore(Protocol):
    """What the runner, rescore, calibrate, prune and the console need from storage (ADR-0005).

    :class:`FileStore` (run folders on disk) is the one implementation today. A server-mode
    store (a database plus an object store) implements the same contract, so nothing above
    it changes. Every method names a run by id; a reader refuses a layout it does not know.
    """

    def layout(self, run_id: str) -> str: ...
    def layout_known(self, run_id: str) -> bool: ...
    def check_layout(self, run_id: str) -> None: ...
    def write_manifest(self, run_id: str, manifest: dict[str, Any]) -> Path: ...
    def load_manifest(self, run_id: str) -> dict[str, Any]: ...
    def save_profile(self, run_id: str, profile: SutProfile) -> Path: ...
    def save_scenario(self, run_id: str, scenario: Scenario) -> Path: ...
    def load_snapshot(self, run_id: str) -> tuple[SutProfile, dict[str, Scenario]] | None: ...
    def save_record(self, record: RunRecord) -> Path: ...
    def load_record(self, run_id: str, test_id: str, attempt: int) -> RunRecord | None: ...
    def load_records(self, run_id: str) -> list[RunRecord]: ...
    def save_progress(self, run_id: str, progress: RunProgress) -> Path: ...
    def load_progress(self, run_id: str) -> RunProgress | None: ...
    def request_cancel(self, run_id: str, *, by: str) -> bool: ...
    def cancel_requested(self, run_id: str) -> bool: ...
    def save_error(self, run_id: str, error: dict[str, Any]) -> Path: ...
    def load_error(self, run_id: str) -> dict[str, Any] | None: ...
    def save_result(self, result: SuiteResult) -> Path: ...
    def load_result(self, run_id: str) -> SuiteResult: ...
    def save_report(self, run_id: str, html: str) -> Path: ...
    def save_junit(self, run_id: str, xml: str) -> Path: ...
    def save_rescore(
        self, run_id: str, stamp: str, result: SuiteResult, html: str, xml: str
    ) -> Path: ...


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


class FileStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def run_dir(self, run_id: str) -> Path:
        name = SAFE.sub("_", run_id)
        if name.strip(".") == "":
            # "", "." and ".." would name the reports folder itself or its parent.
            raise ValueError(f"not a run id: {run_id!r}")
        return self.root / name

    def save_error(self, run_id: str, error: dict[str, Any]) -> Path:
        """Record that a run could not be carried out (a harness or configuration error).

        It takes the place of ``result.json``, so a run that failed to start or crashed is
        a visible harness error rather than a run that is "running" forever or never
        appears at all.
        """
        path = self.run_dir(run_id) / "error.json"
        _atomic_write(path, json.dumps(error, indent=2, ensure_ascii=False, default=str))
        return path

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

    def write_manifest(self, run_id: str, manifest: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "manifest.json"
        body = {"layout": LAYOUT, **manifest}
        _atomic_write(path, json.dumps(body, indent=2, ensure_ascii=False, default=str))
        return path

    def load_manifest(self, run_id: str) -> dict[str, Any]:
        """The run's manifest, or ``{}`` if it is missing or unreadable.

        Re-scoring an older run must not fail just because its manifest is absent.
        """
        try:
            data = json.loads((self.run_dir(run_id) / "manifest.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_profile(self, run_id: str, profile: SutProfile) -> Path:
        path = self.run_dir(run_id) / "profile.json"
        _atomic_write(path, profile.model_dump_json(indent=2))
        return path

    def save_scenario(self, run_id: str, scenario: Scenario) -> Path:
        path = self.run_dir(run_id) / "scenarios" / f"{SAFE_ID.sub('_', scenario.id)}.json"
        _atomic_write(path, scenario.model_dump_json(indent=2, by_alias=True))
        return path

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
            profile = SutProfile.model_validate_json(profile_path.read_text("utf-8"))
            scenarios = []
            for current in sorted((folder / "scenarios").glob("*.json")):
                scenarios.append(Scenario.model_validate_json(current.read_text("utf-8")))
        except (OSError, ValidationError) as exc:
            raise ConfigError(
                f"the scenarios and profile saved with run {run_id!r} no longer load "
                f"({current.name}: {_first_error(exc)}); pass --catalogue and --profile to "
                "score it against the current files"
            ) from exc
        return profile, {s.id: s for s in scenarios}

    def load_result(self, run_id: str) -> SuiteResult:
        return load_result_at(self.run_dir(run_id))

    def finished_results(self) -> list[SuiteResult]:
        """Every finished run this version can read, newest first. A run without
        ``result.json`` is still in flight; a re-score lives inside its run's folder and is
        not a run of its own."""
        out: list[SuiteResult] = []
        for folder in self.root.glob("*/result.json"):
            if not self.layout_known(folder.parent.name):
                continue  # listed by the console as an error row: shown, never misread
            try:
                out.append(self.load_result(folder.parent.name))
            except (ValueError, OSError):
                continue  # a half-written or foreign folder is not a run
        return sorted(out, key=lambda r: r.started_at, reverse=True)

    def save_record(self, record: RunRecord) -> Path:
        name = f"{SAFE_ID.sub('_', record.test_id)}__attempt{record.attempt_index}.json"
        path = self.run_dir(record.run_id) / "records" / name
        _atomic_write(path, record.model_dump_json(indent=2))
        return path

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

    def save_progress(self, run_id: str, progress: RunProgress) -> Path:
        path = self.run_dir(run_id) / "progress.json"
        _atomic_write(path, progress.model_dump_json(indent=2))
        return path

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

    def save_result(self, result: SuiteResult) -> Path:
        path = self.run_dir(result.run_id) / "result.json"
        _atomic_write(path, result.model_dump_json(indent=2))
        return path

    def save_report(self, run_id: str, html: str) -> Path:
        path = self.run_dir(run_id) / "report.html"
        _atomic_write(path, html)
        return path

    def save_rescore(
        self, run_id: str, stamp: str, result: SuiteResult, html: str, xml: str
    ) -> Path:
        """Write a re-scored result next to the original run; never over it."""
        folder = self.run_dir(run_id) / f"rescore-{stamp}"
        if folder.exists():
            raise FileExistsError(folder)
        _atomic_write(folder / "report.html", html)
        _atomic_write(folder / "junit.xml", xml)
        _atomic_write(folder / "result.json", result.model_dump_json(indent=2))  # last
        return folder

    def save_junit(self, run_id: str, xml: str) -> Path:
        path = self.run_dir(run_id) / "junit.xml"
        _atomic_write(path, xml)
        return path
