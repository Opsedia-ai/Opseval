# SPDX-License-Identifier: Apache-2.0
"""The FastAPI application behind ``opseval serve`` (ADR-0029, ADR-0031).

The API can start runs that hold the operator's agent token and LLM keys, so every
request has to prove it came from the operator, not from a web page they happen to have
open:

- **Host check.** Only loopback names are served. A page that rebinds its own hostname to
  127.0.0.1 still sends its hostname in ``Host`` and is refused (DNS rebinding).
- **Bearer token** on every ``/api`` request, minted per start unless configured.
- **Origin check** on writes: a browser always sends ``Origin`` on a cross-site POST.
- **No CORS.** The console is served same-origin, or through the Vite dev proxy.
"""

from __future__ import annotations

import hmac
import re
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from opseval import __version__
from opseval.catalogue import load_profile
from opseval.config import Settings
from opseval.plugins import load_scorers
from opseval.server import actions, views
from opseval.server import schemas as s
from opseval.storage import FileStore

API = "/api/v1"
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def host_name(header: str) -> str:
    """The host part of a ``Host`` header: ``[::1]:8000`` -> ``::1``, ``a:80`` -> ``a``."""
    header = header.strip().lower()
    if header.startswith("["):
        return header[1 : header.find("]")] if "]" in header else ""
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def _deny(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def _run_id(run_id: str) -> str:
    if not RUN_ID.match(run_id) or ".." in run_id:
        raise HTTPException(404, f"no run {run_id!r}")
    return run_id


def create_app(
    *,
    settings: Settings | None = None,
    catalogue: Path | None = None,
    profile: str | Path | None = None,
    target: str | None = None,
    token: str | None = None,
    read_only: bool = False,
    allow_commit: bool = False,
    allowed_hosts: frozenset[str] = LOOPBACK_NAMES,
) -> FastAPI:
    settings = settings or Settings()
    # A suite may name a plugin's scorer; without this the console would reject it as
    # UNKNOWN_SCORER and the checks page would hide it. A broken plugin fails the start.
    load_scorers()
    if token is None:
        configured = settings.serve_token
        token = configured.get_secret_value() if configured else secrets.token_urlsafe(24)
    ctx = actions.ServerContext(
        settings=settings,
        store=FileStore(settings.reports_dir),
        catalogue=catalogue,
        profile=load_profile(profile) if profile else None,
        profile_path=profile,
        target=target,
        read_only=read_only,
        allow_commit=allow_commit,
    )
    app = FastAPI(title="Opseval API", version=__version__)
    app.state.token = token
    app.state.ctx = ctx
    expected = f"Bearer {token}".encode()

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if host_name(request.headers.get("host", "")) not in allowed_hosts:
            return _deny(403, "HOST_NOT_ALLOWED", "opseval serve answers on loopback names only")
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if (
                request.method in UNSAFE_METHODS
                and origin is not None
                and host_name(origin.split("://", 1)[-1]) not in allowed_hosts
            ):
                return _deny(403, "ORIGIN_NOT_ALLOWED", "cross-site requests are refused")
            given = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(given, expected):
                return _deny(
                    401,
                    "UNAUTHORIZED",
                    "open the console from the link `opseval serve` printed (it carries the "
                    "token), or send Authorization: Bearer <token>",
                )
        return await call_next(request)

    @app.exception_handler(actions.ApiError)
    def _api_error(_: Request, exc: actions.ApiError) -> JSONResponse:
        return _deny(exc.status, exc.code, exc.message)

    @app.get(f"{API}/health")
    def health() -> s.Health:
        return s.Health(
            status="ok",
            version=__version__,
            reports_dir=str(ctx.store.root),
            catalogue=str(catalogue) if catalogue else None,
            profile=ctx.profile.name if ctx.profile else None,
            read_only=read_only,
            allow_commit=allow_commit,
        )

    @app.get(f"{API}/runs")
    def runs() -> list[s.RunSummary]:
        return views.list_runs(ctx.store)

    @app.get(API + "/runs/{run_id}")
    def run(run_id: str) -> s.RunDetail:
        try:
            detail = views.run_detail(ctx.store, _run_id(run_id))
        except views.RunInProgress as exc:
            raise actions.ApiError(
                409, "RUN_IN_PROGRESS", f"run {run_id!r} has not finished yet"
            ) from exc
        if detail is None:
            raise actions.ApiError(404, "UNKNOWN_RUN", f"no run {run_id!r}")
        return detail

    @app.get(f"{API}/compare")
    def compare(baseline: str, candidate: str) -> s.Comparison:
        result = views.compare(ctx.store, _run_id(baseline), _run_id(candidate))
        if result is None:
            raise HTTPException(404, "both runs must exist and be finished")
        return result

    @app.get(f"{API}/trend")
    def trend() -> list[s.TrendPoint]:
        return views.trend(ctx.store)

    @app.get(f"{API}/usage")
    def usage() -> list[s.RunUsage]:
        return views.usage(ctx.store)

    @app.get(f"{API}/scenarios")
    def scenarios() -> s.CatalogueOut:
        return views.load_scenarios(catalogue, ctx.profile)

    @app.get(f"{API}/suites")
    def suites() -> list[s.SuiteOut]:
        return views.list_suites(catalogue, ctx.profile)

    @app.get(f"{API}/checks")
    def checks() -> list[s.CheckOut]:
        return views.checks()

    @app.get(f"{API}/readiness")
    def readiness() -> list[s.ReadinessItem]:
        return views.readiness(settings, catalogue, ctx.profile)

    @app.get(f"{API}/config")
    def config() -> list[s.SettingRow]:
        return views.settings_rows(settings)

    @app.get(API + "/scenarios/{scenario_id}/persona")
    def persona(scenario_id: str) -> s.PersonaOut:
        return actions.persona(ctx, scenario_id)

    @app.post(f"{API}/simulator/rehearse")
    async def rehearse(body: s.RehearseIn) -> s.RehearseOut:
        return await actions.rehearse(ctx, body)

    @app.post(f"{API}/runs", status_code=202)
    def start(body: s.LaunchIn) -> s.LaunchOut:
        return actions.start_run(ctx, body)

    @app.put(f"{API}/suites")
    def save_suite(body: s.SuiteIn) -> s.SuiteOut:
        return actions.save_suite(ctx, body)

    @app.post(f"{API}/scenarios/import")
    def import_scenarios(body: s.ImportIn) -> s.ImportOut:
        out = actions.import_scenarios(ctx, body)
        if out.problems:
            raise HTTPException(422, detail=[p.model_dump(by_alias=True) for p in out.problems])
        return out

    return app
