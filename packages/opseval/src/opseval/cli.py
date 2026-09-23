# SPDX-License-Identifier: Apache-2.0
"""The ``opseval`` command line.

Exit codes: 0 pass · 1 fail · 2 not judged · 3 harness or configuration error.
"""

from __future__ import annotations

import asyncio
import shutil
import webbrowser
from pathlib import Path
from typing import Annotated, Optional

import typer

from opseval import __version__
from opseval.catalogue import builtin_profiles, load_catalogue, load_profile
from opseval.config import Settings
from opseval.errors import ConfigError
from opseval.models.results import SuiteResult
from opseval.reporting import console_summary
from opseval.runner import RunOptions, launch
from opseval.scaffold import TEMPLATES, scaffold
from opseval.scoring import INVARIANTS, REGISTRY
from opseval.verdicts import EXIT_HARNESS

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


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Where to create the catalogue.")] = Path(
        "opseval"
    ),
    template: Annotated[
        str, typer.Option(help=" | ".join(f"{k}: {v}" for k, v in TEMPLATES.items()))
    ] = "openai-chat",
    force: Annotated[bool, typer.Option(help="Overwrite existing files.")] = False,
) -> None:
    """Create a starter catalogue (profile, scenarios, suites) that validates as is."""
    try:
        written = scaffold(directory, template, force=force)
    except ConfigError as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    for path in written:
        typer.echo(f"  created {path}")
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
            path, prof, known_scorers=REGISTRY.keys(), known_invariants=set(INVARIANTS)
        )
    except ConfigError as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    for problem in cat.problems:
        typer.secho(f"✗ {problem}", fg="red")
    typer.echo(
        f"{len(cat.scenarios)} valid scenario(s) in {suite!r}; {len(cat.problems)} problem(s)."
    )
    raise typer.Exit(EXIT_HARNESS if cat.problems else 0)


@app.command("list-scorers")
def list_scorers() -> None:
    """List every scorer with its axis and the evidence it needs, plus built-in profiles."""
    for sid, cls in sorted(REGISTRY.items()):
        typer.echo(f"{sid:<32} {cls.axis.value:<9} needs {cls.requires:<7} {cls.description}")
    typer.echo("\nInvariant bundles: " + ", ".join(f"{k} → {v}" for k, v in INVARIANTS.items()))
    typer.echo("Built-in profiles: " + ", ".join(f"builtin:{n}" for n in builtin_profiles()))


def _finish(result: SuiteResult, settings: Settings, open_report: bool, junit: Path | None) -> int:
    typer.echo("")
    typer.echo(console_summary(result))
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
    return _finish(result, settings, open_report, junit)


@app.command()
def run(
    path: Annotated[Path, typer.Argument(help="Scenario file, directory or suite file.")],
    profile: ProfileOpt,
    target: Annotated[
        str, typer.Option("--target", "-t", help="Agent URL (ws://, wss://, http(s)://).")
    ],
    transport: Annotated[
        str, typer.Option(help="websocket | http-json | openai-chat")
    ] = "websocket",
    repeats: Annotated[Optional[int], typer.Option(min=1, max=20, help="Override repeats.")] = None,  # noqa: UP045
    allow_commit: Annotated[
        bool, typer.Option(help="Allow scenarios to perform the irreversible outcome.")
    ] = False,
    fail_fast: Annotated[bool, typer.Option(help="Stop after the first failed scenario.")] = False,
    filter_: Annotated[
        list[str] | None, typer.Option("--filter", help="tag=…, id=… or criticality=…")
    ] = None,
    reports_dir: Annotated[Optional[Path], typer.Option(help="Where to write reports.")] = None,  # noqa: UP045
    traces: Annotated[
        bool, typer.Option(help="Read the agent's traces if the profile declares a source.")
    ] = True,
    junit: JunitOpt = None,
    open_report: Annotated[
        bool, typer.Option("--open", help="Open report.html when done.")
    ] = False,
) -> None:
    """Run scenarios against a deployed agent and gate the release."""
    settings = Settings()
    if reports_dir:
        settings.reports_dir = str(reports_dir)
    options = RunOptions(
        target=target,
        transport=transport,
        profile=profile,
        repeats=repeats,
        allow_commit=allow_commit,
        fail_fast=fail_fast,
        filters=filter_ or [],
        traces=traces,
    )
    raise typer.Exit(_execute(path, options, settings, open_report, junit))


@app.command()
def demo(
    bug: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            help="Inject a defect into the sample agent: skip-approval | forget-method | crash"
        ),
    ] = None,
    repeats: Annotated[int, typer.Option(min=1, max=20)] = 3,
    junit: JunitOpt = None,
    open_report: Annotated[
        bool, typer.Option("--open", help="Open report.html when done.")
    ] = False,
) -> None:
    """Start the sample refund agent and run the smoke suite against it (no LLM keys needed)."""
    from opseval.demo import catalogue_dir
    from opseval.demo.refund_agent import BUGS, run_agent

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


if __name__ == "__main__":  # pragma: no cover
    app()
