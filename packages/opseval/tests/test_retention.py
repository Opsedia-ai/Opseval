# SPDX-License-Identifier: Apache-2.0
"""Retention (FR-SAF-06, ADR-0037): the one feature that deletes data.

It must delete old finished runs and nothing else: never a run that may still be going,
never something that is not a run folder, never through a link, and never without --yes.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opseval.cli import app
from opseval.config import Settings
from opseval.retention import apply, plan
from opseval.storage import FileStore

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
HERE = socket.gethostname()


def _dead_pid() -> int:
    pid = 999_999
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except OSError:
            pass
        pid += 1


def make_run(
    root: Path,
    run_id: str,
    *,
    days_old: float | None,
    ends: str | None = "result",
    pid: int | None = None,
    host: str = HERE,
) -> Path:
    folder = root / run_id
    (folder / "records").mkdir(parents=True)
    manifest: dict[str, object] = {"run_id": run_id, "suite": "s", "host": host}
    if days_old is not None:
        manifest["started_at"] = (NOW - timedelta(days=days_old)).isoformat()
    if pid is not None:
        manifest["pid"] = pid
    (folder / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    (folder / "records" / "t__attempt0.json").write_text("{}", "utf-8")
    if ends == "result":
        (folder / "result.json").write_text("{}", "utf-8")
    elif ends == "error":
        (folder / "error.json").write_text("{}", "utf-8")
    return folder


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    root = tmp_path / "reports"
    make_run(root, "old-finished", days_old=120)
    make_run(root, "old-error", days_old=100, ends="error")
    make_run(root, "old-interrupted", days_old=95, ends=None, pid=_dead_pid())
    make_run(root, "new-finished", days_old=10)
    make_run(root, "old-running-here", days_old=200, ends=None, pid=os.getpid())
    make_run(root, "old-running-elsewhere", days_old=200, ends=None, pid=1, host="other-box")
    make_run(root, "no-start", days_old=None)
    (root / "notes").mkdir()  # not a run folder: no manifest
    (root / "notes" / "keep.txt").write_text("mine", "utf-8")
    (root / "README.txt").write_text("mine", "utf-8")
    return root


def actions(root: Path, days: int = 90) -> dict[str, str]:
    return {i.run_id: i.action for i in plan(FileStore(root), older_than_days=days, now=NOW)}


class TestThePlan:
    def test_only_old_runs_that_have_ended_are_deleted(self, reports: Path) -> None:
        assert actions(reports) == {
            "old-finished": "delete",
            "old-error": "delete",
            "old-interrupted": "delete",
            "new-finished": "keep",
            "old-running-here": "keep",
            "old-running-elsewhere": "keep",
            "no-start": "keep",
        }

    def test_every_keep_says_why(self, reports: Path) -> None:
        why = {i.run_id: i.reason for i in plan(FileStore(reports), older_than_days=90, now=NOW)}
        assert "may still be running" in why["old-running-here"]
        assert "may still be running" in why["old-running-elsewhere"], "never guessed dead"
        assert "start time cannot be read" in why["no-start"]
        assert "newer than 90 days" in why["new-finished"]

    def test_the_cutoff_is_the_number_of_days_asked_for(self, reports: Path) -> None:
        assert actions(reports, days=5)["new-finished"] == "delete"
        assert actions(reports, days=150)["old-finished"] == "keep"

    def test_a_plan_deletes_nothing(self, reports: Path) -> None:
        before = sorted(p.name for p in reports.iterdir())
        plan(FileStore(reports), older_than_days=1, now=NOW)
        assert sorted(p.name for p in reports.iterdir()) == before

    def test_a_missing_reports_folder_is_an_empty_plan(self, tmp_path: Path) -> None:
        assert plan(FileStore(tmp_path / "nope"), older_than_days=90) == []

    def test_less_than_a_day_is_refused(self, reports: Path) -> None:
        with pytest.raises(ValueError):
            plan(FileStore(reports), older_than_days=0)


class TestDeleting:
    def test_it_deletes_exactly_what_the_plan_marked(self, reports: Path) -> None:
        store = FileStore(reports)
        outcome = apply(store, plan(store, older_than_days=90, now=NOW))
        assert sorted(outcome.deleted) == ["old-error", "old-finished", "old-interrupted"]
        assert outcome.failed == []
        left = sorted(p.name for p in reports.iterdir())
        assert left == [
            "README.txt",
            "new-finished",
            "no-start",
            "notes",
            "old-running-elsewhere",
            "old-running-here",
        ]
        assert (reports / "notes" / "keep.txt").read_text("utf-8") == "mine"

    def test_never_through_a_link(self, reports: Path, tmp_path: Path) -> None:
        """A link inside the reports folder is not ours to follow, even to an old run."""
        outside = tmp_path / "elsewhere"
        target = make_run(outside, "old-linked", days_old=300)
        (reports / "old-linked").symlink_to(target, target_is_directory=True)
        store = FileStore(reports)
        assert "old-linked" not in actions(reports)
        apply(store, plan(store, older_than_days=90, now=NOW))
        assert (target / "result.json").is_file()

    def test_a_folder_that_changed_since_the_plan_is_left(self, reports: Path) -> None:
        """Checked again just before deleting: a plan is never trusted blindly."""
        store = FileStore(reports)
        items = plan(store, older_than_days=90, now=NOW)
        (reports / "old-finished" / "result.json").unlink()  # say, a re-run started writing
        outcome = apply(store, items)
        assert "old-finished" not in outcome.deleted and (reports / "old-finished").is_dir()

    def test_one_folder_that_cannot_go_does_not_stop_the_rest(
        self, reports: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file another user wrote (a container running as root): report it, carry on,
        and never leave the run looking as if it were still going."""
        real = shutil.rmtree

        def stubborn(path: object, *args: object, **kwargs: object) -> None:
            if "old-error" in str(path):
                raise PermissionError(13, "Permission denied", str(path))
            real(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("opseval.storage.shutil.rmtree", stubborn)
        store = FileStore(reports)
        outcome = apply(store, plan(store, older_than_days=90, now=NOW))
        assert sorted(outcome.deleted) == ["old-finished", "old-interrupted"]
        assert [run for run, _ in outcome.failed] == ["old-error"]
        assert "Permission denied" in outcome.failed[0][1]
        left = reports / "old-error"
        assert (left / "manifest.json").is_file() and (left / "error.json").is_file(), (
            "stopped before its markers: it still reads as a run that ended"
        )


class TestCommand:
    runner = CliRunner()

    def test_without_yes_it_only_shows(self, reports: Path) -> None:
        res = self.runner.invoke(app, ["prune", "--reports-dir", str(reports), "--older-than", "1"])
        assert res.exit_code == 0, res.output
        assert "would be deleted" in res.output and "--yes" in res.output
        assert (reports / "old-finished").is_dir()

    def test_with_yes_it_deletes(self, reports: Path) -> None:
        res = self.runner.invoke(
            app, ["prune", "--reports-dir", str(reports), "--older-than", "1", "--yes"]
        )
        assert res.exit_code == 0, res.output
        assert "Deleted" in res.output
        assert not (reports / "old-finished").exists()
        assert (reports / "old-running-here").is_dir()

    def test_a_failure_is_listed_and_exits_3(
        self, reports: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def denied(path: object, *args: object, **kwargs: object) -> None:
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr("opseval.storage.shutil.rmtree", denied)
        res = self.runner.invoke(
            app, ["prune", "--reports-dir", str(reports), "--older-than", "1", "--yes"]
        )
        assert res.exit_code == 3
        assert "could not delete" in res.output and "remove them by hand" in res.output

    def test_nothing_old_enough_says_so(self, tmp_path: Path) -> None:
        root = tmp_path / "r"
        make_run(root, "fresh", days_old=0)
        res = self.runner.invoke(app, ["prune", "--reports-dir", str(root), "--yes"])
        assert res.exit_code == 0 and "Nothing to prune" in res.output
        assert (root / "fresh").is_dir()

    def test_the_default_is_ninety_days(self) -> None:
        assert Settings().retention_days == 90
