# SPDX-License-Identifier: Apache-2.0
"""Accounts mode on a real database (ADR-0050): sign-in, sessions, CSRF, API keys, the role
matrix, project lowering, task-scoped keys, protected labels, OIDC through the API, audit
events and ``opseval admin``.

Needs ``OPSEVAL_TEST_DATABASE_URL`` (see ``conftest.pg_url``).
"""

from __future__ import annotations

import base64
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from opseval.accounts import store as acc
from opseval.accounts.service import Accounts
from opseval.accounts.setup import access_config
from opseval.blobs import FileBlobStore
from opseval.config import Settings
from opseval.db import migrate
from opseval.db.catalogue_store import ensure_project, ensure_task, get_task, push, read_folder
from opseval.db.engine import make_engine, sessions
from opseval.db.run_store import DbRunStore
from opseval.db.schema import ApiKey, AuditEvent, Organization, Project
from opseval.demo import catalogue_dir
from opseval.demo.refund_agent import run_agent
from opseval.runner import RunOptions, launch
from opseval.scoring import INVARIANTS, REGISTRY
from opseval.server import create_app
from opseval.server.app import API

PROJECT = "acme/bot"
RIVAL = "rival/bot"
PW = "correct horse battery staple"
BASE = "https://127.0.0.1"
ORIGIN = {"Origin": BASE}
COOKIE = "opseval-session"  # loopback without an https public URL (no Secure)
ROLES = ("owner", "admin", "member", "viewer")


def settings_for(url: str, tmp_path: Path, **kw: Any) -> Settings:
    return Settings(
        _env_file=None,
        allowed_hosts="127.0.0.1,idp.example.com",
        database_url=url,
        reports_dir=str(tmp_path / "reports"),
        **kw,
    )


async def prepare(url: str, tmp_path: Path) -> None:
    engine = make_engine(settings_for(url, tmp_path))
    try:
        await migrate.upgrade(engine)
        async with sessions(engine)() as s, s.begin():
            await ensure_project(s, PROJECT)
            await ensure_task(s, PROJECT, "other")
            await ensure_project(s, RIVAL)
            cat = tmp_path / "cat"
            shutil.copytree(catalogue_dir(), cat, dirs_exist_ok=True)
            files = read_folder(cat, known_scorers=REGISTRY, known_invariants=set(INVARIANTS))
            await push(s, await get_task(s, PROJECT), files, label="staging")
            org = await acc.organization(s, "acme")
            rival = await acc.organization(s, "rival")
            for role in ROLES:
                user = await acc.create_user(s, f"{role}@acme.test", password=PW)
                await acc.set_org_role(s, org.id, user.id, role)
            lowered = await acc.create_user(s, "lowered@acme.test", password=PW)
            await acc.set_org_role(s, org.id, lowered.id, "member")
            project = await s.scalar(select(Project).where(Project.organization_id == org.id))
            assert project is not None
            await acc.set_project_role(s, project.id, lowered.id, "viewer")
            outsider = await acc.create_user(s, "outsider@rival.test", password=PW)
            await acc.set_org_role(s, rival.id, outsider.id, "owner")
    finally:
        await engine.dispose()


@dataclass
class World:
    app: FastAPI
    store: DbRunStore
    accounts: Accounts
    clock: list[datetime]
    url: str
    tmp: Path

    def client(self, **kw: Any) -> TestClient:
        return TestClient(self.app, base_url=BASE, **kw)

    def login(self, email: str, password: str = PW, client: TestClient | None = None) -> TestClient:
        c = client or self.client()
        r = c.post(f"{API}/auth/login", json={"email": email, "password": password}, headers=ORIGIN)
        assert r.status_code == 200, r.text
        c.headers["X-CSRF-Token"] = r.json()["csrfToken"]
        c.headers["Origin"] = BASE
        return c

    def sql(self, query: str, **params: Any) -> list[Any]:
        async def go(s: AsyncSession) -> list[Any]:
            return list((await s.execute(text(query), params)).all())

        return self.store.run_in_session(go)

    def execute(self, query: str, **params: Any) -> None:
        async def go(s: AsyncSession) -> None:
            await s.execute(text(query), params)

        self.store.run_in_session(go, write=True)

    def events(self, action: str) -> list[AuditEvent]:
        async def go(s: AsyncSession) -> list[AuditEvent]:
            return list(await s.scalars(select(AuditEvent).where(AuditEvent.action == action)))

        return self.store.run_in_session(go)

    def key(self, by: str, scopes: list[str], **kw: Any) -> str:
        """A key created by ``by`` straight in the store; the Authorization header value."""

        async def go(s: AsyncSession) -> str:
            creator = await acc.find_user(s, by) if by else None
            key, secret = await acc.create_key(
                s,
                project_id=kw.pop("project_id", self.accounts.served.project_id),
                name="test",
                scopes=scopes,
                creator=creator,
                **kw,
            )
            return f"Bearer {key.public_key}:{secret}"

        return self.store.run_in_session(go, write=True)


@pytest.fixture
async def world(pg_url: str, tmp_path: Path) -> AsyncIterator[World]:
    await prepare(pg_url, tmp_path)
    settings = settings_for(pg_url, tmp_path)
    store = DbRunStore.connect(
        settings,
        FileBlobStore(tmp_path / "blobs"),
        project=PROJECT,
        task="default",
        task_reads=True,
    )
    clock = [datetime.now(UTC)]
    access = access_config(settings, store, project=PROJECT, task="default")
    access.accounts.now = lambda: clock[0]
    cat = tmp_path / "cat"
    app = create_app(
        settings=settings, catalogue=cat, profile=cat / "profile.yaml", store=store, access=access
    )
    try:
        yield World(app, store, access.accounts, clock, pg_url, tmp_path)
    finally:
        store.close()


# --- signing in ---


class TestSignIn:
    def test_the_session_cookie_has_every_flag(self, world: World) -> None:
        r = world.client().post(
            f"{API}/auth/login", json={"email": "Viewer@ACME.test", "password": PW}, headers=ORIGIN
        )
        assert r.status_code == 200
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE}=")
        for flag in ("HttpOnly", "SameSite=lax", "Path=/"):
            assert flag in cookie, flag
        # Plain-http loopback: no __Host- name and no Secure, which some browsers would drop.
        assert "Domain" not in cookie and "Secure" not in cookie
        body = r.json()
        assert body["user"] == {"kind": "user", "name": "viewer@acme.test", "role": "viewer"}
        assert body["capabilities"] == ["runs:read"] and body["hasAccess"]
        token = cookie.split(";")[0].split("=", 1)[1]
        # Only the token's hash is stored.
        assert world.sql("SELECT count(*) FROM sessions WHERE id = :t", t=token)[0][0] == 0

    def test_behind_https_the_cookie_is_host_prefixed_and_secure(self, world: World) -> None:
        from opseval.server.access import PublicUrl

        settings = settings_for(world.url, world.tmp)
        access = access_config(
            settings,
            world.store,
            project=PROJECT,
            task="default",
            public_url="https://opseval.example.com",
        )
        assert isinstance(access.public_url, PublicUrl)
        cat = world.tmp / "cat"
        app = create_app(
            settings=settings,
            catalogue=cat,
            profile=cat / "profile.yaml",
            store=world.store,
            access=access,
        )
        c = TestClient(app, base_url="https://opseval.example.com")
        r = c.post(
            f"{API}/auth/login",
            json={"email": "viewer@acme.test", "password": PW},
            headers={"Origin": "https://opseval.example.com"},
        )
        assert r.status_code == 200, r.text
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-opseval-session=")
        for flag in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
            assert flag in cookie, flag
        assert "Domain" not in cookie
        assert c.get(f"{API}/runs").status_code == 200
        # The loopback name is not read behind https.
        plain = TestClient(app, base_url="https://opseval.example.com")
        token = cookie.split(";")[0].split("=", 1)[1]
        assert plain.get(f"{API}/runs", cookies={COOKIE: token}).status_code == 401

    def test_a_new_session_at_every_sign_in_and_the_old_one_ends(self, world: World) -> None:
        c = world.login("member@acme.test")
        old = c.cookies[COOKIE]
        world.login("member@acme.test", client=c)
        assert c.cookies[COOKIE] != old
        stale = world.client(cookies={COOKIE: old})
        assert stale.get(f"{API}/runs").status_code == 401
        assert c.get(f"{API}/runs").status_code == 200

    def test_logout_deletes_the_session(self, world: World) -> None:
        c = world.login("member@acme.test")
        token = c.cookies[COOKIE]
        assert c.post(f"{API}/auth/logout").status_code == 204
        assert world.client(cookies={COOKIE: token}).get(f"{API}/runs").status_code == 401
        assert world.sql("SELECT count(*) FROM sessions")[0][0] == 0
        assert world.events("logout")

    def test_wrong_and_unknown_get_one_answer_and_an_audit_event(self, world: World) -> None:
        c = world.client()
        for email in ("member@acme.test", "nobody@acme.test"):
            r = c.post(
                f"{API}/auth/login", json={"email": email, "password": "wrong" * 3}, headers=ORIGIN
            )
            assert r.status_code == 401 and r.json()["code"] == "LOGIN_FAILED"
        failed = world.events("login.failed")
        assert {e.actor_id for e in failed} == {"member@acme.test", "nobody@acme.test"}
        assert all(e.actor_kind == "anonymous" and e.address for e in failed)

    def test_a_disabled_user_cannot_sign_in_and_loses_their_sessions(self, world: World) -> None:
        c = world.login("member@acme.test")

        async def disable(s: AsyncSession) -> None:
            user = await acc.find_user(s, "member@acme.test")
            assert user is not None
            await acc.disable_user(s, user, datetime.now(UTC))

        world.store.run_in_session(disable, write=True)
        assert c.get(f"{API}/runs").status_code == 401
        r = world.client().post(
            f"{API}/auth/login", json={"email": "member@acme.test", "password": PW}, headers=ORIGIN
        )
        assert r.status_code == 401

    def test_sign_in_is_rate_limited(self, world: World) -> None:
        c = world.client()
        codes = [
            c.post(
                f"{API}/auth/login",
                json={"email": "admin@acme.test", "password": "not-it-at-all"},
                headers=ORIGIN,
            ).status_code
            for _ in range(6)
        ]
        assert codes[:5] == [401] * 5 and codes[5] == 429
        r = c.post(
            f"{API}/auth/login", json={"email": "admin@acme.test", "password": PW}, headers=ORIGIN
        )
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1  # even the right one

    def test_idle_and_absolute_expiry(self, world: World) -> None:
        c = world.login("viewer@acme.test")
        world.clock[0] += timedelta(minutes=119)
        assert c.get(f"{API}/runs").status_code == 200  # used: the idle clock restarts
        world.clock[0] += timedelta(minutes=121)
        assert c.get(f"{API}/runs").status_code == 401  # idle too long
        c = world.login("viewer@acme.test")
        for _ in range(25):
            world.clock[0] += timedelta(minutes=60)
            last = c.get(f"{API}/runs").status_code
        assert last == 401  # past 24 h, however busy

    def test_the_session_route_says_who_and_what(self, world: World) -> None:
        body = world.login("admin@acme.test").get(f"{API}/auth/session").json()
        assert body["mode"] == "accounts" and body["project"] == PROJECT
        assert "manage" in body["capabilities"] and "label:protected" in body["capabilities"]


# --- CSRF and keys ---


class TestCsrfAndKeys:
    def test_a_cookie_write_needs_the_csrf_token_and_the_origin(self, world: World) -> None:
        c = world.login("member@acme.test")
        body = {"kind": "suite", "name": "smoke", "version": 1, "label": "qa"}
        token = c.headers.pop("X-CSRF-Token")
        r = c.put(f"{API}/catalogue/labels", json=body)
        assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"
        r = c.put(f"{API}/catalogue/labels", json=body, headers={"X-CSRF-Token": "0" * 64})
        assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"
        r = c.put(
            f"{API}/catalogue/labels",
            json=body,
            headers={"X-CSRF-Token": token, "Origin": "https://evil.example"},
        )
        assert r.status_code == 403 and r.json()["code"] == "ORIGIN_NOT_ALLOWED"
        r = c.put(f"{API}/catalogue/labels", json=body, headers={"X-CSRF-Token": token})
        assert r.status_code == 200, r.text

    def test_another_sessions_csrf_token_is_worthless(self, world: World) -> None:
        a = world.login("member@acme.test")
        b = world.login("admin@acme.test")
        body = {"kind": "suite", "name": "smoke", "version": 1, "label": "qa"}
        r = a.put(
            f"{API}/catalogue/labels",
            json=body,
            headers={"X-CSRF-Token": b.headers["X-CSRF-Token"]},
        )
        assert r.status_code == 403

    def test_a_key_is_exempt_from_csrf(self, world: World) -> None:
        key = world.key("member@acme.test", ["runs:read", "catalogue:label"])
        c = world.client(headers={"Authorization": key})
        body = {"kind": "suite", "name": "smoke", "version": 1, "label": "qa"}
        assert c.put(f"{API}/catalogue/labels", json=body).status_code == 200
        basic = base64.b64encode(key.removeprefix("Bearer ").encode()).decode()
        c = world.client(headers={"Authorization": f"Basic {basic}"})
        assert c.get(f"{API}/runs").status_code == 200

    def test_a_bad_key_is_401_even_with_a_valid_cookie(self, world: World) -> None:
        c = world.login("owner@acme.test")
        assert c.get(f"{API}/runs").status_code == 200
        for bad in ("Bearer pk-oe-nope:sk-oe-nope", "Bearer not-a-key", "Basic xx"):
            r = c.get(f"{API}/runs", headers={"Authorization": bad})
            assert r.status_code == 401, bad
        good = world.key("owner@acme.test", ["runs:read"])
        public = good.split()[1].split(":")[0]
        forged = f"Bearer {public}:sk-oe-{'x' * 43}"
        assert c.get(f"{API}/runs", headers={"Authorization": forged}).status_code == 401

    def test_revoked_and_expired_keys_are_401(self, world: World) -> None:
        key = world.key("admin@acme.test", ["runs:read"])
        c = world.client(headers={"Authorization": key})
        assert c.get(f"{API}/runs").status_code == 200
        admin = world.login("admin@acme.test")
        public = key.split()[1].split(":")[0]
        assert admin.post(f"{API}/auth/keys/{public}/revoke").status_code == 204
        assert c.get(f"{API}/runs").status_code == 401
        expired = world.key(
            "admin@acme.test", ["runs:read"], expires_at=world.clock[0] - timedelta(seconds=1)
        )
        assert (
            world.client(headers={"Authorization": expired}).get(f"{API}/runs").status_code == 401
        )

    def test_a_key_follows_its_creators_role_at_each_use(self, world: World) -> None:
        key = world.key("admin@acme.test", ["runs:read", "label:protected", "catalogue:label"])
        c = world.client(headers={"Authorization": key})
        move = {"kind": "suite", "name": "smoke", "version": 1, "label": "production"}
        assert c.put(f"{API}/catalogue/labels", json=move).status_code == 200

        async def demote(s: AsyncSession) -> None:
            user = await acc.find_user(s, "admin@acme.test")
            org = await acc.organization(s, "acme")
            assert user is not None
            await acc.set_org_role(s, org.id, user.id, "member")

        world.store.run_in_session(demote, write=True)
        r = c.put(f"{API}/catalogue/labels", json=move)
        assert r.status_code == 403 and r.json()["code"] == "PROTECTED_LABEL"


# --- the role matrix ---

ROUTES: list[tuple[str, str, dict[str, Any] | None, str]] = [
    ("GET", "/runs", None, "runs:read"),
    ("GET", "/scenarios", None, "runs:read"),
    ("POST", "/runs/no-such-run/cancel", None, "runs:start"),
    ("POST", "/runs", {"suite": "no-such-suite"}, "runs:start"),
    (
        "PUT",
        "/catalogue/labels",
        {"kind": "suite", "name": "smoke", "version": 1, "label": "qa"},
        "catalogue:label",
    ),
    ("PUT", "/suites", {"name": "x", "scenarios": []}, "catalogue:write"),
    ("GET", "/auth/keys", None, "manage"),
    ("GET", "/auth/members", None, "manage"),
    ("GET", "/analytics/quality", None, "runs:read"),
    ("GET", "/prices", None, "runs:read"),
    (
        "POST",
        "/prices",
        {"pattern": "m-role", "matchKind": "exact", "unit": "tokens", "input": 1, "output": 2},
        "manage",
    ),
]
ALLOWED = {
    "owner": {"runs:read", "runs:start", "catalogue:write", "catalogue:label", "manage"},
    "admin": {"runs:read", "runs:start", "catalogue:write", "catalogue:label", "manage"},
    "member": {"runs:read", "runs:start", "catalogue:write", "catalogue:label"},
    "viewer": {"runs:read"},
    "lowered": {"runs:read"},  # a member of the org, a viewer on this project
}


class TestRoleMatrix:
    @pytest.mark.parametrize("who", list(ALLOWED))
    def test_each_role_on_each_route(self, world: World, who: str) -> None:
        c = world.login(f"{who}@acme.test")
        for method, path, body, need in ROUTES:
            r = c.request(method, API + path, json=body)
            if need in ALLOWED[who]:
                assert r.status_code not in (401, 403, 404) or r.json()["code"] not in (
                    "FORBIDDEN",
                    "NOT_FOUND",
                    "UNAUTHORIZED",
                ), (who, method, path, r.status_code, r.text)
            else:
                assert r.status_code == 403, (who, method, path, r.status_code)
                assert r.json()["code"] == "FORBIDDEN"

    def test_someone_from_another_organisation_sees_nothing(self, world: World) -> None:
        c = world.login("outsider@rival.test")
        assert c.get(f"{API}/runs").status_code == 404
        assert c.get(f"{API}/scenarios").status_code == 404
        body = c.get(f"{API}/auth/session").json()
        assert body["hasAccess"] is False and body["capabilities"] == []

    def test_a_project_role_can_only_lower(self, world: World) -> None:
        admin = world.login("admin@acme.test")
        r = admin.put(
            f"{API}/auth/members",
            json={"email": "viewer@acme.test", "role": "viewer", "projectRole": "admin"},
        )
        assert r.status_code == 403 and r.json()["code"] == "PROJECT_ROLE_ABOVE_ORG"
        # And a row written past the check still cannot raise anyone.
        world.execute(
            "UPDATE project_memberships SET role = 'owner' "
            "WHERE user_id = (SELECT id FROM users WHERE email = 'lowered@acme.test')"
        )
        lowered = world.login("lowered@acme.test")
        assert lowered.get(f"{API}/auth/session").json()["user"]["role"] == "member"

    def test_only_an_owner_grants_owner_and_nobody_their_own_role(self, world: World) -> None:
        admin = world.login("admin@acme.test")
        r = admin.put(f"{API}/auth/members", json={"email": "member@acme.test", "role": "owner"})
        assert r.status_code == 403 and r.json()["code"] == "OWNER_ONLY"
        r = admin.put(f"{API}/auth/members", json={"email": "admin@acme.test", "role": "member"})
        assert r.status_code == 403 and r.json()["code"] == "OWN_ROLE"
        owner = world.login("owner@acme.test")
        r = owner.put(f"{API}/auth/members", json={"email": "member@acme.test", "role": "admin"})
        assert r.status_code == 200
        roles = {m["email"]: m["role"] for m in r.json()}
        assert roles["member@acme.test"] == "admin"
        assert world.events("role.set")[0].detail == {"from": "member", "to": "admin"}

    def test_an_admin_adds_a_member_who_can_sign_in(self, world: World) -> None:
        admin = world.login("admin@acme.test")
        r = admin.put(
            f"{API}/auth/members",
            json={"email": "new@acme.test", "role": "viewer", "password": "a long first password"},
        )
        assert r.status_code == 200
        world.login("new@acme.test", "a long first password")
        assert world.events("member.create")


# --- task-scoped keys -----------------------------------------------------------------------------


class TestTaskScope:
    async def test_a_key_for_task_a_cannot_read_task_b(self, world: World, tmp_path: Path) -> None:
        other = DbRunStore.connect(
            settings_for(world.url, tmp_path),
            FileBlobStore(tmp_path / "blobs"),
            project=PROJECT,
            task="other",
        )
        try:
            async with run_agent() as url:
                cat = catalogue_dir()
                options = RunOptions(target=url, profile=cat / "profile.yaml", store=other)
                result = await launch(
                    cat / "suites" / "smoke.yaml", options, settings_for(world.url, tmp_path)
                )
        finally:
            other.close()
        owner = world.login("owner@acme.test")
        # Served task "default": the other task's run is not there, by list or by id.
        assert result.run_id not in {r["id"] for r in owner.get(f"{API}/runs").json()}
        assert owner.get(f"{API}/runs/{result.run_id}").status_code == 404

        async def other_task(s: AsyncSession) -> uuid.UUID:
            return (await get_task(s, PROJECT, "other")).id

        other_id = world.store.run_in_session(other_task)
        scoped = world.key("owner@acme.test", ["runs:read"], task_ids=[other_id])
        c = world.client(headers={"Authorization": scoped})
        assert c.get(f"{API}/runs").status_code == 404  # 404, not 403: nothing to learn
        assert c.get(f"{API}/scenarios").status_code == 404
        mine = world.key("owner@acme.test", ["runs:read"], task_ids=[world.accounts.served.task_id])
        assert world.client(headers={"Authorization": mine}).get(f"{API}/runs").status_code == 200

    def test_a_key_of_another_project_sees_nothing(self, world: World) -> None:
        async def rival(s: AsyncSession) -> uuid.UUID:
            org = await s.scalar(select(Organization).where(Organization.slug == "rival"))
            assert org is not None
            project = await s.scalar(select(Project).where(Project.organization_id == org.id))
            assert project is not None
            return project.id

        key = world.key(
            "outsider@rival.test", ["runs:read"], project_id=world.store.run_in_session(rival)
        )
        assert world.client(headers={"Authorization": key}).get(f"{API}/runs").status_code == 404


# --- protected labels ---

MOVE = {"kind": "suite", "name": "smoke", "version": 1}


class TestProtectedLabels:
    def test_a_member_moves_staging_not_production(self, world: World) -> None:
        c = world.login("member@acme.test")
        r = c.put(f"{API}/catalogue/labels", json={**MOVE, "label": "staging"})
        assert r.status_code == 200 and r.json() == {"protected": False}
        r = c.put(f"{API}/catalogue/labels", json={**MOVE, "label": "production"})
        assert r.status_code == 403 and r.json()["code"] == "PROTECTED_LABEL"

    def test_catalogue_label_cannot_move_production(self, world: World) -> None:
        key = world.key("owner@acme.test", ["runs:read", "catalogue:label"])
        c = world.client(headers={"Authorization": key})
        r = c.put(f"{API}/catalogue/labels", json={**MOVE, "label": "production"})
        assert r.status_code == 403 and r.json()["code"] == "PROTECTED_LABEL"

    def test_an_admin_moves_production_and_it_stays_protected(self, world: World) -> None:
        c = world.login("admin@acme.test")
        r = c.put(f"{API}/catalogue/labels", json={**MOVE, "label": "production"})
        assert r.status_code == 200 and r.json() == {"protected": True}
        rows = {(x["name"], x["label"]): x for x in c.get(f"{API}/catalogue/labels").json()}
        assert rows[("smoke", "production")]["protected"] is True
        event = world.events("label.move")[-1]
        assert event.detail == {"label": "production", "version": 1, "protected": True}
        assert event.actor_kind == "user"

    def test_an_admin_protects_another_label(self, world: World) -> None:
        admin = world.login("admin@acme.test")
        r = admin.put(
            f"{API}/catalogue/labels/protection", json={"label": "staging", "protected": True}
        )
        assert r.status_code == 200
        member = world.login("member@acme.test")
        r = member.put(f"{API}/catalogue/labels", json={**MOVE, "label": "staging"})
        assert r.status_code == 403
        r = admin.put(
            f"{API}/catalogue/labels/protection", json={"label": "production", "protected": False}
        )
        assert r.status_code == 409 and r.json()["code"] == "ALWAYS_PROTECTED"
        assert (
            member.put(
                f"{API}/catalogue/labels/protection", json={"label": "staging", "protected": False}
            ).status_code
            == 403
        )

    def test_label_protected_is_granted_only_by_admins_and_never_to_a_system_key(
        self, world: World
    ) -> None:
        admin = world.login("admin@acme.test")
        r = admin.post(
            f"{API}/auth/keys",
            json={"name": "nightly", "scopes": ["runs:read", "label:protected"], "system": True},
        )
        assert r.status_code == 403 and r.json()["code"] == "SYSTEM_KEY_PROTECTED_LABEL"
        r = admin.post(
            f"{API}/auth/keys",
            json={"name": "release", "scopes": ["catalogue:label", "label:protected"]},
        )
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["authorization"].startswith("Bearer pk-oe-")
        assert created["secret"].startswith("sk-oe-")
        event = world.events("key.create")[-1]
        assert event.detail["grants_label_protected"] is True
        release = world.client(headers={"Authorization": created["authorization"]})
        assert (
            release.put(f"{API}/catalogue/labels", json={**MOVE, "label": "production"}).status_code
            == 200
        )
        listed = admin.get(f"{API}/auth/keys").json()
        assert "secret" not in listed[0] and listed[0]["secretPrefix"].startswith("sk-oe-")
        # A member cannot manage keys at all, and a key can never manage keys.
        member = world.login("member@acme.test")
        assert (
            member.post(f"{API}/auth/keys", json={"name": "x", "scopes": ["runs:read"]}).status_code
            == 403
        )
        r = release.post(f"{API}/auth/keys", json={"name": "x", "scopes": ["runs:read"]})
        assert r.status_code == 403

    def test_the_database_refuses_a_system_key_with_label_protected(self, world: World) -> None:
        async def sneak(s: AsyncSession) -> None:
            s.add(
                ApiKey(
                    project_id=world.accounts.served.project_id,
                    name="sneaky",
                    public_key="pk-oe-sneaky",
                    secret_hash="0" * 64,
                    secret_prefix="sk-oe-xxxx",
                    scopes=["label:protected"],
                    system=True,
                )
            )
            await s.flush()

        with pytest.raises(Exception) as caught:
            world.store.run_in_session(sneak, write=True)
        assert isinstance(caught.value.__cause__, IntegrityError) or "ck_api_keys_system" in str(
            caught.value
        )

    def test_a_key_beyond_its_creators_role_is_refused(self, world: World) -> None:
        async def go(s: AsyncSession) -> None:
            creator = await acc.find_user(s, "member@acme.test")
            await acc.create_key(
                s,
                project_id=world.accounts.served.project_id,
                name="x",
                scopes=["label:protected"],
                creator=creator,
            )

        with pytest.raises(acc.AccessRefused, match="cannot grant label:protected"):
            world.store.run_in_session(go, write=True)


# --- audit ---


def test_writes_and_sign_ins_are_audited(world: World) -> None:
    c = world.login("member@acme.test")
    r = c.post(f"{API}/gate/resolve", json={"preset": "strict"})
    assert r.status_code == 200, r.text
    login = world.events("login")[0]
    assert login.actor_kind == "user" and login.detail == {"method": "password"}
    assert login.organization_id == world.accounts.served.organization_id
    write = world.events("POST /gate/resolve")[0]
    assert write.target == f"{API}/gate/resolve" and write.address
    assert write.project_id == world.accounts.served.project_id
    # A refused write leaves no event of a write.
    viewer = world.login("viewer@acme.test")
    assert viewer.put(f"{API}/suites", json={"name": "x", "scenarios": []}).status_code == 403
    assert not world.events("PUT /suites")


# --- OIDC through the API ---


@pytest.fixture
def oidc_world(world: World, tmp_path: Path) -> Iterator[tuple[World, Any]]:
    from .test_accounts import FakeIdp

    idp = FakeIdp()
    settings = settings_for(
        world.url,
        tmp_path,
        oidc_issuer="https://idp.example.com",
        oidc_client_id="opseval-console",
        oidc_client_secret="s3cret",
    )
    access = access_config(
        settings,
        world.store,
        project=PROJECT,
        task="default",
        oidc_http=httpx.Client(transport=httpx.MockTransport(idp.handler)),
    )
    cat = world.tmp / "cat"
    world.app = create_app(
        settings=settings,
        catalogue=cat,
        profile=cat / "profile.yaml",
        store=world.store,
        access=access,
    )
    world.accounts = access.accounts
    yield world, idp


class TestOidcSignIn:
    def link(self, world: World, email: str, subject: str) -> None:
        async def go(s: AsyncSession) -> None:
            user = await acc.find_user(s, email)
            assert user is not None
            await acc.link_identity(s, user, "https://idp.example.com", subject)

        world.store.run_in_session(go, write=True)

    def start(self, c: TestClient) -> dict[str, str]:
        r = c.get(f"{API}/auth/oidc/start", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://idp.example.com/authorize?")
        assert "opseval-oidc=" in r.headers["set-cookie"]
        return {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}

    def test_a_linked_identity_signs_in(self, oidc_world: tuple[World, Any]) -> None:
        world, idp = oidc_world
        self.link(world, "member@acme.test", "user-123")
        c = world.client(follow_redirects=False)
        q = self.start(c)
        assert q["redirect_uri"] == f"{BASE}{API}/auth/oidc/callback"
        code = idp.issue(q["nonce"])
        r = c.get(f"{API}/auth/oidc/callback", params={"code": code, "state": q["state"]})
        assert r.status_code == 302 and r.headers["location"] == "/"
        assert c.cookies.get(COOKIE)
        body = c.get(f"{API}/auth/session").json()
        assert body["user"]["name"] == "member@acme.test" and body["oidc"] is True
        assert world.events("login")[-1].detail == {"method": "oidc"}

    def test_an_unknown_identity_is_never_matched_by_email(
        self, oidc_world: tuple[World, Any]
    ) -> None:
        world, idp = oidc_world
        c = world.client(follow_redirects=False)
        q = self.start(c)
        code = idp.issue(q["nonce"], email="member@acme.test")  # a known email, unknown sub
        r = c.get(f"{API}/auth/oidc/callback", params={"code": code, "state": q["state"]})
        assert r.status_code == 302 and r.headers["location"] == "/login?error=NO_ACCOUNT"
        assert not c.cookies.get(COOKIE)
        assert world.events("login.failed")[-1].detail["reason"] == "no account"

    def test_a_callback_from_another_browser_is_refused(
        self, oidc_world: tuple[World, Any]
    ) -> None:
        world, idp = oidc_world
        self.link(world, "member@acme.test", "user-123")
        q = self.start(world.client(follow_redirects=False))
        code = idp.issue(q["nonce"])
        stranger = world.client(follow_redirects=False)  # no flow cookie
        r = stranger.get(f"{API}/auth/oidc/callback", params={"code": code, "state": q["state"]})
        assert r.headers["location"] == "/login?error=OIDC_NO_FLOW"

    def test_a_forged_state_is_refused(self, oidc_world: tuple[World, Any]) -> None:
        world, idp = oidc_world
        self.link(world, "member@acme.test", "user-123")
        c = world.client(follow_redirects=False)
        q = self.start(c)
        r = c.get(
            f"{API}/auth/oidc/callback", params={"code": idp.issue(q["nonce"]), "state": "forged"}
        )
        assert r.headers["location"] == "/login?error=OIDC_FAILED"
        assert not c.cookies.get(COOKIE)


# --- opseval admin ---


class TestAdminCli:
    def cli(
        self, world: World, monkeypatch: pytest.MonkeyPatch, *args: str, stdin: str = ""
    ) -> Any:
        from opseval.cli import app

        monkeypatch.setenv("OPSEVAL_DATABASE_URL", world.url)
        monkeypatch.setenv("OPSEVAL_ALLOWED_HOSTS", "127.0.0.1")
        monkeypatch.setenv("OPSEVAL_DOTENV", "false")
        return CliRunner().invoke(app, ["admin", *args], input=stdin)

    def test_create_user_then_key_then_revoke(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = self.cli(
            world,
            monkeypatch,
            "create-user",
            "CI@acme.test",
            "--org",
            "acme",
            "--role",
            "admin",
            "--password-stdin",
            stdin="a very long password\n",
        )
        assert res.exit_code == 0, res.output
        world.login("ci@acme.test", "a very long password")
        res = self.cli(
            world,
            monkeypatch,
            "create-key",
            "--project",
            PROJECT,
            "--name",
            "ci",
            "--scope",
            "runs:read",
            "--scope",
            "runs:start",
            "--created-by",
            "ci@acme.test",
        )
        assert res.exit_code == 0, res.output
        header = next(ln for ln in res.output.splitlines() if ln.startswith("header:"))
        authorization = header.split("Authorization: ", 1)[1]
        c = world.client(headers={"Authorization": authorization})
        assert c.get(f"{API}/runs").status_code == 200
        public = authorization.split()[1].split(":")[0]
        res = self.cli(world, monkeypatch, "revoke-key", public, "--project", PROJECT)
        assert res.exit_code == 0, res.output
        assert c.get(f"{API}/runs").status_code == 401
        assert {e.actor_kind for e in world.events("key.revoke")} == {"cli"}

    def test_refusals_exit_3_and_change_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = self.cli(
            world,
            monkeypatch,
            "create-key",
            "--project",
            PROJECT,
            "--name",
            "nightly",
            "--scope",
            "label:protected",
            "--system",
        )
        assert res.exit_code == 3 and "system key" in res.output
        res = self.cli(
            world,
            monkeypatch,
            "create-user",
            "x@acme.test",
            "--org",
            "acme",
            "--role",
            "boss",
            "--no-password",
        )
        assert res.exit_code == 3
        assert world.sql("SELECT count(*) FROM users WHERE email = 'x@acme.test'")[0][0] == 0
        res = self.cli(
            world, monkeypatch, "set-role", "owner@acme.test", "--org", "acme", "--role", "member"
        )
        assert res.exit_code == 3 and "at least one owner" in res.output

    def test_set_role_lowers_on_a_project(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = self.cli(
            world,
            monkeypatch,
            "set-role",
            "member@acme.test",
            "--org",
            "acme",
            "--role",
            "member",
            "--project",
            PROJECT,
            "--project-role",
            "viewer",
        )
        assert res.exit_code == 0, res.output
        c = world.login("member@acme.test")
        assert c.get(f"{API}/auth/session").json()["user"]["role"] == "viewer"
        res = self.cli(
            world,
            monkeypatch,
            "set-role",
            "member@acme.test",
            "--org",
            "acme",
            "--role",
            "member",
            "--project",
            PROJECT,
            "--project-role",
            "admin",
        )
        assert res.exit_code == 3 and "Enterprise" in res.output


def test_serve_in_accounts_mode_prints_no_token(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    from opseval.cli import app

    seen: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda api, **kw: seen.update(kw, api=api))
    monkeypatch.setattr("opseval.server.app.find_console", lambda _s: None)
    monkeypatch.setenv("OPSEVAL_DATABASE_URL", world.url)
    monkeypatch.setenv("OPSEVAL_ALLOWED_HOSTS", "127.0.0.1")
    monkeypatch.setenv("OPSEVAL_DOTENV", "false")
    monkeypatch.setenv("OPSEVAL_REPORTS_DIR", str(world.tmp / "served"))
    res = CliRunner().invoke(app, ["serve", "--project", PROJECT])
    assert res.exit_code == 0, res.output
    assert "Accounts: acme/bot task default" in res.output and "#token=" not in res.output
    assert seen["proxy_headers"] is False and seen["api"].state.token is None
    assert seen["api"].state.access.accounts.served.project_ref == PROJECT


# --- review of #95 ---


class TestReviewFixes:
    def test_a_parallel_burst_of_wrong_passwords_hits_the_database_only_the_allowance(
        self, world: World
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor

        from opseval.accounts.ratelimit import ACCOUNT_FREE
        from opseval.accounts.service import LoginRefused

        def attempt(_: int) -> str:
            try:
                world.accounts.login("admin@acme.test", "wrong-password", address="9.9.9.9")
            except LoginRefused as exc:
                return exc.code
            return "OK"

        with ThreadPoolExecutor(12) as pool:
            codes = list(pool.map(attempt, range(12)))
        assert codes.count("LOGIN_FAILED") == ACCOUNT_FREE
        assert codes.count("TOO_MANY_ATTEMPTS") == 12 - ACCOUNT_FREE
        assert len(world.events("login.failed")) == ACCOUNT_FREE

    def test_a_system_key_whose_creator_is_disabled_holds_nothing(self, world: World) -> None:
        key = world.key("admin@acme.test", ["runs:read"], system=True)
        c = world.client(headers={"Authorization": key})
        assert c.get(f"{API}/runs").status_code == 200

        async def disable(s: AsyncSession) -> None:
            user = await acc.find_user(s, "admin@acme.test")
            assert user is not None
            await acc.disable_user(s, user, datetime.now(UTC))

        world.store.run_in_session(disable, write=True)
        r = c.get(f"{API}/runs")
        assert r.status_code == 403  # never the system cap of a CLI key

    def test_deleting_a_person_deletes_their_keys(self, world: World) -> None:
        key = world.key("admin@acme.test", ["runs:read"], system=True)
        world.execute("DELETE FROM users WHERE email = 'admin@acme.test'")
        assert world.sql("SELECT count(*) FROM api_keys")[0][0] == 0
        assert world.client(headers={"Authorization": key}).get(f"{API}/runs").status_code == 401

    def test_a_cli_system_key_has_the_system_cap_and_names_nobody(self, world: World) -> None:
        key = world.key("", ["runs:read", "runs:start"], system=True, via_cli=True)
        c = world.client(headers={"Authorization": key})
        assert c.get(f"{API}/runs").status_code == 200
        assert world.sql("SELECT creator_kind, created_by FROM api_keys")[0] == ("cli", None)
        # The database refuses a creator-less key that is not a CLI system key.
        with pytest.raises(Exception) as refused:
            world.execute(
                "INSERT INTO api_keys (id, project_id, name, public_key, secret_hash, "
                "secret_prefix, scopes, system, creator_kind) VALUES (gen_random_uuid(), "
                ":p, 'x', 'pk-oe-x', :h, 'sk-oe-x', ARRAY['runs:read'], false, 'cli')",
                p=world.accounts.served.project_id,
                h="0" * 64,
            )
        assert "ck_api_keys_creator" in str(refused.value.__cause__)

    def test_a_role_change_keeps_the_project_role_unless_asked(self, world: World) -> None:
        owner = world.login("owner@acme.test")
        r = owner.put(f"{API}/auth/members", json={"email": "lowered@acme.test", "role": "admin"})
        assert r.status_code == 200, r.text
        rows = {m["email"]: m for m in r.json()}
        assert rows["lowered@acme.test"]["projectRole"] == "viewer"  # untouched
        assert rows["lowered@acme.test"]["role"] == "viewer"
        r = owner.put(
            f"{API}/auth/members",
            json={"email": "lowered@acme.test", "role": "admin", "projectRole": None},
        )
        rows = {m["email"]: m for m in r.json()}
        assert rows["lowered@acme.test"]["projectRole"] is None  # an explicit null clears it
        assert rows["lowered@acme.test"]["role"] == "admin"

    def test_protecting_an_unused_label_holds_at_its_first_move(self, world: World) -> None:
        admin = world.login("admin@acme.test")
        r = admin.put(
            f"{API}/catalogue/labels/protection", json={"label": "release", "protected": True}
        )
        assert r.status_code == 200
        member = world.login("member@acme.test")
        r = member.put(f"{API}/catalogue/labels", json={**MOVE, "label": "release"})
        assert r.status_code == 403 and r.json()["code"] == "PROTECTED_LABEL"
        r = admin.put(f"{API}/catalogue/labels", json={**MOVE, "label": "release"})
        assert r.status_code == 200 and r.json() == {"protected": True}

    def test_resolving_a_caller_writes_only_when_due(self, world: World) -> None:
        writes: list[bool] = []
        real = world.store.run_in_session

        def counting(fn: Any, *, write: bool = False) -> Any:
            writes.append(write)
            return real(fn, write=write)

        c = world.login("viewer@acme.test")
        token = c.cookies[COOKIE]
        key = world.key("admin@acme.test", ["runs:read"])
        world.accounts.db = type("Counting", (), {"run_in_session": staticmethod(counting)})()
        assert world.accounts.from_key(key) is not None  # first use: last_used_at written
        assert writes == [False, True]
        writes.clear()
        for _ in range(3):
            assert world.accounts.from_cookie(token) is not None
            assert world.accounts.from_key(key) is not None
        assert writes == [False] * 6  # nothing due: read transactions only
        world.clock[0] += timedelta(minutes=2)
        world.accounts.from_cookie(token)
        assert writes[-2:] == [False, True]  # last_seen_at is due


class TestLinkingIdentities:
    def test_link_identity_from_the_cli_then_sign_in(
        self, oidc_world: tuple[World, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.cli import app

        world, idp = oidc_world
        monkeypatch.setenv("OPSEVAL_DATABASE_URL", world.url)
        monkeypatch.setenv("OPSEVAL_ALLOWED_HOSTS", "127.0.0.1,idp.example.com")
        monkeypatch.setenv("OPSEVAL_OIDC_ISSUER", "https://idp.example.com")
        monkeypatch.setenv("OPSEVAL_DOTENV", "false")
        res = CliRunner().invoke(
            app, ["admin", "link-identity", "viewer@acme.test", "--oidc-subject", "user-123"]
        )
        assert res.exit_code == 0, res.output
        c = world.client(follow_redirects=False)
        q = TestOidcSignIn().start(c)
        r = c.get(
            f"{API}/auth/oidc/callback", params={"code": idp.issue(q["nonce"]), "state": q["state"]}
        )
        assert r.headers["location"] == "/"
        assert c.get(f"{API}/auth/session").json()["user"]["name"] == "viewer@acme.test"
        res = CliRunner().invoke(
            app, ["admin", "link-identity", "member@acme.test", "--oidc-subject", "user-123"]
        )
        assert res.exit_code == 3 and "linked to a user already" in res.output

    def test_an_admin_links_an_identity_through_the_api(
        self, oidc_world: tuple[World, Any]
    ) -> None:
        world, idp = oidc_world
        admin = world.login("admin@acme.test")
        r = admin.put(
            f"{API}/auth/members",
            json={"email": "sso@acme.test", "role": "member", "oidcSubject": "user-123"},
        )
        assert r.status_code == 200, r.text
        c = world.client(follow_redirects=False)
        q = TestOidcSignIn().start(c)
        c.get(
            f"{API}/auth/oidc/callback", params={"code": idp.issue(q["nonce"]), "state": q["state"]}
        )
        assert c.get(f"{API}/auth/session").json()["user"]["name"] == "sso@acme.test"
        assert world.events("identity.link")[0].detail["subject"] == "user-123"

    def test_without_oidc_an_identity_cannot_be_linked(self, world: World) -> None:
        admin = world.login("admin@acme.test")
        r = admin.put(
            f"{API}/auth/members",
            json={"email": "member@acme.test", "role": "member", "oidcSubject": "x"},
        )
        assert r.status_code == 409 and r.json()["code"] == "OIDC_OFF"

    def test_no_account_says_how_to_link(self, oidc_world: tuple[World, Any]) -> None:
        from opseval.accounts.service import LoginRefused

        world, idp = oidc_world
        _, handle = world.accounts.oidc_start("https://127.0.0.1/cb", address="1.1.1.1")
        flow = next(iter(world.accounts._flows.values()))[0]
        with pytest.raises(LoginRefused) as refused:
            world.accounts.oidc_finish(
                handle, code=idp.issue(flow.nonce), state=flow.state, address="1.1.1.1"
            )
        assert "opseval admin link-identity" in refused.value.message
