# SPDX-License-Identifier: Apache-2.0
"""The runner's progress log: it says what it is doing, so nothing has to guess."""

from __future__ import annotations

import threading

from opseval.models.progress import RunProgress, ScenarioProgress
from opseval.models.results import TestVerdict, VerdictStatus
from opseval.storage import RunStore

SKIPS = (VerdictStatus.SKIPPED_PRECONDITION, VerdictStatus.BLOCKED)


class ProgressLog:
    """Thread-safe: the runner calls it from worker threads (storage is off the event loop),
    and with lanes several at once; each change and its write happen under one lock."""

    def __init__(self, store: RunStore, run_id: str, planned: list[str]) -> None:
        self._lock = threading.RLock()
        self._store, self._run_id = store, run_id
        self._rows = {sid: ScenarioProgress(id=sid) for sid in planned}
        self._order = list(planned)
        self._stage: str = "running"
        self._running: list[str] = []  # in the order they started; lanes run several
        self._write()

    def started(self, sid: str, repeats: int) -> None:
        with self._lock:
            self._update(sid, state="running", repeats=repeats)
            self._running.append(sid)
            self._write()

    def attempt(self, sid: str) -> None:
        with self._lock:
            row = self._rows[sid]
            self._update(sid, attempts=row.attempts + 1)
            self._write()

    def ended(self, verdict: TestVerdict) -> None:
        """A scenario has its verdict: scored and folded, or skipped."""
        with self._lock:
            state = "skipped" if verdict.status in SKIPS else "done"
            self._update(verdict.test_id, state=state)
            if verdict.test_id in self._running:
                self._running.remove(verdict.test_id)
            self._write()

    def finishing(self) -> None:
        with self._lock:
            self._stage = "finishing"
            self._running = []
            self._write()

    def _update(self, sid: str, **changes: object) -> None:
        self._rows[sid] = self._rows[sid].model_copy(update=changes)

    def _write(self) -> None:
        self._store.save_progress(
            self._run_id,
            RunProgress.model_validate(
                {
                    "stage": self._stage,
                    # The most recently started of those still running. With lanes, every
                    # running scenario is also marked so in its own row.
                    "current": self._running[-1] if self._running else None,
                    "scenarios": [self._rows[sid].model_dump() for sid in self._order],
                }
            ),
        )
