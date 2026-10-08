# SPDX-License-Identifier: Apache-2.0
"""Write-side of the API: rehearse the simulator, launch runs, save suites, import scenarios.

Everything here either succeeds completely or leaves the catalogue exactly as it was.
"""

from __future__ import annotations

import asyncio
import re
from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import ValidationError

from opseval.catalogue import load_catalogue
from opseval.config import Settings
from opseval.errors import ConfigError, OpsevalError
from opseval.llm import UsageMeter, client_for_role
from opseval.models.profile import SutProfile
from opseval.models.scenario import Scenario, Suite
from opseval.runner import RunOptions, launch
from opseval.scoring import INVARIANTS, REGISTRY
from opseval.server import schemas as s
from opseval.server.launches import LaunchBook
from opseval.simulator import LLMSimulator, ScriptedSimulator, Simulator, attempt_seed

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _scenario(catalogue: Path | None, profile: SutProfile | None, sid: str) -> Scenario:
    if catalogue is None or profile is None:
        raise ApiError(404, "NO_CATALOGUE", "the server was started without a catalogue")
    root = catalogue / "scenarios" if (catalogue / "scenarios").is_dir() else catalogue
    cat, _ = load_catalogue(root, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS))
    for sc in cat.scenarios:
        if sc.id == sid:
            return sc
    raise ApiError(404, "UNKNOWN_SCENARIO", f"no valid scenario {sid!r}")


def persona(
    settings: Settings, catalogue: Path | None, profile: SutProfile | None, sid: str
) -> s.PersonaOut:
    sc = _scenario(catalogue, profile, sid)
    conv = sc.conversation
    return s.PersonaOut(
        scenario_id=sc.id,
        role=sc.persona.role,
        goal=sc.goal,
        flow=conv.flow,
        language=sc.persona.language,
        dialect=sc.persona.dialect,
        timezone=sc.persona.timezone,
        typing_noise=sc.persona.typing_noise,
        scripted=bool(conv.scripted_turns),
        opening=conv.opening_message or (conv.scripted_turns[0] if conv.scripted_turns else None),
        simulator_model=settings.simulator_model,
        judge_model=settings.judge_model,
    )


async def rehearse(
    settings: Settings, catalogue: Path | None, profile: SutProfile | None, body: s.RehearseIn
) -> s.RehearseOut:
    """One simulator turn against a transcript the human typed. Touches no agent."""
    sc = _scenario(catalogue, profile, body.scenario_id)
    transcript = [(t.who, t.text) for t in body.transcript][-40:]
    if sum(len(t) for _, t in transcript) > 20_000:
        raise ApiError(
            413, "TRANSCRIPT_TOO_LONG", "the rehearsal transcript is capped at 20k characters"
        )
    sim: Simulator
    model: str | None = None
    if sc.conversation.scripted_turns:
        sim = ScriptedSimulator(sc, attempt_seed(sc.id, 0))
    else:
        client = client_for_role(settings, "simulator", UsageMeter())
        if client is None:
            raise ApiError(
                503,
                "SIMULATOR_NOT_CONFIGURED",
                "set OPSEVAL_SIMULATOR_BASE_URL and OPSEVAL_SIMULATOR_MODEL "
                "to rehearse this persona",
            )
        sim, model = LLMSimulator(sc, attempt_seed(sc.id, 0), client), client.model
    try:
        if not transcript:
            turn = await sim.opening()
        else:
            if isinstance(sim, ScriptedSimulator):
                await sim.opening()
                # replay: a scripted persona advances one turn per agent reply
                for _ in range(sum(1 for who, _ in transcript if who == "agent") - 1):
                    await sim.next_turn(transcript)
            turn = await sim.next_turn(transcript)
    except OpsevalError as exc:
        raise ApiError(502, "SIMULATOR_FAILED", str(exc)) from exc
    return s.RehearseOut(
        turn=s.Turn(who="customer", text=turn.text), goal_reached=turn.goal_reached, model=model
    )


_tasks: set[asyncio.Task[object]] = set()


def start_run(
    settings: Settings,
    catalogue: Path | None,
    profile: SutProfile | None,
    profile_path: str | Path,
    default_target: str | None,
    body: s.LaunchIn,
    *,
    allow_commit: bool = False,
    launches: LaunchBook,
) -> s.LaunchOut:
    """Start a run in the background. The console polls /runs for its progress.

    ``allow_commit`` is the *server's*, from ``opseval serve --allow-commit``, never the
    request body's. ADR-0008 makes the irreversible outcome need two keys — the
    scenario's own flag and the operator's — and an HTTP caller holding one of them
    would leave the guard with one.
    """
    if catalogue is None or profile is None:
        raise ApiError(400, "NO_CATALOGUE", "the server was started without a catalogue")
    target = body.target or default_target
    if not target:
        raise ApiError(400, "NO_TARGET", "give a target (ws://…) or start the server with --target")
    filters: list[str] = []
    label: str | None = None
    if body.scenarios:
        # An ad-hoc subset runs through a filter rather than a generated suite file.
        # Writing one into the catalogue (the first version did) means a run mutates the
        # thing it is measuring, and fails outright when the catalogue is read-only.
        root = catalogue / "scenarios" if (catalogue / "scenarios").is_dir() else catalogue
        cat, _ = load_catalogue(
            root, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
        )
        unknown = sorted(set(body.scenarios) - set(cat.sources))
        if unknown:
            raise ApiError(404, "UNKNOWN_SCENARIO", f"no valid scenario {', '.join(unknown)}")
        suite_path = root
        filters = [f"id={','.join(sorted(set(body.scenarios)))}"]
        label = "selection"
    elif (
        body.suite
        and SAFE_NAME.match(body.suite)
        and (catalogue / "suites" / f"{body.suite}.yaml").is_file()
    ):
        suite_path = catalogue / "suites" / f"{body.suite}.yaml"
    else:
        raise ApiError(404, "UNKNOWN_SUITE", "choose an existing suite or some scenarios")
    record = launches.start(label or body.suite or "")

    async def _go() -> None:
        try:
            opts = RunOptions(
                target=target,
                profile=profile_path,
                repeats=body.repeats,
                allow_commit=allow_commit,
                fail_fast=body.fail_fast,
                filters=filters,
                suite_label=label,
                on_start=lambda run_id: launches.attach(record.token, run_id),
            )
            await launch(suite_path, opts, settings)
            launches.finished(record.token)
        except (ConfigError, OpsevalError) as exc:
            # The 202 already went out, so this cannot be raised to anyone. Recording it
            # is the whole point: a run that died must not keep reading as "running".
            launches.failed(record.token, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - an unexpected crash is still ours to report
            launches.failed(record.token, f"{type(exc).__name__}: {exc}")

    task = asyncio.create_task(_go())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return s.LaunchOut(
        accepted=True,
        token=record.token,
        message=f"run started against {target}",
        allow_commit=allow_commit,
    )


def is_writable(catalogue: Path | None) -> bool:
    """False for the demo catalogue inside site-packages.

    ``opseval serve`` with no ``--catalogue`` serves the bundled demo so there is
    something to look at. Writing to it would edit the installed package: the change
    vanishes on reinstall, and on a shared machine it is not even the caller's to make.
    """
    if catalogue is None:
        return False
    package = Path(str(files("opseval"))).resolve()
    try:
        return not catalogue.resolve().is_relative_to(package)
    except (OSError, ValueError):
        return False


def _require_writable(
    catalogue: Path | None, profile: SutProfile | None
) -> tuple[Path, SutProfile]:
    if catalogue is None or profile is None:
        raise ApiError(400, "NO_CATALOGUE", "the server was started without a catalogue")
    if not is_writable(catalogue):
        raise ApiError(
            409,
            "READ_ONLY_CATALOGUE",
            "this is the bundled demo catalogue inside the installed package. Start the "
            "server with --catalogue pointing at your own, or `opseval init` a new one",
        )
    return catalogue, profile


def save_suite(catalogue: Path | None, profile: SutProfile | None, body: s.SuiteIn) -> s.SuiteOut:
    root, profile = _require_writable(catalogue, profile)
    if not SAFE_NAME.match(body.file):
        raise ApiError(422, "BAD_NAME", "file name may use letters, digits, '.', '_' and '-' only")
    doc: dict[str, object] = {
        "name": body.display_name,
        "description": body.description,
        "include": body.include,
    }
    if body.repeats:
        doc["overrides"] = {"execution": {"repeats": body.repeats}}
    if body.stop_at:
        doc["defaults"] = {"conversation": {"stop_at": body.stop_at}}
    try:
        Suite.model_validate(doc)
    except ValidationError as exc:
        raise ApiError(422, "INVALID_SUITE", str(exc)) from exc
    path = root / "suites" / f"{body.file}.yaml"
    if path.exists() and not body.overwrite:
        raise ApiError(
            409,
            "SUITE_EXISTS",
            f"{body.file}.yaml already exists; pass overwrite to replace it. Renaming a "
            "suite instead would stop its old runs lining up in Compare and Trend",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve against the real catalogue *before* the file is in place, so a suite that
    # matches nothing is rejected without having overwritten a working one.
    scratch = path.with_name(f".{body.file}.checking.yaml")
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    scratch.write_text(text, "utf-8")
    try:
        cat, _ = load_catalogue(
            scratch, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
        )
    except ConfigError as exc:
        raise ApiError(422, "INVALID_SUITE", str(exc)) from exc
    finally:
        scratch.unlink(missing_ok=True)
    if not cat.scenarios:
        # A suite matching nothing runs, passes, and reports a clean gate on having
        # tested nothing — the failure ADR-0020 refuses for preflight, arriving by a
        # different door. Saving it is where it can still be stopped.
        raise ApiError(
            422,
            "SUITE_MATCHES_NOTHING",
            "these patterns match no scenario, so the suite would report a clean gate "
            "on having tested nothing. Patterns are relative to the catalogue root, "
            "e.g. scenarios/*.yaml",
        )
    path.write_text(text, "utf-8")
    return s.SuiteOut(
        file=body.file,
        display_name=body.display_name,
        description=body.description,
        include=body.include,
        scenario_count=len(cat.scenarios),
        repeats=body.repeats,
        stop_at=body.stop_at,
    )


def import_scenarios(
    catalogue: Path | None, profile: SutProfile | None, body: s.ImportIn
) -> s.ImportOut:
    """All-or-nothing: validate every file first, write only if all are valid."""
    catalogue, profile = _require_writable(catalogue, profile)
    if not SAFE_NAME.match(body.family):
        raise ApiError(422, "BAD_FAMILY", "family may use letters, digits, '.', '_' and '-' only")
    root = catalogue / "scenarios"
    existing_ids = {sc.id for sc in load_scenarios_ids(root, profile)}
    problems: list[s.Problem] = []
    parsed: list[tuple[str, str, Scenario]] = []
    for f in body.files:
        name, text = f.name, f.text
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml$", name):
            problems.append(
                s.Problem(path=name, field="name", message="not a safe .yaml file name")
            )
            continue
        try:
            raw = yaml.safe_load(text)
            sc = Scenario.model_validate(raw)
        except (yaml.YAMLError, ValidationError, TypeError) as exc:
            problems.append(s.Problem(path=name, field="", message=str(exc)[:500]))
            continue
        from opseval.catalogue import check_references

        for msg in check_references(sc, profile, REGISTRY, set(INVARIANTS)):
            problems.append(s.Problem(path=name, field="", message=msg))
        parsed.append((name, text, sc))
    if problems:
        return s.ImportOut(imported=[], problems=problems)
    clashes = [
        s.Problem(path=name, field="id", message=f"would replace existing scenario {sc.id!r}")
        for name, _, sc in parsed
        if sc.id in existing_ids
    ]
    if clashes:  # never silently overwrite
        return s.ImportOut(imported=[], problems=clashes)
    folder = root / body.family
    folder.mkdir(parents=True, exist_ok=True)
    for name, text, _ in parsed:
        (folder / name).write_text(text, "utf-8")
    return s.ImportOut(imported=[sc.id for _, _, sc in parsed], problems=[])


def load_scenarios_ids(root: Path, profile: SutProfile) -> list[Scenario]:
    if not root.is_dir():
        return []
    cat, _ = load_catalogue(root, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS))
    return cat.scenarios
