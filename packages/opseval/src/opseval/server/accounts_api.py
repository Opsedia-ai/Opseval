# SPDX-License-Identifier: Apache-2.0
"""The accounts routes (ADR-0050): the session, signing in and out, OIDC, API keys, members
and catalogue labels. Authorization is not done here: each route is in
``app.ROUTE_NEEDS`` and the app's one ``authorize`` dependency has already decided. What is
checked here is what a role alone cannot say (a protected label, a key's scopes within its
creator's role, who may grant ``owner``), and those checks live in the accounts service.

In shared-token mode only ``GET /auth/session`` answers (the operator, who may do anything);
the rest are 409 ``ACCOUNTS_MODE_ONLY``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from opseval.accounts.roles import OPERATOR, Principal
from opseval.errors import ConfigError
from opseval.server import schemas as s
from opseval.server.actions import ApiError

if TYPE_CHECKING:
    from opseval.accounts.service import Accounts, KeyRow
    from opseval.server.access import AccessConfig

FLOW_COOKIE = "__Host-opseval-oidc"
LOGIN_PAGE = "/login"
"""Where the console shows sign-in (and an OIDC failure, as ``?error=CODE``)."""


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when is not None else None


def _key_out(row: KeyRow) -> s.KeyOut:
    return s.KeyOut(
        public_key=row.public_key,
        name=row.name,
        secret_prefix=row.secret_prefix,
        scopes=cast(Any, row.scopes),
        tasks=row.tasks,
        system=row.system,
        created_by=row.created_by,
        created_at=row.created_at.isoformat(),
        expires_at=_iso(row.expires_at),
        revoked_at=_iso(row.revoked_at),
        last_used_at=_iso(row.last_used_at),
    )


def session_out(
    principal: Principal | None, *, mode: str, accounts: Accounts | None
) -> s.SessionOut:
    user = None
    if principal is not None:
        user = s.SessionUser(kind=principal.kind, name=principal.name, role=principal.role)
    served = accounts.served if accounts is not None else None
    return s.SessionOut(
        mode=cast(Any, mode),
        user=user,
        capabilities=cast(Any, principal.capabilities() if principal is not None else []),
        has_access=principal is not None and principal.visible,
        csrf_token=principal.csrf if principal is not None else None,
        project=served.project_ref if served else None,
        task=served.task_slug if served else None,
        oidc=accounts is not None and accounts.oidc is not None,
    )


def register(app: FastAPI, access: AccessConfig | None, api: str, *, cookie: str) -> None:
    accounts = access.accounts if access is not None else None

    def need() -> Accounts:
        if accounts is None:
            raise ApiError(
                409,
                "ACCOUNTS_MODE_ONLY",
                "this needs accounts mode: serve with a database (OPSEVAL_DATABASE_URL)",
            )
        return accounts

    def principal(request: Request) -> Principal:
        found = getattr(request.state, "principal", None)
        if found is None:  # pragma: no cover - authorize refused it already
            raise ApiError(401, "UNAUTHORIZED", "sign in, or send an API key")
        return cast(Principal, found)

    def address(request: Request) -> str:
        return str(getattr(request.state, "address", ""))

    def refused(exc: Exception) -> ApiError:
        from opseval.accounts.store import AccessRefused

        if isinstance(exc, AccessRefused):
            return ApiError(exc.status, exc.code, exc.message)
        return ApiError(422, "REFUSED", str(exc))

    def set_session(response: Response, token: str) -> None:
        assert accounts is not None
        response.set_cookie(
            cookie,
            token,
            max_age=int(accounts.lifetime.total_seconds()),
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )

    def login_refused(exc: Any) -> JSONResponse:
        headers = {}
        if exc.retry_after:
            headers["Retry-After"] = str(max(1, int(exc.retry_after + 0.999)))
        return JSONResponse(
            status_code=exc.status,
            content={"code": exc.code, "message": exc.message},
            headers=headers,
        )

    @app.get(api + "/auth/session")
    def session(request: Request) -> s.SessionOut:
        if accounts is None:
            return session_out(OPERATOR, mode="token", accounts=None)
        return session_out(
            getattr(request.state, "principal", None), mode="accounts", accounts=accounts
        )

    @app.post(api + "/auth/login", response_model=s.SessionOut)
    def login(body: s.LoginIn, request: Request) -> Response:
        from opseval.accounts.service import LoginRefused

        acc = need()
        try:
            new = acc.login(
                body.email,
                body.password,
                address=address(request),
                replacing=request.cookies.get(cookie),
            )
        except LoginRefused as exc:
            return login_refused(exc)
        out = session_out(acc.from_cookie(new.token), mode="accounts", accounts=acc)
        response = JSONResponse(out.model_dump(by_alias=True, mode="json"))
        set_session(response, new.token)
        return response

    @app.post(api + "/auth/logout", status_code=204)
    def logout(request: Request) -> Response:
        acc = need()
        acc.logout(principal(request), address=address(request))
        response = Response(status_code=204)
        response.delete_cookie(cookie, path="/", secure=True, httponly=True, samesite="lax")
        return response

    @app.get(api + "/auth/oidc/start")
    def oidc_start(request: Request) -> Response:
        from opseval.accounts.service import LoginRefused

        acc = need()
        redirect_uri = f"{request.state.origin}{api}/auth/oidc/callback"
        try:
            url, handle = acc.oidc_start(redirect_uri)
        except LoginRefused as exc:
            return login_refused(exc)
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            FLOW_COOKIE, handle, max_age=600, path="/", secure=True, httponly=True, samesite="lax"
        )
        return response

    @app.get(api + "/auth/oidc/callback")
    def oidc_callback(
        request: Request, code: str = "", state: str = "", error: str = ""
    ) -> Response:
        from opseval.accounts.service import LoginRefused

        acc = need()
        handle = request.cookies.get(FLOW_COOKIE, "")
        try:
            if error or not code or not state:
                raise LoginRefused(401, "OIDC_FAILED", "the identity provider refused")
            new = acc.oidc_finish(
                handle,
                code=code[:2048],
                state=state[:256],
                address=address(request),
                replacing=request.cookies.get(cookie),
            )
        except LoginRefused as exc:
            response: Response = RedirectResponse(f"{LOGIN_PAGE}?error={exc.code}", 302)
        else:
            response = RedirectResponse("/", status_code=302)
            set_session(response, new.token)
        response.delete_cookie(FLOW_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    # --- API keys ------------------------------------------------------------------------

    @app.get(api + "/auth/keys")
    def keys() -> list[s.KeyOut]:
        return [_key_out(k) for k in need().list_keys()]

    @app.post(api + "/auth/keys", status_code=201)
    def create_key(body: s.KeyIn, request: Request) -> s.KeyCreatedOut:
        acc = need()
        try:
            row, secret = acc.create_key(
                principal(request),
                name=body.name,
                scopes=list(body.scopes),
                tasks=body.tasks,
                system=body.system,
                expires_days=body.expires_days,
                address=address(request),
            )
        except ValueError as exc:
            raise refused(exc) from exc
        return s.KeyCreatedOut(
            key=_key_out(row),
            secret=secret,
            authorization=f"Bearer {row.public_key}:{secret}",
        )

    @app.post(api + "/auth/keys/{public_key}/revoke", status_code=204)
    def revoke_key(public_key: str, request: Request) -> Response:
        try:
            need().revoke_key(principal(request), public_key[:64], address=address(request))
        except ValueError as exc:
            raise refused(exc) from exc
        return Response(status_code=204)

    # --- members -------------------------------------------------------------------------

    def members_out() -> list[s.MemberOut]:
        return [
            s.MemberOut(
                email=m.email,
                name=m.name,
                org_role=m.org_role,
                project_role=m.project_role,
                role=m.role,
                disabled=m.disabled,
            )
            for m in need().list_members()
        ]

    @app.get(api + "/auth/members")
    def members() -> list[s.MemberOut]:
        return members_out()

    @app.put(api + "/auth/members")
    def set_member(body: s.MemberIn, request: Request) -> list[s.MemberOut]:
        try:
            need().set_member(
                principal(request),
                email=body.email,
                role=body.role,
                project_role=body.project_role,
                password=body.password,
                address=address(request),
            )
        except ValueError as exc:
            raise refused(exc) from exc
        return members_out()

    # --- catalogue labels ----------------------------------------------------------------

    def labels_out() -> list[s.LabelOut]:
        return [
            s.LabelOut(
                kind=r.kind, name=r.name, label=r.label, version=r.version, protected=r.protected
            )
            for r in need().list_labels()
        ]

    @app.get(api + "/catalogue/labels")
    def labels() -> list[s.LabelOut]:
        return labels_out()

    @app.put(api + "/catalogue/labels")
    def move_label(body: s.LabelMoveIn, request: Request) -> s.LabelMoveOut:
        try:
            protected = need().move_label(
                principal(request),
                kind=body.kind,
                name=body.name,
                version=body.version,
                label=body.label,
                address=address(request),
            )
        except ConfigError as exc:
            raise ApiError(422, "LABEL_REFUSED", str(exc)) from exc
        except ValueError as exc:
            raise refused(exc) from exc
        return s.LabelMoveOut(protected=protected)

    @app.put(api + "/catalogue/labels/protection")
    def protect_label(body: s.LabelProtectIn, request: Request) -> list[s.LabelOut]:
        try:
            need().protect_label(
                principal(request),
                label=body.label,
                protected=body.protected,
                address=address(request),
            )
        except ConfigError as exc:
            raise ApiError(422, "LABEL_REFUSED", str(exc)) from exc
        except ValueError as exc:
            raise refused(exc) from exc
        return labels_out()


__all__ = ["FLOW_COOKIE", "LOGIN_PAGE", "register", "session_out"]
