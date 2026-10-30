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

When a built console is found, it is served from ``/`` on the same origin as the API
(ADR-0034): one link, one port, and no Node needed to use it.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from opseval import __version__
from opseval.analytics import FileAnalytics, UnsupportedFilter, UsageFilter
from opseval.analytics.models import Bucket
from opseval.catalogue import load_profile
from opseval.config import Settings
from opseval.errors import ConfigError
from opseval.plugins import load_scorers
from opseval.server import actions, lookup, views
from opseval.server import audio as audio_api
from opseval.server import schemas as s
from opseval.server.lookup import safe_id
from opseval.storage import FileStore

API = "/api/v1"
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
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
    if not safe_id(run_id):
        raise HTTPException(404, f"no run {run_id!r}")
    return run_id


CONSOLE_HEADERS = {
    # The console can start runs and edit suites: it must never be framed by another site
    # (clickjacking), and it loads nothing from anywhere but its own origin, fonts included.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
        # Recorded audio is fetched with the token and played from a blob: URL (ADR-0053).
        "media-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; font-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def find_console(settings: Settings) -> Path | None:
    """The built console to serve: ``OPSEVAL_CONSOLE_DIR``, or a source checkout's build
    (``<repo>/packages/console/dist``), or ``None`` (only the API is served).

    A configured folder without ``index.html`` is a configuration error, never silently
    ignored: the operator asked for a console and must hear why there is none.
    """
    if settings.console_dir:
        configured = Path(settings.console_dir).resolve()
        if not (configured / "index.html").is_file():
            raise ConfigError(
                f"OPSEVAL_CONSOLE_DIR={settings.console_dir}: no index.html there; point it at "
                "the console build (packages/console/dist after `npm run build`)"
            )
        return configured
    parents = Path(__file__).resolve().parents
    # <repo>/packages/opseval/src/opseval/server/app.py: only a source checkout has this.
    if len(parents) > 4 and parents[4].name == "packages":
        candidate = parents[4] / "console" / "dist"
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    return None


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


class _Console:
    """Serves the built console for requests the API did not answer (ADR-0034).

    It is consulted only after routing found nothing, for GET and HEAD outside /api, so
    the API keeps its own behaviour exactly (slash redirects, 405s, 404s). Files come
    from Starlette's ``StaticFiles`` (traversal, NUL bytes, HEAD and ranges handled
    there); any other path gets index.html, because the console routes in the browser.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.files = StaticFiles(directory=self.root)
        self.index = self.root / "index.html"

    async def respond(self, request: Request) -> Response:
        path = request.url.path.lstrip("/")
        if path and path != "index.html":
            try:
                response = await self.files.get_response(path, request.scope)
            except (HTTPException, StarletteHTTPException):
                response = None
            if response is not None and response.status_code == 200:
                if path.startswith("assets/"):  # content-hashed by the build
                    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return response
        page = FileResponse(self.index)
        page.headers["Cache-Control"] = "no-store"
        return page


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
    console_dir: Path | None = None,
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
    # No unauthenticated /docs or /openapi.json: the schema lives under /api, behind the token.
    app = FastAPI(
        title="Opseval API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=f"{API}/openapi.json",
    )
    console_root = console_dir.resolve() if console_dir is not None else find_console(settings)
    console = _Console(console_root) if console_root is not None else None
    app.state.console = console_root
    app.state.token = token
    app.state.ctx = ctx
    expected = f"Bearer {token}".encode()
    audio_key = audio_api.ServerKey(settings)

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
        response = await call_next(request)
        if _is_api(request.url.path):
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        if (
            console is not None
            and response.status_code == 404
            and request.method
            in (
                "GET",
                "HEAD",
            )
        ):
            response = await console.respond(request)
        # Every non-API response, not only index.html: any page on this origin can read the
        # token the console keeps, so none of them may be framed or run foreign scripts.
        response.headers.update(CONSOLE_HEADERS)
        return response

    @app.exception_handler(actions.ApiError)
    def _api_error(_: Request, exc: actions.ApiError) -> JSONResponse:
        return _deny(exc.status, exc.code, exc.message)

    @app.exception_handler(UnsupportedFilter)
    def _unsupported_filter(_: Request, exc: UnsupportedFilter) -> JSONResponse:
        # Refused, never ignored: a dashboard that dropped a filter would show every run as
        # if it were the ones asked for (ADR-0052).
        return _deny(400, "FILTER_NEEDS_SERVER_MODE", str(exc))

    @app.exception_handler(views.RunInProgress)
    def _in_progress(_: Request, exc: views.RunInProgress) -> JSONResponse:
        # The progress rides along, so a page waiting on this run needs no second poll.
        progress = exc.progress.model_dump(by_alias=True) if exc.progress else None
        return JSONResponse(
            status_code=409,
            content={
                "code": "RUN_IN_PROGRESS",
                "message": f"run {exc} has not finished yet",
                "detail": {"progress": progress},
            },
        )

    @app.exception_handler(RequestValidationError)
    def _bad_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        # A body the API's own models refuse, said as problems at their fields like the
        # loader's, so the console can point at the field. The value sent is never echoed.
        problems = [
            {
                "path": "request",
                "field": ".".join(str(part) for part in err.get("loc", ())[1:]),
                "message": str(err.get("msg", "invalid")),
            }
            for err in exc.errors()
        ]
        first = problems[0] if problems else {"field": "", "message": "invalid request"}
        return JSONResponse(
            status_code=422,
            content={
                "code": "INVALID_REQUEST",
                "message": f"{first['field']}: {first['message']}".lstrip(": "),
                "detail": problems,
            },
        )

    @app.exception_handler(StarletteHTTPException)
    def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Every API error carries a machine code the console can act on, including the
        # router's own 404s. `detail` is kept for the import route's list of problems.
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code)
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        body: dict[str, object] = {
            "code": code or f"HTTP_{exc.status_code}",
            "message": message,
            "detail": exc.detail,
        }
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    @app.get(f"{API}/health")
    def health() -> s.Health:
        return s.Health(
            status="ok",
            version=__version__,
            reports_dir=ctx.store.where(),
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
        detail = views.run_detail(ctx.store, _run_id(run_id))  # RunInProgress: 409
        if detail is None:
            raise actions.ApiError(404, "UNKNOWN_RUN", f"no run {run_id!r}")
        return detail

    @app.get(API + "/runs/{run_id}/attempts/{test_id}/{index}")
    def attempt(run_id: str, test_id: str, index: int) -> s.AttemptRecordOut:
        record = lookup.attempt(ctx, run_id, test_id, index)  # read once, for both
        out = views.attempt_record(ctx.store, run_id, test_id, index, record=record)
        if out is None:  # pragma: no cover - the record was just read
            raise actions.ApiError(
                404, "UNKNOWN_ATTEMPT", f"no record of {test_id} attempt {index}"
            )
        return audio_api.with_audio(out, ctx, record, audio_key)

    audio_api.register(app, ctx, audio_key, API)

    @app.get(f"{API}/compare")
    def compare(baseline: str, candidate: str) -> s.Comparison:
        result = views.compare(ctx.store, _run_id(baseline), _run_id(candidate))
        if result is None:
            raise HTTPException(404, "both runs must exist and be finished")
        return result

    @app.get(f"{API}/trend")
    def trend() -> list[s.TrendPoint]:
        return views.trend(ctx.store)

    @app.get(f"{API}/trend/scenarios")
    def scenario_trend(
        suite: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> s.ScenarioTrend:
        return views.scenario_trend(ctx.store, suite=suite, limit=limit)

    @app.get(f"{API}/usage")
    def usage() -> list[s.RunUsage]:
        return views.usage(ctx.store)

    analytics = FileAnalytics(ctx.store)

    def usage_filter(
        suite: str | None,
        model: str | None,
        role: str | None,
        since: date | None,
        until: date | None,
        bucket: Bucket,
        org: str | None,
        project: str | None,
        task: str | None,
        label: str | None,
    ) -> UsageFilter:
        if since is not None and until is not None and since > until:
            raise actions.ApiError(422, "BAD_RANGE", "since is after until")
        return UsageFilter(
            suite=suite or None,
            model=model or None,
            role=role or None,
            since=since,
            until=until,
            bucket=bucket,
            org=org or None,
            project=project or None,
            task=task or None,
            label=label or None,
        )

    @app.get(f"{API}/analytics/cost")
    def cost_dashboard(
        suite: str | None = None,
        model: str | None = None,
        role: str | None = None,
        since: date | None = None,
        until: date | None = None,
        bucket: Bucket = "day",
        org: str | None = None,
        project: str | None = None,
        task: str | None = None,
        label: str | None = None,
    ) -> s.CostDashboard:
        f = usage_filter(suite, model, role, since, until, bucket, org, project, task, label)
        return views.cost_dashboard(analytics, f)

    @app.get(f"{API}/analytics/tokens")
    def tokens_dashboard(
        suite: str | None = None,
        model: str | None = None,
        role: str | None = None,
        since: date | None = None,
        until: date | None = None,
        bucket: Bucket = "day",
        org: str | None = None,
        project: str | None = None,
        task: str | None = None,
        label: str | None = None,
    ) -> s.TokensDashboard:
        f = usage_filter(suite, model, role, since, until, bucket, org, project, task, label)
        return views.tokens_dashboard(analytics, f)

    @app.get(f"{API}/scenarios")
    def scenarios() -> s.CatalogueOut:
        return views.load_scenarios(catalogue, ctx.profile)

    @app.get(f"{API}/suites")
    def suites() -> list[s.SuiteOut]:
        return views.list_suites(catalogue, ctx.profile, fail_on_flaky=settings.gate_fail_on_flaky)

    @app.get(f"{API}/gate")
    def gate() -> s.GateCatalogOut:
        return views.gate_catalog()

    @app.post(f"{API}/gate/resolve")
    def gate_resolve(body: s.GateIn) -> s.GateOut:
        return actions.preview_gate(ctx, body)

    @app.get(f"{API}/checks")
    def checks() -> list[s.CheckOut]:
        return views.checks()

    @app.get(f"{API}/readiness")
    def readiness() -> list[s.ReadinessItem]:
        return views.readiness(settings, catalogue, ctx.profile)

    @app.get(f"{API}/config")
    def config() -> list[s.SettingRow]:
        return views.settings_rows(settings)

    @app.get(f"{API}/scenarios/form")
    def scenario_form() -> s.FormOut:
        return actions.scenario_form(ctx)

    @app.post(f"{API}/scenarios/draft")
    def draft_scenario(body: s.ScenarioDraftIn) -> s.DraftOut:
        return actions.draft(ctx, body)

    @app.get(f"{API}/scenarios/source")
    def scenario_file(file: str) -> s.ScenarioSourceOut:
        return actions.scenario_source(ctx, file)

    @app.put(f"{API}/scenarios/source")
    def save_scenario_file(file: str, body: s.ScenarioSaveIn) -> s.ScenarioSourceOut:
        return actions.save_scenario_source(ctx, file, body)

    @app.get(API + "/scenarios/{scenario_id}/source")
    def scenario_source(scenario_id: str) -> s.ScenarioSourceOut:
        return actions.scenario_source_by_id(ctx, scenario_id)

    @app.post(f"{API}/scenarios/validate")
    def validate_scenario(body: s.ScenarioTextIn) -> s.ScenarioCheckOut:
        return actions.check_scenario(ctx, body)

    @app.get(API + "/scenarios/{scenario_id}/persona")
    def persona(scenario_id: str) -> s.PersonaOut:
        return actions.persona(ctx, scenario_id)

    @app.post(f"{API}/simulator/rehearse")
    async def rehearse(body: s.RehearseIn) -> s.RehearseOut:
        return await actions.rehearse(ctx, body)

    @app.post(f"{API}/runs", status_code=202)
    def start(body: s.LaunchIn) -> s.LaunchOut:
        return actions.start_run(ctx, body)

    @app.post(API + "/runs/{run_id}/cancel", status_code=202)
    def cancel(run_id: str) -> s.CancelOut:
        return actions.cancel_run(ctx, _run_id(run_id))

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
