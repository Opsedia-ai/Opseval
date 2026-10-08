# SPDX-License-Identifier: Apache-2.0
"""The FastAPI application behind ``opseval serve``."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from opseval import __version__
from opseval.catalogue import load_profile
from opseval.config import Settings
from opseval.models.profile import SutProfile
from opseval.server import actions, views
from opseval.server import schemas as s
from opseval.storage import FileStore

API = "/api/v1"


def create_app(
    *,
    settings: Settings | None = None,
    catalogue: Path | None = None,
    profile: str | Path | None = None,
    target: str | None = None,
    dev_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173"),
) -> FastAPI:
    settings = settings or Settings()
    store = FileStore(settings.reports_dir)
    loaded: SutProfile | None = load_profile(profile) if profile else None
    app = FastAPI(title="Opseval API", version=__version__)
    app.add_middleware(
        CORSMiddleware, allow_origins=list(dev_origins), allow_methods=["*"], allow_headers=["*"]
    )

    @app.exception_handler(actions.ApiError)
    def _api_error(_: Request, exc: actions.ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"code": exc.code, "message": exc.message}
        )

    @app.get(f"{API}/health")
    def health() -> s.Health:
        return s.Health(
            status="ok",
            version=__version__,
            reports_dir=str(store.root),
            catalogue=str(catalogue) if catalogue else None,
            profile=loaded.name if loaded else None,
        )

    @app.get(f"{API}/runs")
    def runs() -> list[s.RunSummary]:
        return views.list_runs(store)

    @app.get(API + "/runs/{run_id}")
    def run(run_id: str) -> s.RunDetail:
        detail = views.run_detail(store, run_id)
        if detail is None:
            raise HTTPException(404, f"no finished run {run_id!r}")
        return detail

    @app.get(f"{API}/compare")
    def compare(baseline: str, candidate: str) -> s.Comparison:
        result = views.compare(store, baseline, candidate)
        if result is None:
            raise HTTPException(404, "both runs must exist and be finished")
        return result

    @app.get(f"{API}/trend")
    def trend() -> list[s.TrendPoint]:
        return views.trend(store)

    @app.get(f"{API}/usage")
    def usage() -> list[s.RunUsage]:
        return views.usage(store)

    @app.get(f"{API}/scenarios")
    def scenarios() -> s.CatalogueOut:
        return views.load_scenarios(catalogue, loaded)

    @app.get(f"{API}/suites")
    def suites() -> list[s.SuiteOut]:
        return views.list_suites(catalogue, loaded)

    @app.get(f"{API}/checks")
    def checks() -> list[s.CheckOut]:
        return views.checks()

    @app.get(f"{API}/readiness")
    def readiness() -> list[s.ReadinessItem]:
        return views.readiness(settings, catalogue, loaded)

    @app.get(f"{API}/config")
    def config() -> list[s.SettingRow]:
        return views.settings_rows(settings)

    @app.get(API + "/scenarios/{scenario_id}/persona")
    def persona(scenario_id: str) -> s.PersonaOut:
        return actions.persona(settings, catalogue, loaded, scenario_id)

    @app.post(f"{API}/simulator/rehearse")
    async def rehearse(body: s.RehearseIn) -> s.RehearseOut:
        return await actions.rehearse(settings, catalogue, loaded, body)

    @app.post(f"{API}/runs", status_code=202)
    async def start(body: s.LaunchIn) -> s.LaunchOut:
        return actions.start_run(settings, catalogue, loaded, profile or "", target, body)

    @app.put(f"{API}/suites")
    def save_suite(body: s.SuiteIn) -> s.SuiteOut:
        return actions.save_suite(catalogue, loaded, body)

    @app.post(f"{API}/scenarios/import")
    def import_scenarios(body: s.ImportIn) -> s.ImportOut:
        out = actions.import_scenarios(catalogue, loaded, body)
        if out.problems:
            raise HTTPException(422, detail=[p.model_dump(by_alias=True) for p in out.problems])
        return out

    return app
