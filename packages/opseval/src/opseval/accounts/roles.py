# SPDX-License-Identifier: Apache-2.0
"""Roles, scopes and the one authorization decision (ADR-0050). Pure: no database, no HTTP.

Every ``/api`` route names what it needs (a :class:`Need`); :func:`decide` is the only place
that says yes or no, and it says no unless something grants the need.

- An organisation role grants scopes (:data:`ROLE_SCOPES`). A project membership may only
  lower it (:func:`effective_role`), whatever the row says.
- A key holds the scopes it was granted, cut to its creator's **current** role at each use
  (:func:`key_scopes`). A key whose creator is gone holds nothing. A system key holds at most
  :data:`SYSTEM_MAX`, never ``label:protected``.
- ``manage`` (members and keys) is for people only: no key ever holds it.
- A key scoped to other tasks, or a person with no role in the project, gets 404 rather than
  403, so it cannot learn what exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, cast

Role = Literal["viewer", "member", "admin", "owner"]
ROLES: tuple[Role, ...] = ("viewer", "member", "admin", "owner")
"""Lowest first."""

Scope = Literal["runs:read", "runs:start", "catalogue:write", "catalogue:label", "label:protected"]
SCOPES: tuple[Scope, ...] = (
    "runs:read",
    "runs:start",
    "catalogue:write",
    "catalogue:label",
    "label:protected",
)
MANAGE = "manage"
"""Manage members and API keys: an admin's or owner's, never a key's."""

READER: frozenset[str] = frozenset({"runs:read"})
MEMBER: frozenset[str] = READER | {"runs:start", "catalogue:write", "catalogue:label"}
ADMIN: frozenset[str] = MEMBER | {"label:protected", MANAGE}
ROLE_SCOPES: dict[Role, frozenset[str]] = {
    "viewer": READER,
    "member": MEMBER,
    "admin": ADMIN,
    "owner": ADMIN,
}
SYSTEM_MAX: frozenset[str] = MEMBER
"""The most a system key (schedules, workers, regression rules) can hold: automation never
moves a protected label."""
DEFAULT_PROTECTED: frozenset[str] = frozenset({"production"})
"""Labels that are protected whatever their rows say."""


def check_role(value: str) -> Role:
    if value not in ROLES:
        raise ValueError(f"role {value!r}: one of {', '.join(ROLES)}")
    return value


def check_scopes(values: list[str] | tuple[str, ...]) -> list[Scope]:
    unknown = [v for v in values if v not in SCOPES]
    if unknown:
        raise ValueError(f"unknown scope(s) {', '.join(unknown)}: use {', '.join(SCOPES)}")
    if not values:
        raise ValueError("a key needs at least one scope")
    return [cast(Scope, v) for v in dict.fromkeys(values)]


def rank(role: Role) -> int:
    return ROLES.index(role)


def effective_role(org_role: str | None, project_role: str | None) -> Role | None:
    """The role on a project: the organisation's, lowered (never raised) by a project
    membership. No organisation role is no role at all."""
    if org_role is None:
        return None
    org = check_role(org_role)
    if project_role is None:
        return org
    project = check_role(project_role)
    return project if rank(project) < rank(org) else org


def key_scopes(granted: list[str], creator_role: Role | None, *, system: bool) -> frozenset[str]:
    """What a key may do now: what it was granted, within its creator's current role (a
    system key also within :data:`SYSTEM_MAX`). Never ``manage``."""
    held = frozenset(granted) - {MANAGE}
    if system:
        cap = SYSTEM_MAX if creator_role is None else SYSTEM_MAX & ROLE_SCOPES[creator_role]
    else:
        cap = ROLE_SCOPES[creator_role] if creator_role is not None else frozenset()
    return held & cap


def grantable(creator_role: Role | None, *, system: bool) -> frozenset[str]:
    """The scopes a person with ``creator_role`` may put on a new key."""
    if creator_role is None:
        return frozenset()
    cap = ROLE_SCOPES[creator_role] - {MANAGE}
    return cap & SYSTEM_MAX if system else cap


class Need(StrEnum):
    """What a route requires. Every ``/api`` route has exactly one."""

    PUBLIC = "public"
    """Anyone, signed in or not: the sign-in routes and the session probe."""
    SIGNED_IN = "signed_in"
    """Any authenticated caller (their own session, signing out)."""
    READ = "runs:read"
    START = "runs:start"
    CATALOGUE = "catalogue:write"
    LABEL = "catalogue:label"
    MANAGE = "manage"


@dataclass(frozen=True)
class Principal:
    """Who is asking, as resolved for the project and task being served."""

    kind: Literal["operator", "user", "key"]
    """``operator``: the shared-token mode's one bearer (ADR-0031), who may do anything."""
    id: str
    """A user's id, a key's public id, or ``operator``."""
    name: str
    role: Role | None = None
    """A user's effective role on the project; for a key, its creator's."""
    scopes: frozenset[str] = frozenset()
    visible: bool = True
    """False when the caller has no business in this project or task at all (404)."""
    user_id: uuid.UUID | None = None
    key_id: uuid.UUID | None = None
    system: bool = False
    via_cookie: bool = False
    csrf: str | None = field(default=None, repr=False)
    """The token a cookie-authenticated write must echo in ``X-CSRF-Token``."""
    session_id: str | None = field(default=None, repr=False)

    def capabilities(self) -> list[str]:
        """What the console may offer, in a fixed order. Computed here, never in a browser."""
        if not self.visible:
            return []
        return [c for c in (*SCOPES, MANAGE) if c in self.scopes]


OPERATOR = Principal(kind="operator", id="operator", name="operator", scopes=ADMIN)


@dataclass(frozen=True)
class Denial:
    status: int
    code: str
    message: str


def decide(principal: Principal | None, need: Need) -> Denial | None:
    """``None`` to allow, or why not. Deny unless the need is granted."""
    if need is Need.PUBLIC:
        return None
    if principal is None:
        return Denial(401, "UNAUTHORIZED", "sign in, or send an API key")
    if need is Need.SIGNED_IN:
        return None
    if not principal.visible:
        return Denial(404, "NOT_FOUND", "not found")
    if need.value not in principal.scopes:
        who = f"role {principal.role}" if principal.kind == "user" else f"this {principal.kind}"
        if principal.kind == "key" and need is Need.MANAGE:
            return Denial(403, "FORBIDDEN", "API keys cannot manage members or keys")
        return Denial(403, "FORBIDDEN", f"{who} does not allow {need.value}")
    return None


__all__ = [
    "ADMIN",
    "DEFAULT_PROTECTED",
    "MANAGE",
    "OPERATOR",
    "ROLES",
    "ROLE_SCOPES",
    "SCOPES",
    "SYSTEM_MAX",
    "Denial",
    "Need",
    "Principal",
    "Role",
    "Scope",
    "check_role",
    "check_scopes",
    "decide",
    "effective_role",
    "grantable",
    "key_scopes",
    "rank",
]
