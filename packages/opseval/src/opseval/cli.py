# SPDX-License-Identifier: Apache-2.0
"""The ``opseval`` command line.

Exit codes: 0 pass · 1 fail · 2 not judged · 3 harness or configuration error.
"""

from __future__ import annotations

import asyncio
import shutil
import traceback
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
from opseval.verdicts import EXIT_HARNESS, EXIT_NOT_JUDGED

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
    if THIRD_PARTY:
        typer.echo(f"\n{len(THIRD_PARTY)} scorer(s) come from installed plugins, marked [dist].")
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
        str, typer.Option(help="websocket | http-json | sse | openai-chat")
    ] = "websocket",
    repeats: Annotated[Optional[int], typer.Option(min=1, max=20, help="Override repeats.")] = None,  # noqa: UP045
    allow_commit: Annotated[
        bool, typer.Option(help="Allow scenarios to perform the irreversible outcome.")
    ] = False,
    fail_fast: Annotated[bool, typer.Option(help="Stop after the first failed scenario.")] = False,
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
        preflight=preflight,
        verify_llm=verify_llm,
    )
    raise typer.Exit(_execute(path, options, settings, open_report, junit))


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
    open_report: Annotated[
        bool, typer.Option("--open", help="Open report.html when done.")
    ] = False,
) -> None:
    """Re-score saved records offline with the current scorers (no agent, no simulator)."""
    from opseval.rescore import rescore_run

    settings = Settings()
    try:
        outcome = asyncio.run(
            rescore_run(run_dir, settings, catalogue=catalogue, profile=profile, log=typer.echo)
        )
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    info = outcome.info
    typer.echo(f"Re-scored {info.of_run_id} (scenarios from the {info.scenario_source}).")
    for change in info.scorer_changes:
        typer.echo(f"  changed {change.scorer_id} {change.what}: {change.before} → {change.after}")
    if not info.scorer_changes:
        typer.echo("  no scorer version or judge prompt changed")
    if info.reused_judge_scores:
        typer.echo(
            f"  {info.reused_judge_scores} judge score(s) reused (same prompt, no judge configured)"
        )
    typer.echo("")
    typer.echo(console_summary(outcome.result))
    typer.echo(f"Report: {(outcome.out_dir / 'report.html').resolve()}")
    if junit is not None:
        junit.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(outcome.out_dir / "junit.xml", junit)
        typer.echo(f"JUnit:  {junit.resolve()}")
    if open_report:
        webbrowser.open((outcome.out_dir / "report.html").resolve().as_uri())
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

    path = run_dir / "result.json"
    try:
        result = SuiteResult.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        typer.secho(f"error: {path}: not a finished run ({exc})", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    typer.echo(markdown_summary(result, report_url=report_url))


telemetry_app = typer.Typer(
    help="See, enable or disable the anonymous usage counts (ADR-0010).", no_args_is_help=True
)
app.add_typer(telemetry_app, name="telemetry")


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
        str, typer.Option(help="websocket | http-json | sse | openai-chat")
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
            known_scorers=REGISTRY.keys(),
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
        return SuiteResult.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
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


@app.command()
def serve(
    catalogue: Annotated[
        Optional[Path],  # noqa: UP045
        typer.Option(
            help="Catalogue folder (scenarios/, suites/, profile.yaml). Default: the demo."
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
        str, typer.Option(help="Bind address. Loopback unless you add auth.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
) -> None:
    """Serve the HTTP API the web console talks to."""
    try:
        import uvicorn

        from opseval.server import create_app
    except ImportError as exc:  # pragma: no cover - depends on the extra
        typer.secho("the API needs the extra: pip install 'opseval[serve]'", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    from opseval.demo import catalogue_dir

    root = catalogue or catalogue_dir()
    prof = profile or str(root / "profile.yaml")
    try:
        api = create_app(settings=Settings(), catalogue=root, profile=prof, target=target)
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg="red", err=True)
        raise typer.Exit(EXIT_HARNESS) from exc
    uvicorn.run(api, host=host, port=port, log_level="info")


def entrypoint() -> None:
    """The ``opseval`` console script: ``app`` with a harness-error boundary.

    An exception nothing else handled (a full disk, a template bug, a bug in Opseval) would
    otherwise reach the interpreter and exit **1**, which CI reads as "the agent failed".
    It is a harness problem, so it exits 3 and keeps the traceback for the bug report
    (ADR-0029). ``SystemExit`` is not an ``Exception`` and passes through unchanged. Note that
    Click already turns Ctrl-C into "Aborted!" and exit 1 before it gets here.
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
