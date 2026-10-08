# SPDX-License-Identifier: Apache-2.0
"""The console API reads real run folders, never invents numbers, and only answers the
operator (ADR-0029, ADR-0031)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from opseval.config import Settings
from opseval.demo import catalogue_dir
from opseval.models.record import TerminalState
from opseval.models.results import (
    AttemptVerdict,
    Axis,
    Gate,
    ModelUsage,
    ScoreResult,
    ScoreStatus,
    SuiteResult,
    TestVerdict,
    VerdictStatus,
)
from opseval.models.scenario import Criticality
from opseval.server import create_app
from opseval.server.app import host_name
from opseval.storage import FileStore

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = "http://127.0.0.1:8000"


def _result(
    run_id: str,
    minute: int,
    statuses: dict[str, VerdictStatus],
    exit_code: int = 0,
    gate: str = "pass",
    attempts: list[AttemptVerdict] | None = None,
    blocking: list[str] | None = None,
) -> SuiteResult:
    when = datetime(2026, 9, 1, 10, minute, tzinfo=UTC)
    return SuiteResult(
        opseval_version="0",
        run_id=run_id,
        suite="smoke",
        target="ws://x",
        profile="refund-agent",
        started_at=when,
        ended_at=when,
        tests=[
            TestVerdict(
                test_id=tid,
                name=tid,
                criticality=Criticality.NORMAL,
                status=st,
                attempts=attempts or [],
            )
            for tid, st in statuses.items()
        ],
        gate=Gate(status=gate, summary="ok", blocking=blocking or []),
        exit_code=exit_code,
        usage=[ModelUsage(role="simulator", model="m", input_tokens=5, output_tokens=5)],
    )


def connect(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=BASE, headers=AUTH)


def app_for(tmp_path: Path, *, copy: bool = False, **kw: Any) -> tuple[FastAPI, Path]:
    cat = catalogue_dir()
    if copy:
        cat = tmp_path / "cat"
        shutil.copytree(catalogue_dir(), cat)
    settings = kw.pop("settings", None) or Settings(reports_dir=str(tmp_path / "r"))
    app = create_app(
        settings=settings, catalogue=cat, profile=cat / "profile.yaml", token=TOKEN, **kw
    )
    return app, cat


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    store = FileStore(tmp_path)
    store.save_result(_result("r1", 1, {"a": VerdictStatus.PASSED, "b": VerdictStatus.PASSED}))
    store.save_result(_result("r2", 2, {"a": VerdictStatus.FAILED, "b": VerdictStatus.PASSED}, 1))
    store.save_result(
        _result("r3", 3, {"a": VerdictStatus.INCONCLUSIVE, "b": VerdictStatus.PASSED}, 2)
    )
    store.save_result(
        _result(
            "r4", 4, {"a": VerdictStatus.FAILED}, 1, gate="block", blocking=["a: failed (critical)"]
        )
    )
    store.save_result(_result("r5", 5, {"a": VerdictStatus.INCONCLUSIVE}, 3, gate="block"))
    cat = catalogue_dir()
    settings = Settings(reports_dir=str(tmp_path))
    return connect(
        create_app(settings=settings, catalogue=cat, profile=cat / "profile.yaml", token=TOKEN)
    )


# --- reading ---------------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok" and body["profile"] == "refund-agent"
    assert body["readOnly"] is False and body["allowCommit"] is False


def test_runs_newest_first(client: TestClient) -> None:
    runs = client.get("/api/v1/runs").json()
    assert [r["id"] for r in runs] == ["r5", "r4", "r3", "r2", "r1"]
    assert runs[2]["unjudged"] == 1 and runs[3]["failed"] == 1


def test_gate_state_never_draws_a_failure_green_or_a_harness_error_as_a_failure(
    client: TestClient,
) -> None:
    gates = {r["id"]: r["gate"] for r in client.get("/api/v1/runs").json()}
    assert gates == {
        "r1": "safe",  # everything passed
        "r2": "gaps",  # a normal failure under a passing gate: not green
        "r3": "gaps",  # something not judged under a passing gate
        "r4": "blocked",  # the gate blocked on an agent failure
        "r5": "harness_error",  # exit 3: Opseval could not do the job
    }


def test_run_detail_and_404(client: TestClient) -> None:
    detail = client.get("/api/v1/runs/r2").json()
    assert detail["summary"]["id"] == "r2" and detail["safeToRelease"] is False
    assert client.get("/api/v1/runs/r1").json()["safeToRelease"] is True
    assert client.get("/api/v1/runs/nope").status_code == 404


def test_run_ids_cannot_climb_out_of_the_reports_folder(client: TestClient) -> None:
    from fastapi import HTTPException

    from opseval.server.app import _run_id

    # Bare dot segments are normalised away by any HTTP client; the validator still holds.
    for bad in ("..", ".", "../x", "a..b", ""):
        with pytest.raises(HTTPException):
            _run_id(bad)
    for bad in ("..%2F..", "a..b"):
        assert client.get(f"/api/v1/runs/{bad}").status_code == 404
    r = client.get("/api/v1/compare", params={"baseline": "..", "candidate": "r1"})
    assert r.status_code == 404


def test_detail_names_unmeasured_vacuous_and_degraded(tmp_path: Path) -> None:
    unmeasured = ScoreResult(
        scorer_id="security.x",
        axis=Axis.SECURITY,
        status=ScoreStatus.INFRA_ERROR,
        score=None,
        threshold=1.0,
        reason="JUDGE_DISAGREEMENT: split",
    )
    vacuous = ScoreResult(
        scorer_id="tools.forbidden_not_called",
        axis=Axis.TOOLS,
        status=ScoreStatus.PASSED,
        score=1.0,
        threshold=1.0,
        degraded=True,
        details={"vacuous": True},
    )
    attempt = AttemptVerdict(
        attempt_index=0,
        passed=True,
        terminal_state=TerminalState.GOAL_REACHED,
        scores=[unmeasured, vacuous],
        record_path="a0.json",
    )
    FileStore(tmp_path).save_result(
        _result("r1", 1, {"a": VerdictStatus.PASSED}, attempts=[attempt])
    )
    c = connect(create_app(settings=Settings(reports_dir=str(tmp_path)), token=TOKEN))
    d = c.get("/api/v1/runs/r1").json()
    assert d["unmeasuredScorers"] == ["security.x"]
    assert d["vacuousScorers"] == ["tools.forbidden_not_called"]
    assert d["degradedScorers"] == ["tools.forbidden_not_called"]
    assert d["safeToRelease"] is False and d["summary"]["gate"] == "gaps"
    assert d["tests"][0]["unmeasuredScorers"] == ["security.x"]
    assert d["tests"][0]["attempts"][0]["scores"][1]["vacuous"] is True


def test_compare_separates_regressions_from_outages(client: TestClient) -> None:
    reg = client.get("/api/v1/compare", params={"baseline": "r1", "candidate": "r2"}).json()
    assert [c["testId"] for c in reg["regressed"]] == ["a"] and not reg["unjudged"]
    out = client.get("/api/v1/compare", params={"baseline": "r1", "candidate": "r3"}).json()
    assert not out["regressed"] and [c["testId"] for c in out["unjudged"]] == ["a"]
    assert (
        client.get("/api/v1/compare", params={"baseline": "r1", "candidate": "x"}).status_code
        == 404
    )


def test_unjudged_run_is_a_gap_in_the_trend_not_zero(client: TestClient) -> None:
    points = {p["run"]: p["passRate"] for p in client.get("/api/v1/trend").json()}
    assert points["r3"] is None and points["r1"] == 1.0 and points["r2"] == 0.5
    assert points["r5"] is None  # a harness error measured nothing


def test_usage_unmeasured_is_null(client: TestClient) -> None:
    rows = {r["run"]: r for r in client.get("/api/v1/usage").json()}
    assert rows["r1"]["simulatorTokens"] == 10 and rows["r1"]["judgeTokens"] is None
    assert rows["r1"]["costUsd"] is None


def test_catalogue_endpoints(client: TestClient) -> None:
    scenarios = client.get("/api/v1/scenarios").json()
    assert scenarios["scenarios"] and not scenarios["problems"]
    assert {s["file"] for s in client.get("/api/v1/suites").json()} >= {"smoke", "full"}
    checks = client.get("/api/v1/checks").json()
    assert any(c["kind"] == "judge" for c in checks) and any(
        c["kind"] == "deterministic" for c in checks
    )
    assert all(c["area"] != "" for c in checks)


def test_config_never_leaks_a_secret(tmp_path: Path) -> None:
    from pydantic import SecretStr

    settings = Settings(
        reports_dir=str(tmp_path),
        judge_api_key=SecretStr("sk-very-secret"),
        serve_token=SecretStr(TOKEN),
    )
    rows = connect(create_app(settings=settings)).get("/api/v1/config")
    assert "sk-very-secret" not in rows.text and TOKEN not in rows.text
    assert any(r["key"] == "opseval_judge_api_key" and r["redacted"] for r in rows.json())


def test_no_catalogue_is_empty_not_an_error(tmp_path: Path) -> None:
    c = connect(create_app(settings=Settings(reports_dir=str(tmp_path)), token=TOKEN))
    assert c.get("/api/v1/scenarios").json() == {"scenarios": [], "problems": []}
    assert c.get("/api/v1/suites").json() == []


def test_one_broken_suite_file_is_a_row_not_a_500(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    (cat / "suites" / "broken.yaml").write_text("not: [valid", "utf-8")
    (cat / "suites" / "list.yaml").write_text("- a\n- b\n", "utf-8")
    (cat / "suites" / ".in-progress.yaml").write_text("name: hidden\n", "utf-8")
    rows = {s["file"]: s for s in connect(app).get("/api/v1/suites").json()}
    assert rows["broken"]["problem"] and rows["list"]["problem"]
    assert rows["smoke"]["problem"] is None
    assert ".in-progress" not in rows


def test_rehearse_scripted_persona_needs_no_model(client: TestClient) -> None:
    first = client.post("/api/v1/simulator/rehearse", json={"scenarioId": "rf-001"}).json()
    assert first["turn"]["who"] == "customer" and first["model"] is None
    assert first["turn"]["text"]


def test_rehearse_llm_persona_without_model_is_503_not_a_failure(client: TestClient) -> None:
    llm = next(
        s
        for s in client.get("/api/v1/scenarios").json()["scenarios"]
        if s["id"] not in ("rf-001", "rf-002", "rf-003")
    )
    r = client.post("/api/v1/simulator/rehearse", json={"scenarioId": llm["id"]})
    assert r.status_code == 503 and r.json()["code"] == "SIMULATOR_NOT_CONFIGURED"


def test_unknown_scenario_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/scenarios/nope/persona").status_code == 404
    assert client.get("/api/v1/scenarios/rf-001/persona").json()["scripted"] is True


def test_readiness_says_what_is_limited(client: TestClient) -> None:
    items = {i["id"]: i for i in client.get("/api/v1/readiness").json()}
    assert items["catalogue"]["ready"] is True
    assert items["judge"]["ready"] is False and "OPSEVAL_JUDGE" in items["judge"]["detail"]
    assert set(items) == {"catalogue", "simulator", "judge", "allowlist", "tracing"}


# --- who may ask (ADR-0031) -------------------------------------------------------------


def test_every_api_request_needs_the_token(client: TestClient) -> None:
    anon = TestClient(client.app, base_url=BASE)
    for path in ("/api/v1/health", "/api/v1/runs"):
        r = anon.get(path)
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"
    wrong = anon.get("/api/v1/runs", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    started = anon.post("/api/v1/runs", json={"suite": "smoke", "target": "ws://127.0.0.1:1"})
    assert started.status_code == 401


def test_a_rebound_hostname_is_refused_even_with_the_token(client: TestClient) -> None:
    """DNS rebinding: the attacker's page reaches 127.0.0.1 under its own hostname."""
    evil = TestClient(client.app, base_url="http://attacker.example:8000", headers=AUTH)
    r = evil.get("/api/v1/runs")
    assert r.status_code == 403 and r.json()["code"] == "HOST_NOT_ALLOWED"
    for ok_host in ("http://localhost:8000", "http://[::1]:8000"):
        ok = TestClient(client.app, base_url=ok_host, headers=AUTH).get("/api/v1/health")
        assert ok.status_code == 200, ok_host


def test_a_cross_site_write_is_refused(client: TestClient) -> None:
    r = client.post(
        "/api/v1/simulator/rehearse",
        json={"scenarioId": "rf-001"},
        headers={"Origin": "https://attacker.example"},
    )
    assert r.status_code == 403 and r.json()["code"] == "ORIGIN_NOT_ALLOWED"
    same = client.post(
        "/api/v1/simulator/rehearse",
        json={"scenarioId": "rf-001"},
        headers={"Origin": "http://localhost:5173"},
    )
    assert same.status_code == 200


@pytest.mark.parametrize(
    ("header", "name"),
    [("127.0.0.1:8000", "127.0.0.1"), ("[::1]:8000", "::1"), ("LocalHost", "localhost"), ("", "")],
)
def test_host_name(header: str, name: str) -> None:
    assert host_name(header) == name


def test_a_configured_token_is_used_and_a_missing_one_is_minted(tmp_path: Path) -> None:
    from pydantic import SecretStr

    fixed = create_app(settings=Settings(reports_dir=str(tmp_path), serve_token=SecretStr("x")))
    assert fixed.state.token == "x"
    a = create_app(settings=Settings(reports_dir=str(tmp_path)))
    b = create_app(settings=Settings(reports_dir=str(tmp_path)))
    assert len(a.state.token) >= 24 and a.state.token != b.state.token


def test_serve_refuses_a_non_loopback_bind() -> None:
    from opseval.cli import app

    res = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0"])  # nosec B104 - the test
    assert res.exit_code == 3
    assert "loopback only" in res.output


# --- request bodies -----------------------------------------------------------------------


def test_request_bodies_are_strict(client: TestClient) -> None:
    typo = client.post("/api/v1/runs", json={"suite": "smoke", "allowCommitt": True})
    assert typo.status_code == 422
    for repeats in (0, -1, 21):
        r = client.post("/api/v1/runs", json={"suite": "smoke", "repeats": repeats})
        assert r.status_code == 422, repeats


def test_commit_needs_the_server_flag_not_just_a_request_field(tmp_path: Path) -> None:
    app, _ = app_for(tmp_path)
    r = connect(app).post(
        "/api/v1/runs", json={"suite": "smoke", "target": "ws://127.0.0.1:1", "allowCommit": True}
    )
    assert r.status_code == 403 and r.json()["code"] == "COMMIT_NOT_ALLOWED"


def test_the_demo_catalogue_is_read_only(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, read_only=True)
    c = connect(app)
    assert c.get("/api/v1/health").json()["readOnly"] is True
    before = sorted(p.name for p in cat.rglob("*.yaml"))
    r = c.put("/api/v1/suites", json={"file": "x", "displayName": "x", "include": ["*"]})
    assert r.status_code == 409 and r.json()["code"] == "READ_ONLY_CATALOGUE"
    r = c.post("/api/v1/scenarios/import", json={"files": [{"name": "a.yaml", "text": "id: x"}]})
    assert r.status_code == 409
    assert sorted(p.name for p in cat.rglob("*.yaml")) == before


# --- writing the catalogue ----------------------------------------------------------------


def test_save_suite_is_validated_and_written(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    ok = c.put(
        "/api/v1/suites",
        json={
            "file": "mine",
            "displayName": "mine",
            "include": ["scenarios/rf-001*.yaml"],
            "repeats": 2,
        },
    )
    assert ok.status_code == 200 and ok.json()["scenarioCount"] == 1
    assert (cat / "suites" / "mine.yaml").is_file()
    assert (
        c.put(
            "/api/v1/suites", json={"file": "../evil", "displayName": "x", "include": ["*"]}
        ).status_code
        == 422
    )


def test_save_suite_never_replaces_unasked_or_leaves_a_broken_file(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    smoke = (cat / "suites" / "smoke.yaml").read_text("utf-8")
    body: dict[str, Any] = {
        "file": "smoke",
        "displayName": "s",
        "include": ["scenarios/rf-001*.yaml"],
    }
    r = c.put("/api/v1/suites", json=body)
    assert r.status_code == 409 and r.json()["code"] == "SUITE_EXISTS"
    assert (cat / "suites" / "smoke.yaml").read_text("utf-8") == smoke
    # A suite that selects nothing (here: includes outside the catalogue) is rejected,
    # and nothing is left on disk.
    bad = {"file": "smoke", "displayName": "s", "include": ["../../*"], "overwrite": True}
    r = c.put("/api/v1/suites", json=bad)
    assert r.status_code == 422 and r.json()["code"] == "EMPTY_SUITE"
    assert (cat / "suites" / "smoke.yaml").read_text("utf-8") == smoke
    assert not [p for p in (cat / "suites").iterdir() if p.name.startswith(".")]
    body["overwrite"] = True
    assert c.put("/api/v1/suites", json=body).status_code == 200


def test_suite_settings_round_trip(tmp_path: Path) -> None:
    app, _ = app_for(tmp_path, copy=True)
    c = connect(app)
    body = {"file": "r", "displayName": "r", "include": ["scenarios/rf-001*.yaml"], "repeats": 4}
    assert c.put("/api/v1/suites", json=body).status_code == 200
    saved = next(s for s in c.get("/api/v1/suites").json() if s["file"] == "r")
    assert saved["repeats"] == 4
    smoke = next(s for s in c.get("/api/v1/suites").json() if s["file"] == "smoke")
    assert smoke["repeats"] is None  # not set means "scenarios decide", never a made-up 3


def test_import_is_all_or_nothing(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    existing = (cat / "scenarios" / "rf-001-happy-path.yaml").read_text()
    new = existing.replace("id: rf-001", "id: rf-900")
    ok = c.post(
        "/api/v1/scenarios/import",
        json={"family": "mine", "files": [{"name": "a.yaml", "text": new}]},
    )
    assert ok.status_code == 200 and ok.json()["imported"] == ["rf-900"]
    before = sorted(p.name for p in (cat / "scenarios").rglob("*.yaml"))
    bad = c.post(
        "/api/v1/scenarios/import",
        json={
            "family": "mine2",
            "files": [
                {"name": "b.yaml", "text": new.replace("rf-900", "rf-901")},
                {"name": "c.yaml", "text": "not: [valid"},
            ],
        },
    )
    assert bad.status_code == 422
    assert sorted(p.name for p in (cat / "scenarios").rglob("*.yaml")) == before
    clash = c.post(
        "/api/v1/scenarios/import",
        json={"family": "mine3", "files": [{"name": "d.yaml", "text": existing}]},
    )
    assert clash.status_code == 422 and "would replace" in clash.text


def test_import_never_overwrites_a_file_or_repeats_an_id(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    template = (cat / "scenarios" / "rf-001-happy-path.yaml").read_text()
    one = template.replace("id: rf-001", "id: rf-910")
    first = c.post(
        "/api/v1/scenarios/import",
        json={"family": "f", "files": [{"name": "a.yaml", "text": one}]},
    )
    assert first.status_code == 200
    # Same file name, different id: it would silently delete rf-910.
    other = template.replace("id: rf-001", "id: rf-911")
    r = c.post(
        "/api/v1/scenarios/import",
        json={"family": "f", "files": [{"name": "a.yaml", "text": other}]},
    )
    assert r.status_code == 422 and "would replace f/a.yaml" in r.text
    assert "rf-910" in (cat / "scenarios" / "f" / "a.yaml").read_text()
    # One id twice in one batch.
    dup = template.replace("id: rf-001", "id: rf-912")
    r = c.post(
        "/api/v1/scenarios/import",
        json={
            "family": "g",
            "files": [{"name": "x.yaml", "text": dup}, {"name": "y.yaml", "text": dup}],
        },
    )
    assert r.status_code == 422 and "given twice" in r.text
    assert not (cat / "scenarios" / "g").exists()


# --- launching runs -------------------------------------------------------------------------


async def _wait_for(http: httpx.AsyncClient, run_id: str, done: set[str]) -> dict[str, Any]:
    for _ in range(300):
        runs = {r["id"]: r for r in (await http.get("/api/v1/runs")).json()}
        if run_id in runs and runs[run_id]["state"] in done:
            return runs[run_id]
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {done}")


def _async_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, headers=AUTH)


async def test_launch_runs_the_suite_in_the_background(tmp_path: Path) -> None:
    from opseval.demo.refund_agent import run_agent

    settings = Settings(reports_dir=str(tmp_path / "r"), allowed_hosts="127.0.0.1")
    app, _ = app_for(tmp_path, settings=settings)
    async with run_agent(bug=None) as url, _async_client(app) as http:
        started = await http.post(
            "/api/v1/runs", json={"suite": "smoke", "repeats": 1, "target": url}
        )
        assert started.status_code == 202
        run = await _wait_for(http, started.json()["runId"], {"finished"})
        assert run["passed"] == 3 and run["failed"] == 0 and run["gate"] in ("safe", "gaps")
        bad = await http.post("/api/v1/runs", json={"suite": "nope", "target": url})
        assert bad.status_code == 404 and bad.json()["code"] == "UNKNOWN_SUITE"
        notarget = await http.post("/api/v1/runs", json={"suite": "smoke"})
        assert notarget.json()["code"] == "NO_TARGET"


async def test_a_selection_runs_only_those_scenarios_and_writes_no_suite(tmp_path: Path) -> None:
    from opseval.demo.refund_agent import run_agent

    settings = Settings(reports_dir=str(tmp_path / "r"), allowed_hosts="127.0.0.1")
    app, cat = app_for(tmp_path, copy=True, settings=settings)
    suites_before = sorted(p.name for p in (cat / "suites").iterdir())
    async with run_agent(bug=None) as url, _async_client(app) as http:
        started = await http.post(
            "/api/v1/runs",
            json={"scenarios": ["rf-001", "rf-001"], "repeats": 1, "target": url},
        )
        run = await _wait_for(http, started.json()["runId"], {"finished"})
        assert run["passed"] == 1
    assert sorted(p.name for p in (cat / "suites").iterdir()) == suites_before


async def test_a_launch_that_cannot_start_is_a_visible_harness_error(tmp_path: Path) -> None:
    """Preflight finds no agent: the CLI exits 3, so the console must say so, not go quiet."""
    app, _ = app_for(tmp_path)
    async with _async_client(app) as http:
        started = await http.post(
            "/api/v1/runs", json={"suite": "smoke", "target": "ws://127.0.0.1:1"}
        )
        assert started.status_code == 202
        run_id = started.json()["runId"]
        run = await _wait_for(http, run_id, {"harness_error"})
        assert run["gate"] == "harness_error" and run["error"].startswith("CONFIG_ERROR")
        assert run["passed"] is None and run["failed"] is None  # not measured, never 0
        detail = (await http.get(f"/api/v1/runs/{run_id}")).json()
        assert detail["exitCode"] == 3 and "not an agent failure" in detail["gateSummary"]


def _dead_pid() -> int:
    pid = 2**22 + 12345  # well above the usual pid range
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except OSError:
            pass
        pid += 1


def test_a_run_whose_process_died_is_not_running_forever(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    here = socket.gethostname()
    store.write_manifest(
        "dead",
        {"run_id": "dead", "suite": "s", "pid": _dead_pid(), "host": here},
    )
    store.write_manifest("live", {"run_id": "live", "suite": "s", "pid": os.getpid(), "host": here})
    store.write_manifest("elsewhere", {"run_id": "elsewhere", "suite": "s", "pid": 1, "host": "x"})
    c = connect(create_app(settings=Settings(reports_dir=str(tmp_path)), token=TOKEN))
    runs = {r["id"]: r for r in c.get("/api/v1/runs").json()}
    assert runs["dead"]["state"] == "harness_error" and "INTERRUPTED" in runs["dead"]["error"]
    assert runs["live"]["state"] == "running" and runs["live"]["passed"] is None
    assert runs["elsewhere"]["state"] == "running"  # cannot tell: never guessed dead
    # The detail agrees with the list: an interrupted run is shown, a live one is "wait",
    # and an id that is not a run is "not found" (never waited on for ever).
    dead = c.get("/api/v1/runs/dead")
    assert dead.status_code == 200 and "INTERRUPTED" in dead.json()["gateSummary"]
    live = c.get("/api/v1/runs/live")
    assert live.status_code == 409 and live.json()["code"] == "RUN_IN_PROGRESS"
    missing = c.get("/api/v1/runs/nope")
    assert missing.status_code == 404 and missing.json()["code"] == "UNKNOWN_RUN"


def test_no_pid_probe_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.kill(pid, 0) is TerminateProcess on Windows: probing would kill the run."""
    from opseval.server import views

    called: list[int] = []
    monkeypatch.setattr(views.os, "name", "nt")
    monkeypatch.setattr(views.os, "kill", lambda pid, sig: called.append(pid))
    assert views._process_gone({"pid": 123, "host": socket.gethostname()}) is False
    assert called == []


def test_save_error_is_what_the_detail_reads(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    store.write_manifest("e1", {"run_id": "e1", "suite": "s", "target": "ws://t"})
    store.save_error("e1", {"code": "HARNESS_ERROR", "message": "disk full", "exit_code": 3})
    assert json.loads((tmp_path / "e1" / "error.json").read_text())["message"] == "disk full"
    c = connect(create_app(settings=Settings(reports_dir=str(tmp_path)), token=TOKEN))
    d = c.get("/api/v1/runs/e1").json()
    assert d["target"] == "ws://t" and d["summary"]["error"] == "HARNESS_ERROR: disk full"
    with pytest.raises(ValueError, match="not a run id"):
        store.run_dir("..")


async def test_an_unknown_scenario_in_a_selection_is_a_config_error_not_dropped(
    tmp_path: Path,
) -> None:
    app, _ = app_for(tmp_path)
    async with _async_client(app) as http:
        started = await http.post(
            "/api/v1/runs",
            json={"scenarios": ["rf-001", "nope"], "target": "ws://127.0.0.1:1"},
        )
        run = await _wait_for(http, started.json()["runId"], {"harness_error"})
        assert "CONFIG_ERROR" in run["error"] and "'nope'" in run["error"]


# --- review fixes ---------------------------------------------------------------------------


async def test_a_broken_file_outside_the_selection_does_not_mark_the_run(tmp_path: Path) -> None:
    from opseval.demo.refund_agent import run_agent

    settings = Settings(reports_dir=str(tmp_path / "r"), allowed_hosts="127.0.0.1")
    app, cat = app_for(tmp_path, copy=True, settings=settings)
    (cat / "scenarios" / "wip").mkdir()
    (cat / "scenarios" / "wip" / "broken.yaml").write_text("id: [not, valid", "utf-8")
    async with run_agent(bug=None) as url, _async_client(app) as http:
        started = await http.post(
            "/api/v1/runs", json={"scenarios": ["rf-001"], "repeats": 1, "target": url}
        )
        run_id = started.json()["runId"]
        run = await _wait_for(http, run_id, {"finished"})
        detail = (await http.get(f"/api/v1/runs/{run_id}")).json()
    assert run["passed"] == 1 and detail["loadErrors"] == [] and detail["exitCode"] == 0


def test_gate_state_follows_what_blocked_not_the_exit_code(tmp_path: Path) -> None:
    from opseval.server.views import gate_state

    def gated(blocking: list[str], exit_code: int) -> SuiteResult:
        r = _result("x", 1, {"a": VerdictStatus.PASSED}, exit_code, gate="block")
        return r.model_copy(update={"gate": Gate(status="block", blocking=blocking)})

    # A normal failure (a warning) plus an inconclusive block: exit 1, but not an agent block.
    assert gate_state(gated(["b: inconclusive"], 1)) == "not_judged"
    # A flaky scenario blocking (fail_on_flaky) with exit 0: the agent was inconsistent.
    assert gate_state(gated(["a: flaky"], 0)) == "blocked"
    assert gate_state(gated(["a: failed (critical)"], 1)) == "blocked"
    assert gate_state(gated(["no scenario was judged"], 2)) == "not_judged"


def test_saving_the_open_suite_keeps_what_the_form_does_not_manage(tmp_path: Path) -> None:
    import yaml

    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    path = cat / "suites" / "kept.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "kept",
                "include": ["scenarios/rf-001*.yaml"],
                "exclude": ["scenarios/none*.yaml"],
                "defaults": {"conversation": {"stop_at": "commit_gate"}},
                "overrides": {"execution": {"repeats": 3, "consistency_threshold": 0.5}},
            }
        ),
        "utf-8",
    )
    body = {
        "file": "kept",
        "displayName": "renamed",
        "include": ["scenarios/rf-001*.yaml"],
        "overwrite": True,
    }
    assert c.put("/api/v1/suites", json=body).status_code == 200
    doc = yaml.safe_load(path.read_text("utf-8"))
    assert doc["name"] == "renamed" and doc["exclude"] == ["scenarios/none*.yaml"]
    assert doc["defaults"]["conversation"]["stop_at"] == "commit_gate"
    assert doc["overrides"]["execution"] == {"repeats": 3, "consistency_threshold": 0.5}
    # An explicit null clears the override and nothing else.
    assert c.put("/api/v1/suites", json={**body, "repeats": None}).status_code == 200
    doc = yaml.safe_load(path.read_text("utf-8"))
    assert doc["overrides"]["execution"] == {"consistency_threshold": 0.5}


def test_import_into_a_flat_catalogue_checks_clashes_and_keeps_scenarios(tmp_path: Path) -> None:
    flat = tmp_path / "flat"
    flat.mkdir()
    src = catalogue_dir()
    shutil.copy(src / "profile.yaml", flat / "profile.yaml")
    shutil.copy(src / "scenarios" / "rf-001-happy-path.yaml", flat / "rf-001.yaml")
    app = create_app(
        settings=Settings(reports_dir=str(tmp_path / "r")),
        catalogue=flat,
        profile=flat / "profile.yaml",
        token=TOKEN,
    )
    c = connect(app)
    text = (flat / "rf-001.yaml").read_text()
    clash = c.post(
        "/api/v1/scenarios/import",
        json={"family": "f", "files": [{"name": "a.yaml", "text": text}]},
    )
    assert clash.status_code == 422 and "would replace existing scenario" in clash.text
    new = text.replace("id: rf-001", "id: rf-950")
    ok = c.post(
        "/api/v1/scenarios/import", json={"family": "f", "files": [{"name": "a.yaml", "text": new}]}
    )
    assert ok.status_code == 200
    ids = {s["id"] for s in c.get("/api/v1/scenarios").json()["scenarios"]}
    assert {"rf-001", "rf-950"} <= ids and not (flat / "scenarios").exists()


def test_serve_never_prints_a_configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from opseval.cli import app as cli

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setenv("OPSEVAL_SERVE_TOKEN", "long-lived-secret")
    out = CliRunner().invoke(cli, ["serve", "--port", "8123"]).output
    assert "long-lived-secret" not in out and "<OPSEVAL_SERVE_TOKEN>" in out
    assert "OPSEVAL_API=http://127.0.0.1:8123" in out
    monkeypatch.delenv("OPSEVAL_SERVE_TOKEN")
    out = CliRunner().invoke(cli, ["serve"]).output
    assert "#token=" in out and "this start only" in out


# --- the gate editor (ADR-0032) --------------------------------------------------------------


def test_the_gate_catalog_offers_only_what_the_loader_accepts(client: TestClient) -> None:
    cat = client.get("/api/v1/gate").json()
    choices = {s["key"]: s["choices"] for s in cat["settings"]}
    assert choices["commit_guard"] == ["warn", "ignore"]  # never block
    assert "ignore" not in choices["inconclusive"] and "ignore" not in choices["load_errors"]
    assert set(cat["presets"]) == {"strict", "standard", "lenient"}
    assert cat["presets"]["standard"]["failed.normal"] == "warn"
    assert all(s["label"] and s["help"] for s in cat["settings"])


def test_a_gate_edit_is_previewed_on_the_server(client: TestClient) -> None:
    out = client.post(
        "/api/v1/gate/resolve",
        json={"preset": "lenient", "overrides": {"failed.normal": "block"}},
    ).json()
    assert out["describe"] == "lenient + failed.normal=block"
    assert out["effective"]["failed.normal"] == "block"
    assert out["yaml"] == "gate:\n  preset: lenient\n  failed:\n    normal: block\n"
    assert "exit_code=gate" in out["looserThanStandard"]
    bad = client.post("/api/v1/gate/resolve", json={"overrides": {"commit_guard": "block"}})
    assert bad.status_code == 422 and bad.json()["code"] == "INVALID_GATE"
    unknown = client.post("/api/v1/gate/resolve", json={"overrides": {"flakey": "warn"}})
    assert unknown.status_code == 422


def test_a_suite_gate_is_saved_kept_and_removed(tmp_path: Path) -> None:
    import yaml

    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    base = {"file": "g", "displayName": "g", "include": ["scenarios/rf-001*.yaml"]}
    gate = {"preset": "strict", "overrides": {"flaky": "warn"}}
    saved = c.put("/api/v1/suites", json={**base, "gate": gate}).json()
    assert saved["gate"]["describe"] == "strict + flaky=warn"
    doc = yaml.safe_load((cat / "suites" / "g.yaml").read_text())
    assert doc["gate"] == {"preset": "strict", "flaky": "warn"}
    # Listed with what it resolves to.
    row = next(s for s in c.get("/api/v1/suites").json() if s["file"] == "g")
    assert (
        row["gate"]["overrides"] == {"flaky": "warn"}
        and row["gate"]["effective"]["flaky"] == "warn"
    )
    # Omitting the gate on an edit keeps it; an explicit null removes it (standard).
    c.put("/api/v1/suites", json={**base, "displayName": "g2", "overwrite": True})
    assert yaml.safe_load((cat / "suites" / "g.yaml").read_text())["gate"]["preset"] == "strict"
    out = c.put("/api/v1/suites", json={**base, "gate": None, "overwrite": True}).json()
    assert "gate" not in yaml.safe_load((cat / "suites" / "g.yaml").read_text())
    assert out["gate"]["describe"] == "standard"
    # A gate the loader would refuse is refused before anything is written.
    r = c.put(
        "/api/v1/suites",
        json={**base, "gate": {"overrides": {"inconclusive": "ignore"}}, "overwrite": True},
    )
    assert r.status_code == 422 and "gate" not in yaml.safe_load(
        (cat / "suites" / "g.yaml").read_text()
    )


def test_run_detail_names_the_policy_it_was_gated_with(tmp_path: Path) -> None:
    from opseval.models.gate import PRESETS
    from opseval.verdicts import release_gate

    tests = [
        TestVerdict(
            test_id="a", name="a", criticality=Criticality.CRITICAL, status=VerdictStatus.PASSED
        )
    ]
    r = _result("p1", 1, {"a": VerdictStatus.PASSED})
    r = r.model_copy(
        update={"tests": tests, "gate": release_gate(tests, policy=PRESETS["lenient"])}
    )
    FileStore(tmp_path).save_result(r)
    d = (
        connect(create_app(settings=Settings(reports_dir=str(tmp_path)), token=TOKEN))
        .get("/api/v1/runs/p1")
        .json()
    )
    assert d["gatePolicy"] == "lenient" and "exit_code=gate" in d["looserThanStandard"]


def test_the_gate_preview_resolves_like_a_run(tmp_path: Path) -> None:
    """OPSEVAL_GATE_FAIL_ON_FLAKY changes what a run does, so the editor must show it."""
    app, _ = app_for(
        tmp_path, settings=Settings(reports_dir=str(tmp_path / "r"), gate_fail_on_flaky=True)
    )
    c = connect(app)
    out = c.post("/api/v1/gate/resolve", json={"preset": "standard"}).json()
    assert out["effective"]["flaky"] == "block" and out["describe"] == "standard + flaky=block"
    smoke = next(s for s in c.get("/api/v1/suites").json() if s["file"] == "smoke")
    assert smoke["gate"]["effective"]["flaky"] == "block"


def test_saving_the_standard_gate_writes_no_block(tmp_path: Path) -> None:
    import yaml

    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    base = {"file": "plain", "displayName": "p", "include": ["scenarios/rf-001*.yaml"]}
    body = {**base, "gate": {"preset": "standard", "overrides": {"failed.normal": "warn"}}}
    out = c.put("/api/v1/suites", json=body).json()
    assert "gate" not in yaml.safe_load((cat / "suites" / "plain.yaml").read_text())
    assert out["gate"]["yaml"] == "" and out["gate"]["overrides"] == {}
