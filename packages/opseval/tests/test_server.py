# SPDX-License-Identifier: Apache-2.0
"""The console API reads real run folders and never invents numbers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opseval.config import Settings
from opseval.demo import catalogue_dir
from opseval.models.results import (
    Gate,
    ModelUsage,
    SuiteResult,
    TestVerdict,
    VerdictStatus,
)
from opseval.models.scenario import Criticality
from opseval.server import create_app
from opseval.storage import FileStore


def _result(
    run_id: str, minute: int, statuses: dict[str, VerdictStatus], exit_code: int = 0
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
            TestVerdict(test_id=tid, name=tid, criticality=Criticality.NORMAL, status=st)
            for tid, st in statuses.items()
        ],
        gate=Gate(status="pass", summary="ok"),
        exit_code=exit_code,
        usage=[ModelUsage(role="simulator", model="m", input_tokens=5, output_tokens=5)],
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    store = FileStore(tmp_path)
    store.save_result(_result("r1", 1, {"a": VerdictStatus.PASSED, "b": VerdictStatus.PASSED}))
    store.save_result(_result("r2", 2, {"a": VerdictStatus.FAILED, "b": VerdictStatus.PASSED}, 1))
    store.save_result(
        _result("r3", 3, {"a": VerdictStatus.INCONCLUSIVE, "b": VerdictStatus.PASSED}, 2)
    )
    cat = catalogue_dir()
    settings = Settings(reports_dir=str(tmp_path))
    return TestClient(create_app(settings=settings, catalogue=cat, profile=cat / "profile.yaml"))


def test_health(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok" and body["profile"] == "refund-agent"


def test_runs_newest_first_with_gate_states(client: TestClient) -> None:
    runs = client.get("/api/v1/runs").json()
    assert [r["id"] for r in runs] == ["r3", "r2", "r1"]
    assert runs[0]["gate"] == "not_judged" and runs[0]["unjudged"] == 1
    assert runs[1]["failed"] == 1


def test_run_detail_and_404(client: TestClient) -> None:
    assert client.get("/api/v1/runs/r2").json()["summary"]["id"] == "r2"
    assert client.get("/api/v1/runs/nope").status_code == 404


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

    settings = Settings(reports_dir=str(tmp_path), judge_api_key=SecretStr("sk-very-secret"))
    rows = TestClient(create_app(settings=settings)).get("/api/v1/config")
    assert "sk-very-secret" not in rows.text
    assert any(r["key"] == "opseval_judge_api_key" and r["redacted"] for r in rows.json())


def test_no_catalogue_is_empty_not_an_error(tmp_path: Path) -> None:
    c = TestClient(create_app(settings=Settings(reports_dir=str(tmp_path))))
    assert c.get("/api/v1/scenarios").json() == {"scenarios": [], "problems": []}
    assert c.get("/api/v1/suites").json() == []


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


def test_save_suite_is_validated_and_written(tmp_path: Path) -> None:
    import shutil

    cat = tmp_path / "cat"
    shutil.copytree(catalogue_dir(), cat)
    c = TestClient(
        create_app(
            settings=Settings(reports_dir=str(tmp_path / "r")),
            catalogue=cat,
            profile=cat / "profile.yaml",
        )
    )
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


def test_import_is_all_or_nothing(tmp_path: Path) -> None:
    import shutil

    cat = tmp_path / "cat"
    shutil.copytree(catalogue_dir(), cat)
    c = TestClient(
        create_app(
            settings=Settings(reports_dir=str(tmp_path / "r")),
            catalogue=cat,
            profile=cat / "profile.yaml",
        )
    )
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
    assert (
        sorted(p.name for p in (cat / "scenarios").rglob("*.yaml")) == before
    )  # nothing half-imported
    clash = c.post(
        "/api/v1/scenarios/import",
        json={"family": "mine3", "files": [{"name": "d.yaml", "text": existing}]},
    )
    assert clash.status_code == 422 and "would replace" in clash.text


async def test_launch_runs_the_suite_in_the_background(tmp_path: Path) -> None:
    import asyncio

    import httpx

    from opseval.demo.refund_agent import run_agent

    cat = catalogue_dir()
    settings = Settings(reports_dir=str(tmp_path), allowed_hosts="127.0.0.1")
    app = create_app(settings=settings, catalogue=cat, profile=cat / "profile.yaml")
    async with (
        run_agent(bug=None) as url,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        started = await http.post(
            "/api/v1/runs", json={"suite": "smoke", "repeats": 1, "target": url}
        )
        assert started.status_code == 202
        for _ in range(100):
            runs = (await http.get("/api/v1/runs")).json()
            if runs and runs[0]["state"] == "finished":
                break
            await asyncio.sleep(0.1)
        assert runs[0]["state"] == "finished" and runs[0]["passed"] == 3 and runs[0]["failed"] == 0
        bad = await http.post("/api/v1/runs", json={"suite": "nope", "target": url})
        assert bad.status_code == 404 and bad.json()["code"] == "UNKNOWN_SUITE"
        notarget = await http.post("/api/v1/runs", json={"suite": "smoke"})
        assert notarget.json()["code"] == "NO_TARGET"


def test_readiness_says_what_is_limited(client: TestClient) -> None:
    items = {i["id"]: i for i in client.get("/api/v1/readiness").json()}
    assert items["catalogue"]["ready"] is True
    assert items["judge"]["ready"] is False and "OPSEVAL_JUDGE" in items["judge"]["detail"]
    assert set(items) == {"catalogue", "simulator", "judge", "allowlist", "tracing"}


def test_suite_settings_round_trip(tmp_path: Path) -> None:
    import shutil

    cat = tmp_path / "cat"
    shutil.copytree(catalogue_dir(), cat)
    c = TestClient(
        create_app(
            settings=Settings(reports_dir=str(tmp_path / "r")),
            catalogue=cat,
            profile=cat / "profile.yaml",
        )
    )
    body = {"file": "r", "displayName": "r", "include": ["scenarios/rf-001*.yaml"], "repeats": 4}
    assert c.put("/api/v1/suites", json=body).status_code == 200
    saved = next(s for s in c.get("/api/v1/suites").json() if s["file"] == "r")
    assert saved["repeats"] == 4
    smoke = next(s for s in c.get("/api/v1/suites").json() if s["file"] == "smoke")
    assert smoke["repeats"] is None  # not set means "scenarios decide", never a made-up 3
