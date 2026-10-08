# SPDX-License-Identifier: Apache-2.0
"""The runner's progress log: it says what it is doing, so nothing has to guess."""

from __future__ import annotations

from opseval.models.progress import RunProgress, ScenarioProgress
from opseval.models.results import TestVerdict, VerdictStatus
from opseval.storage import FileStore

SKIPS = (VerdictStatus.SKIPPED_PRECONDITION, VerdictStatus.BLOCKED)


class ProgressLog:
    def __init__(self, store: FileStore, run_id: str, planned: list[str]) -> None:
        self._store, self._run_id = store, run_id
        self._rows = {sid: ScenarioProgress(id=sid) for sid in planned}
        self._order = list(planned)
        self._stage: str = "running"
        self._current: str | None = None
        self._write()

    def started(self, sid: str, repeats: int) -> None:
        self._update(sid, state="running", repeats=repeats)
        self._current = sid
        self._write()

    def attempt(self, sid: str) -> None:
        row = self._rows[sid]
        self._update(sid, attempts=row.attempts + 1)
        self._write()

    def ended(self, verdict: TestVerdict) -> None:
        """A scenario has its verdict: scored and folded, or skipped."""
        state = "skipped" if verdict.status in SKIPS else "done"
        self._update(verdict.test_id, state=state)
        if self._current == verdict.test_id:
            self._current = None
        self._write()

    def finishing(self) -> None:
        self._stage, self._current = "finishing", None
        self._write()

    def _update(self, sid: str, **changes: object) -> None:
        self._rows[sid] = self._rows[sid].model_copy(update=changes)

    def _write(self) -> None:
        self._store.save_progress(
            self._run_id,
            RunProgress.model_validate(
                {
                    "stage": self._stage,
                    "current": self._current,
                    "scenarios": [self._rows[sid].model_dump() for sid in self._order],
                }
            ),
        )
