# SPDX-License-Identifier: Apache-2.0
"""Write-side of the API: rehearse the simulator, launch runs, save suites, import scenarios.

Everything here either succeeds completely or leaves the catalogue exactly as it was, and
a run it starts always ends in a visible state: finished, or a harness error with the
reason. Never a silent absence (ADR-0030, ADR-0031).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from opseval.catalogue import check_references, load_catalogue
from opseval.config import Settings
from opseval.errors import ConfigError, OpsevalError
from opseval.llm import UsageMeter, client_for_role
from opseval.models.profile import SutProfile
from opseval.models.scenario import Scenario, Suite
from opseval.runner import RunOptions, launch, new_run_id
from opseval.scoring import INVARIANTS, REGISTRY
from opseval.server import schemas as s
from opseval.simulator import LLMSimulator, ScriptedSimulator, Simulator, attempt_seed
from opseval.storage import FileStore
from opseval.verdicts import EXIT_HARNESS

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.ya?ml$")

log = logging.getLogger("opseval.server")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class ServerContext:
    """What one ``opseval serve`` process was started with."""

    settings: Settings
    store: FileStore
    catalogue: Path | None = None
    profile: SutProfile | None = None
    profile_path: str | Path | None = None
    target: str | None = None
    read_only: bool = False
    """The catalogue may not be edited: the built-in demo lives inside the installed
    package, and writing there would corrupt it (or fail on a read-only install)."""
    allow_commit: bool = False
    """Started with ``--allow-commit``: the operator's half of the ADR-0008 double opt-in.
    A request body can never supply it."""
    threads: set[threading.Thread] = field(default_factory=set)

    def need_catalogue(self) -> tuple[Path, SutProfile]:
        if self.catalogue is None or self.profile is None:
            raise ApiError(400, "NO_CATALOGUE", "the server was started without a catalogue")
        return self.catalogue, self.profile

    def need_writable(self) -> tuple[Path, SutProfile]:
        root, profile = self.need_catalogue()
        if self.read_only:
            raise ApiError(
                409,
                "READ_ONLY_CATALOGUE",
                "the built-in demo catalogue cannot be edited; create your own with "
                "`opseval init ./my-catalogue` and start "
                "`opseval serve --catalogue ./my-catalogue`",
            )
        return root, profile


def scenario_root(catalogue: Path) -> Path:
    return catalogue / "scenarios" if (catalogue / "scenarios").is_dir() else catalogue


def _load(target: Path, profile: SutProfile) -> list[Scenario]:
    cat, _ = load_catalogue(
        target, profile, known_scorers=REGISTRY.keys(), known_invariants=set(INVARIANTS)
    )
    return cat.scenarios


def _scenario(ctx: ServerContext, sid: str) -> Scenario:
    root, profile = ctx.need_catalogue()
    for sc in _load(scenario_root(root), profile):
        if sc.id == sid:
            return sc
    raise ApiError(404, "UNKNOWN_SCENARIO", f"no valid scenario {sid!r}")


def persona(ctx: ServerContext, sid: str) -> s.PersonaOut:
    sc = _scenario(ctx, sid)
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
        simulator_model=ctx.settings.simulator_model,
        judge_model=ctx.settings.judge_model,
    )


async def rehearse(ctx: ServerContext, body: s.RehearseIn) -> s.RehearseOut:
    """One simulator turn against a transcript the human typed. Touches no agent."""
    sc = await asyncio.to_thread(_scenario, ctx, body.scenario_id)
    transcript: list[tuple[str, str]] = [(str(t.who), t.text) for t in body.transcript][-40:]
    if sum(len(t) for _, t in transcript) > 20_000:
        raise ApiError(
            413, "TRANSCRIPT_TOO_LONG", "the rehearsal transcript is capped at 20k characters"
        )
    sim: Simulator
    model: str | None = None
    if sc.conversation.scripted_turns:
        sim = ScriptedSimulator(sc, attempt_seed(sc.id, 0))
    else:
        client = client_for_role(ctx.settings, "simulator", UsageMeter())
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


def record_failure(store: FileStore, run_id: str, exc: BaseException) -> None:
    """A run that could not be carried out is a harness error, recorded where the run is."""
    store.save_error(
        run_id,
        {
            "run_id": run_id,
            "code": "CONFIG_ERROR" if isinstance(exc, ConfigError) else "HARNESS_ERROR",
            "message": str(exc) or type(exc).__name__,
            "exit_code": EXIT_HARNESS,
            "at": datetime.now(UTC).isoformat(),
        },
    )


def start_run(ctx: ServerContext, body: s.LaunchIn) -> s.LaunchOut:
    """Start a run in its own thread and return its id at once; the console polls /runs."""
    root, _ = ctx.need_catalogue()
    if body.allow_commit and not ctx.allow_commit:
        raise ApiError(
            403,
            "COMMIT_NOT_ALLOWED",
            "performing the irreversible outcome needs the server started with "
            "`opseval serve --allow-commit` as well as `allow_commit: true` in the scenario "
            "(ADR-0008)",
        )
    target = body.target or ctx.target
    if not target:
        raise ApiError(400, "NO_TARGET", "give a target (ws://…) or start the server with --target")
    only_ids: list[str] | None = None
    if body.scenarios:
        only_ids = list(dict.fromkeys(body.scenarios))  # a repeated id is the same scenario
        suite_path = scenario_root(root)
    elif (
        body.suite
        and SAFE_NAME.match(body.suite)
        and (root / "suites" / f"{body.suite}.yaml").is_file()
    ):
        suite_path = root / "suites" / f"{body.suite}.yaml"
    else:
        raise ApiError(404, "UNKNOWN_SUITE", "choose an existing suite or some scenarios")

    run_id = new_run_id()
    options = RunOptions(
        target=target,
        profile=ctx.profile_path,
        repeats=body.repeats,
        allow_commit=body.allow_commit and ctx.allow_commit,
        fail_fast=body.fail_fast,
        run_id=run_id,
        only_ids=only_ids,
    )

    def work() -> None:
        # Its own thread and event loop: a run does blocking file I/O and can take minutes,
        # and must not stall the API the console is polling meanwhile.
        try:
            asyncio.run(launch(suite_path, options, ctx.settings))
        except Exception as exc:  # the run boundary: every failure must leave a record
            log.exception("run %s could not be carried out", run_id)
            record_failure(ctx.store, run_id, exc)

    thread = threading.Thread(target=work, name=f"opseval-run-{run_id}", daemon=True)
    ctx.threads.add(thread)
    thread.start()
    return s.LaunchOut(
        accepted=True, run_id=run_id, message=f"run {run_id} started against {target}"
    )


def _suite_doc(body: s.SuiteIn) -> dict[str, object]:
    doc: dict[str, object] = {
        "name": body.display_name,
        "description": body.description,
        "include": body.include,
    }
    if body.repeats:
        doc["overrides"] = {"execution": {"repeats": body.repeats}}
    if body.stop_at:
        doc["defaults"] = {"conversation": {"stop_at": body.stop_at}}
    return doc


def save_suite(ctx: ServerContext, body: s.SuiteIn) -> s.SuiteOut:
    """Validate the suite under a temporary name next to its home, then swap it in."""
    root, profile = ctx.need_writable()
    if not SAFE_NAME.match(body.file):
        raise ApiError(422, "BAD_NAME", "file name may use letters, digits, '.', '_' and '-' only")
    doc = _suite_doc(body)
    try:
        Suite.model_validate(doc)
    except ValidationError as exc:
        raise ApiError(422, "INVALID_SUITE", str(exc)) from exc
    folder = root / "suites"
    path = folder / f"{body.file}.yaml"
    if path.exists() and not body.overwrite:
        raise ApiError(
            409, "SUITE_EXISTS", f"suite {body.file!r} exists; send overwrite to replace it"
        )
    folder.mkdir(parents=True, exist_ok=True)
    # A dot-file beside the real one, so includes resolve exactly as they will. It is never
    # listed (list_suites skips dot-files) and is removed whatever happens.
    temp = folder / f".{body.file}.{uuid.uuid4().hex[:8]}.yaml"
    try:
        temp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), "utf-8")
        try:
            count = len(_load(temp, profile))
        except ConfigError as exc:
            raise ApiError(422, "INVALID_SUITE", str(exc)) from exc
        if count == 0:
            # A suite that selects nothing would "pass" by running nothing.
            raise ApiError(422, "EMPTY_SUITE", "the include patterns match no valid scenario")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return s.SuiteOut(
        file=body.file,
        display_name=body.display_name,
        description=body.description,
        include=body.include,
        scenario_count=count,
        repeats=body.repeats,
        stop_at=body.stop_at,
    )


def _problem(path: str, fld: str, message: str) -> s.Problem:
    return s.Problem(path=path, field=fld, message=message)


def import_scenarios(ctx: ServerContext, body: s.ImportIn) -> s.ImportOut:
    """All-or-nothing: validate every file first, write only if all are valid and new."""
    root, profile = ctx.need_writable()
    if not SAFE_NAME.match(body.family):
        raise ApiError(422, "BAD_FAMILY", "family may use letters, digits, '.', '_' and '-' only")
    scenarios = root / "scenarios"
    folder = scenarios / body.family
    existing_ids = {sc.id for sc in _load(scenarios, profile)} if scenarios.is_dir() else set()
    problems: list[s.Problem] = []
    parsed: list[tuple[str, str, Scenario]] = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    for f in body.files:
        if not SAFE_FILE.match(f.name):
            problems.append(_problem(f.name, "name", "not a safe .yaml file name"))
            continue
        if f.name in seen_names:
            problems.append(_problem(f.name, "name", "file name given twice"))
            continue
        seen_names.add(f.name)
        if (folder / f.name).exists():
            problems.append(_problem(f.name, "name", f"would replace {body.family}/{f.name}"))
            continue
        try:
            sc = Scenario.model_validate(yaml.safe_load(f.text))
        except (yaml.YAMLError, ValidationError, TypeError) as exc:
            problems.append(_problem(f.name, "", str(exc)[:500]))
            continue
        for msg in check_references(sc, profile, REGISTRY, set(INVARIANTS)):
            problems.append(_problem(f.name, "", msg))
        if sc.id in existing_ids:
            problems.append(_problem(f.name, "id", f"would replace existing scenario {sc.id!r}"))
        elif sc.id in seen_ids:
            problems.append(_problem(f.name, "id", f"id {sc.id!r} given twice"))
        seen_ids.add(sc.id)
        parsed.append((f.name, f.text, sc))
    if problems:
        return s.ImportOut(imported=[], problems=problems)
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        for name, text, _ in parsed:
            path = folder / name
            with path.open("x", encoding="utf-8") as out:  # "x": never over a file
                written.append(path)  # ours from the moment it exists
                out.write(text)
    except OSError as exc:
        for path in written:  # all or nothing, even when the disk says no halfway
            path.unlink(missing_ok=True)
        raise ApiError(500, "WRITE_FAILED", f"nothing was imported: {exc}") from exc
    return s.ImportOut(imported=[sc.id for _, _, sc in parsed], problems=[])
