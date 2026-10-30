# SPDX-License-Identifier: Apache-2.0
"""``opseval admin …``: accounts from the command line, headless (ADR-0050).

Sign-up is off, so the first owner is made here: ``opseval admin create-user you@example.com
--org acme --role owner``. Whoever can run these holds the database credentials and so is
trusted like the database itself; every change is still an audit event (actor ``cli``).

A password is never an argument (a command line is visible to every user of the machine): it
is prompted for, or read from standard input with ``--password-stdin``.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Optional, TypeVar

import typer

from opseval.config import Settings
from opseval.db.cli import EXIT_HARNESS, _fail, _run
from opseval.errors import ConfigError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")

admin_app = typer.Typer(
    help="Accounts (ADR-0050): users, roles and API keys, without the console.",
    no_args_is_help=True,
)

OrgOpt = Annotated[str, typer.Option("--org", help="The organisation's slug.")]
RoleOpt = Annotated[str, typer.Option("--role", help="viewer | member | admin | owner")]
ProjectOpt = Annotated[
    Optional[str],  # noqa: UP045
    typer.Option("--project", help="org/project, for a project role."),
]
ProjectRoleOpt = Annotated[
    Optional[str],  # noqa: UP045
    typer.Option(
        "--project-role",
        help="A lower role on --project only (raising it is an Enterprise feature); 'none' "
        "clears it.",
    ),
]


def _who() -> str:
    try:
        return f"cli:{getpass.getuser()}"[:320]
    except (OSError, KeyError):  # no login name (a container without a passwd entry)
        return "cli"


def _password(from_stdin: bool) -> str:
    if from_stdin:
        line = sys.stdin.readline()
        return line.rstrip("\r\n")
    if not sys.stdin.isatty():
        raise _fail(
            ConfigError("no terminal to ask for a password: use --password-stdin or --no-password")
        )
    first = getpass.getpass("Password: ")
    if getpass.getpass("Again: ") != first:
        raise _fail(ConfigError("the passwords differ"))
    return first


def _refused(exc: Exception) -> typer.Exit:
    message = getattr(exc, "message", None) or str(exc)
    typer.secho(f"refused: {message}", fg="red", err=True)
    return typer.Exit(EXIT_HARNESS)


async def _audit(s: AsyncSession, action: str, **kw: object) -> None:
    from opseval.accounts import store

    await store.audit(s, action=action, actor_kind="cli", actor_id=_who(), **kw)  # type: ignore[arg-type]


async def _project_role(
    s: AsyncSession, project: str | None, user_id: object, project_role: str | None
) -> None:
    if project_role is None:
        return
    if project is None:
        raise ConfigError("--project-role needs --project")
    from opseval.accounts import store
    from opseval.db.catalogue_store import get_project

    target = await get_project(s, project)
    role = None if project_role == "none" else project_role
    await store.set_project_role(s, target.id, user_id, role)  # type: ignore[arg-type]


@admin_app.command("create-user")
def create_user(
    email: Annotated[str, typer.Argument(help="The user's email (their sign-in name).")],
    org: OrgOpt,
    role: RoleOpt,
    name: Annotated[Optional[str], typer.Option(help="A display name.")] = None,  # noqa: UP045
    password_stdin: Annotated[
        bool, typer.Option("--password-stdin", help="Read the password from standard input.")
    ] = False,
    no_password: Annotated[
        bool, typer.Option("--no-password", help="An OIDC-only user: no password at all.")
    ] = False,
    oidc_subject: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            "--oidc-subject",
            help="Link the user's identity at OPSEVAL_OIDC_ISSUER (the ID token's sub). "
            "Email is never used to match a sign-in.",
        ),
    ] = None,
    project: ProjectOpt = None,
    project_role: ProjectRoleOpt = None,
) -> None:
    """Create a user with a role in an organisation (sign-up is off: this is how the first
    owner is made)."""
    settings = Settings()
    if oidc_subject is not None and not settings.oidc_issuer:
        raise _fail(ConfigError("--oidc-subject needs OPSEVAL_OIDC_ISSUER"))
    password = None if no_password else _password(password_stdin)

    async def work(s: AsyncSession) -> None:
        from opseval.accounts import store
        from opseval.accounts.roles import check_role

        check_role(role)
        organization = await store.organization(s, org)
        user = await store.create_user(s, email, password=password, name=name)
        if oidc_subject is not None and settings.oidc_issuer:
            await store.link_identity(
                s, user, settings.oidc_issuer.rstrip("/"), oidc_subject.strip()
            )
        await store.set_org_role(s, organization.id, user.id, role)
        await _project_role(s, project, user.id, project_role)
        await _audit(
            s,
            "member.create",
            organization_id=organization.id,
            target=user.email,
            detail={"role": role, "project": project, "project_role": project_role},
        )

    _apply(work)
    typer.echo(f"created {email.strip().lower()} as {role} of {org}")


@admin_app.command("set-role")
def set_role(
    email: Annotated[str, typer.Argument(help="The user's email.")],
    org: OrgOpt,
    role: Annotated[
        str, typer.Option("--role", help="viewer | member | admin | owner | none (remove)")
    ],
    project: ProjectOpt = None,
    project_role: ProjectRoleOpt = None,
) -> None:
    """Change a user's organisation role (and optionally lower it on one project)."""

    async def work(s: AsyncSession) -> None:
        from opseval.accounts import store

        organization = await store.organization(s, org)
        user = await store.find_user(s, email)
        if user is None:
            raise store.AccessRefused(404, "UNKNOWN_USER", f"no user {email!r}")
        new = None if role == "none" else role
        old = await store.set_org_role(s, organization.id, user.id, new)
        if new is not None:
            await _project_role(s, project, user.id, project_role)
        await _audit(
            s,
            "role.set",
            organization_id=organization.id,
            target=user.email,
            detail={"from": old, "to": new, "project": project, "project_role": project_role},
        )

    _apply(work)
    typer.echo(f"{email.strip().lower()}: {role} in {org}")


@admin_app.command("create-key")
def create_key(
    project: Annotated[str, typer.Option("--project", help="org/project the key is for.")],
    name: Annotated[str, typer.Option("--name", help="What the key is for (ci, nightly).")],
    scope: Annotated[
        list[str],
        typer.Option(
            "--scope",
            help="runs:read | runs:start | catalogue:write | catalogue:label | "
            "label:protected (repeatable).",
        ),
    ],
    created_by: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            "--created-by",
            help="The user whose role caps the key, checked at each use (required unless "
            "--system).",
        ),
    ] = None,
    task: Annotated[
        Optional[list[str]],  # noqa: UP045
        typer.Option("--task", help="A task slug the key may touch (repeatable). Default: all."),
    ] = None,
    system: Annotated[
        bool,
        typer.Option(
            "--system",
            help="A key for Opseval's own automation: it can never hold label:protected.",
        ),
    ] = False,
    expires_days: Annotated[
        Optional[int],  # noqa: UP045
        typer.Option("--expires-days", min=1, max=3650, help="Expire after this many days."),
    ] = None,
) -> None:
    """Create a project API key. The secret is printed once and stored only as its hash."""

    async def work(s: AsyncSession) -> tuple[str, str]:
        from sqlalchemy import select

        from opseval.accounts import store
        from opseval.db.catalogue_store import get_project
        from opseval.db.schema import Task

        target = await get_project(s, project)
        creator = None
        if created_by is not None:
            creator = await store.find_user(s, created_by)
            if creator is None:
                raise store.AccessRefused(404, "UNKNOWN_USER", f"no user {created_by!r}")
        task_ids = None
        if task:
            found = {
                slug: tid
                for tid, slug in await s.execute(
                    select(Task.id, Task.slug).where(
                        Task.project_id == target.id, Task.slug.in_(task)
                    )
                )
            }
            unknown = [t for t in task if t not in found]
            if unknown:
                raise store.AccessRefused(422, "UNKNOWN_TASK", f"no task {', '.join(unknown)}")
            task_ids = [found[t] for t in task]
        key, secret = await store.create_key(
            s,
            project_id=target.id,
            name=name,
            scopes=scope,
            task_ids=task_ids,
            system=system,
            creator=creator,
            expires_at=datetime.now(UTC) + timedelta(days=expires_days) if expires_days else None,
        )
        await _audit(
            s,
            "key.create",
            organization_id=target.organization_id,
            project_id=target.id,
            target=key.public_key,
            detail={
                "scopes": list(key.scopes),
                "tasks": task,
                "system": system,
                "grants_label_protected": "label:protected" in key.scopes,
            },
        )
        return key.public_key, secret

    public, secret = _apply(work)
    typer.echo(f"public key: {public}")
    typer.echo(f"secret:     {secret}   (shown once; only its hash is stored)")
    typer.echo(f"header:     Authorization: Bearer {public}:{secret}")


@admin_app.command("revoke-key")
def revoke_key(
    public_key: Annotated[str, typer.Argument(help="The key's public id (pk-oe-…).")],
    project: Annotated[str, typer.Option("--project", help="org/project the key is for.")],
) -> None:
    """Revoke a key at once: its next request is 401."""

    async def work(s: AsyncSession) -> bool:
        from opseval.accounts import store
        from opseval.db.catalogue_store import get_project

        target = await get_project(s, project)
        done = await store.revoke_key(s, target.id, public_key, datetime.now(UTC))
        if done:
            await _audit(
                s,
                "key.revoke",
                organization_id=target.organization_id,
                project_id=target.id,
                target=public_key,
            )
        return done

    if not _apply(work):
        typer.secho(f"refused: no live key {public_key} in {project}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS)
    typer.echo(f"revoked {public_key}")


def _apply(work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` in one transaction. A refused change, a database or a configuration
    problem all exit 3, naming why; nothing is written."""
    try:
        from opseval.accounts.store import AccessRefused
    except ImportError as exc:
        raise _fail(
            ConfigError(f"accounts need the server extra: pip install 'opseval[server]' ({exc})")
        ) from exc
    try:
        return _run(work)
    except AccessRefused as exc:
        raise _refused(exc) from exc
    except ValueError as exc:
        raise _refused(exc) from exc


__all__ = ["admin_app"]
