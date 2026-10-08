# SPDX-License-Identifier: Apache-2.0
"""The FastAPI application behind ``opseval serve``."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response

from opseval import __version__
from opseval.catalogue import load_profile
from opseval.config import Settings
from opseval.errors import ConfigError
from opseval.models.profile import SutProfile
from opseval.plugins import load_scorers
from opseval.server import actions, views
from opseval.server import schemas as s
from opseval.server.launches import LaunchBook
from opseval.storage import FileStore

API = "/api/v1"

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
"""The only Host values a local server answers to.

Binding to loopback is **not** by itself protection: with DNS rebinding, a page on
``evil.com`` that re-resolves to 127.0.0.1 is *same-origin* with itself, so no CORS check
ever runs and the request reaches this server carrying ``Host: evil.com``. Checking the
Host header is what refuses it. Without this, any page the operator visits could start
runs against their agent and spend their LLM credit.
"""


def create_app(
    *,
    settings: Settings | None = None,
    catalogue: Path | None = None,
    profile: str | Path | None = None,
    target: str | None = None,
    allow_commit: bool = False,
    token: str | None = None,
    extra_hosts: tuple[str, ...] = (),
    dev_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173"),
) -> FastAPI:
    settings = settings or Settings()
    # An empty or blank token is *no* token, not a token equal to "". Left as a string it
    # would authenticate a request that sent no credentials at all, and callers get one
    # from an unset environment variable (`--token "$OPSEVAL_TOKEN"`) without noticing.
    token = token.strip() if token else None
    if token is not None and not token.isascii():
        # secrets.compare_digest raises on non-ASCII strings, which would turn every
        # request into a 500 — an unusable server rather than a refused one.
        raise ConfigError("--token must be ASCII")
    store = FileStore(settings.reports_dir)
    # Before any catalogue is read: a suite may name a plugin's scorer, and without this
    # the console would report it as an unknown scorer while the CLI accepts it.
    load_scorers()
    loaded: SutProfile | None = load_profile(profile) if profile else None
    launches = LaunchBook()
    app = FastAPI(title="Opseval API", version=__version__)
    app.add_middleware(
        CORSMiddleware, allow_origins=list(dev_origins), allow_methods=["*"], allow_headers=["*"]
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*LOOPBACK_HOSTS, *extra_hosts])

    @app.middleware("http")
    async def _require_token(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # A CORS preflight carries no credentials by design, so demanding one here
        # answers it with a bare 401 and no CORS headers — the browser then reports a
        # CORS failure and the real request is never sent.
        if token is not None and request.method != "OPTIONS" and request.url.path.startswith(API):
            header = request.headers.get("authorization", "")
            offered = header[7:] if header.lower().startswith("bearer ") else ""
            if not secrets.compare_digest(offered.encode(), token.encode()):
                return JSONResponse(
                    status_code=401,
                    content={
                        "code": "UNAUTHORIZED",
                        "message": "send Authorization: Bearer <token>",
                    },
                )
        return await call_next(request)

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
            allow_commit=allow_commit,
            catalogue_writable=actions.is_writable(catalogue),
        )

    @app.get(f"{API}/runs")
    def runs() -> list[s.RunSummary]:
        return views.list_runs(store, launches)

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
        return actions.start_run(
            settings,
            catalogue,
            loaded,
            profile or "",
            target,
            body,
            allow_commit=allow_commit,
            launches=launches,
        )

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
