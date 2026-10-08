# SPDX-License-Identifier: Apache-2.0
"""What happened to a run the API was asked to start.

``POST /runs`` answers 202 before the run exists, so the failures between "accepted" and
"a folder on disk" have nowhere else to land:

- ``launch()`` raised before writing a manifest — no run id was ever assigned, so the run
  is invisible on disk and the caller was told it started;
- it raised after the manifest — a folder with no ``result.json``, which otherwise reads
  as "still running" for ever.

Both are harness failures, and a harness failure that looks like work in progress is the
thing this project refuses to ship. This is in-memory: a restart loses it, which is
honest for a single-process local server and is why a failure is also surfaced the moment
it happens rather than only being recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from opseval.server import schemas as s

MAX_REMEMBERED = 50


@dataclass
class Launch:
    token: str
    suite: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    run_id: str | None = None
    """Set from ``RunOptions.on_start`` the moment the manifest exists."""
    error: str | None = None


class LaunchBook:
    """Launches this process started, newest last. Bounded; only the unfinished and the
    failed are worth keeping, since a run that got as far as ``result.json`` describes
    itself."""

    def __init__(self) -> None:
        self._launches: dict[str, Launch] = {}

    def start(self, suite: str) -> Launch:
        from uuid import uuid4

        launch = Launch(token=uuid4().hex[:8], suite=suite)
        self._launches[launch.token] = launch
        self._prune()
        return launch

    def attach(self, token: str, run_id: str) -> None:
        if (launch := self._launches.get(token)) is not None:
            launch.run_id = run_id

    def failed(self, token: str, reason: str) -> None:
        if (launch := self._launches.get(token)) is not None:
            launch.error = reason

    def finished(self, token: str) -> None:
        """A run that reached ``result.json`` needs no entry: the folder is the record."""
        self._launches.pop(token, None)

    def failures(self) -> dict[str, str]:
        """run id → why it died, for launches that got far enough to have one."""
        return {
            launch.run_id: launch.error
            for launch in self._launches.values()
            if launch.run_id and launch.error
        }

    def never_started(self) -> list[s.RunSummary]:
        """Failures with no run id. Nothing on disk represents these, so the API has to."""
        return [
            s.RunSummary(
                id=f"launch-{launch.token}",
                suite=launch.suite,
                started_at=launch.started_at.isoformat(),
                state="error",
                gate="harness_error",
                passed=0,
                failed=0,
                unjudged=0,
                flaky=0,
                reason=launch.error,
            )
            for launch in sorted(self._launches.values(), key=lambda x: x.started_at, reverse=True)
            if launch.error and not launch.run_id
        ]

    def _prune(self) -> None:
        """Drop old *settled* launches only.

        Evicting by age alone would discard a long-running launch that has not failed
        yet, and its later crash would then have nowhere to land — back to a folder
        reading "running" for ever, which is the hole this class exists to close.
        """
        if len(self._launches) <= MAX_REMEMBERED:
            return
        settled = sorted(
            (x for x in self._launches.values() if x.error), key=lambda x: x.started_at
        )
        for launch in settled[: len(self._launches) - MAX_REMEMBERED]:
            self._launches.pop(launch.token, None)
