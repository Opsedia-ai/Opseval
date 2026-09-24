# SPDX-License-Identifier: Apache-2.0
"""FileStore: the on-disk layout contract (ADR-0005).

<root>/<run_id>/manifest.json
<root>/<run_id>/records/<test_id>__attempt<N>.json
<root>/<run_id>/result.json        (written last: marks completion)
<root>/<run_id>/report.html
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from opseval.models.record import RunRecord
from opseval.models.results import SuiteResult

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
        return self.root / SAFE.sub("_", run_id)

    def write_manifest(self, run_id: str, manifest: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "manifest.json"
        _atomic_write(path, json.dumps(manifest, indent=2, ensure_ascii=False, default=str))
        return path

    def save_record(self, record: RunRecord) -> Path:
        name = f"{SAFE.sub('_', record.test_id)}__attempt{record.attempt_index}.json"
        path = self.run_dir(record.run_id) / "records" / name
        _atomic_write(path, record.model_dump_json(indent=2))
        return path

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
