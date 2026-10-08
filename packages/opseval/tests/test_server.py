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


def test_serve_never_prints_a_configured_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import uvicorn

    from opseval.cli import app as cli

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr("opseval.server.app.find_console", lambda _s: None)  # no build here
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


# --- the console, served by the API (ADR-0034) ------------------------------------------------


@pytest.fixture
def console_dir(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=root></div>", "utf-8")
    (root / "assets" / "app.js").write_text("console.log('opseval')", "utf-8")
    (tmp_path / "secret.txt").write_text("not for the browser", "utf-8")
    return root


def test_the_console_is_served_same_origin(tmp_path: Path, console_dir: Path) -> None:
    app = create_app(
        settings=Settings(reports_dir=str(tmp_path / "r")), token=TOKEN, console_dir=console_dir
    )
    anon = TestClient(app, base_url=BASE)  # the page itself needs no token: it holds no data
    page = anon.get("/")
    assert page.status_code == 200 and "id=root" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    # Nothing from another origin, fonts included: a page view is reported to no one.
    assert _csp_sources_are_local(page.headers["content-security-policy"])
    assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"
    assert anon.get("/runs/20261008T000000Z-abc").text == page.text  # a console route
    asset = anon.get("/assets/app.js")
    assert asset.status_code == 200 and "opseval" in asset.text
    # The API is unchanged: still behind the token, and an unknown route is not the page.
    assert anon.get("/api/v1/runs").status_code == 401
    missing = TestClient(app, base_url=BASE, headers=AUTH).get("/api/v1/nope")
    assert missing.status_code == 404 and "id=root" not in missing.text


def test_the_console_cannot_serve_files_outside_its_folder(
    tmp_path: Path, console_dir: Path
) -> None:
    app = create_app(
        settings=Settings(reports_dir=str(tmp_path / "r")), token=TOKEN, console_dir=console_dir
    )
    anon = TestClient(app, base_url=BASE)
    for path in ("/%2e%2e/secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt", "/..%2Fsecret.txt"):
        r = anon.get(path)
        assert "not for the browser" not in r.text, path
    evil = TestClient(app, base_url="http://attacker.example:8000")
    assert evil.get("/").status_code == 403  # DNS rebinding cannot load the page either


def test_without_a_built_console_only_the_api_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("opseval.server.app.find_console", lambda _s: None)
    app = create_app(settings=Settings(reports_dir=str(tmp_path / "r")), token=TOKEN)
    assert app.state.console is None
    assert TestClient(app, base_url=BASE).get("/").status_code == 404


def test_a_configured_console_dir_without_a_build_is_a_config_error(tmp_path: Path) -> None:
    from opseval.errors import ConfigError

    settings = Settings(reports_dir=str(tmp_path / "r"), console_dir=str(tmp_path / "none"))
    with pytest.raises(ConfigError, match="no index.html"):
        create_app(settings=settings, token=TOKEN)


def test_the_console_never_changes_how_the_api_routes(tmp_path: Path, console_dir: Path) -> None:
    """Review of #34: a catch-all route had turned redirects into 404s and /api into the page."""
    app = create_app(
        settings=Settings(reports_dir=str(tmp_path / "r")), token=TOKEN, console_dir=console_dir
    )
    c = TestClient(app, base_url=BASE, headers=AUTH, follow_redirects=False)
    assert c.get("/api/v1/runs/").status_code == 307  # Starlette's slash redirect, as before
    assert c.get("/api").status_code == 404 and "id=root" not in c.get("/api").text
    assert c.post("/api/v1/nope").status_code == 404
    assert c.get("/%00").status_code in (200, 404)  # never a 500
    assert c.head("/").status_code == 200
    # No public API docs: the schema is under /api, behind the token.
    assert "id=root" in TestClient(app, base_url=BASE).get("/docs").text  # just the page
    assert TestClient(app, base_url=BASE).get("/api/v1/openapi.json").status_code == 401
    assert c.get("/api/v1/openapi.json").status_code == 200


def test_a_relative_console_dir_and_every_page_get_the_headers(
    tmp_path: Path, console_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (console_dir / "extra.html").write_text("<p>public/ file</p>", "utf-8")
    monkeypatch.chdir(console_dir.parent)
    app = create_app(
        settings=Settings(reports_dir=str(tmp_path / "r")), token=TOKEN, console_dir=Path("dist")
    )
    anon = TestClient(app, base_url=BASE)
    js = anon.get("/assets/app.js")
    assert "opseval" in js.text and "immutable" in js.headers["cache-control"]
    extra = anon.get("/extra.html")
    assert "public/ file" in extra.text and extra.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in js.headers["content-security-policy"]


def test_serve_prints_the_served_console_link(
    monkeypatch: pytest.MonkeyPatch, console_dir: Path
) -> None:
    import uvicorn

    from opseval.cli import app as cli

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setenv("OPSEVAL_CONSOLE_DIR", str(console_dir))
    monkeypatch.delenv("OPSEVAL_SERVE_TOKEN", raising=False)
    out = CliRunner().invoke(cli, ["serve", "--port", "8123"]).output
    assert "Console:  http://127.0.0.1:8123/#token=" in out
    assert "OPSEVAL_API" not in out and "npm run build" not in out


# --- transcripts (run detail) -------------------------------------------------------------------


async def test_an_attempt_shows_its_conversation(tmp_path: Path) -> None:
    from opseval.demo.refund_agent import run_agent
    from opseval.runner import RunOptions, launch

    cat = catalogue_dir()
    settings = Settings(reports_dir=str(tmp_path), allowed_hosts="127.0.0.1")
    async with run_agent() as url:
        result = await launch(
            cat / "suites" / "smoke.yaml",
            RunOptions(target=url, profile=cat / "profile.yaml", repeats=1),
            settings,
        )
    c = connect(create_app(settings=settings, token=TOKEN))
    rec = c.get(f"/api/v1/runs/{result.run_id}/attempts/rf-001/0").json()
    kinds = [e["kind"] for e in rec["entries"]]
    who = {e["who"] for e in rec["entries"] if e["kind"] == "message"}
    assert who == {"customer", "agent"} and "checkpoint" in kinds and "decision" in kinds
    assert rec["userTurns"] >= 1 and rec["redactionChecked"] is True
    # The Egyptian Arabic persona's turns arrive as they were said.
    ar = c.get(f"/api/v1/runs/{result.run_id}/attempts/rf-003/0").json()
    assert ar["language"] == "ar" and ar["dialect"] == "egyptian"
    assert any(
        e["who"] == "customer" and any("؀" <= ch <= "ۿ" for ch in e["text"] or "")
        for e in ar["entries"]
    )


def test_an_attempt_that_does_not_exist_is_a_404(client: TestClient) -> None:
    for path in (
        "r1/attempts/a/0",
        "r1/attempts/a/-1",
        "r1/attempts/..%2Fx/0",
        "nope/attempts/a/0",
    ):
        r = client.get(f"/api/v1/runs/{path}")
        assert r.status_code == 404, path
        assert r.json()["code"] in ("UNKNOWN_ATTEMPT", "NOT_FOUND"), path  # a code, always


def test_an_unreadable_record_is_a_harness_error_not_absent(tmp_path: Path) -> None:
    folder = tmp_path / "bad" / "records"
    folder.mkdir(parents=True)
    (folder / "t__attempt0.json").write_text('{"truncated": ', "utf-8")
    r = connect(create_app(settings=Settings(reports_dir=str(tmp_path)), token=TOKEN)).get(
        "/api/v1/runs/bad/attempts/t/0"
    )
    assert r.status_code == 500 and r.json()["code"] == "UNREADABLE_RECORD"


def test_a_record_from_before_redaction_says_it_was_never_checked(tmp_path: Path) -> None:
    from opseval.models.record import FrameRecord, RunRecord

    now = datetime(2026, 9, 1, tzinfo=UTC)
    record = RunRecord(
        run_id="old",
        test_id="t",
        attempt_index=0,
        seed=1,
        target="ws://x",
        started_at=now,
        ended_at=now,
        terminal_state=TerminalState.GOAL_REACHED,
        frames=(
            FrameRecord(seq=0, at=now, direction="received", kind="error", payload={"why": "boom"}),
        ),
    )
    FileStore(tmp_path).save_record(record)
    rec = (
        connect(create_app(settings=Settings(reports_dir=str(tmp_path)), token=TOKEN))
        .get("/api/v1/runs/old/attempts/t/0")
        .json()
    )
    assert rec["redactionChecked"] is False  # never checked, not "nothing found"
    assert rec["entries"] == [
        {
            "seq": 0,
            "kind": "error",
            "who": None,
            "text": None,
            "phase": None,
            "latencyMs": None,
            "detail": '{"why": "boom"}',
        }
    ]


# --- live progress ------------------------------------------------------------------------------


def _live(tmp_path: Path, planned: list[str]) -> Any:
    from opseval.progress import ProgressLog

    store = FileStore(tmp_path)
    store.write_manifest(
        "live",
        {
            "run_id": "live",
            "suite": "s",
            "planned": planned,
            "pid": os.getpid(),
            "host": socket.gethostname(),
        },
    )
    return ProgressLog(store, "live", planned)


def _verdict(tid: str, status: VerdictStatus) -> TestVerdict:
    return TestVerdict(test_id=tid, name=tid, criticality=Criticality.NORMAL, status=status)


def test_a_running_run_reports_the_runners_own_progress(tmp_path: Path) -> None:
    log = _live(tmp_path, ["a", "b", "c"])
    log.started("a", 2)
    log.attempt("a")
    log.attempt("a")
    log.ended(_verdict("a", VerdictStatus.PASSED))
    log.started("b", 2)
    log.attempt("b")
    c = connect(create_app(settings=Settings(reports_dir=str(tmp_path)), token=TOKEN))
    run = next(r for r in c.get("/api/v1/runs").json() if r["id"] == "live")
    p = run["progress"]
    assert run["state"] == "running" and run["passed"] is None  # progress, not a verdict
    assert (p["scenariosPlanned"], p["scenariosDone"], p["attemptsRecorded"]) == (3, 1, 3)
    assert p["current"] == "b" and p["stage"] == "running"
    assert [(x["id"], x["state"], x["attempts"], x["repeats"]) for x in p["scenarios"]] == [
        ("a", "done", 2, 2),
        ("b", "running", 1, 2),
        ("c", "waiting", 0, None),
    ]
    # The run's own page gets the same progress inside its 409: no second poll needed.
    r = c.get("/api/v1/runs/live")
    assert r.status_code == 409 and r.json()["code"] == "RUN_IN_PROGRESS"
    assert r.json()["detail"]["progress"]["current"] == "b"


def test_skipped_and_finishing_are_said_not_guessed(tmp_path: Path) -> None:
    """Review of #37: a skipped last scenario looked "running" for ever, and a scenario
    looked "done" while its attempts were still being scored."""
    log = _live(tmp_path, ["a", "b"])
    log.started("a", 3)
    log.attempt("a")
    log.attempt("a")
    log.attempt("a")  # all recorded, still being scored: not done yet
    c = connect(create_app(settings=Settings(reports_dir=str(tmp_path)), token=TOKEN))
    rows = lambda: next(r for r in c.get("/api/v1/runs").json() if r["id"] == "live")["progress"]  # noqa: E731
    assert rows()["scenarios"][0]["state"] == "running"
    log.ended(_verdict("a", VerdictStatus.PASSED))
    log.started("b", 2)
    log.ended(_verdict("b", VerdictStatus.SKIPPED_PRECONDITION))
    log.finishing()
    p = rows()
    assert [x["state"] for x in p["scenarios"]] == ["done", "skipped"]
    assert p["current"] is None and p["stage"] == "finishing" and p["scenariosDone"] == 2


def test_an_unreadable_progress_file_is_no_progress_not_a_500(tmp_path: Path) -> None:
    _live(tmp_path, ["a"])
    (tmp_path / "live" / "progress.json").write_text('{"scenarios": null}', "utf-8")
    c = connect(create_app(settings=Settings(reports_dir=str(tmp_path)), token=TOKEN))
    runs = c.get("/api/v1/runs")
    assert runs.status_code == 200
    assert next(r for r in runs.json() if r["id"] == "live")["progress"] is None


def test_a_finished_run_carries_no_progress(client: TestClient) -> None:
    assert all(r["progress"] is None for r in client.get("/api/v1/runs").json())


async def test_the_runner_records_a_skipped_last_scenario(tmp_path: Path) -> None:
    """rf-101 needs an LLM persona and there is none, so it is the last and is skipped."""
    from opseval.demo.refund_agent import run_agent
    from opseval.runner import RunOptions, launch

    cat = catalogue_dir()
    settings = Settings(reports_dir=str(tmp_path), allowed_hosts="127.0.0.1")
    async with run_agent() as url:
        result = await launch(
            cat / "suites" / "full.yaml",
            RunOptions(target=url, profile=cat / "profile.yaml", repeats=1),
            settings,
        )
    p = FileStore(tmp_path).load_progress(result.run_id)
    assert p is not None and p.stage == "finishing" and p.current is None
    states = {r.id: r.state for r in p.scenarios}
    assert states["rf-101"] == "skipped" and states["rf-001"] == "done"


# --- the scenario editor -------------------------------------------------------------------------

RF1 = "scenarios/rf-001-happy-path.yaml"


def _put(c: TestClient, file: str, text: str, etag: str) -> Any:
    return c.put(
        "/api/v1/scenarios/source", params={"file": file}, json={"text": text, "etag": etag}
    )


def test_the_editor_reads_a_scenarios_own_yaml(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    by_id = c.get("/api/v1/scenarios/rf-001/source").json()
    by_file = c.get("/api/v1/scenarios/source", params={"file": RF1}).json()
    assert by_id == by_file and by_id["id"] == "rf-001" and by_id["file"] == RF1
    assert by_id["text"] == (cat / RF1).read_text("utf-8") and len(by_id["etag"]) == 16
    assert c.get("/api/v1/scenarios/nope/source").status_code == 404
    for bad in ("../profile.yaml", "suites/smoke.yaml", "scenarios/nope.yaml", "/etc/passwd"):
        assert c.get("/api/v1/scenarios/source", params={"file": bad}).status_code == 404, bad


def test_validation_is_the_loaders_with_field_paths(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    own = (cat / RF1).read_text("utf-8")
    assert c.post("/api/v1/scenarios/validate", json={"text": own, "file": RF1}).json()["ok"]
    dup = c.post("/api/v1/scenarios/validate", json={"text": own}).json()
    assert not dup["ok"] and "already used by scenarios/rf-001-happy-path.yaml" in str(dup)
    bad = c.post("/api/v1/scenarios/validate", json={"text": own.replace("goal:", "gaol:")}).json()
    assert not bad["ok"] and {"goal", "gaol"} <= {p["field"] for p in bad["problems"]}
    broken = c.post("/api/v1/scenarios/validate", json={"text": "id: [x"}).json()
    assert not broken["ok"] and broken["problems"][0]["field"].startswith("line ")
    listy = c.post("/api/v1/scenarios/validate", json={"text": "- a\n- b\n"}).json()
    assert "mapping" in listy["problems"][0]["message"]
    # A reference problem carries its field, as the loader reports it.
    ref = c.post(
        "/api/v1/scenarios/validate",
        json={"text": own.replace("id: rf-001", "id: rf-777") + "\nscorers: [{id: no.such}]\n"},
    ).json()
    assert not ref["ok"] and all(p["field"] for p in ref["problems"])


def test_a_save_is_valid_atomic_and_never_over_a_newer_file(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    path = cat / RF1
    src = c.get("/api/v1/scenarios/source", params={"file": RF1}).json()
    edited = src["text"].replace("criticality: critical", "criticality: blocker")
    out = _put(c, RF1, edited, src["etag"])
    assert out.status_code == 200 and path.read_text("utf-8") == edited
    stale = _put(c, RF1, src["text"], src["etag"])
    assert stale.status_code == 409 and stale.json()["code"] == "SCENARIO_CHANGED"
    fresh = c.get("/api/v1/scenarios/source", params={"file": RF1}).json()
    assert _put(c, RF1, "id: [x", fresh["etag"]).status_code == 422
    assert path.read_text("utf-8") == edited
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".")]


def test_two_saves_of_one_version_cannot_both_win(tmp_path: Path) -> None:
    """Review of #38: the version check and the write share one lock per file."""
    from concurrent.futures import ThreadPoolExecutor

    app, cat = app_for(tmp_path, copy=True)
    src = connect(app).get("/api/v1/scenarios/source", params={"file": RF1}).json()
    texts = [
        src["text"].replace("criticality: critical", f"criticality: {c}")
        for c in ("blocker", "normal")
    ]

    def save(text: str) -> int:
        return int(_put(connect(app), RF1, text, src["etag"]).status_code)

    with ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(save, texts))
    assert codes == [200, 409]
    assert (cat / RF1).read_text("utf-8") in texts


def test_a_save_keeps_line_endings_and_permissions(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    path = cat / RF1
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    path.chmod(0o640)
    src = c.get("/api/v1/scenarios/source", params={"file": RF1}).json()
    edited = src["text"].replace("criticality: critical", "criticality: blocker")
    assert _put(c, RF1, edited.replace("\r\n", "\n"), src["etag"]).status_code == 200
    data = path.read_bytes()
    assert b"criticality: blocker\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    assert path.stat().st_mode & 0o777 == 0o640


def test_a_broken_file_opens_and_can_be_fixed(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    path = cat / RF1
    good = path.read_text("utf-8")
    path.write_text(good.replace("goal:", "gaol:"), "utf-8")
    listed = c.get("/api/v1/scenarios").json()
    assert RF1 in {p["path"] for p in listed["problems"]}  # relative: the editor can open it
    src = c.get("/api/v1/scenarios/source", params={"file": RF1}).json()
    assert src["id"] is None
    assert _put(c, RF1, good, src["etag"]).status_code == 200
    assert "rf-001" in {s["id"] for s in c.get("/api/v1/scenarios").json()["scenarios"]}


def test_a_rename_that_would_empty_a_suite_is_refused(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    (cat / "suites" / "only1.yaml").write_text(
        "name: only1\ninclude: ['scenarios/**/*.yaml']\nfilters: [id=rf-001]\n", "utf-8"
    )
    own = (cat / RF1).read_text("utf-8")
    renamed = own.replace("id: rf-001", "id: rf-901")
    out = c.post("/api/v1/scenarios/validate", json={"text": renamed, "file": RF1}).json()
    assert not out["ok"] and "suite 'only1' selects id=rf-001" in str(out)


def test_a_new_file_is_checked_before_it_is_written(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    c = connect(app)
    text = (cat / RF1).read_text("utf-8").replace("id: rf-001", "id: rf-960")
    ok = c.post(
        "/api/v1/scenarios/validate", json={"text": text, "family": "mine", "fileName": "a.yaml"}
    ).json()
    assert ok["ok"]
    taken = c.post(
        "/api/v1/scenarios/validate",
        json={"text": text, "family": ".", "fileName": "rf-001-happy-path.yaml"},
    ).json()
    assert not taken["ok"]
    bad_name = c.post(
        "/api/v1/scenarios/validate", json={"text": text, "family": "mine", "fileName": "a.txt"}
    ).json()
    assert not bad_name["ok"] and bad_name["problems"][0]["field"] == "file"


def test_a_symlink_out_of_the_catalogue_is_reported_not_a_500(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, copy=True)
    outside = tmp_path / "shared.yaml"
    outside.write_text((cat / RF1).read_text("utf-8").replace("id: rf-001", "id: rf-970"), "utf-8")
    (cat / "scenarios" / "linked.yaml").symlink_to(outside)
    text = outside.read_text("utf-8")
    r = connect(app).post("/api/v1/scenarios/validate", json={"text": text})
    assert r.status_code == 200 and not r.json()["ok"]


def test_the_demo_catalogue_cannot_be_edited(tmp_path: Path) -> None:
    app, cat = app_for(tmp_path, read_only=True)
    c = connect(app)
    src = c.get("/api/v1/scenarios/rf-001/source").json()
    r = _put(c, src["file"], src["text"], src["etag"])
    assert r.status_code == 409 and r.json()["code"] == "READ_ONLY_CATALOGUE"


def _csp_sources_are_local(header: str) -> bool:
    """Every source in every directive is this origin or a keyword, never a host, a scheme
    or a wildcard (review of #39: a substring check would let a bare font host back in)."""
    local = {"'self'", "'none'", "'unsafe-inline'"}
    for directive in filter(None, (d.strip() for d in header.split(";"))):
        name, *sources = directive.split()
        allowed = local | ({"data:"} if name == "img-src" else set())
        if not set(sources) <= allowed:
            return False
    return True


def test_the_csp_check_itself_catches_a_host_coming_back() -> None:
    from opseval.server.app import CONSOLE_HEADERS

    assert _csp_sources_are_local(CONSOLE_HEADERS["Content-Security-Policy"])
    assert not _csp_sources_are_local("default-src 'self'; font-src 'self' fonts.gstatic.com")
    assert not _csp_sources_are_local("font-src 'self' data:")
    assert not _csp_sources_are_local("script-src *")
