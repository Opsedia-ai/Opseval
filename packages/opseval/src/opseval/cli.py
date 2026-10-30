# SPDX-License-Identifier: Apache-2.0
"""The ``opseval`` command line.

Exit codes: 0 pass · 1 fail · 2 not judged · 3 harness or configuration error.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import traceback
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn, Optional

import typer

from opseval import __version__
from opseval.accounts.cli import admin_app
from opseval.catalogue import builtin_profiles, load_catalogue, load_profile
from opseval.config import Settings
from opseval.db.cli import (
    DatabaseUrlOpt,
    catalogue_app,
    db_app,
    open_run_store,
    runs_app,
    server_settings,
    task_app,
)
from opseval.errors import ConfigError, HarnessError
from opseval.models.gate import Preset
from opseval.models.results import SuiteResult
from opseval.models.stop import StopPolicy
from opseval.reporting import console_summary
from opseval.runner import VOICE_TRANSPORTS, RunOptions, launch, voice_language_for
from opseval.scaffold import PLACEHOLDER_URL, TEMPLATES, Answers, scaffold
from opseval.scoring import INVARIANTS, REGISTRY, describe_params
from opseval.storage import FileStore, RunStore, load_result_at
from opseval.verdicts import EXIT_HARNESS, EXIT_NOT_JUDGED
from opseval.voice.cli import audio_app

if TYPE_CHECKING:
    from opseval.db.run_store import DbRunStore

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Opseval: release-grade, black-box evaluation for conversational AI agents.",
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"opseval {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        Optional[bool],  # noqa: UP045 - typer needs Optional here
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = None,
) -> None:
    """Opseval command line."""


ProfileOpt = Annotated[
    str, typer.Option("--profile", "-p", help="SUT profile YAML, or builtin:<name>.")
]
JunitOpt = Annotated[
    Optional[Path],  # noqa: UP045 - typer needs Optional here
    typer.Option("--junit", help="Also write JUnit XML here (for CI test reports)."),
]
GateOpt = Annotated[
    Optional[Preset],  # noqa: UP045
    typer.Option(
        "--gate",
        help="Gate preset (strict | standard | lenient) replacing the suite's gate policy.",
    ),
]


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Where to create the catalogue.")] = Path(
        "opseval"
    ),
    template: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(help=" | ".join(f"{k}: {v}" for k, v in TEMPLATES.items())),
    ] = None,
    target: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(help="Your agent's URL; its host is put in the allowlist."),
    ] = None,
    name: Annotated[Optional[str], typer.Option(help="A name for your agent.")] = None,  # noqa: UP045
    sut_model: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(help="The model your agent runs on, so a same-family judge is refused."),
    ] = None,
    interactive: Annotated[
        Optional[bool],  # noqa: UP045
        typer.Option(
            "--interactive/--no-interactive",
            help="Ask for what was not given. Default: ask only in a terminal.",
            show_default=False,
        ),
    ] = None,
    force: Annotated[bool, typer.Option(help="Overwrite existing files.")] = False,
) -> None:
    """Create a starter catalogue (profile, scenarios, suites) that validates as is.

    In a terminal it asks what kind of agent you have, where it is, and what it runs on, and
    writes those into the files; flags answer the same questions without asking. In CI (no
    terminal) it never prompts.
    """
    ask = sys.stdin.isatty() if interactive is None else interactive
    if ask:
        if template is None:
            typer.echo("Which kind of agent are you testing?")
            for key, text in TEMPLATES.items():
                typer.echo(f"  {key:<12} {text}")
            template = typer.prompt("Template", default="openai-chat").strip()
            while template not in TEMPLATES:
                typer.echo(f"Choose one of: {', '.join(TEMPLATES)}")
                template = typer.prompt("Template", default="openai-chat").strip()
        if template == "openai-chat":
            if target is None:
                target = typer.prompt("Your agent's URL", default=PLACEHOLDER_URL)
            if name is None:
                name = typer.prompt("A name for your agent", default="my-agent")
            if sut_model is None:
                sut_model = (
                    typer.prompt(
                        "The model it runs on (blank if unknown)", default="", show_default=False
                    ).strip()
                    or None
                )
    template = template or "openai-chat"
    if template != "openai-chat" and any(v is not None for v in (target, name, sut_model)):
        typer.secho(
            "error: --target, --name and --sut-model apply to the openai-chat template only; "
            "the refund-demo template is a fixed worked example",
            fg="red",
            err=True,
        )
        raise typer.Exit(EXIT_HARNESS)
    answers = Answers(
        target=None if target == PLACEHOLDER_URL else target, name=name, sut_model=sut_model
    )
    try:
        written = scaffold(directory, template, force=force, answers=answers)
        profile = load_profile(directory / "profile.yaml")
        cat, _ = load_catalogue(
            directory / "suites" / "full.yaml",
            profile,
            known_scorers=REGISTRY,
            known_invariants=set(INVARIANTS),
        )
    except ConfigError as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    for path in written:
        typer.echo(f"  created {path}")
    typer.echo(f"\nValid: {len(cat.scenarios)} scenario(s), {len(cat.problems)} problem(s).")
    if template == "openai-chat":
        where = answers.target or PLACEHOLDER_URL
        typer.echo(
            f"\nNext: copy {directory / '.env.example'} to .env, then\n"
            f"  opseval run {directory / 'suites' / 'smoke.yaml'} --profile "
            f"{directory / 'profile.yaml'} --transport openai-chat --target {where}"
        )
    else:
        typer.echo(f"\nNext: read {directory / 'README.md'}")


@app.command()
def validate(
    path: Annotated[Path, typer.Argument(help="Scenario file, directory or suite file.")],
    profile: ProfileOpt,
) -> None:
    """Validate scenarios offline (no network, no credentials)."""
    try:
        prof = load_profile(profile)
        cat, suite = load_catalogue(
            path, prof, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
        )
    except ConfigError as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    from opseval.advice import Configured, expansion_warnings, scenario_warnings

    # Validation is offline and needs no credentials: a broken setting elsewhere must not
    # fail it. Without readable settings, the warnings about this machine are left out.
    try:
        settings = Settings()
    except ValueError:
        here = None
    else:
        here = Configured(
            judge=bool(settings.judge_base_url and settings.judge_model),
            simulator=bool(settings.simulator_base_url and settings.simulator_model),
        )
    # The expansions of one scenario share most warnings: each is said once (ADR-0060).
    warnings = list(
        dict.fromkeys(
            w
            for sc in cat.scenarios
            for w in scenario_warnings(sc, cat.sources.get(sc.id, sc.id), prof, here)
        )
    )
    for expansion in cat.expansions.values():
        warnings += expansion_warnings(expansion)
        typer.echo(
            f"• {expansion.base_id}: expands to {expansion.count} scenario(s), "
            f"{expansion.attempts} attempt(s) ({expansion.count} x {expansion.repeats} repeats)"
        )
    for problem in cat.problems:
        typer.secho(f"✗ {problem}", fg="red")
    for warning in warnings:
        typer.secho(f"⚠ {warning}", fg="yellow")
    typer.echo(
        f"{len(cat.scenarios)} valid scenario(s) in {suite!r}; {len(cat.problems)} problem(s); "
        f"{len(warnings)} warning(s)."
    )
    # Warnings never change the exit code: the scenarios are valid.
    raise typer.Exit(EXIT_HARNESS if cat.problems else 0)


@app.command("list-scorers")
def list_scorers() -> None:
    """List every scorer with its axis and the evidence it needs, plus built-in profiles."""
    from opseval.plugins import THIRD_PARTY, load_scorers

    try:
        load_scorers()
    except ConfigError as exc:
        typer.secho(f"plugin error: {exc}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    for sid, cls in sorted(REGISTRY.items()):
        origin = f"  [{THIRD_PARTY[sid]}]" if sid in THIRD_PARTY else ""
        typer.echo(
            f"{sid:<32} {cls.axis.value:<9} needs {cls.requires:<7} {cls.description}{origin}"
        )
        params = describe_params(cls)
        if params is None:
            typer.echo(f"{'':<32} params: not declared, so not checked")
        elif params:
            typer.echo(f"{'':<32} params: {params}  (* required)")
    if THIRD_PARTY:
        typer.echo(f"\n{len(THIRD_PARTY)} scorer(s) come from installed plugins, marked [dist].")
    typer.echo("\nInvariant bundles: " + ", ".join(f"{k} → {v}" for k, v in INVARIANTS.items()))
    typer.echo("Built-in profiles: " + ", ".join(f"builtin:{n}" for n in builtin_profiles()))


def _finish(
    result: SuiteResult,
    settings: Settings,
    open_report: bool,
    junit: Path | None,
    store: RunStore | None = None,
) -> int:
    typer.echo("")
    typer.echo(console_summary(result))
    if store is not None and not isinstance(store, FileStore):
        typer.echo(f"Stored: run {result.run_id} in the {store.where()}")
        xml = store.load_junit(result.run_id)
        if junit is not None and xml is not None:
            junit.parent.mkdir(parents=True, exist_ok=True)
            junit.write_text(xml, encoding="utf-8")
            typer.echo(f"JUnit:  {junit.resolve()}")
        if open_report:
            typer.echo("The report is in the database: open the run in the console.")
        return result.exit_code
    run_dir = (Path(settings.reports_dir) / result.run_id).resolve()
    typer.echo(f"Report: {run_dir / 'report.html'}")
    if junit is not None:
        junit.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run_dir / "junit.xml", junit)
        typer.echo(f"JUnit:  {junit.resolve()}")
    if open_report:
        webbrowser.open((run_dir / "report.html").as_uri())
    return result.exit_code


def _execute(
    path: Path, options: RunOptions, settings: Settings, open_report: bool, junit: Path | None
) -> int:
    try:
        result = asyncio.run(launch(path, options, settings, log=typer.echo))
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg="red", err=True)
        return EXIT_HARNESS
    return _finish(result, settings, open_report, junit, options.store)


def _server_store(
    settings: Settings, database_url: str | None, project: str | None, task: str | None
) -> DbRunStore | None:
    """The database store when server mode is asked for (``--project``), else ``None``: run
    folders, as always. Exit 3 on anything that keeps it from opening."""
    if project is None:
        if database_url:
            typer.secho("configuration error: --database-url needs --project", fg="red", err=True)
            raise typer.Exit(EXIT_HARNESS)
        return None
    try:
        return open_run_store(server_settings(settings, database_url), project=project, task=task)
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc


def _config_error(message: str) -> NoReturn:
    typer.secho(f"configuration error: {message}", fg="red", err=True)
    raise typer.Exit(EXIT_HARNESS)


def _inside(folder: Path, path: Path) -> Path:
    """``path`` inside the pulled catalogue, or exit 3: a run with --server evaluates the
    label, never a file from elsewhere."""
    if path.is_absolute() or ".." in path.parts:
        _config_error(f"{path}: with --server, give a path inside the pulled catalogue")
    found = (folder / path).resolve()
    if not found.is_relative_to(folder) or not found.exists():
        _config_error(f"{path}: not in the pulled catalogue")
    return found


def _execute_remote(
    path: Path,
    options: RunOptions,
    settings: Settings,
    open_report: bool,
    junit: Path | None,
    url: str,
    project: str,
    task: str,
    label: str | None,
) -> int:
    """``run --server`` (ADR-0058 E2): pull, run locally, upload. Exit 3 when the pull fails
    (nothing could be evaluated); a failed upload never changes the code."""
    import tempfile

    from opseval import remote

    try:
        server = remote.server_from(settings, url, project, task, label or "production")
    except ConfigError as exc:
        _config_error(str(exc))
    pulled_into = Path(tempfile.mkdtemp(prefix="opseval-pull-"))
    try:
        with remote.make_client(server, settings) as client:
            folder = pulled_into / "catalogue"
            try:
                pulled = remote.pull(client, server, folder)
            except ConfigError as exc:
                _config_error(str(exc))
            folder = folder.resolve()
            typer.echo(
                f"Pulled {len(pulled.items)} item(s) of {server.project}/{server.task} under "
                f"{server.label!r} from {server.origin}."
            )
            stored_profile = folder / "profile.yaml"
            if options.profile and stored_profile.is_file():
                _config_error(
                    f"--profile with --server: the label {server.label!r} has its own profile; "
                    "leave --profile out so the label decides what runs"
                )
            if not options.profile:
                if not stored_profile.is_file():
                    _config_error(f"the label {server.label!r} has no profile.yaml: give --profile")
                options.profile = str(stored_profile)
            target = _inside(folder, path)
            store = FileStore(settings.reports_dir)
            options.store = store
            try:
                result = asyncio.run(launch(target, options, settings, log=typer.echo))
            except ConfigError as exc:
                typer.secho(f"configuration error: {exc}", fg="red", err=True)
                typer.secho(
                    f"Nothing was reported to {server.origin}: the run could not be carried out.",
                    fg="yellow",
                    err=True,
                )
                return EXIT_HARNESS
            result = remote.report_run(
                client,
                server,
                store,
                result,
                ci=remote.ci_metadata(),
                catalogue=pulled,
                retries=settings.server_upload_retries,
                log=typer.echo,
            )
    finally:
        shutil.rmtree(pulled_into, ignore_errors=True)
    return _finish(result, settings, open_report, junit, store)


ProjectRunOpt = Annotated[
    Optional[str],  # noqa: UP045
    typer.Option(
        "--project",
        help="Server mode: org/project. Runs go to the database (OPSEVAL_DATABASE_URL) and "
        "the blob store, not to a folder (ADR-0051). With --server: the server's project.",
    ),
]
TaskRunOpt = Annotated[str, typer.Option("--task", help="Server mode: the project's task.")]


@app.command()
def run(
    path: Annotated[
        Path,
        typer.Argument(
            help="Scenario file, directory or suite file. With --server, a path inside the "
            "pulled catalogue (suites/smoke.yaml)."
        ),
    ],
    profile: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            "--profile",
            "-p",
            help="SUT profile YAML, or builtin:<name>. With --server, the label's profile.yaml.",
        ),
    ] = None,
    *,
    target: Annotated[
        str, typer.Option("--target", "-t", help="Agent URL (ws://, wss://, http(s)://).")
    ],
    transport: Annotated[
        str, typer.Option(help="websocket | http-json | sse | openai-chat | audio-ws | audio-http")
    ] = "websocket",
    repeats: Annotated[Optional[int], typer.Option(min=1, max=20, help="Override repeats.")] = None,  # noqa: UP045
    allow_commit: Annotated[
        bool, typer.Option(help="Allow scenarios to perform the irreversible outcome.")
    ] = False,
    fail_fast: Annotated[
        Optional[bool],  # noqa: UP045
        typer.Option(
            "--fail-fast/--no-fail-fast",
            help="Stop after the first failed scenario the gate blocks on.",
            show_default="the suite's stop: policy",
        ),
    ] = None,
    max_consecutive_failures: Annotated[
        Optional[int],  # noqa: UP045
        typer.Option(min=1, help="Stop after this many failed scenarios in a row."),
    ] = None,
    stop_on_critical_failure: Annotated[
        Optional[bool],  # noqa: UP045
        typer.Option(
            "--stop-on-critical-failure/--no-stop-on-critical-failure",
            help="Stop after a critical or blocker scenario fails.",
            show_default="the suite's stop: policy",
        ),
    ] = None,
    stop_on_rate_limit: Annotated[
        Optional[bool],  # noqa: UP045
        typer.Option(
            "--stop-on-rate-limit/--no-stop-on-rate-limit",
            help="Stop when a rate limit outlasts its retries and invalidates an attempt.",
            show_default="the suite's stop: policy",
        ),
    ] = None,
    tenants: Annotated[
        Optional[Path],  # noqa: UP045
        typer.Option(help="Test-tenant pool YAML. Each scenario leases one tenant exclusively."),
    ] = None,
    lanes: Annotated[
        Optional[int],  # noqa: UP045
        typer.Option(min=1, help="Scenarios in flight. Never more than the pool has tenants."),
    ] = None,
    max_wall_clock: Annotated[
        Optional[float],  # noqa: UP045
        typer.Option(
            "--max-wall-clock",
            min=1,
            help="Start no scenario once the run has taken this many seconds.",
        ),
    ] = None,
    preflight: Annotated[
        bool,
        typer.Option(help="Check dependencies first, so an outage skips instead of failing."),
    ] = True,
    verify_llm: Annotated[
        bool, typer.Option(help="Spend one token per LLM role proving the credentials work.")
    ] = False,
    filter_: Annotated[
        list[str] | None, typer.Option("--filter", help="tag=…, id=… or criticality=…")
    ] = None,
    reports_dir: Annotated[Optional[Path], typer.Option(help="Where to write reports.")] = None,  # noqa: UP045
    traces: Annotated[
        bool, typer.Option(help="Read the agent's traces if the profile declares a source.")
    ] = True,
    junit: JunitOpt = None,
    gate: GateOpt = None,
    open_report: Annotated[
        bool, typer.Option("--open", help="Open report.html when done.")
    ] = False,
    database_url: DatabaseUrlOpt = None,
    project: ProjectRunOpt = None,
    task: TaskRunOpt = "default",
    server: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            "--server",
            help="Report to an Opseval server (ADR-0058): pull the catalogue under --label for "
            "--project/--task, run here, upload the run. Key: OPSEVAL_PUBLIC_KEY and "
            "OPSEVAL_SECRET_KEY. The exit code is always the local verdict's.",
        ),
    ] = None,
    label: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            "--label", help="With --server: the catalogue label to run.", show_default="production"
        ),
    ] = None,
) -> None:
    """Run scenarios against a deployed agent and gate the release."""
    settings = Settings()
    if reports_dir:
        settings.reports_dir = str(reports_dir)
    if tenants:
        settings.tenants_file = str(tenants)
    if lanes is not None:
        settings.lanes = lanes
    options = RunOptions(
        target=target,
        transport=transport,
        profile=profile,
        repeats=repeats,
        allow_commit=allow_commit,
        stop=_stop_flags(
            fail_fast=fail_fast,
            max_consecutive_failures=max_consecutive_failures,
            stop_on_critical_failure=stop_on_critical_failure,
            stop_on_rate_limit=stop_on_rate_limit,
            max_wall_clock_seconds=max_wall_clock,
        ),
        filters=filter_ or [],
        traces=traces,
        preflight=preflight,
        verify_llm=verify_llm,
        gate=gate,
    )
    if server is not None:
        if database_url:
            _config_error("--server and --database-url: report to a server, or write to a database")
        if not project:
            _config_error("--server needs --project org/project (and --task, default 'default')")
        raise typer.Exit(
            _execute_remote(
                path, options, settings, open_report, junit, server, project, task, label
            )
        )
    if label is not None:
        _config_error("--label needs --server (or use `opseval catalogue pull --label`)")
    if profile is None:
        _config_error("--profile is required (a SUT profile YAML, or builtin:<name>)")
    store = _server_store(settings, database_url, project, task)
    options.store = store
    try:
        code = _execute(path, options, settings, open_report, junit)
    finally:
        if store is not None:
            store.close()
    raise typer.Exit(code)


def _stop_flags(**flags: object) -> StopPolicy | None:
    """Only the flags given: each replaces that one field of the suite's ``stop:`` policy."""
    given = {name: value for name, value in flags.items() if value is not None}
    return StopPolicy.model_validate(given) if given else None


@app.command()
def prune(
    older_than: Annotated[
        Optional[int],  # noqa: UP045
        typer.Option("--older-than", min=1, help="Days. Default: OPSEVAL_RETENTION_DAYS (90)."),
    ] = None,
    audio_older_than: Annotated[
        Optional[int],  # noqa: UP045
        typer.Option(
            "--audio-older-than",
            min=1,
            help="Days for recorded audio. Default: OPSEVAL_AUDIO_RETENTION_DAYS (30).",
        ),
    ] = None,
    reports_dir: Annotated[Optional[Path], typer.Option(help="The reports folder.")] = None,  # noqa: UP045
    yes: Annotated[
        bool, typer.Option("--yes", help="Delete. Without it, only show what would go.")
    ] = False,
    database_url: DatabaseUrlOpt = None,
    project: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            "--project",
            help="Server mode: prune org/project's runs in the database and its blob store, "
            "and the blobs left pending by a crash (ADR-0051).",
        ),
    ] = None,
) -> None:
    """Delete finished runs older than N days, and recorded audio older than M days (a dry
    run unless you pass --yes).

    A run that may still be going is never touched: only one with a result, an error, or a
    process on this host that is gone. Nothing but run folders is touched. Audio goes before
    the run: the run keeps every turn's hash and transcript, and reads "audio pruned on
    <date>". Exit 3 if anything could not be fully deleted (the rest still is, and listed).
    """

    settings = Settings()
    root = reports_dir or Path(settings.reports_dir)
    days = older_than or settings.retention_days
    audio_days = audio_older_than or settings.audio_retention_days
    db = _server_store(settings, database_url, project, None)
    try:
        _prune(db or FileStore(root), db, days, audio_days, yes)
    finally:
        if db is not None:
            db.close()


def _prune(store: RunStore, db: DbRunStore | None, days: int, audio_days: int, yes: bool) -> None:
    from opseval.retention import apply, apply_audio, plan_audio
    from opseval.retention import plan as plan_prune

    root = store.where()
    items = plan_prune(store, older_than_days=days)
    doomed = [i for i in items if i.action == "delete"]
    sounds = plan_audio(store, older_than_days=audio_days, skip=frozenset(i.run_id for i in doomed))
    for item in items:
        mark = "delete" if item.action == "delete" else "keep  "
        when = item.started_at.date().isoformat() if item.started_at else "unknown date"
        typer.echo(f"  {mark}  {item.run_id}  ({when}: {item.reason})")
    for sound in sounds:
        when = sound.started_at.date().isoformat()
        typer.echo(
            f"  audio   {sound.run_id}  ({when}: {sound.files} audio file(s), older than "
            f"{audio_days} days; the record stays)"
        )
    orphans = db.orphans() if db is not None else []
    for orphan in orphans:
        typer.echo(
            f"  orphan  {orphan.run_id}/{orphan.name}  (pending since "
            f"{orphan.created_at:%Y-%m-%d %H:%M}; its run is no longer running)"
        )
    if not doomed and not sounds and not orphans:
        typer.echo(
            f"Nothing to prune in {root}: no finished run is older than {days} days, and no "
            f"recorded audio older than {audio_days} days."
        )
        return
    if not yes:
        typer.echo(
            f"{len(doomed)} run(s) and the audio of {len(sounds)} run(s) would be deleted. "
            "Re-run with --yes to delete them."
        )
        return
    heard = apply_audio(store, sounds, older_than_days=audio_days)
    outcome = apply(store, items)
    if db is not None:
        # Listed again: a run whose delete failed part-way left its blobs pending, and they
        # are swept now, whatever happened to the other runs (review of #93).
        try:
            cleaned = db.delete_orphans(db.orphans())
        except HarnessError as exc:
            outcome.failed.append(("orphaned blobs", f"{type(exc).__name__}: {exc}"))
        else:
            if cleaned.deleted:
                typer.echo(f"Deleted {len(cleaned.deleted)} orphaned blob(s).")
            outcome.failed.extend(cleaned.failed)
    typer.echo(
        f"Deleted {len(outcome.deleted)} run(s) and the audio of {len(heard.deleted)} run(s) "
        f"from {root}."
    )
    failed = [*heard.failed, *outcome.failed]
    for run_id, why in failed:
        typer.secho(f"  could not delete {run_id}: {why}", fg="red", err=True)
    if failed:
        typer.secho(
            f"{len(failed)} run(s) could not be fully pruned; "
            + (
                "the next prune retries what is left." if db is not None else "remove them by hand."
            ),
            fg="red",
            err=True,
        )
        raise typer.Exit(EXIT_HARNESS)


@app.command()
def cancel(
    run_dir: Annotated[Path, typer.Argument(help="A running run's folder, e.g. reports/<run_id>.")],
) -> None:
    """Ask a running run to stop (it may run in another process, or the console).

    The run stops before its next attempt and records everything it did not run as
    cancelled, so the result still says what was and was not tested. Exit 0 when the
    request is recorded, 3 when there is no such running run.
    """
    folder = run_dir.resolve()
    if not (folder / "manifest.json").is_file():
        typer.secho(f"error: {run_dir}: not a run folder (no manifest.json)", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS)
    if (folder / "result.json").is_file() or (folder / "error.json").is_file():
        typer.secho(f"error: {run_dir}: this run has already finished", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS)
    store = FileStore(folder.parent)
    progress = store.load_progress(folder.name)
    if progress is not None and progress.stage == "finishing":
        typer.secho(
            f"error: {run_dir}: this run is writing its result; it is too late to cancel",
            fg="red",
            err=True,
        )
        raise typer.Exit(EXIT_HARNESS)
    if store.request_cancel(folder.name, by="cli"):
        typer.echo(f"Cancel requested: {folder.name} stops before its next attempt.")
    else:
        typer.echo(f"Cancel was already requested for {folder.name}.")


@app.command()
def rescore(
    run_dir: Annotated[Path, typer.Argument(help="A finished run folder, e.g. reports/<run_id>.")],
    catalogue: Annotated[
        Optional[Path],  # noqa: UP045
        typer.Option(help="Score against these scenario files instead of the run's snapshot."),
    ] = None,
    profile: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option("--profile", "-p", help="SUT profile to use with --catalogue."),
    ] = None,
    junit: JunitOpt = None,
    gate: GateOpt = None,
    open_report: Annotated[
        bool, typer.Option("--open", help="Open report.html when done.")
    ] = False,
) -> None:
    """Re-score saved records offline with the current scorers (no agent, no simulator)."""
    from opseval.rescore import rescore_run

    settings = Settings()
    try:
        outcome = asyncio.run(
            rescore_run(
                run_dir, settings, catalogue=catalogue, profile=profile, gate=gate, log=typer.echo
            )
        )
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    info = outcome.info
    typer.echo(f"Re-scored {info.of_run_id} (scenarios from the {info.scenario_source}).")
    for change in info.scorer_changes:
        if change.what in ("added", "removed"):
            typer.echo(f"  {change.what} {change.scorer_id}")
        else:
            typer.echo(
                f"  changed {change.scorer_id} {change.what}: {change.before} → {change.after}"
            )
    if not info.scorer_changes:
        typer.echo("  no scorer version or judge prompt changed")
    if info.reused_judge_scores:
        typer.echo(
            f"  {info.reused_judge_scores} judge score(s) reused (same prompt, no judge configured)"
        )
    typer.echo("")
    typer.echo(console_summary(outcome.result))
    out_dir = outcome.out_dir or run_dir / f"rescore-{outcome.stamp}"
    typer.echo(f"Report: {(out_dir / 'report.html').resolve()}")
    if junit is not None:
        junit.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out_dir / "junit.xml", junit)
        typer.echo(f"JUnit:  {junit.resolve()}")
    if open_report:
        webbrowser.open((out_dir / "report.html").resolve().as_uri())
    raise typer.Exit(outcome.result.exit_code)


@app.command()
def summary(
    run_dir: Annotated[Path, typer.Argument(help="A finished run folder, e.g. reports/<run_id>.")],
    report_url: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(help="Link to the full report (e.g. a CI artifact URL)."),
    ] = None,
) -> None:
    """Print a Markdown gate summary (for PR comments and CI job summaries)."""
    from opseval.reporting.markdown import markdown_summary

    typer.echo(markdown_summary(_finished_run(run_dir), report_url=report_url))


telemetry_app = typer.Typer(
    help="See, enable or disable the anonymous usage counts (ADR-0010).", no_args_is_help=True
)
app.add_typer(telemetry_app, name="telemetry")
app.add_typer(db_app, name="db")
app.add_typer(catalogue_app, name="catalogue")
app.add_typer(task_app, name="task")
app.add_typer(runs_app, name="runs")
app.add_typer(admin_app, name="admin")
app.add_typer(audio_app, name="audio")


def _sample_result() -> SuiteResult:
    """A stand-in result, so `show --sample` needs no run."""
    from datetime import UTC, datetime

    from opseval.models.results import Gate

    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SuiteResult(
        opseval_version=__version__,
        run_id="sample",
        suite="smoke",
        target="wss://agent.example.com/ws",
        profile="my-agent",
        started_at=now,
        ended_at=now,
        tests=[],
        gate=Gate(status="pass", summary="sample"),
        exit_code=0,
    )


@telemetry_app.command("show")
def telemetry_show(
    run_dir: Annotated[
        Optional[Path],  # noqa: UP045
        typer.Option(help="Show the payload for this finished run instead of a sample."),
    ] = None,
    sample: Annotated[
        bool, typer.Option("--sample", help="Show a sample payload without reading a run.")
    ] = False,
) -> None:
    """Print the status and the exact JSON that would be sent. Sends nothing."""
    import json as _json

    from opseval.models.telemetry import TelemetryEvent
    from opseval.telemetry import build_payload, config_path, decide, install_id

    settings = Settings()
    decision = decide(settings)
    typer.echo(f"Status:    {'enabled' if decision.send else 'not sending'} — {decision.reason}")
    typer.echo(f"Endpoint:  {decision.endpoint or '(none configured; nothing is ever sent)'}")
    typer.echo(f"Config:    {config_path()}")
    typer.echo(f"Install:   {install_id()}")
    typer.echo("")
    result = _sample_result() if sample or run_dir is None else _finished_run(run_dir)
    payload = build_payload(
        result,
        event=TelemetryEvent.RUN,
        install=install_id(),
        transports=["websocket"] if sample or run_dir is None else [],
        languages=["en"] if sample or run_dir is None else [],
        ci=settings.ci,
    )
    typer.echo("This is everything that would be sent:")
    typer.echo(_json.dumps(payload.model_dump(by_alias=True, mode="json"), indent=2))


@telemetry_app.command("disable")
def telemetry_disable() -> None:
    """Turn telemetry off for this user, permanently."""
    from opseval.telemetry import set_enabled

    typer.echo(f"Telemetry disabled in {set_enabled(False)}")


@telemetry_app.command("enable")
def telemetry_enable() -> None:
    """Turn telemetry back on for this user."""
    from opseval.telemetry import set_enabled

    path = set_enabled(True)
    typer.echo(
        f"Telemetry enabled in {path}. It still sends nothing unless "
        "OPSEVAL_TELEMETRY_ENDPOINT is set and allowlisted."
    )


@app.command()
def schemas(
    out: Annotated[Path, typer.Option(help="Where to write them.")] = Path("schemas/v1"),
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Do not write; fail if the committed schemas are out of date."
        ),
    ] = False,
) -> None:
    """Generate the published JSON Schemas from the models.

    The models are the source of truth. `--check` is what CI runs: a committed schema
    that has drifted is worse than none, because an editor would accept a file the
    loader rejects.
    """
    from opseval.schemas import PUBLISHED, stale, write_all

    if check:
        problems = stale(out)
        if problems:
            typer.secho("the published schemas are out of date:", fg="red", err=True)
            for problem in problems:
                typer.secho(f"  {problem}", fg="red", err=True)
            typer.secho("run `opseval schemas` and commit the result", fg="red", err=True)
            raise typer.Exit(EXIT_HARNESS)
        typer.echo(f"{len(PUBLISHED)} schema(s) match the models.")
        return
    for path in write_all(out):
        typer.echo(f"wrote {path}")


@app.command()
def preflight(
    catalogue: Annotated[Path, typer.Argument(help="Suite or scenario folder to check for.")],
    profile: Annotated[str, typer.Option("--profile", "-p", help="SUT profile file.")],
    target: Annotated[str, typer.Option("--target", "-t", help="Agent URL.")],
    transport: Annotated[
        str, typer.Option(help="websocket | http-json | sse | openai-chat | audio-ws | audio-http")
    ] = "websocket",
    traces: Annotated[bool, typer.Option(help="Include the trace source check.")] = True,
    verify_llm: Annotated[
        bool, typer.Option(help="Spend one token per LLM role proving the credentials work.")
    ] = False,
) -> None:
    """Check what a suite depends on, without running it.

    Exit codes: 0 everything needed is available, 2 something is missing (scenarios
    depending on it would be skipped, not failed), 3 configuration error.
    """
    from opseval.preflight import preflight as run_preflight

    settings = Settings()
    try:
        sut_profile = load_profile(profile)
        found, _name = load_catalogue(
            catalogue,
            sut_profile,
            known_scorers=REGISTRY,
            known_invariants=set(INVARIANTS),
        )
        report = asyncio.run(
            run_preflight(
                found.scenarios,
                sut_profile,
                settings,
                target=target,
                transport=transport,
                traces_enabled=traces and sut_profile.traces is not None,
                verify_llm=verify_llm,
                # A voice transport speaks and listens: check the TTS and STT too (ADR-0054).
                voice_scenarios=len(found.scenarios) if transport in VOICE_TRANSPORTS else 0,
                voice_language=voice_language_for(found.scenarios, sut_profile),
            )
        )
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    typer.echo(report.summary())
    raise typer.Exit(EXIT_NOT_JUDGED if report.blocking() else 0)


def _finished_run(run_dir: Path) -> SuiteResult:
    path = run_dir / "result.json"
    try:
        return load_result_at(run_dir)
    except (OSError, ValueError, ConfigError) as exc:
        typer.secho(f"error: {path}: not a finished run ({exc})", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc


@app.command()
def calibrate(
    run_dir: Annotated[Path, typer.Argument(help="A finished run folder, e.g. reports/<run_id>.")],
    labels: Annotated[
        Optional[Path],  # noqa: UP045
        typer.Option(help="YAML file of human labels to compare the scores against."),
    ] = None,
    template_for: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            "--template-for",
            help="Instead of calibrating, print a blank label file for this scorer id.",
        ),
    ] = None,
    min_labels: Annotated[
        int, typer.Option(min=1, max=10_000, help="Usable labels needed before reporting.")
    ] = 5,
    min_agreement: Annotated[float, typer.Option(min=0.0, max=1.0)] = 0.8,
    max_false_pass_rate: Annotated[
        float,
        typer.Option(min=0.0, max=1.0, help="Share of human-failed attempts the judge may pass."),
    ] = 0.1,
    labelled_by: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(help="Who produced the labels (recorded in calibration.json)."),
    ] = None,
) -> None:
    """Measure how well a scorer agrees with human labels (offline, read-only).

    Exit codes follow the suite: 0 agrees, 1 disagrees, 2 not enough usable labels to
    tell, 3 configuration error.
    """
    from opseval.calibrate import (
        calibrate_run,
        calibration_summary,
        label_template,
        save_calibration,
    )

    if template_for:
        try:
            typer.echo(label_template(_finished_run(run_dir), template_for))
        except ConfigError as exc:
            typer.secho(f"configuration error: {exc}", fg="red", err=True)
            raise typer.Exit(EXIT_HARNESS) from exc
        return
    if labels is None:
        typer.secho(
            "error: pass --labels path/to/labels.yaml, or --template-for <scorer id> to start one",
            fg="red",
            err=True,
        )
        raise typer.Exit(EXIT_HARNESS)
    try:
        result = calibrate_run(
            run_dir,
            labels,
            min_labels=min_labels,
            min_agreement=min_agreement,
            max_false_pass_rate=max_false_pass_rate,
            labelled_by=labelled_by,
        )
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    typer.echo(calibration_summary(result))
    typer.echo(f"Written: {save_calibration(run_dir, result)}")
    raise typer.Exit(result.exit_code)


@app.command()
def demo(
    bug: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            help="Inject a defect into the sample agent: skip-approval | forget-method | crash; "
            "with --voice: silent | garbled; with --voice --duplex: talks-over"
        ),
    ] = None,
    repeats: Annotated[int, typer.Option(min=1, max=20)] = 3,
    junit: JunitOpt = None,
    open_report: Annotated[
        bool, typer.Option("--open", help="Open report.html when done.")
    ] = False,
    voice: Annotated[
        bool,
        typer.Option(
            "--voice",
            help="Run the speaking demo agent through the voice bridge, with fake loopback "
            "TTS and STT (synthetic voices, no speech model or key).",
        ),
    ] = False,
    voice_transport: Annotated[
        str, typer.Option(help="With --voice: audio-ws | audio-http.")
    ] = "audio-ws",
    stt_down: Annotated[
        bool, typer.Option("--stt-down", help="With --voice: the fake STT answers 503.")
    ] = False,
    stt_deaf: Annotated[
        bool,
        typer.Option(
            "--stt-deaf", help="With --voice: the fake STT hears nothing, not even the canary."
        ),
    ] = False,
    duplex: Annotated[
        bool,
        typer.Option(
            "--duplex",
            help="With --voice: run the full-duplex driver (ADR-0055): the caller pauses, "
            "speaks over noise and cuts in on the agent, and the voice metrics gate the run.",
        ),
    ] = False,
) -> None:
    """Start the sample refund agent and run the smoke suite against it (no LLM keys needed)."""
    from opseval.demo import catalogue_dir
    from opseval.demo.refund_agent import BUGS, run_agent

    if voice:
        raise typer.Exit(
            _voice_demo(
                bug, repeats, junit, open_report, voice_transport, stt_down, stt_deaf, duplex
            )
        )
    if stt_down or stt_deaf or duplex:
        typer.secho("--stt-down, --stt-deaf and --duplex need --voice", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS)
    if bug is not None and bug not in BUGS:
        typer.secho(f"unknown bug {bug!r}; choose from {', '.join(BUGS)}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS)
    cat = catalogue_dir()
    settings = Settings()
    settings.allowed_hosts = ",".join(filter(None, [settings.allowed_hosts, "127.0.0.1"]))

    async def _go() -> int:
        async with run_agent(bug=bug) as url:  # type: ignore[arg-type]
            typer.echo(f"Sample refund agent running at {url} (bug: {bug or 'none'})\n")
            options = RunOptions(target=url, profile=cat / "profile.yaml", repeats=repeats)
            try:
                result = await launch(
                    cat / "suites" / "smoke.yaml", options, settings, log=typer.echo
                )
            except ConfigError as exc:
                typer.secho(f"configuration error: {exc}", fg="red", err=True)
                return EXIT_HARNESS
        return _finish(result, settings, open_report, junit)

    raise typer.Exit(asyncio.run(_go()))


def _voice_demo(
    bug: str | None,
    repeats: int,
    junit: Path | None,
    open_report: bool,
    transport: str,
    stt_down: bool,
    stt_deaf: bool,
    duplex: bool = False,
) -> int:
    """``opseval demo --voice`` (ADR-0053): the speaking agent and fake speech on loopback.
    ``--duplex`` runs the full-duplex suite (ADR-0055) on ``audio-ws``."""
    from opseval.demo import voice_catalogue_dir
    from opseval.demo.voice_agent import DUPLEX_BUGS, VOICE_BUGS, run_voice_demo

    bugs = (*VOICE_BUGS, *DUPLEX_BUGS) if duplex else VOICE_BUGS
    if bug is not None and bug not in bugs:
        typer.secho(
            f"unknown voice bug {bug!r}; choose from {', '.join(bugs)}"
            + ("" if duplex else f" ({', '.join(DUPLEX_BUGS)} needs --duplex)"),
            fg="red",
            err=True,
        )
        return EXIT_HARNESS
    if transport not in ("audio-ws", "audio-http"):
        typer.secho("--voice-transport is audio-ws or audio-http", fg="red", err=True)
        return EXIT_HARNESS
    if duplex and transport != "audio-ws":
        typer.secho(
            "--duplex needs --voice-transport audio-ws: audio-http hears the caller only once "
            "a turn is over",
            fg="red",
            err=True,
        )
        return EXIT_HARNESS
    if stt_down and stt_deaf:
        typer.secho("choose one of --stt-down and --stt-deaf", fg="red", err=True)
        return EXIT_HARNESS
    cat = voice_catalogue_dir()
    speech = "down" if stt_down else "deaf" if stt_deaf else "ok"

    async def _go() -> int:
        async with run_voice_demo(
            bug,  # type: ignore[arg-type]
            speech=speech,  # type: ignore[arg-type]
            transport=transport,  # type: ignore[arg-type]
            duplex=duplex,
        ) as (url, speech_url):
            settings = Settings()
            settings.allowed_hosts = ",".join(filter(None, [settings.allowed_hosts, "127.0.0.1"]))
            # The demo's own synthetic speech: the fake TTS and STT on loopback, every other
            # speech setting cleared so a real provider in the environment is never used.
            settings.tts_provider = settings.stt_provider = "openai"
            settings.tts_url = settings.stt_url = speech_url
            settings.tts_model, settings.stt_model = "demo-tts", "demo-stt"
            settings.tts_key = settings.stt_key = None
            settings.tts_voice_map = {"default": "demo"}
            settings.tts_format = "wav"
            settings.stt_second_provider = settings.stt_second_url = None
            settings.stt_second_model = None
            settings.stt_canary_wav = settings.stt_canary_text = None
            settings.stt_canary_language = None
            settings.tts_timeout_seconds = settings.stt_timeout_seconds = 5.0
            # Synthetic voices may be kept as plain WAV; with OPSEVAL_AUDIO_ENCRYPTION_KEY set
            # they are sealed like a real run's, so `opseval audio export` can be tried.
            settings.record_audio = "files"
            what = f"bug: {bug or 'none'}; STT: {speech}" + ("; full duplex" if duplex else "")
            typer.echo(f"Speaking refund agent at {url} over {transport} ({what})\n")
            options = RunOptions(
                target=url,
                transport=transport,
                profile=cat / "profile.yaml",
                repeats=repeats,
                synthetic_voices=True,
            )
            try:
                suite = "duplex.yaml" if duplex else "smoke.yaml"
                result = await launch(cat / "suites" / suite, options, settings, log=typer.echo)
            except ConfigError as exc:
                typer.secho(f"configuration error: {exc}", fg="red", err=True)
                return EXIT_HARNESS
        return _finish(result, settings, open_report, junit)

    return asyncio.run(_go())


@app.command()
def serve(
    catalogue: Annotated[
        Optional[Path],  # noqa: UP045
        typer.Option(
            help="Catalogue folder (scenarios/, suites/, profile.yaml). Default: the "
            "built-in demo, read-only."
        ),
    ] = None,
    profile: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(help="SUT profile file or builtin:<name>. Default: <catalogue>/profile.yaml."),
    ] = None,
    target: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(help="Default agent target for runs started from the console."),
    ] = None,
    host: Annotated[
        str, typer.Option(help="Bind address. Loopback only (127.0.0.1, ::1 or localhost).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    allow_commit: Annotated[
        bool,
        typer.Option(
            help="Let runs started from the console perform the irreversible outcome, for "
            "scenarios that also say allow_commit: true (ADR-0008)."
        ),
    ] = False,
    console_url: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            help="Where the console is, for the printed link. Default: served here when "
            "built, otherwise the Vite dev server (http://localhost:5173)."
        ),
    ] = None,
    database_url: DatabaseUrlOpt = None,
    project: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            "--project",
            help="Server mode: read runs from the database for org/project, and write the "
            "runs the console starts there (ADR-0051).",
        ),
    ] = None,
    task: TaskRunOpt = "default",
    public_url: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            "--public-url",
            help="Accounts mode (ADR-0050): the https address people reach this server at. "
            "Default: OPSEVAL_PUBLIC_URL.",
        ),
    ] = None,
    trusted_proxy: Annotated[
        Optional[list[str]],  # noqa: UP045
        typer.Option(
            "--trusted-proxy",
            help="Address or CIDR of a reverse proxy whose forwarded headers are believed "
            "(repeatable). Default: OPSEVAL_TRUSTED_PROXIES.",
        ),
    ] = None,
    tls_cert: Annotated[
        Optional[Path],  # noqa: UP045
        typer.Option("--tls-cert", help="TLS certificate (PEM) to serve https directly."),
    ] = None,
    tls_key: Annotated[
        Optional[Path],  # noqa: UP045
        typer.Option("--tls-key", help="The certificate's private key (PEM)."),
    ] = None,
) -> None:
    """Serve the HTTP API the web console talks to.

    Shared-token mode (no database, no public URL): loopback only, one token (ADR-0031).
    Accounts mode (a database or --public-url): users, roles and API keys (ADR-0050).
    """
    try:
        import uvicorn

        from opseval.server import create_app
        from opseval.server.access import PublicUrl, check_bind, host_name
        from opseval.server.access import parse_proxies as _proxies
    except ImportError as exc:  # pragma: no cover - depends on the extra
        typer.secho("the API needs the extra: pip install 'opseval[serve]'", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    from opseval.demo import catalogue_dir

    settings = Settings()
    url = public_url or settings.public_url
    accounts_mode = bool(database_url or settings.database_url or url)

    def refuse(message: str) -> typer.Exit:
        typer.secho(f"configuration error: {message}", fg="red", err=True)
        return typer.Exit(EXIT_HARNESS)

    if (tls_cert is None) != (tls_key is None):
        raise refuse("--tls-cert and --tls-key go together")
    try:
        parsed = PublicUrl.parse(url) if url else None
        proxies = _proxies(trusted_proxy or settings.trusted_proxies)
        check_bind(
            host,
            accounts=accounts_mode,
            public_url=parsed,
            tls=tls_cert is not None,
            proxies=proxies,
        )
    except ConfigError as exc:
        raise refuse(str(exc)) from exc
    if accounts_mode and not (database_url or settings.database_url):
        raise refuse(
            "accounts mode (--public-url) keeps users, roles and keys in the database: set "
            "OPSEVAL_DATABASE_URL and --project org/project"
        )
    if accounts_mode and project is None:
        raise refuse(
            "a database is configured, so opseval serve runs in accounts mode (ADR-0050), and "
            "users and roles belong to a project: add --project org/project (and --task)"
        )
    root = catalogue or catalogue_dir()
    prof = profile or str(root / "profile.yaml")
    access = None
    store: DbRunStore | None
    if accounts_mode and project is not None:
        try:
            store = open_run_store(
                server_settings(settings, database_url),
                project=project,
                task=task,
                task_reads=True,
            )
        except ConfigError as exc:
            raise refuse(str(exc)) from exc
        try:
            from opseval.accounts.setup import access_config

            access = access_config(
                settings,
                store,
                project=project,
                task=task or "default",
                public_url=url,
                trusted_proxies=trusted_proxy,
            )
        except ImportError as exc:  # pragma: no cover - depends on the extra
            store.close()
            raise refuse("accounts mode needs the extra: pip install 'opseval[server]'") from exc
        except ConfigError as exc:
            store.close()
            raise refuse(str(exc)) from exc
    else:
        store = _server_store(settings, database_url, project, task)
    try:
        api = create_app(
            store=store,
            close_store=True,
            settings=settings,
            catalogue=root,
            profile=prof,
            target=target,
            read_only=catalogue is None,
            allow_commit=allow_commit,
            access=access,
        )
    except ConfigError as exc:
        raise refuse(str(exc)) from exc
    scheme = "https" if tls_cert is not None else "http"
    here = f"{scheme}://{'[::1]' if host_name(host) == '::1' else host}:{port}"
    typer.echo(f"Opseval API on {here}/api/v1")
    if console_url is None:
        if api.state.console is not None:
            console_url = parsed.origin if parsed is not None else here
        else:
            console_url = "http://localhost:5173"
            typer.echo(
                "No built console found: run `cd packages/console && npm ci && npm run build` "
                "(or set OPSEVAL_CONSOLE_DIR) to have it served here; meanwhile use `npm run dev`."
            )
    if access is not None:
        served = access.accounts.served
        typer.echo(f"Accounts: {served.project_ref} task {served.task_slug} (ADR-0050)")
        typer.echo(f"Console:  {console_url}/login   (no shared token: sign in, or use an API key)")
    elif settings.serve_token is not None:
        # A configured token is long-lived: never echo it into logs or scrollback.
        typer.echo(f"Console:  {console_url}/#token=<OPSEVAL_SERVE_TOKEN>")
    else:
        typer.echo(f"Console:  {console_url}/#token={api.state.token}   (this start only)")
    if port != 8000 and console_url != here:  # the Vite proxy points at :8000 by default
        typer.echo(f"          for `npm run dev`, set OPSEVAL_API=http://127.0.0.1:{port}")
    if catalogue is None:
        typer.echo("Catalogue: the built-in demo (read-only). Use --catalogue to edit your own.")
    if allow_commit:
        typer.secho("Runs may perform the irreversible outcome (--allow-commit).", fg="yellow")
    try:
        uvicorn.run(
            api,
            host=host,
            port=port,
            log_level="info",
            # Forwarded headers are read by the app, from trusted proxies only (ADR-0050):
            # uvicorn's own handling would believe them from loopback.
            proxy_headers=False,
            ssl_certfile=str(tls_cert) if tls_cert else None,
            ssl_keyfile=str(tls_key) if tls_key else None,
        )
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()  # idempotent: the app's lifespan closed it already on a clean stop


def entrypoint() -> None:
    """The ``opseval`` console script: ``app`` with a harness-error boundary.

    An exception nothing else handled (a full disk, a template bug, a bug in Opseval) would
    otherwise reach the interpreter and exit **1**, which CI reads as "the agent failed".
    It is a harness problem, so it exits 3 and keeps the traceback for the bug report
    (ADR-0030). ``SystemExit`` is not an ``Exception`` and passes through unchanged. Ctrl-C
    (and the SIGINT a cancelled CI job sends) never gets here: Typer turns it into exit 130,
    which is why ``typer>=0.15`` is required (older Typer exits 1, "the agent failed").
    """
    try:
        app()
    except Exception as exc:  # the CLI boundary: the one place a catch-all belongs
        traceback.print_exc()
        typer.secho(
            f"internal error ({type(exc).__name__}): this is a harness problem, not an agent "
            "failure. Please report it with the traceback above.",
            fg="red",
            err=True,
        )
        raise SystemExit(EXIT_HARNESS) from exc


if __name__ == "__main__":  # pragma: no cover
    entrypoint()
