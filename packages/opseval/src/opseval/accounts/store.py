# SPDX-License-Identifier: Apache-2.0
"""Accounts in the database (ADR-0050): users, memberships, sessions, API keys, audit events
and protected labels. Every function takes an open ``AsyncSession``; the caller owns the
transaction, so a change and its audit event commit together or not at all.

The rules that must hold whatever calls these are checked here, not in the routes:

- a project membership may only **lower** the organisation role (raising it is ``ee/``);
- a key's scopes are within what its creator may grant, and a system key never holds
  ``label:protected`` (the database refuses it too);
- an organisation keeps at least one owner.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opseval.accounts import tokens
from opseval.accounts.passwords import hash_password
from opseval.accounts.roles import (
    DEFAULT_PROTECTED,
    Role,
    check_role,
    check_scopes,
    effective_role,
    grantable,
    rank,
)
from opseval.db.catalogue_store import check_label, set_label
from opseval.db.schema import (
    ApiKey,
    AuditEvent,
    CatalogueItem,
    CatalogueLabel,
    CatalogueVersion,
    Identity,
    Organization,
    OrgMembership,
    Project,
    ProjectMembership,
    Task,
    User,
    UserSession,
)

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
KEY_NAME_MAX = 200


class AccessRefused(ValueError):
    """A change the rules refuse, with an HTTP status and a machine code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def fold_email(email: str) -> str:
    folded = email.strip().lower()
    if not EMAIL.match(folded):
        raise AccessRefused(422, "BAD_EMAIL", f"{email!r} is not an email address")
    return folded


# --- users ------------------------------------------------------------------------------------


async def find_user(s: AsyncSession, email: str) -> User | None:
    user: User | None = await s.scalar(select(User).where(User.email == email.strip().lower()))
    return user


async def get_user(s: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await s.get(User, user_id)


async def create_user(
    s: AsyncSession, email: str, *, password: str | None = None, name: str | None = None
) -> User:
    folded = fold_email(email)
    if await find_user(s, folded) is not None:
        raise AccessRefused(409, "USER_EXISTS", f"a user {folded} already exists")
    user = User(
        email=folded,
        name=name,
        password_hash=hash_password(password) if password is not None else None,
    )
    s.add(user)
    await s.flush()
    return user


async def set_password(s: AsyncSession, user: User, password: str) -> None:
    user.password_hash = hash_password(password)
    # A new password ends every session the old one opened.
    await s.execute(delete(UserSession).where(UserSession.user_id == user.id))


async def rehash(s: AsyncSession, user: User, password: str) -> None:
    """Store a stronger hash of a password that just verified, keeping sessions."""
    user.password_hash = hash_password(password)


async def disable_user(s: AsyncSession, user: User, at: datetime) -> None:
    user.disabled_at = at
    await s.execute(delete(UserSession).where(UserSession.user_id == user.id))


# --- organisations, projects and roles --------------------------------------------------------


async def organization(s: AsyncSession, slug: str) -> Organization:
    org: Organization | None = await s.scalar(select(Organization).where(Organization.slug == slug))
    if org is None:
        raise AccessRefused(404, "UNKNOWN_ORG", f"no organisation {slug!r}")
    return org


async def project_org(s: AsyncSession, project_id: uuid.UUID) -> uuid.UUID:
    org: uuid.UUID | None = await s.scalar(
        select(Project.organization_id).where(Project.id == project_id)
    )
    if org is None:
        raise AccessRefused(404, "UNKNOWN_PROJECT", "no such project")
    return org


async def org_role(s: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> Role | None:
    role: str | None = await s.scalar(
        select(OrgMembership.role).where(
            OrgMembership.organization_id == org_id, OrgMembership.user_id == user_id
        )
    )
    return check_role(role) if role is not None else None


async def role_on(s: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID) -> Role | None:
    """A user's effective role on a project: the organisation role, lowered by a project
    membership. ``None``: no role there at all."""
    row = (
        await s.execute(
            select(OrgMembership.role, ProjectMembership.role)
            .select_from(Project)
            .join(
                OrgMembership,
                (OrgMembership.organization_id == Project.organization_id)
                & (OrgMembership.user_id == user_id),
            )
            .outerjoin(
                ProjectMembership,
                (ProjectMembership.project_id == Project.id)
                & (ProjectMembership.user_id == user_id),
            )
            .where(Project.id == project_id)
        )
    ).one_or_none()
    return None if row is None else effective_role(row[0], row[1])


async def _owners(s: AsyncSession, org_id: uuid.UUID) -> int:
    return int(
        await s.scalar(
            select(func.count())
            .select_from(OrgMembership)
            .where(OrgMembership.organization_id == org_id, OrgMembership.role == "owner")
        )
        or 0
    )


async def set_org_role(
    s: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, role: str | None
) -> Role | None:
    """Give a user a role in an organisation (``None`` removes it, with their project
    memberships). Returns the previous role. The last owner cannot be demoted or removed."""
    new = check_role(role) if role is not None else None
    old = await org_role(s, org_id, user_id)
    if old == "owner" and new != "owner" and await _owners(s, org_id) <= 1:
        raise AccessRefused(409, "LAST_OWNER", "an organisation keeps at least one owner")
    if new is None:
        await s.execute(
            delete(OrgMembership).where(
                OrgMembership.organization_id == org_id, OrgMembership.user_id == user_id
            )
        )
    elif old is None:
        s.add(OrgMembership(organization_id=org_id, user_id=user_id, role=new))
    else:
        await s.execute(
            update(OrgMembership)
            .where(OrgMembership.organization_id == org_id, OrgMembership.user_id == user_id)
            .values(role=new)
        )
    await s.flush()
    return old


async def set_project_role(
    s: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID, role: str | None
) -> None:
    """Lower a user's role on one project (``None`` clears it). Raising it above the
    organisation role is refused: that is an Enterprise feature (ADR-0050)."""
    org_id = await project_org(s, project_id)
    await s.execute(
        delete(ProjectMembership).where(
            ProjectMembership.project_id == project_id, ProjectMembership.user_id == user_id
        )
    )
    if role is None:
        return
    new = check_role(role)
    current = await org_role(s, org_id, user_id)
    if current is None:
        raise AccessRefused(409, "NOT_A_MEMBER", "give the user a role in the organisation first")
    if rank(new) > rank(current):
        raise AccessRefused(
            403,
            "PROJECT_ROLE_ABOVE_ORG",
            f"a project role ({new}) may only lower the organisation role ({current}); "
            "raising it is an Enterprise feature",
        )
    s.add(
        ProjectMembership(project_id=project_id, user_id=user_id, organization_id=org_id, role=new)
    )
    await s.flush()


@dataclass(frozen=True)
class Member:
    user_id: uuid.UUID
    email: str
    name: str | None
    org_role: Role
    project_role: Role | None
    role: Role
    disabled: bool


async def members(s: AsyncSession, project_id: uuid.UUID) -> list[Member]:
    rows = await s.execute(
        select(User, OrgMembership.role, ProjectMembership.role)
        .select_from(Project)
        .join(OrgMembership, OrgMembership.organization_id == Project.organization_id)
        .join(User, User.id == OrgMembership.user_id)
        .outerjoin(
            ProjectMembership,
            (ProjectMembership.project_id == Project.id) & (ProjectMembership.user_id == User.id),
        )
        .where(Project.id == project_id)
        .order_by(User.email)
    )
    out = []
    for user, org, project in rows:
        role = effective_role(org, project) or "viewer"  # the join always has an org role
        out.append(
            Member(
                user.id,
                user.email,
                user.name,
                check_role(org),
                check_role(project) if project else None,
                role,
                user.disabled_at is not None,
            )
        )
    return out


# --- identities (OIDC) ------------------------------------------------------------------------


async def user_for_identity(s: AsyncSession, issuer: str, subject: str) -> User | None:
    user: User | None = await s.scalar(
        select(User)
        .join(Identity, Identity.user_id == User.id)
        .where(Identity.issuer == issuer, Identity.subject == subject)
    )
    return user


async def link_identity(s: AsyncSession, user: User, issuer: str, subject: str) -> None:
    if await user_for_identity(s, issuer, subject) is not None:
        raise AccessRefused(409, "IDENTITY_LINKED", "that identity is linked to a user already")
    s.add(Identity(user_id=user.id, issuer=issuer, subject=subject))
    await s.flush()


# --- sessions ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class NewSession:
    token: str
    """The cookie's value: never stored."""
    id: str
    csrf: str


async def open_session(
    s: AsyncSession, user: User, *, now: datetime, lifetime: timedelta, address: str | None
) -> NewSession:
    token = tokens.new_token()
    sid = tokens.digest(token)
    secret = tokens.new_token()[:64]
    s.add(
        UserSession(
            id=sid,
            user_id=user.id,
            csrf_secret=secret,
            last_seen_at=now,
            expires_at=now + lifetime,
            address=(address or "")[:64] or None,
        )
    )
    await s.flush()
    return NewSession(token, sid, tokens.csrf_token(secret, sid))


async def get_session(s: AsyncSession, sid: str) -> tuple[UserSession, User] | None:
    row = (
        await s.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.id == sid)
        )
    ).one_or_none()
    return None if row is None else (row[0], row[1])


async def touch_session(s: AsyncSession, sid: str, now: datetime) -> None:
    await s.execute(update(UserSession).where(UserSession.id == sid).values(last_seen_at=now))


async def close_session(s: AsyncSession, sid: str) -> None:
    await s.execute(delete(UserSession).where(UserSession.id == sid))


# --- API keys ---------------------------------------------------------------------------------


async def create_key(
    s: AsyncSession,
    *,
    project_id: uuid.UUID,
    name: str,
    scopes: list[str],
    task_ids: list[uuid.UUID] | None = None,
    system: bool = False,
    creator: User | None,
    expires_at: datetime | None = None,
) -> tuple[ApiKey, str]:
    """A new key and its secret (shown once). The scopes must be within what the creator may
    grant on this project now; a system key never holds ``label:protected``."""
    try:
        granted = check_scopes(scopes)
    except ValueError as exc:
        raise AccessRefused(422, "BAD_SCOPES", str(exc)) from exc
    name = name.strip()
    if not name or len(name) > KEY_NAME_MAX:
        raise AccessRefused(422, "BAD_NAME", f"a key needs a name of 1-{KEY_NAME_MAX} characters")
    if system and "label:protected" in granted:
        raise AccessRefused(
            403,
            "SYSTEM_KEY_PROTECTED_LABEL",
            "a system key can never hold label:protected: automation never moves a protected label",
        )
    if creator is None and not system:
        raise AccessRefused(422, "NO_CREATOR", "a key needs the user whose role caps it")
    if creator is not None:
        role = await role_on(s, creator.id, project_id)
        allowed = grantable(role, system=system)
        beyond = [g for g in granted if g not in allowed]
        if beyond:
            raise AccessRefused(
                403,
                "SCOPE_BEYOND_ROLE",
                f"{creator.email} ({role or 'no role'}) cannot grant {', '.join(beyond)}",
            )
    if task_ids is not None:
        task_ids = list(dict.fromkeys(task_ids))
        known = set(
            await s.scalars(
                select(Task.id).where(Task.project_id == project_id, Task.id.in_(task_ids))
            )
        )
        if not task_ids or known != set(task_ids):
            raise AccessRefused(422, "UNKNOWN_TASK", "every task must be a task of this project")
    public, secret = tokens.mint_key()
    key = ApiKey(
        project_id=project_id,
        name=name,
        public_key=public,
        secret_hash=tokens.digest(secret),
        secret_prefix=secret[: tokens.SHOWN_PREFIX],
        scopes=list(granted),
        task_ids=task_ids,
        system=system,
        created_by=creator.id if creator is not None else None,
        expires_at=expires_at,
    )
    s.add(key)
    await s.flush()
    return key, secret


async def find_key(s: AsyncSession, public: str) -> ApiKey | None:
    key: ApiKey | None = await s.scalar(select(ApiKey).where(ApiKey.public_key == public))
    return key


async def list_keys(s: AsyncSession, project_id: uuid.UUID) -> list[tuple[ApiKey, str | None]]:
    rows = await s.execute(
        select(ApiKey, User.email)
        .outerjoin(User, User.id == ApiKey.created_by)
        .where(ApiKey.project_id == project_id)
        .order_by(ApiKey.created_at)
    )
    return [(k, e) for k, e in rows]


async def revoke_key(s: AsyncSession, project_id: uuid.UUID, public: str, now: datetime) -> bool:
    result = await s.execute(
        update(ApiKey)
        .where(
            ApiKey.project_id == project_id,
            ApiKey.public_key == public,
            ApiKey.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    return bool(getattr(result, "rowcount", 0))


async def touch_key(s: AsyncSession, key_id: uuid.UUID, now: datetime) -> None:
    await s.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=now))


# --- audit ------------------------------------------------------------------------------------


async def audit(
    s: AsyncSession,
    *,
    action: str,
    actor_kind: str,
    actor_id: str | None,
    organization_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    target: str | None = None,
    address: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    s.add(
        AuditEvent(
            action=action[:64],
            actor_kind=actor_kind,
            actor_id=actor_id[:320] if actor_id else None,
            organization_id=organization_id,
            project_id=project_id,
            task_id=task_id,
            target=target[:500] if target else None,
            address=address[:64] if address else None,
            detail=detail or {},
        )
    )
    await s.flush()


# --- protected labels -------------------------------------------------------------------------


async def label_protected(s: AsyncSession, task_id: uuid.UUID, label: str) -> bool:
    if label in DEFAULT_PROTECTED:
        return True
    hit = await s.scalar(
        select(func.count())
        .select_from(CatalogueLabel)
        .join(CatalogueItem, CatalogueItem.id == CatalogueLabel.item_id)
        .where(
            CatalogueItem.task_id == task_id,
            CatalogueLabel.label == label,
            CatalogueLabel.protected.is_(True),
        )
    )
    return bool(hit)


async def move_label(
    s: AsyncSession, task: Task, kind: str, name: str, version: int, label: str
) -> bool:
    """Point ``label`` at a version, keeping its protection. Returns whether it is protected.
    Authorization is the caller's: this only moves it."""
    protected = await label_protected(s, task.id, label)
    await set_label(s, task, kind, name, version, label)
    await s.execute(
        update(CatalogueLabel)
        .where(
            CatalogueLabel.label == label,
            CatalogueLabel.item_id.in_(
                select(CatalogueItem.id).where(CatalogueItem.task_id == task.id)
            ),
        )
        .values(protected=protected)
    )
    return protected


async def set_protection(s: AsyncSession, task_id: uuid.UUID, label: str, protected: bool) -> int:
    """Protect or unprotect a label on every item of a task that carries it. A label that is
    protected by default cannot be unprotected."""
    check_label(label)
    if not protected and label in DEFAULT_PROTECTED:
        raise AccessRefused(409, "ALWAYS_PROTECTED", f"{label!r} is always protected")
    result = await s.execute(
        update(CatalogueLabel)
        .where(
            CatalogueLabel.label == label,
            CatalogueLabel.item_id.in_(
                select(CatalogueItem.id).where(CatalogueItem.task_id == task_id)
            ),
        )
        .values(protected=protected)
    )
    return int(getattr(result, "rowcount", 0))


@dataclass(frozen=True)
class LabelRow:
    kind: str
    name: str
    label: str
    version: int
    protected: bool


async def labels(s: AsyncSession, task_id: uuid.UUID) -> list[LabelRow]:
    rows = await s.execute(
        select(
            CatalogueItem.kind,
            CatalogueItem.name,
            CatalogueLabel.label,
            CatalogueVersion.version,
            CatalogueLabel.protected,
        )
        .join(CatalogueLabel, CatalogueLabel.item_id == CatalogueItem.id)
        .join(CatalogueVersion, CatalogueVersion.id == CatalogueLabel.version_id)
        .where(CatalogueItem.task_id == task_id)
        .order_by(CatalogueItem.kind, CatalogueItem.name, CatalogueLabel.label)
    )
    return [
        LabelRow(k, n, lab, int(v), bool(p) or lab in DEFAULT_PROTECTED) for k, n, lab, v, p in rows
    ]


__all__ = [
    "AccessRefused",
    "LabelRow",
    "Member",
    "NewSession",
    "audit",
    "close_session",
    "create_key",
    "create_user",
    "disable_user",
    "find_key",
    "find_user",
    "fold_email",
    "get_session",
    "get_user",
    "label_protected",
    "labels",
    "link_identity",
    "list_keys",
    "members",
    "move_label",
    "open_session",
    "organization",
    "project_org",
    "rehash",
    "revoke_key",
    "role_on",
    "set_org_role",
    "set_password",
    "set_project_role",
    "set_protection",
    "touch_key",
    "touch_session",
    "user_for_identity",
]
