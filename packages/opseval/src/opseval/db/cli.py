# SPDX-License-Identifier: Apache-2.0
"""``opseval db …`` and ``opseval catalogue …``: server mode on Postgres (ADR-0047).

Nothing here imports SQLAlchemy at module level: the base install, without the ``server``
extra, keeps a working CLI, and these commands say what to install instead of failing to
import.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional, TypeVar

import typer

from opseval.config import Settings
from opseval.errors import ConfigError

if TYPE_CHECKING:
    from types import ModuleType

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from opseval.db.catalogue_store import ItemSummary, PushReport, VersionInfo

EXIT_HARNESS = 3  # opseval.verdicts.EXIT_HARNESS, without importing the run path
T = TypeVar("T")

db_app = typer.Typer(
    help="Server mode: the Postgres database (OPSEVAL_DATABASE_URL, ADR-0047).",
    no_args_is_help=True,
)
catalogue_app = typer.Typer(
    help="The versioned catalogue in the database: push a folder, label it, pull a label.",
    no_args_is_help=True,
)

ProjectOpt = Annotated[
    str, typer.Option("--project", help="org/project (or just project, in 'default').")
]
KindArg = Annotated[str, typer.Argument(help="scenario | suite | profile")]


def _fail(exc: Exception) -> typer.Exit:
    typer.secho(f"configuration error: {exc}", fg="red", err=True)
    return typer.Exit(EXIT_HARNESS)


def _modules() -> tuple[ModuleType, ModuleType]:
    try:
        from opseval.db import engine, migrate
    except ImportError as exc:
        needed = "server mode needs the server extra: pip install 'opseval[server]'"
        raise _fail(ConfigError(f"{needed} ({exc})")) from exc
    return engine, migrate


def _with_engine(go: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    """Run ``go`` against the configured database; every failure to reach it is exit 3."""
    db_engine, _ = _modules()

    async def main() -> T:
        engine = db_engine.make_engine(Settings())
        try:
            return await go(engine)
        finally:
            await engine.dispose()

    from alembic.util import CommandError
    from sqlalchemy.exc import DBAPIError, SQLAlchemyError

    try:
        return asyncio.run(main())
    except ConfigError as exc:
        raise _fail(exc) from exc
    except (ConnectionError, TimeoutError, socket.gaierror) as exc:
        raise _fail(ConfigError(f"cannot reach the database: {exc}")) from exc
    except DBAPIError as exc:
        # Wrong password, no such database, no permission. The driver's own message only:
        # the statement and its parameters can carry catalogue content.
        reason = str(exc.orig).splitlines()[0] if exc.orig is not None else type(exc).__name__
        raise _fail(ConfigError(f"the database refused: {reason}")) from exc
    except (SQLAlchemyError, CommandError) as exc:
        raise _fail(ConfigError(f"database error ({type(exc).__name__}): {exc}")) from exc
    except OSError as exc:  # a file the command reads or writes, never the database
        raise _fail(ConfigError(f"{exc.strerror or exc}: {exc.filename}")) from exc


def _run(work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """``work`` in one transaction, on a database whose schema is current."""
    db_engine, migrate = _modules()

    async def go(engine: AsyncEngine) -> T:
        have, want = await migrate.current(engine), migrate.head()
        if have != want:
            raise ConfigError(
                f"the database schema is at {have or 'nothing'}, this Opseval needs {want}: "
                "run `opseval db upgrade`"
            )
        async with db_engine.sessions(engine)() as session, session.begin():
            return await work(session)

    return _with_engine(go)


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Create or migrate the schema to what this version of Opseval needs."""
    _, migrate = _modules()

    async def go(engine: AsyncEngine) -> str | None:
        await migrate.upgrade(engine)
        revision: str | None = await migrate.current(engine)
        return revision

    typer.echo(f"Database schema at {_with_engine(go)}.")


@db_app.command("status")
def db_status() -> None:
    """Show the schema revision; exit 3 when `opseval db upgrade` is needed."""
    _, migrate = _modules()

    async def go(engine: AsyncEngine) -> str | None:
        revision: str | None = await migrate.current(engine)
        return revision

    have, want = _with_engine(go), migrate.head()
    if have != want:
        raise _fail(
            ConfigError(f"schema at {have or 'nothing'}, needs {want}: run `opseval db upgrade`")
        )
    typer.echo(f"Database schema at {have} (current).")


@db_app.command("init")
def db_init(
    project: ProjectOpt,
    name: Annotated[
        Optional[str],  # noqa: UP045 - typer needs Optional here
        typer.Option(help="A display name for the project."),
    ] = None,
) -> None:
    """Create an organisation and project if they do not exist (headless initialisation)."""
    _modules()
    from opseval.db.catalogue_store import ensure_project

    async def work(session: AsyncSession) -> None:
        await ensure_project(session, project, name=name)

    _run(work)
    typer.echo(f"Project {project} is ready.")


@catalogue_app.command("push")
def catalogue_push(
    folder: Annotated[Path, typer.Argument(help="A catalogue folder (profile.yaml, suites/, …).")],
    project: ProjectOpt,
    label: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(help="Point this label at what was pushed (production, staging, …)."),
    ] = None,
    message: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option("--message", "-m", help="Why; kept with each new version."),
    ] = None,
) -> None:
    """Check a folder exactly like `opseval validate`, then store what changed as new versions.
    One problem anywhere and nothing is stored."""
    _modules()
    from opseval.db.catalogue_store import get_project, push, read_folder
    from opseval.plugins import load_scorers
    from opseval.scoring import INVARIANTS, REGISTRY

    try:
        load_scorers()
        files = read_folder(folder, known_scorers=REGISTRY, known_invariants=set(INVARIANTS))
    except ConfigError as exc:
        raise _fail(exc) from exc

    async def work(session: AsyncSession) -> PushReport:
        target = await get_project(session, project)
        return await push(session, target, files, label=label, message=message)

    report = _run(work)
    for kind, item, version in report.added:
        typer.echo(f"  new        {kind:<8} {item}  v{version}")
    for kind, item, version in report.unchanged:
        typer.echo(f"  unchanged  {kind:<8} {item}  v{version}")
    for kind, item in report.unlabelled:
        typer.echo(f"  unlabelled {kind:<8} {item}  (no longer in the folder)")
    where = f", labelled {report.label!r}" if report.label else ""
    typer.echo(
        f"Pushed {len(report.added)} new version(s), {len(report.unchanged)} unchanged{where}."
    )


@catalogue_app.command("pull")
def catalogue_pull(
    folder: Annotated[Path, typer.Argument(help="A new or empty folder to write into.")],
    project: ProjectOpt,
    label: Annotated[str, typer.Option(help="Which label to pull, or latest.")] = "production",
) -> None:
    """Write the catalogue under a label to a folder, ready for `opseval run`."""
    _modules()
    from opseval.db.catalogue_store import get_project, materialize

    async def work(session: AsyncSession) -> int:
        target = await get_project(session, project)
        return len(await materialize(session, target, label, folder))

    count = _run(work)
    typer.echo(f"Wrote {count} file(s) under {label!r} to {folder}.")


@catalogue_app.command("list")
def catalogue_list(project: ProjectOpt) -> None:
    """Every item, its newest version and where each label points."""
    _modules()
    from opseval.db.catalogue_store import get_project, list_items

    async def work(session: AsyncSession) -> list[ItemSummary]:
        return await list_items(session, await get_project(session, project))

    items = _run(work)
    if not items:
        typer.echo("The catalogue is empty: push a folder with `opseval catalogue push`.")
    for item in items:
        labels = ", ".join(f"{k}=v{v}" for k, v in item.labels.items()) or "no labels"
        typer.echo(f"  {item.kind:<8} {item.name:<40} v{item.latest:<4} {labels}")


@catalogue_app.command("history")
def catalogue_history(kind: KindArg, name: str, project: ProjectOpt) -> None:
    """Every version of one item, newest first, with its labels."""
    _modules()
    from opseval.db.catalogue_store import get_project, history

    async def work(session: AsyncSession) -> list[VersionInfo]:
        return await history(session, await get_project(session, project), kind, name)

    for v in _run(work):
        labels = f"  [{', '.join(v.labels)}]" if v.labels else ""
        note = f"  {v.message}" if v.message else ""
        typer.echo(f"  v{v.version:<4} {v.created_at:%Y-%m-%d %H:%M}  {v.path}{labels}{note}")


@catalogue_app.command("label")
def catalogue_label(
    kind: KindArg, name: str, version: int, label: str, project: ProjectOpt
) -> None:
    """Point a label at one version of one item (promote staging to production)."""
    _modules()
    from opseval.db.catalogue_store import get_project, set_label

    async def work(session: AsyncSession) -> None:
        await set_label(session, await get_project(session, project), kind, name, version, label)

    _run(work)
    typer.echo(f"{kind} {name}: {label} → v{version}")
