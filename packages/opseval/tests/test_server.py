# SPDX-License-Identifier: Apache-2.0
"""The console API reads real run folders and never invents numbers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opseval.config import Settings
from opseval.demo import catalogue_dir
from opseval.errors import ConfigError
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


def client_for(app: object) -> TestClient:
    """Always talk to the server as a browser on loopback would.

    The default ``http://testserver`` base url is refused by TrustedHostMiddleware,
    which is the DNS-rebinding guard doing its job.
    """
    return TestClient(app, base_url="http://127.0.0.1")  # type: ignore[arg-type]


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
    return client_for(create_app(settings=settings, catalogue=cat, profile=cat / "profile.yaml"))


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
    rows = client_for(create_app(settings=settings)).get("/api/v1/config")
    assert "sk-very-secret" not in rows.text
    assert any(r["key"] == "opseval_judge_api_key" and r["redacted"] for r in rows.json())


def test_no_catalogue_is_empty_not_an_error(tmp_path: Path) -> None:
    c = client_for(create_app(settings=Settings(reports_dir=str(tmp_path))))
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
    c = client_for(
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
    c = client_for(
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
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as http,
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
    c = client_for(
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


# --- the blockers found in review of the first version --------------------------------


def test_a_foreign_host_header_is_refused(tmp_path: Path) -> None:
    """DNS rebinding: a page on evil.com re-resolving to 127.0.0.1 is same-origin with
    itself, so CORS never runs. The Host header is what refuses it."""
    app = create_app(settings=Settings(reports_dir=str(tmp_path)))
    assert TestClient(app, base_url="http://evil.com").get("/api/v1/runs").status_code == 400
    assert TestClient(app, base_url="http://127.0.0.1").get("/api/v1/runs").status_code == 200


def test_a_token_is_required_when_one_is_set(tmp_path: Path) -> None:
    app = create_app(settings=Settings(reports_dir=str(tmp_path)), token="s3cret")
    c = client_for(app)
    assert c.get("/api/v1/runs").status_code == 401
    assert c.get("/api/v1/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/v1/runs", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_the_commit_guard_is_the_operators_not_the_callers(tmp_path: Path) -> None:
    """ADR-0008 needs two keys. The request body must not be able to supply one."""
    cat = catalogue_dir()
    app = create_app(
        settings=Settings(reports_dir=str(tmp_path), allowed_hosts="127.0.0.1"),
        catalogue=cat,
        profile=cat / "profile.yaml",
        target="ws://127.0.0.1:1",
    )
    c = client_for(app)
    refused = c.post("/api/v1/runs", json={"suite": "smoke", "allowCommit": True})
    assert refused.status_code == 422  # extra="forbid": the field does not exist
    assert c.post("/api/v1/runs", json={"suite": "smoke"}).json()["allowCommit"] is False

    permitted = create_app(
        settings=Settings(reports_dir=str(tmp_path), allowed_hosts="127.0.0.1"),
        catalogue=cat,
        profile=cat / "profile.yaml",
        target="ws://127.0.0.1:1",
        allow_commit=True,
    )
    assert client_for(permitted).post("/api/v1/runs", json={"suite": "smoke"}).json()["allowCommit"]


def test_unknown_request_fields_are_refused(tmp_path: Path) -> None:
    cat = catalogue_dir()
    c = client_for(
        create_app(
            settings=Settings(reports_dir=str(tmp_path)),
            catalogue=cat,
            profile=cat / "profile.yaml",
            target="ws://127.0.0.1:1",
        )
    )
    assert c.post("/api/v1/runs", json={"suite": "smoke", "repeats": 999}).status_code == 422
    assert c.post("/api/v1/runs", json={"suite": "smoke", "nonsense": 1}).status_code == 422


async def test_a_launch_that_dies_is_an_error_never_perpetual_progress(tmp_path: Path) -> None:
    """The 202 has already gone out, so the failure has to surface in /runs."""
    import asyncio

    import httpx

    cat = catalogue_dir()
    app = create_app(
        settings=Settings(reports_dir=str(tmp_path), allowed_hosts="127.0.0.1"),
        catalogue=cat,
        profile=cat / "profile.yaml",
        # Nothing is listening here, and the host is not allowlisted for TLS either.
        target="ws://127.0.0.1:9",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as http:
        assert (
            await http.post("/api/v1/runs", json={"suite": "smoke", "repeats": 1})
        ).status_code == 202
        for _ in range(100):
            runs = (await http.get("/api/v1/runs")).json()
            if runs and runs[0]["state"] == "error":
                break
            await asyncio.sleep(0.1)
    assert runs[0]["state"] == "error", (
        f"still {runs[0]['state']!r}: a dead run still reads as running"
    )
    assert runs[0]["gate"] == "harness_error"  # never "failed": this is not the agent
    assert runs[0]["reason"]


def test_gate_states_follow_the_exit_codes(client: TestClient) -> None:
    """Exit 1 used to render as "judged" (pass-coloured) and exit 3 as "blocked" (the
    same red as a failure). That is the one rule, inverted, where people look."""
    gates = {r["id"]: r["gate"] for r in client.get("/api/v1/runs").json()}
    assert gates["r1"] == "passed" and gates["r2"] == "failed" and gates["r3"] == "not_judged"


def test_the_bundled_demo_catalogue_is_read_only(tmp_path: Path) -> None:
    """With no --catalogue the server shows the demo. Writing would edit site-packages."""
    cat = catalogue_dir()
    c = client_for(
        create_app(
            settings=Settings(reports_dir=str(tmp_path)),
            catalogue=cat,
            profile=cat / "profile.yaml",
        )
    )
    assert c.get("/api/v1/health").json()["catalogueWritable"] is False
    suite = c.put("/api/v1/suites", json={"file": "x", "displayName": "x", "include": ["*.yaml"]})
    assert suite.status_code == 409 and suite.json()["code"] == "READ_ONLY_CATALOGUE"
    imported = c.post(
        "/api/v1/scenarios/import", json={"files": [{"name": "a.yaml", "text": "{}"}]}
    )
    assert imported.status_code == 409


def test_saving_a_suite_never_silently_replaces_one(tmp_path: Path) -> None:
    import shutil

    cat = tmp_path / "cat"
    shutil.copytree(catalogue_dir(), cat)
    c = client_for(
        create_app(
            settings=Settings(reports_dir=str(tmp_path / "r")),
            catalogue=cat,
            profile=cat / "profile.yaml",
        )
    )
    before = (cat / "suites" / "smoke.yaml").read_text()
    clash = c.put(
        "/api/v1/suites", json={"file": "smoke", "displayName": "mine", "include": ["*.yaml"]}
    )
    assert clash.status_code == 409 and clash.json()["code"] == "SUITE_EXISTS"
    assert (cat / "suites" / "smoke.yaml").read_text() == before
    ok = c.put(
        "/api/v1/suites",
        json={
            "file": "smoke",
            "displayName": "mine",
            "include": ["scenarios/rf-001*.yaml"],
            "overwrite": True,
        },
    )
    assert ok.status_code == 200 and ok.json()["scenarioCount"] == 1


def test_an_unresolvable_suite_does_not_overwrite_a_working_one(tmp_path: Path) -> None:
    """Validation used to happen after the write, so a bad save destroyed a good file."""
    import shutil

    cat = tmp_path / "cat"
    shutil.copytree(catalogue_dir(), cat)
    c = client_for(
        create_app(
            settings=Settings(reports_dir=str(tmp_path / "r")),
            catalogue=cat,
            profile=cat / "profile.yaml",
        )
    )
    before = (cat / "suites" / "smoke.yaml").read_text()
    bad = c.put(
        "/api/v1/suites",
        json={"file": "smoke", "displayName": "x", "include": [], "overwrite": True},
    )
    assert bad.status_code == 422
    assert (cat / "suites" / "smoke.yaml").read_text() == before
    assert not list((cat / "suites").glob(".*checking*"))  # no scratch file left behind


def test_runs_are_newest_first_whatever_their_source(tmp_path: Path) -> None:
    """A failed launch used to be pinned above every run that finished after it."""
    from opseval.server.launches import LaunchBook
    from opseval.server.views import list_runs

    store = FileStore(tmp_path)
    store.save_result(_result("old", 1, {"a": VerdictStatus.PASSED}))
    book = LaunchBook()
    dead = book.start("smoke")
    book.failed(dead.token, "ConfigError: unreachable")
    store.save_result(_result("new", 59, {"a": VerdictStatus.PASSED}))
    ids = [r.id for r in list_runs(store, book)]
    # The launch failed just now, so it leads; the 10:59 run still beats the 10:01 one.
    assert ids == [f"launch-{dead.token}", "new", "old"]


# --- found reviewing the fixes themselves ---------------------------------------------


def test_an_empty_token_is_no_token_not_a_token_equal_to_empty(tmp_path: Path) -> None:
    """`--token "$UNSET"` must not produce a server that authenticates everyone.

    secrets.compare_digest("", "") is True, so a blank token left as a string would
    accept a request carrying no credentials at all — while the CLI's off-loopback
    refusal was skipped because the value was not None.
    """
    for blank in ("", "   "):
        app = create_app(settings=Settings(reports_dir=str(tmp_path)), token=blank)
        c = client_for(app)
        # Treated as "no token": an ordinary open loopback server, and crucially a
        # request with no Authorization header is not being *authenticated* by it.
        assert c.get("/api/v1/runs").status_code == 200


def test_a_non_ascii_token_is_refused_at_startup_not_per_request(tmp_path: Path) -> None:
    """compare_digest raises on non-ASCII: every request would be a 500."""
    with pytest.raises(ConfigError):
        create_app(settings=Settings(reports_dir=str(tmp_path)), token="tökén")


def test_a_cors_preflight_is_not_refused_for_having_no_token(tmp_path: Path) -> None:
    """A preflight carries no credentials by design. A bare 401 here reaches the browser
    as a CORS failure and the real request is never sent."""
    c = client_for(create_app(settings=Settings(reports_dir=str(tmp_path)), token="s3cret"))
    pre = c.options(
        "/api/v1/runs",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert pre.status_code == 200 and "access-control-allow-origin" in pre.headers


def test_a_selection_refuses_rather_than_returning_not_judged(tmp_path: Path) -> None:
    """A filter picks which scenarios run, but the whole directory is still read, so an
    unrelated broken file forces exit 2 and the chosen scenario's real verdict is lost."""
    import shutil

    cat = tmp_path / "cat"
    shutil.copytree(catalogue_dir(), cat)
    (cat / "scenarios" / "broken.yaml").write_text("id: nope\nthis is: not a scenario\n")
    c = client_for(
        create_app(
            settings=Settings(reports_dir=str(tmp_path / "r"), allowed_hosts="127.0.0.1"),
            catalogue=cat,
            profile=cat / "profile.yaml",
            target="ws://127.0.0.1:1",
        )
    )
    refused = c.post("/api/v1/runs", json={"scenarios": ["rf-001"]})
    assert refused.status_code == 409 and refused.json()["code"] == "CATALOGUE_HAS_ERRORS"
    assert "broken.yaml" in refused.json()["message"]
    # A suite scopes its own files through include, so it is unaffected.
    assert c.post("/api/v1/runs", json={"suite": "smoke"}).status_code == 202


def test_the_suite_scratch_file_is_never_listed_as_a_suite(tmp_path: Path) -> None:
    """pathlib's *.yaml matches dotfiles, so a .yaml-suffixed scratch file appears in
    the suites list while it exists, and for ever if validation crashes."""
    import shutil

    cat = tmp_path / "cat"
    shutil.copytree(catalogue_dir(), cat)
    (cat / "suites" / ".leftover.checking.tmp").write_text("name: ghost\ninclude: []\n")
    files = {
        s["file"]
        for s in client_for(
            create_app(
                settings=Settings(reports_dir=str(tmp_path / "r")),
                catalogue=cat,
                profile=cat / "profile.yaml",
            )
        )
        .get("/api/v1/suites")
        .json()
    }
    assert files == {"smoke", "full"}


def test_pruning_never_forgets_a_launch_that_is_still_running() -> None:
    """Evicting by age alone drops a long-running launch, and its later crash then has
    nowhere to land — back to a folder reading 'running' for ever."""
    from opseval.server import launches as mod
    from opseval.server.launches import LaunchBook

    book = LaunchBook()
    long_running = book.start("slow")
    for _ in range(mod.MAX_REMEMBERED * 2):
        done = book.start("x")
        book.failed(done.token, "boom")
    book.attach(long_running.token, "run-1")
    book.failed(long_running.token, "died late")
    assert book.failures() == {"run-1": "died late"}
