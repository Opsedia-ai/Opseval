# SPDX-License-Identifier: Apache-2.0
"""FileStore: the on-disk layout contract (ADR-0005).

<root>/<run_id>/manifest.json
<root>/<run_id>/profile.json                     (SUT profile as run, for offline re-scoring)
<root>/<run_id>/scenarios/<test_id>.json         (each scenario as run, suite merges applied)
<root>/<run_id>/records/<test_id>__attempt<N>.json
<root>/<run_id>/result.json        (written last: marks completion)
<root>/<run_id>/error.json         (instead of result.json: the run could not be carried out)
<root>/<run_id>/progress.json      (while it runs: the runner's own account of how far it got)
<root>/<run_id>/report.html
<root>/<run_id>/junit.xml
<root>/<run_id>/rescore-<UTC time>/{result.json, report.html, junit.xml}   (opseval rescore)

Records are written once and never modified; re-scoring writes a new folder.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from opseval.models.profile import SutProfile
from opseval.models.progress import RunProgress
from opseval.models.record import RunRecord
from opseval.models.results import SuiteResult
from opseval.models.scenario import Scenario

SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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

    def write_manifest(self, run_id: str, manifest: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "manifest.json"
        _atomic_write(path, json.dumps(manifest, indent=2, ensure_ascii=False, default=str))
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
        path = self.run_dir(run_id) / "scenarios" / f"{SAFE.sub('_', scenario.id)}.json"
        _atomic_write(path, scenario.model_dump_json(indent=2, by_alias=True))
        return path

    def load_snapshot(self, run_id: str) -> tuple[SutProfile, dict[str, Scenario]] | None:
        """The profile and scenarios exactly as run, or ``None`` for runs without one."""
        folder = self.run_dir(run_id)
        profile_path = folder / "profile.json"
        if not profile_path.is_file():
            return None
        profile = SutProfile.model_validate_json(profile_path.read_text("utf-8"))
        scenarios = [
            Scenario.model_validate_json(p.read_text("utf-8"))
            for p in (folder / "scenarios").glob("*.json")
        ]
        return profile, {s.id: s for s in scenarios}

    def load_result(self, run_id: str) -> SuiteResult:
        return SuiteResult.model_validate_json(
            (self.run_dir(run_id) / "result.json").read_text("utf-8")
        )

    def save_record(self, record: RunRecord) -> Path:
        name = f"{SAFE.sub('_', record.test_id)}__attempt{record.attempt_index}.json"
        path = self.run_dir(record.run_id) / "records" / name
        _atomic_write(path, record.model_dump_json(indent=2))
        return path

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
        name = f"{SAFE.sub('_', test_id)}__attempt{attempt}.json"
        path = self.run_dir(run_id) / "records" / name
        try:
            return RunRecord.model_validate_json(path.read_text("utf-8"))
        except FileNotFoundError:
            return None

    def load_records(self, run_id: str) -> list[RunRecord]:
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
