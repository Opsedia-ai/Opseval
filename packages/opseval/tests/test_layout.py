# SPDX-License-Identifier: Apache-2.0
"""The run-folder layout is versioned (FR-REC-05, ADR-0005 amendment).

A folder from before versioning reads as layout 1. A folder in a layout this version does not
know is never read with rules that may not apply to it, and never silently dropped: every
reader says it was written by a newer Opseval, and prune leaves it alone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opseval.cli import app
from opseval.config import Settings
from opseval.demo.refund_agent import run_agent
from opseval.rescore import rescore_run
from opseval.retention import apply, plan
from opseval.runner import RunOptions, launch
from opseval.storage import LAYOUT, LEGACY_LAYOUT, PRE_USAGE_LAYOUTS, FileStore, UnknownLayout

FUTURE = "opseval-runs/9"


@pytest.fixture
async def run(cat_dir: Path, tmp_path: Path) -> Path:
    settings = Settings(reports_dir=str(tmp_path / "reports"), allowed_hosts="127.0.0.1")
    async with run_agent() as url:
        result = await launch(
            cat_dir / "suites" / "smoke.yaml",
            RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1),
            settings,
        )
    return Path(settings.reports_dir) / result.run_id


def relayout(run: Path, layout: str | None, *, started: datetime | None = None) -> None:
    manifest = json.loads((run / "manifest.json").read_text("utf-8"))
    if layout is None:
        manifest.pop("layout", None)
    else:
        manifest["layout"] = layout
    if started is not None:
        manifest["started_at"] = started.isoformat()
    (run / "manifest.json").write_text(json.dumps(manifest), "utf-8")


def test_a_new_run_records_its_layout(run: Path) -> None:
    assert json.loads((run / "manifest.json").read_text("utf-8"))["layout"] == LAYOUT
    assert LAYOUT == "opseval-runs/6", (
        "6: records carry usage events; usage rows reasoning/cached tokens (ADR-0066)"
    )


@pytest.mark.parametrize(
    "older", ["opseval-runs/2", "opseval-runs/3", "opseval-runs/4", "opseval-runs/5"]
)
def test_an_older_layout_run_is_still_read(run: Path, older: str) -> None:
    relayout(run, older)
    store = FileStore(run.parent)
    assert store.layout_known(run.name)
    assert store.load_result(run.name).run_id == run.name


def test_a_layout_2_run_is_still_read(run: Path) -> None:
    relayout(run, "opseval-runs/2")
    store = FileStore(run.parent)
    assert store.layout_known(run.name)
    assert store.load_result(run.name).run_id == run.name
    assert all(r.voice is None for r in store.load_records(run.name))


def test_a_run_from_before_versioning_reads_as_layout_1(run: Path) -> None:
    relayout(run, None)
    store = FileStore(run.parent)
    assert store.layout(run.name) == LEGACY_LAYOUT == "opseval-runs/1"
    assert store.layout_known(run.name), "layout 1 is still read"
    assert store.load_result(run.name).run_id == run.name


def test_every_reader_refuses_a_newer_layout(run: Path) -> None:
    relayout(run, FUTURE)
    store = FileStore(run.parent)
    for read in (store.load_result, store.load_records, store.load_snapshot):
        with pytest.raises(UnknownLayout, match="upgrade Opseval"):
            read(run.name)


async def test_rescore_and_summary_say_upgrade(run: Path, tmp_path: Path) -> None:
    relayout(run, FUTURE)
    with pytest.raises(UnknownLayout):
        await rescore_run(run, Settings(reports_dir=str(tmp_path / "reports")))
    res = CliRunner().invoke(app, ["summary", str(run)])
    assert res.exit_code == 3 and "upgrade Opseval" in res.output


def test_prune_never_deletes_a_layout_it_cannot_read(run: Path) -> None:
    relayout(run, FUTURE, started=datetime(2020, 1, 1, tzinfo=UTC))
    store = FileStore(run.parent)
    items = plan(store, older_than_days=1)
    assert [(i.action, "newer Opseval" in i.reason) for i in items] == [("keep", True)]
    assert apply(store, items).deleted == [] and run.is_dir()


def test_the_console_shows_it_as_an_error_never_a_gap(run: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from opseval.server import create_app

    relayout(run, FUTURE)
    api = TestClient(
        create_app(settings=Settings(reports_dir=str(run.parent)), token="t"),
        base_url="http://127.0.0.1:8000",
        headers={"Authorization": "Bearer t"},
    )
    rows = {r["id"]: r for r in api.get("/api/v1/runs").json()}
    assert rows[run.name]["state"] == "harness_error"
    assert "UNKNOWN_LAYOUT" in rows[run.name]["error"]
    detail = api.get(f"/api/v1/runs/{run.name}")
    assert detail.status_code == 200 and detail.json()["summary"]["gate"] == "harness_error"
    attempt = api.get(f"/api/v1/runs/{run.name}/attempts/rf-001/0")
    assert attempt.status_code == 409 and attempt.json()["code"] == "UNKNOWN_LAYOUT"
    assert api.get(f"/api/v1/compare?baseline={run.name}&candidate={run.name}").status_code == 404
    assert run.name not in [p["run"] for p in api.get("/api/v1/trend").json()]


def test_the_file_store_meets_the_storage_contract(tmp_path: Path) -> None:
    """ADR-0005: a server-mode store implements the same protocol, so nothing above changes."""
    from opseval.storage import RunStore

    assert isinstance(FileStore(tmp_path), RunStore)
    store: RunStore = FileStore(tmp_path)  # and mypy checks every signature matches
    assert store.layout("nothing-here") == LEGACY_LAYOUT


@pytest.mark.parametrize("name", ["run 1", "rün"])
def test_summary_reads_the_folder_it_is_given(run: Path, name: str) -> None:
    """Any folder name, and `.` from inside it (review of #54): the path is read as given."""
    import os

    renamed = run.rename(run.parent / name)
    assert CliRunner().invoke(app, ["summary", str(renamed)]).exit_code == 0
    here = os.getcwd()
    os.chdir(renamed)
    try:
        assert CliRunner().invoke(app, ["summary", "."]).exit_code == 0
    finally:
        os.chdir(here)


def test_pre_usage_layouts_is_closed_and_never_holds_the_current_layout() -> None:
    """Runs written before per-call usage (ADR-0066) are layouts 1-5, for ever: when LAYOUT
    moves on, a layout-6 run must not be read as one without usage events."""
    assert LAYOUT not in PRE_USAGE_LAYOUTS
    assert frozenset(f"opseval-runs/{n}" for n in range(1, 6)) == PRE_USAGE_LAYOUTS
