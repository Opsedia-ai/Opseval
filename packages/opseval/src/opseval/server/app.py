# SPDX-License-Identifier: Apache-2.0
"""The FastAPI application behind ``opseval serve`` (ADR-0029, ADR-0031, ADR-0050).

The API can start runs that hold the operator's agent token and LLM keys, so every
request has to prove who it comes from, not merely that a web page could send it.

**Shared-token mode** (ADR-0031, no database and no public URL):

- **Host check.** Only loopback names are served. A page that rebinds its own hostname to
  127.0.0.1 still sends its hostname in ``Host`` and is refused (DNS rebinding).
- **Bearer token** on every ``/api`` request, minted per start unless configured.
- **Origin check** on writes: a browser always sends ``Origin`` on a cross-site POST.

**Accounts mode** (ADR-0050, a database or a public URL is configured): sessions (a
``__Host-`` cookie behind https, ``opseval-session`` on plain-http loopback) and API keys
authenticate; the shared token is off. An ``Authorization``
header is authoritative: a bad key is 401 even beside a valid cookie. A cookie-authenticated
write needs a same-origin ``Origin`` and the session's ``X-CSRF-Token``. With a public URL,
``Host`` must be its host, HSTS is sent, and forwarded headers count only from a trusted proxy.

In both modes every ``/api`` route is listed in :data:`ROUTE_NEEDS` with what it needs, and
one dependency (``authorize``) applies :func:`opseval.accounts.roles.decide` to it. A route
missing from the table fails the start; a request reaching one anyway is refused.

**No CORS.** The console is served same-origin, or through the Vite dev proxy.

When a built console is found, it is served from ``/`` on the same origin as the API
(ADR-0034): one link, one port, and no Node needed to use it.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from opseval import __version__
from opseval.accounts.roles import OPERATOR, Need, Principal, decide
from opseval.accounts.tokens import same
from opseval.analytics import FileAnalytics, UnsupportedFilter, UsageFilter
from opseval.analytics.models import Bucket
from opseval.catalogue import load_profile
from opseval.config import Settings
from opseval.errors import ConfigError, StorageError
from opseval.plugins import load_scorers
from opseval.server import access as access_mod
from opseval.server import accounts_api, actions, ci_api, lookup, views
from opseval.server import audio as audio_api
from opseval.server import schemas as s
from opseval.server.access import HSTS, LOOPBACK_NAMES, AccessConfig, host_name
from opseval.server.lookup import safe_id
from opseval.storage import FileStore, RunStore

API = "/api/v1"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
log = logging.getLogger("opseval.server")

_RUN = "/runs/{run_id}"
_ATTEMPT = _RUN + "/attempts/{test_id}/{index}"
ROUTE_NEEDS: dict[tuple[str, str], Need] = {
    ("GET", "/health"): Need.READ,
    ("GET", "/runs"): Need.READ,
    ("GET", _RUN): Need.READ,
    ("GET", _ATTEMPT): Need.READ,
    ("GET", _ATTEMPT + "/audio"): Need.READ,
    ("GET", _ATTEMPT + "/audio/call"): Need.READ,
    ("GET", _ATTEMPT + "/audio/{seq}"): Need.READ,
    ("GET", "/compare"): Need.READ,
    ("GET", "/trend"): Need.READ,
    ("GET", "/trend/scenarios"): Need.READ,
    ("GET", "/usage"): Need.READ,
    ("GET", "/analytics/cost"): Need.READ,
    ("GET", "/analytics/tokens"): Need.READ,
    ("GET", "/scenarios"): Need.READ,
    ("GET", "/suites"): Need.READ,
    ("GET", "/gate"): Need.READ,
    ("POST", "/gate/resolve"): Need.READ,  # a preview: nothing is written
    ("GET", "/checks"): Need.READ,
    ("GET", "/readiness"): Need.READ,
    ("GET", "/config"): Need.READ,  # the redacted digest
    ("GET", "/scenarios/form"): Need.READ,
    ("POST", "/scenarios/draft"): Need.CATALOGUE,
    ("GET", "/scenarios/source"): Need.READ,
    ("PUT", "/scenarios/source"): Need.CATALOGUE,
    ("GET", "/scenarios/{scenario_id}/source"): Need.READ,
    ("POST", "/scenarios/validate"): Need.READ,  # checks text, writes nothing
    ("GET", "/scenarios/{scenario_id}/persona"): Need.READ,
    ("POST", "/simulator/rehearse"): Need.START,  # spends the simulator's LLM budget
    ("POST", "/runs"): Need.START,
    ("POST", _RUN + "/cancel"): Need.START,
    ("PUT", "/suites"): Need.CATALOGUE,
    ("POST", "/scenarios/import"): Need.CATALOGUE,
    ("GET", "/openapi.json"): Need.READ,
    # Accounts and access (ADR-0050).
    ("GET", "/auth/session"): Need.PUBLIC,
    ("POST", "/auth/login"): Need.PUBLIC,
    ("POST", "/auth/logout"): Need.SIGNED_IN,
    ("GET", "/auth/oidc/start"): Need.PUBLIC,
    ("GET", "/auth/oidc/callback"): Need.PUBLIC,
    ("GET", "/auth/keys"): Need.MANAGE,
    ("POST", "/auth/keys"): Need.MANAGE,
    ("POST", "/auth/keys/{public_key}/revoke"): Need.MANAGE,
    ("GET", "/auth/members"): Need.MANAGE,
    ("PUT", "/auth/members"): Need.MANAGE,
    ("GET", "/catalogue/labels"): Need.READ,
    ("PUT", "/catalogue/labels"): Need.LABEL,
    ("PUT", "/catalogue/labels/protection"): Need.MANAGE,
    # Reporting CI runs (ADR-0058 E2): `opseval run --server` pulls a label, then uploads.
    ("GET", "/catalogue/export"): Need.READ,  # runs:read covers the catalogue (ADR-0050)
    ("PUT", _RUN + "/upload"): Need.START,  # runs:start: "report a CI run" (ADR-0050)
}
"""What every ``/api/v1`` route needs (method, path below ``/api/v1``). The one table
authorization reads; a route not in it fails the start (deny by default)."""
SELF_AUDITED = frozenset(
    path for (_m, path) in ROUTE_NEEDS if path.startswith(("/auth/", "/catalogue/labels"))
) | {_RUN + "/upload"}
"""Writes that record their own, more specific audit event."""


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
    store: RunStore | None = None,
    close_store: bool = False,
    access: AccessConfig | None = None,
) -> FastAPI:
    """The API. ``access`` set is accounts mode (ADR-0050); unset, shared-token mode."""
    settings = settings or Settings()
    # A suite may name a plugin's scorer; without this the console would reject it as
    # UNKNOWN_SCORER and the checks page would hide it. A broken plugin fails the start.
    load_scorers()
    if access is not None:
        token = None  # accounts mode: only sessions and API keys authenticate
    elif token is None:
        configured = settings.serve_token
        token = configured.get_secret_value() if configured else secrets.token_urlsafe(24)
    ctx = actions.ServerContext(
        settings=settings,
        store=store or FileStore(settings.reports_dir),
        catalogue=catalogue,
        profile=load_profile(profile) if profile else None,
        profile_path=profile,
        target=target,
        read_only=read_only,
        allow_commit=allow_commit,
    )

    # No unauthenticated /docs or /openapi.json: the schema lives under /api, behind the
    # same authorization as every other route.
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            # The server's own store (server mode): its connections and loop go with it.
            close = getattr(ctx.store, "close", None)
            if close_store and callable(close):
                close()

    async def authorize(request: Request) -> None:
        """The one authorization check, on every route (deny by default)."""
        route = request.scope.get("route")
        path = getattr(route, "path", "")
        method = "GET" if request.method == "HEAD" else request.method
        need = ROUTE_NEEDS.get((method, path.removeprefix(API)))
        if need is None or not path.startswith(API):
            log.error("no authorization policy for %s %s: refused", method, path)
            raise actions.ApiError(403, "NO_POLICY", "this route has no authorization policy")
        principal: Principal | None = (
            getattr(request.state, "principal", None) if access is not None else OPERATOR
        )
        denial = decide(principal, need)
        if denial is not None:
            raise actions.ApiError(denial.status, denial.code, denial.message)

    app = FastAPI(
        lifespan=lifespan,
        title="Opseval API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(authorize)],
    )
    console_root = console_dir.resolve() if console_dir is not None else find_console(settings)
    console = _Console(console_root) if console_root is not None else None
    app.state.console = console_root
    app.state.token = token
    app.state.ctx = ctx
    app.state.access = access
    expected = f"Bearer {token}".encode() if token is not None else None
    audio_key = audio_api.ServerKey(settings)

    def finish(request: Request, response: Response) -> Response:
        if _is_api(request.url.path):
            response.headers["X-Content-Type-Options"] = "nosniff"
            if access is not None:  # sessions, CSRF tokens and key secrets are never cached
                response.headers["Cache-Control"] = "no-store"
        else:
            # Every non-API response, not only index.html: any page on this origin can read
            # what the console keeps, so none of them may be framed or run foreign scripts.
            response.headers.update(CONSOLE_HEADERS)
        if access is not None and access.secure:
            response.headers["Strict-Transport-Security"] = HSTS
        return response

    async def console_or(request: Request, response: Response) -> Response:
        if (
            not _is_api(request.url.path)
            and console is not None
            and response.status_code == 404
            and request.method in ("GET", "HEAD")
        ):
            response = await console.respond(request)
        return finish(request, response)

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if access is not None:
            return await accounts_guard(request, call_next)
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
            if expected is None or not hmac.compare_digest(given, expected):
                return _deny(
                    401,
                    "UNAUTHORIZED",
                    "open the console from the link `opseval serve` printed (it carries the "
                    "token), or send Authorization: Bearer <token>",
                )
        return await console_or(request, await call_next(request))

    async def accounts_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if access is None:  # pragma: no cover - only called in accounts mode
            return _deny(500, "NO_ACCESS_CONFIG", "accounts mode is not configured")
        seen = access_mod.view(request, access)
        if not isinstance(seen, access_mod.View):
            return finish(request, _deny(seen.status, seen.code, seen.message))
        request.state.address = seen.address
        request.state.origin = seen.origin
        request.state.principal = None
        if not _is_api(request.url.path):
            return await console_or(request, await call_next(request))
        accounts = access.accounts
        principal: Principal | None = None
        header = request.headers.get("authorization")
        if header is not None:
            # Authoritative: a key that is not valid is 401, whatever cookie came with it.
            principal = await run_in_threadpool(accounts.from_key, header)
            if principal is None:
                return finish(
                    request,
                    _deny(401, "INVALID_KEY", "the API key is unknown, expired or revoked"),
                )
        else:
            cookie = request.cookies.get(access.session_cookie)
            if cookie:
                principal = await run_in_threadpool(accounts.from_cookie, cookie)
        if request.method in UNSAFE_METHODS and (principal is None or principal.via_cookie):
            # A browser's write: same origin, and (signed in) the session's CSRF token.
            if request.headers.get("origin") != seen.origin:
                return finish(
                    request, _deny(403, "ORIGIN_NOT_ALLOWED", "cross-site requests are refused")
                )
            if principal is not None and not same(
                request.headers.get("x-csrf-token", ""), principal.csrf or "\0"
            ):
                return finish(
                    request,
                    _deny(403, "CSRF_FAILED", "send the session's X-CSRF-Token with every write"),
                )
        request.state.principal = principal
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", "").removeprefix(API)
        if (
            request.method in UNSAFE_METHODS
            and response.status_code < 400
            and principal is not None
            and path not in SELF_AUDITED
        ):
            # Every write leaves a trail (ADR-0050); the specific routes write their own.
            try:
                await run_in_threadpool(
                    accounts.audit,
                    principal,
                    f"{request.method} {path}"[:64],
                    target=request.url.path,
                    address=seen.address,
                )
            except Exception:  # the write happened; say loudly that its trail did not
                log.exception("audit event for %s %s was not written", request.method, path)
        return finish(request, response)

    @app.exception_handler(actions.ApiError)
    def _api_error(_: Request, exc: actions.ApiError) -> JSONResponse:
        return _deny(exc.status, exc.code, exc.message)

    @app.exception_handler(StorageError)
    def _storage_error(_: Request, exc: StorageError) -> JSONResponse:
        # The database or the blob store is down: a harness problem, said as such. Never a
        # 404 (the run is not gone) and never an empty list (nothing was left out on purpose).
        return _deny(503, "STORAGE_UNAVAILABLE", str(exc))

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

    @app.get(f"{API}/openapi.json", include_in_schema=False)
    def openapi() -> JSONResponse:
        return JSONResponse(app.openapi())

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
        failed = ctx.unrecorded()  # first: a failure written now is listed by the store
        return views.with_unrecorded(views.list_runs(ctx.store), failed)

    @app.get(API + "/runs/{run_id}")
    def run(run_id: str) -> s.RunDetail:
        failed = ctx.unrecorded().get(run_id)
        if failed is not None:
            return views.unrecorded_detail(run_id, failed)
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

    accounts_api.register(app, access, API)
    ci_api.register(app, ctx, access, API)
    check_policies(app)
    return app


def check_policies(app: FastAPI) -> None:
    """Fail the start when an ``/api`` route has no entry in :data:`ROUTE_NEEDS`: a route
    nobody decided about is a route nobody guarded (deny by default)."""
    missing = [
        f"{method} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods or ())
        if (method, route.path.removeprefix(API)) not in ROUTE_NEEDS
    ]
    if missing:
        raise RuntimeError(f"routes without an authorization policy: {', '.join(missing)}")


__all__ = [
    "API",
    "LOOPBACK_NAMES",
    "ROUTE_NEEDS",
    "check_policies",
    "create_app",
    "find_console",
    "host_name",
]
