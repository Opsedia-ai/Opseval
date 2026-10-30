# SPDX-License-Identifier: Apache-2.0
"""``opseval db …``, ``opseval task …`` and ``opseval catalogue …``: server mode on Postgres
(ADR-0047), with tasks inside a project (ADR-0049).

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

    from opseval.db.catalogue_store import ItemSummary, PushReport, TaskSummary, VersionInfo
    from opseval.db.run_store import DbRunStore

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

task_app = typer.Typer(
    help="Tasks inside a project (ADR-0049): one agent endpoint and modality each.",
    no_args_is_help=True,
)

ProjectOpt = Annotated[
    str, typer.Option("--project", help="org/project (or just project, in 'default').")
]
TaskOpt = Annotated[str, typer.Option("--task", help="The task inside the project (ADR-0049).")]
TaskArg = Annotated[str, typer.Argument(help="The task's slug.")]
KindOpt = Annotated[
    Optional[str],  # noqa: UP045
    typer.Option("--kind", help="chat | agentic | voice | batch"),
]
ModalityOpt = Annotated[
    Optional[str],  # noqa: UP045
    typer.Option("--modality", help="text | audio_file | audio_stream | telephony | webrtc"),
]
RegionOpt = Annotated[
    Optional[str],  # noqa: UP045
    typer.Option("--region", help="Where the task's data may live."),
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
    task: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option("--task", help="Also create this task in the project (ADR-0049)."),
    ] = None,
    kind: KindOpt = None,
    modality: ModalityOpt = None,
    region: RegionOpt = None,
) -> None:
    """Create an organisation and project if they do not exist (headless initialisation),
    with its `default` task; `--task` creates another. Nothing that exists is changed."""
    _modules()
    from opseval.db.catalogue_store import ensure_project, ensure_task, parse_project

    if task is None and (kind or modality or region):
        raise _fail(ConfigError("--kind, --modality and --region describe a task: add --task"))

    async def work(session: AsyncSession) -> tuple[str, str] | None:
        await ensure_project(session, project, name=name)
        if task is None:
            return None
        made, created = await ensure_task(
            session, project, task, kind=kind, modality=modality, region=region
        )
        if created:
            state = "created"
        elif made.archived_at is None:
            state = "already exists"
        else:
            ref = "/".join(parse_project(project))
            state = (
                f"exists (archived; `opseval task unarchive {made.slug} --project {ref}` "
                "to push to it)"
            )
        return f"{made.slug} ({made.kind}, {made.modality})", state

    made = _run(work)
    typer.echo(f"Project {project} is ready.")
    if made is not None:
        what, state = made
        typer.echo(f"Task {what} {state}.")


@task_app.command("list")
def task_list(
    project: ProjectOpt,
    archived: Annotated[bool, typer.Option("--all", help="Include archived tasks.")] = False,
) -> None:
    """A project's tasks: kind, modality, profile and how many items each holds."""
    _modules()
    from opseval.db.catalogue_store import list_tasks

    async def work(session: AsyncSession) -> list[TaskSummary]:
        return await list_tasks(session, project, archived=archived)

    for t in _run(work):
        extra = [f"profile {t.profile or 'none yet'}", f"{t.items} item(s)"]
        extra += [f"default suite {t.default_suite}"] if t.default_suite else []
        extra += [f"region {t.region}"] if t.region else []
        extra += [f"archived {t.archived_at:%Y-%m-%d}"] if t.archived_at else []
        typer.echo(f"  {t.slug:<24} {t.kind:<8} {t.modality:<13} {', '.join(extra)}")


@task_app.command("update")
def task_update(
    task: TaskArg,
    project: ProjectOpt,
    kind: KindOpt = None,
    modality: ModalityOpt = None,
    region: RegionOpt = None,
    name: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option("--name", help="A display name for the task."),
    ] = None,
    default_suite: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option("--default-suite", help="The suite (by name) a run uses when it names none."),
    ] = None,
) -> None:
    """Change a task's kind, modality, region, name or default suite."""
    _modules()
    from opseval.db.catalogue_store import get_task, update_task

    async def work(session: AsyncSession) -> str:
        made = await update_task(
            session,
            await get_task(session, project, task),
            kind=kind,
            modality=modality,
            name=name,
            region=region,
            default_suite=default_suite,
        )
        return f"{made.slug} ({made.kind}, {made.modality})"

    typer.echo(f"Task {_run(work)} updated.")


def _archive(task: str, project: str, *, archived: bool) -> None:
    _modules()
    from opseval.db.catalogue_store import archive_task, get_task

    async def work(session: AsyncSession) -> None:
        await archive_task(session, await get_task(session, project, task), archived=archived)

    _run(work)
    typer.echo(f"Task {task} {'archived' if archived else 'restored'}.")


@task_app.command("archive")
def task_archive(task: TaskArg, project: ProjectOpt) -> None:
    """Hide a task. Nothing is deleted; it takes no push or label move until unarchived."""
    _archive(task, project, archived=True)


@task_app.command("unarchive")
def task_unarchive(task: TaskArg, project: ProjectOpt) -> None:
    """Bring an archived task back."""
    _archive(task, project, archived=False)


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
    task: TaskOpt = "default",
) -> None:
    """Check a folder exactly like `opseval validate`, then store what changed as new versions.
    One problem anywhere and nothing is stored."""
    _modules()
    from opseval.db.catalogue_store import get_task, push, read_folder
    from opseval.plugins import load_scorers
    from opseval.scoring import INVARIANTS, REGISTRY

    try:
        load_scorers()
        files = read_folder(folder, known_scorers=REGISTRY, known_invariants=set(INVARIANTS))
    except ConfigError as exc:
        raise _fail(exc) from exc

    async def work(session: AsyncSession) -> PushReport:
        target = await get_task(session, project, task)
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
    task: TaskOpt = "default",
) -> None:
    """Write the catalogue under a label to a folder, ready for `opseval run`."""
    _modules()
    from opseval.db.catalogue_store import get_task, materialize

    async def work(session: AsyncSession) -> int:
        target = await get_task(session, project, task)
        return len(await materialize(session, target, label, folder))

    count = _run(work)
    typer.echo(f"Wrote {count} file(s) under {label!r} to {folder}.")


@catalogue_app.command("list")
def catalogue_list(project: ProjectOpt, task: TaskOpt = "default") -> None:
    """Every item of a task, its newest version and where each label points."""
    _modules()
    from opseval.db.catalogue_store import get_task, list_items

    async def work(session: AsyncSession) -> list[ItemSummary]:
        return await list_items(session, await get_task(session, project, task))

    items = _run(work)
    if not items:
        typer.echo("The catalogue is empty: push a folder with `opseval catalogue push`.")
    for item in items:
        labels = ", ".join(f"{k}=v{v}" for k, v in item.labels.items()) or "no labels"
        typer.echo(f"  {item.kind:<8} {item.name:<40} v{item.latest:<4} {labels}")


@catalogue_app.command("history")
def catalogue_history(
    kind: KindArg, name: str, project: ProjectOpt, task: TaskOpt = "default"
) -> None:
    """Every version of one item, newest first, with its labels."""
    _modules()
    from opseval.db.catalogue_store import get_task, history

    async def work(session: AsyncSession) -> list[VersionInfo]:
        return await history(session, await get_task(session, project, task), kind, name)

    for v in _run(work):
        labels = f"  [{', '.join(v.labels)}]" if v.labels else ""
        note = f"  {v.message}" if v.message else ""
        typer.echo(f"  v{v.version:<4} {v.created_at:%Y-%m-%d %H:%M}  {v.path}{labels}{note}")


@catalogue_app.command("label")
def catalogue_label(
    kind: KindArg,
    name: str,
    version: int,
    label: str,
    project: ProjectOpt,
    task: TaskOpt = "default",
) -> None:
    """Point a label at one version of one item (promote staging to production)."""
    _modules()
    from opseval.db.catalogue_store import get_task, set_label

    async def work(session: AsyncSession) -> None:
        target = await get_task(session, project, task)
        await set_label(session, target, kind, name, version, label)

    _run(work)
    typer.echo(f"{kind} {name}: {label} → v{version}")


# --- runs in the database (ADR-0051) ---------------------------------------------------------

runs_app = typer.Typer(
    help="Runs in the database (ADR-0051): import run folders.", no_args_is_help=True
)


def open_run_store(settings: Settings, *, project: str | None, task: str | None) -> DbRunStore:
    """The database run store the settings name, or :class:`ConfigError` (exit 3) saying why
    not: the extra missing, the database unreachable or refusing, the schema behind."""
    _modules()
    from sqlalchemy.exc import DBAPIError, SQLAlchemyError

    from opseval.blobs import blob_store
    from opseval.db.run_store import DbRunStore

    try:
        return DbRunStore.connect(settings, blob_store(settings), project=project, task=task)
    except (ConnectionError, TimeoutError, socket.gaierror) as exc:
        raise ConfigError(f"cannot reach the database: {exc}") from exc
    except DBAPIError as exc:
        reason = str(exc.orig).splitlines()[0] if exc.orig is not None else type(exc).__name__
        raise ConfigError(f"the database refused: {reason}") from exc
    except SQLAlchemyError as exc:
        raise ConfigError(f"database error ({type(exc).__name__}): {exc}") from exc


def server_settings(settings: Settings, database_url: str | None) -> Settings:
    """``--database-url`` in place of ``OPSEVAL_DATABASE_URL`` (the variable is better: a
    command line is visible to every user of the machine)."""
    if database_url:
        from pydantic import SecretStr

        settings.database_url = SecretStr(database_url)
    return settings


DatabaseUrlOpt = Annotated[
    Optional[str],  # noqa: UP045
    typer.Option(
        "--database-url",
        help="Postgres for server mode. Prefer OPSEVAL_DATABASE_URL: a command line is "
        "visible to other users.",
    ),
]


@runs_app.command("import")
def runs_import(
    folders: Annotated[list[Path], typer.Argument(help="Run folders, e.g. reports/<run_id>.")],
    project: ProjectOpt,
    task: TaskOpt = "default",
    database_url: DatabaseUrlOpt = None,
) -> None:
    """Import finished run folders into the database (idempotent on the run id).

    Each folder is read and checked as every reader checks it; one that cannot be read whole
    is reported and skipped, never half-imported. A run already imported unchanged is
    skipped; a different run under an id the database holds is refused. Exit 3 when any
    folder was refused."""
    from opseval.storage import FileStore

    settings = server_settings(Settings(), database_url)
    try:
        store = open_run_store(settings, project=project, task=task)
    except ConfigError as exc:
        raise _fail(exc) from exc
    from opseval.db.run_import import import_run

    refused = 0
    try:
        for folder in folders:
            path = folder.resolve()
            outcome = import_run(FileStore(path.parent), path.name, store)
            refused += outcome.outcome == "refused"
            colour = "red" if outcome.outcome == "refused" else None
            typer.secho(f"  {outcome.outcome:<9} {outcome.run_id}: {outcome.detail}", fg=colour)
    finally:
        store.close()
    if refused:
        typer.secho(f"{refused} folder(s) were not imported.", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS)
