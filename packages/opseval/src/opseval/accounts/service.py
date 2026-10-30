# SPDX-License-Identifier: Apache-2.0
"""What ``opseval serve`` asks of accounts mode (ADR-0050), as plain synchronous calls.

One :class:`Accounts` serves one task of one project, the one ``serve --project/--task``
names: every principal is resolved **for that project and task**. It reaches the database
through the run store's own loop and engine (``DbRunStore.run_in_session``), so there is one
pool, and every database failure is a :class:`~opseval.errors.StorageError` (503), never an
answer of "no such user".

Every change here and its audit event commit in one transaction.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opseval.accounts import store, tokens
from opseval.accounts.oidc import Flow, OidcClient, OidcError
from opseval.accounts.passwords import needs_rehash, verify_password
from opseval.accounts.ratelimit import LoginLimiter
from opseval.accounts.roles import (
    MANAGE,
    ROLE_SCOPES,
    Principal,
    Role,
    check_role,
    key_scopes,
    rank,
)
from opseval.accounts.store import AccessRefused, LabelRow, Member, NewSession
from opseval.db.schema import ApiKey, Task, User

T = TypeVar("T")

TOUCH_EVERY = timedelta(seconds=60)
"""``last_seen_at`` and ``last_used_at`` are written at most this often, not on every call."""
MAX_FLOWS = 1000
"""OIDC sign-ins in flight, at most. Each lives :data:`FLOW_TTL` seconds; a flood would have
to come from many addresses (:data:`FLOW_STARTS`) to push real ones out."""
FLOW_TTL = 600.0
FLOW_STARTS = 10
"""OIDC sign-ins one address may start per :data:`FLOW_WINDOW` seconds."""
FLOW_WINDOW = 60.0


class Database(Protocol):
    def run_in_session(
        self, fn: Callable[[AsyncSession], Awaitable[T]], *, write: bool = False
    ) -> T: ...


@dataclass(frozen=True)
class Served:
    """The project and task this server serves."""

    organization_id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID
    project_ref: str
    task_slug: str


class LoginRefused(Exception):
    def __init__(
        self, status: int, code: str, message: str, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class KeyRow:
    public_key: str
    name: str
    secret_prefix: str
    scopes: list[str]
    tasks: list[str] | None
    system: bool
    created_by: str | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


def _actor(principal: Principal | None) -> tuple[str, str | None]:
    if principal is None:
        return "anonymous", None
    if principal.kind == "key":
        return "key", principal.id
    return "user", principal.id


class Accounts:
    def __init__(
        self,
        db: Database,
        served: Served,
        *,
        idle: timedelta,
        lifetime: timedelta,
        limiter: LoginLimiter | None = None,
        oidc: OidcClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.served = served
        self.idle = idle
        self.lifetime = lifetime
        self.limiter = limiter or LoginLimiter()
        self.oidc = oidc
        self.now = now
        self._flows: OrderedDict[str, tuple[Flow, float]] = OrderedDict()
        """Flows by the digest of their browser handle, with when (monotonic) they began."""
        self._starts: dict[str, list[float]] = {}
        self._flows_lock = threading.Lock()
        self.clock: Callable[[], float] = time.monotonic

    # --- plumbing ------------------------------------------------------------------------

    def _read(self, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
        return self.db.run_in_session(fn)

    def _write(self, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
        return self.db.run_in_session(fn, write=True)

    async def _audit(
        self,
        s: AsyncSession,
        principal: Principal | None,
        action: str,
        *,
        target: str | None = None,
        address: str | None = None,
        detail: dict[str, Any] | None = None,
        actor_id: str | None = None,
    ) -> None:
        kind, actor = _actor(principal)
        await store.audit(
            s,
            action=action,
            actor_kind=kind,
            actor_id=actor_id or actor,
            organization_id=self.served.organization_id,
            project_id=self.served.project_id,
            task_id=self.served.task_id,
            target=target,
            address=address,
            detail=detail,
        )

    def audit(
        self,
        principal: Principal | None,
        action: str,
        *,
        target: str | None = None,
        address: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        async def go(s: AsyncSession) -> None:
            await self._audit(s, principal, action, target=target, address=address, detail=detail)

        self._write(go)

    # --- who is asking -------------------------------------------------------------------

    def _user_principal(self, user: User, role: Role | None, **extra: Any) -> Principal:
        return Principal(
            kind="user",
            id=str(user.id),
            name=user.email,
            role=role,
            scopes=ROLE_SCOPES[role] if role is not None else frozenset(),
            visible=role is not None,
            user_id=user.id,
            **extra,
        )

    def from_cookie(self, token: str) -> Principal | None:
        """The signed-in user a session cookie names, or ``None`` (unknown, expired, idle too
        long, or the user disabled). Read in a read-only transaction; a write happens only when
        the session must end (it is deleted) or ``last_seen_at`` is due."""
        if not token or len(token) > 256:
            return None
        sid = tokens.digest(token)
        now = self.now()

        async def read(s: AsyncSession) -> tuple[str, Principal | None]:
            found = await store.get_session(s, sid)
            if found is None:
                return "none", None
            session, user = found
            if (
                user.disabled_at is not None
                or now >= session.expires_at
                or now - session.last_seen_at > self.idle
            ):
                return "close", None
            role = await store.role_on(s, user.id, self.served.project_id)
            principal = self._user_principal(
                user,
                role,
                via_cookie=True,
                csrf=tokens.csrf_token(session.csrf_secret, sid),
                session_id=sid,
            )
            return ("touch" if now - session.last_seen_at > TOUCH_EVERY else "ok"), principal

        what, principal = self._read(read)
        if what == "close":

            async def close(s: AsyncSession) -> None:
                await store.close_session(s, sid)

            self._write(close)
        elif what == "touch":

            async def touch(s: AsyncSession) -> None:
                await store.touch_session(s, sid, now)

            self._write(touch)
        return principal

    def from_key(self, header: str) -> Principal | None:
        """The key an ``Authorization`` header carries, or ``None`` when it is not a valid,
        current key (401). A valid key of another project or task is a principal that sees
        nothing (404). Read-only, except ``last_used_at`` when it is due."""
        parsed = tokens.parse_key(header)
        if parsed is None:
            return None
        public, secret = parsed
        now = self.now()

        async def read(s: AsyncSession) -> tuple[bool, Principal | None]:
            key = await store.find_key(s, public)
            given = tokens.digest(secret)
            if key is None or not tokens.same(given, key.secret_hash):
                return False, None
            if key.revoked_at is not None or (key.expires_at is not None and now >= key.expires_at):
                return False, None
            creator_role: Role | None = None
            if key.creator_kind == "user" and key.created_by is not None:
                creator = await store.get_user(s, key.created_by)
                if creator is not None and creator.disabled_at is None:
                    creator_role = await store.role_on(s, creator.id, key.project_id)
            visible = key.project_id == self.served.project_id and (
                key.task_ids is None or self.served.task_id in key.task_ids
            )
            due = key.last_used_at is None or now - key.last_used_at > TOUCH_EVERY
            return due, Principal(
                kind="key",
                id=key.public_key,
                name=key.name,
                role=creator_role,
                scopes=key_scopes(
                    key.scopes, creator_role, system=key.system, creator_kind=key.creator_kind
                ),
                visible=visible,
                key_id=key.id,
                system=key.system,
            )

        due, principal = self._read(read)
        if due and principal is not None and principal.key_id is not None:
            key_id = principal.key_id

            async def touch(s: AsyncSession) -> None:
                await store.touch_key(s, key_id, now)

            self._write(touch)
        return principal

    # --- signing in and out --------------------------------------------------------------

    def login(
        self, email: str, password: str, *, address: str, replacing: str | None = None
    ) -> NewSession:
        """A new session for a right email and password. The session the browser had, if
        any, is ended (a new id at every sign-in). Wrong, unknown or disabled: one answer."""
        folded = email.strip().lower()[:320]
        wait = self.limiter.begin(address, folded)  # reserved and counted before the check
        if wait > 0:
            raise LoginRefused(
                429, "TOO_MANY_ATTEMPTS", "too many failed sign-ins; wait and try again", wait
            )

        async def go(s: AsyncSession) -> NewSession | None:
            user = await store.find_user(s, folded)
            ok = verify_password(user.password_hash if user else None, password)
            if user is None or not ok or user.disabled_at is not None:
                await self._audit(
                    s,
                    None,
                    "login.failed",
                    address=address,
                    actor_id=folded,
                    detail={"method": "password"},
                )
                return None
            if user.password_hash and needs_rehash(user.password_hash):
                await store.rehash(s, user, password)
            if replacing:
                await store.close_session(s, tokens.digest(replacing))
            new = await store.open_session(
                s, user, now=self.now(), lifetime=self.lifetime, address=address
            )
            principal = self._user_principal(user, None)
            await self._audit(s, principal, "login", address=address, detail={"method": "password"})
            return new

        new = self._write(go)
        if new is None:
            raise LoginRefused(401, "LOGIN_FAILED", "the email or password is wrong")
        self.limiter.succeeded(address, folded)
        return new

    def logout(self, principal: Principal, *, address: str) -> None:
        async def go(s: AsyncSession) -> None:
            if principal.session_id is not None:
                await store.close_session(s, principal.session_id)
            await self._audit(s, principal, "logout", address=address)

        self._write(go)

    # --- OIDC ----------------------------------------------------------------------------

    def _prune(self, now: float) -> None:
        """Drop expired flows (oldest first) and start counts older than the window. Under
        the lock."""
        while self._flows:
            _, (_, began) = next(iter(self._flows.items()))
            if now - began <= FLOW_TTL:
                break
            self._flows.popitem(last=False)
        for address in list(self._starts):
            recent = [t for t in self._starts[address] if now - t < FLOW_WINDOW]
            if recent:
                self._starts[address] = recent
            else:
                del self._starts[address]

    def oidc_start(self, redirect_uri: str, *, address: str = "") -> tuple[str, str]:
        """The IdP URL and the browser-binding handle to set as a cookie. At most
        :data:`FLOW_STARTS` per address per minute (429)."""
        if self.oidc is None:
            raise LoginRefused(404, "OIDC_OFF", "OIDC sign-in is not configured")
        with self._flows_lock:
            now = self.clock()
            self._prune(now)
            starts = self._starts.setdefault(address, [])
            if len(starts) >= FLOW_STARTS:
                wait = FLOW_WINDOW - (now - starts[0])
                raise LoginRefused(
                    429, "TOO_MANY_ATTEMPTS", "too many sign-ins started; wait a minute", wait
                )
            starts.append(now)
        try:
            url, flow = self.oidc.start(redirect_uri)
        except OidcError as exc:
            raise LoginRefused(502, "OIDC_UNAVAILABLE", str(exc)) from exc
        handle = tokens.new_token()
        with self._flows_lock:
            now = self.clock()
            self._prune(now)
            self._flows[tokens.digest(handle)] = (flow, now)
            while len(self._flows) > MAX_FLOWS:
                self._flows.popitem(last=False)
        return url, handle

    def oidc_finish(
        self, handle: str, *, code: str, state: str, address: str, replacing: str | None = None
    ) -> NewSession:
        if self.oidc is None:
            raise LoginRefused(404, "OIDC_OFF", "OIDC sign-in is not configured")
        with self._flows_lock:
            now = self.clock()
            self._prune(now)
            held = self._flows.pop(tokens.digest(handle), None) if handle else None
        flow = held[0] if held is not None else None
        if flow is None:
            raise LoginRefused(400, "OIDC_NO_FLOW", "no sign-in was started from this browser")
        try:
            claims = self.oidc.finish(flow, code=code, state=state)
        except OidcError as exc:
            self.audit(
                None, "login.failed", address=address, detail={"method": "oidc", "reason": str(exc)}
            )
            raise LoginRefused(401, "OIDC_FAILED", str(exc)) from exc

        async def go(s: AsyncSession) -> NewSession | None:
            user = await store.user_for_identity(s, claims.issuer, claims.subject)
            if user is None or user.disabled_at is not None:
                await self._audit(
                    s,
                    None,
                    "login.failed",
                    address=address,
                    actor_id=claims.email,
                    detail={
                        "method": "oidc",
                        "reason": "no account" if user is None else "disabled",
                        "issuer": claims.issuer,
                        "subject": claims.subject,
                    },
                )
                return None
            if replacing:
                await store.close_session(s, tokens.digest(replacing))
            new = await store.open_session(
                s, user, now=self.now(), lifetime=self.lifetime, address=address
            )
            await self._audit(
                s,
                self._user_principal(user, None),
                "login",
                address=address,
                detail={"method": "oidc"},
            )
            return new

        new = self._write(go)
        if new is None:
            raise LoginRefused(
                403,
                "NO_ACCOUNT",
                "no Opseval account is linked to this sign-in (sign-up is off): ask an admin to "
                "link it, `opseval admin link-identity <email> --oidc-subject <sub>` for an "
                "existing user or `opseval admin create-user … --oidc-subject <sub>` for a new "
                "one; the subject is in the failed sign-in's audit event",
            )
        return new

    # --- API keys ------------------------------------------------------------------------

    async def _task_slugs(self, s: AsyncSession) -> dict[uuid.UUID, str]:
        rows = await s.execute(
            select(Task.id, Task.slug).where(Task.project_id == self.served.project_id)
        )
        return {i: slug for i, slug in rows}

    def _key_row(self, key: ApiKey, creator: str | None, slugs: dict[uuid.UUID, str]) -> KeyRow:
        return KeyRow(
            public_key=key.public_key,
            name=key.name,
            secret_prefix=key.secret_prefix,
            scopes=list(key.scopes),
            tasks=None if key.task_ids is None else [slugs.get(t, str(t)) for t in key.task_ids],
            system=key.system,
            created_by=creator,
            created_at=key.created_at,
            expires_at=key.expires_at,
            revoked_at=key.revoked_at,
            last_used_at=key.last_used_at,
        )

    def list_keys(self) -> list[KeyRow]:
        async def go(s: AsyncSession) -> list[KeyRow]:
            slugs = await self._task_slugs(s)
            rows = await store.list_keys(s, self.served.project_id)
            return [self._key_row(k, e, slugs) for k, e in rows]

        return self._read(go)

    def create_key(
        self,
        principal: Principal,
        *,
        name: str,
        scopes: list[str],
        tasks: list[str] | None,
        system: bool,
        expires_days: int | None,
        address: str,
    ) -> tuple[KeyRow, str]:
        if principal.user_id is None or MANAGE not in principal.scopes:
            raise AccessRefused(403, "FORBIDDEN", "only an admin or owner manages API keys")
        now = self.now()

        async def go(s: AsyncSession) -> tuple[KeyRow, str]:
            slugs = await self._task_slugs(s)
            by_slug = {v: k for k, v in slugs.items()}
            task_ids = None
            if tasks is not None:
                unknown = [t for t in tasks if t not in by_slug]
                if unknown:
                    raise AccessRefused(422, "UNKNOWN_TASK", f"no task {', '.join(unknown)}")
                task_ids = [by_slug[t] for t in tasks]
            creator = await store.get_user(s, principal.user_id)  # type: ignore[arg-type]
            key, secret = await store.create_key(
                s,
                project_id=self.served.project_id,
                name=name,
                scopes=scopes,
                task_ids=task_ids,
                system=system,
                creator=creator,
                expires_at=now + timedelta(days=expires_days) if expires_days else None,
            )
            await self._audit(
                s,
                principal,
                "key.create",
                target=key.public_key,
                address=address,
                detail={
                    "scopes": list(key.scopes),
                    "tasks": tasks,
                    "system": system,
                    # Granting label:protected is its own fact in the trail (ADR-0050).
                    "grants_label_protected": "label:protected" in key.scopes,
                },
            )
            return self._key_row(key, creator.email if creator else None, slugs), secret

        return self._write(go)

    def revoke_key(self, principal: Principal, public: str, *, address: str) -> None:
        if MANAGE not in principal.scopes:
            raise AccessRefused(403, "FORBIDDEN", "only an admin or owner manages API keys")

        async def go(s: AsyncSession) -> bool:
            done = await store.revoke_key(s, self.served.project_id, public, self.now())
            if done:
                await self._audit(s, principal, "key.revoke", target=public, address=address)
            return done

        if not self._write(go):
            raise AccessRefused(404, "UNKNOWN_KEY", "no such key, or it was revoked already")

    # --- members -------------------------------------------------------------------------

    def list_members(self) -> list[Member]:
        async def go(s: AsyncSession) -> list[Member]:
            return await store.members(s, self.served.project_id)

        return self._read(go)

    def set_member(
        self,
        principal: Principal,
        *,
        email: str,
        role: str | None,
        password: str | None,
        address: str,
        project_role: str | None = None,
        project_role_given: bool = False,
        oidc_subject: str | None = None,
    ) -> None:
        """Give a person a role in this project's organisation; a new email creates the user
        (with ``password``, or OIDC-only). Only an owner grants or changes ``owner``; nobody
        changes their own role.

        The project role changes only when ``project_role_given``: then ``project_role`` is
        the new, lower role, or ``None`` to clear it. Otherwise an existing project membership
        is left as it is. ``oidc_subject`` links the person's identity at the configured
        issuer (email is never the join key)."""
        if principal.user_id is None or MANAGE not in principal.scopes:
            raise AccessRefused(403, "FORBIDDEN", "only an admin or owner manages members")
        new = check_role(role) if role is not None else None
        issuer = self.oidc.issuer if self.oidc is not None else None
        if oidc_subject is not None and issuer is None:
            raise AccessRefused(409, "OIDC_OFF", "OIDC sign-in is not configured on this server")

        async def go(s: AsyncSession) -> None:
            org = self.served.organization_id
            mine = await store.org_role(s, org, principal.user_id)  # type: ignore[arg-type]
            if mine is None or rank(mine) < rank("admin"):
                raise AccessRefused(403, "FORBIDDEN", "only an admin or owner manages members")
            user = await store.find_user(s, email)
            created = False
            if user is None:
                if new is None:
                    raise AccessRefused(404, "UNKNOWN_USER", f"no user {email!r}")
                user = await store.create_user(s, email, password=password)
                created = True
            if user.id == principal.user_id:
                raise AccessRefused(403, "OWN_ROLE", "ask another admin to change your own role")
            old = await store.org_role(s, org, user.id)
            if mine != "owner" and "owner" in (old, new):
                raise AccessRefused(403, "OWNER_ONLY", "only an owner grants or changes owner")
            await store.set_org_role(s, org, user.id, new)
            if new is not None and project_role_given:
                await store.set_project_role(s, self.served.project_id, user.id, project_role)
            detail: dict[str, Any] = {"from": old, "to": new}
            if project_role_given:
                detail["project_role"] = project_role
            await self._audit(
                s,
                principal,
                "member.create" if created else "role.set",
                target=user.email,
                address=address,
                detail=detail,
            )
            if oidc_subject is not None and issuer is not None:
                await store.link_identity(s, user, issuer, oidc_subject.strip())
                await self._audit(
                    s,
                    principal,
                    "identity.link",
                    target=user.email,
                    address=address,
                    detail={"issuer": issuer, "subject": oidc_subject.strip()},
                )

        self._write(go)

    # --- labels --------------------------------------------------------------------------

    def list_labels(self) -> list[LabelRow]:
        async def go(s: AsyncSession) -> list[LabelRow]:
            return await store.labels(s, self.served.task_id)

        return self._read(go)

    def move_label(
        self,
        principal: Principal,
        *,
        kind: str,
        name: str,
        version: int,
        label: str,
        address: str,
    ) -> bool:
        """Move a label; a protected one only with ``label:protected`` (403 otherwise)."""

        async def go(s: AsyncSession) -> bool:
            task = await s.get(Task, self.served.task_id)
            if task is None:  # pragma: no cover - the served task was resolved at start
                raise AccessRefused(404, "UNKNOWN_TASK", "the served task is gone")
            protected = await store.label_protected(s, task.id, label)
            if protected and "label:protected" not in principal.scopes:
                raise AccessRefused(
                    403,
                    "PROTECTED_LABEL",
                    f"{label!r} is protected: only an admin or owner, or a key holding "
                    "label:protected, may move it",
                )
            await store.move_label(s, task, kind, name, version, label)
            await self._audit(
                s,
                principal,
                "label.move",
                target=f"{kind}/{name}",
                address=address,
                detail={"label": label, "version": version, "protected": protected},
            )
            return protected

        return self._write(go)

    def protect_label(
        self, principal: Principal, *, label: str, protected: bool, address: str
    ) -> bool:
        if MANAGE not in principal.scopes:
            raise AccessRefused(403, "FORBIDDEN", "only an admin or owner protects labels")

        async def go(s: AsyncSession) -> bool:
            changed = await store.set_protection(s, self.served.task_id, label, protected)
            await self._audit(
                s,
                principal,
                "label.protect" if protected else "label.unprotect",
                target=label,
                address=address,
                detail={"changed": changed},
            )
            return changed

        return self._write(go)


async def resolve_served(s: AsyncSession, project_ref: str, task_slug: str) -> Served:
    """The served project and task, by name (both must exist)."""
    from opseval.db.catalogue_store import get_project, get_task

    project = await get_project(s, project_ref)
    task = await get_task(s, project_ref, task_slug)
    return Served(project.organization_id, project.id, task.id, project_ref, task_slug)


__all__ = [
    "Accounts",
    "Database",
    "KeyRow",
    "LoginRefused",
    "Served",
    "resolve_served",
]
