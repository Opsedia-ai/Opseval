# SPDX-License-Identifier: Apache-2.0
"""Write-side of the API: rehearse the simulator, launch runs, save suites, import scenarios.

Everything here either succeeds completely or leaves the catalogue exactly as it was, and
a run it starts always ends in a visible state: finished, or a harness error with the
reason. Never a silent absence (ADR-0030, ADR-0031).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from opseval.advice import Configured, scenario_warnings
from opseval.catalogue import check_scenario_text, load_catalogue
from opseval.config import Settings
from opseval.errors import ConfigError, OpsevalError
from opseval.llm import UsageMeter, client_for_role
from opseval.models.gate import GatePolicy, from_flat, to_yaml_block
from opseval.models.profile import SutProfile
from opseval.models.scenario import Scenario, Suite
from opseval.models.stop import StopPolicy
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
        target, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
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
        try:
            client = client_for_role(ctx.settings, "simulator", UsageMeter())
        except ConfigError as exc:
            # The persona would be sent to a host the operator has not allowlisted.
            raise ApiError(503, "SIMULATOR_NOT_ALLOWED", str(exc)) from exc
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
        turn=s.Turn(who="customer", text=turn.text),
        goal_reached=turn.goal_reached and not turn.script_exhausted,
        script_exhausted=turn.script_exhausted,
        model=model,
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
        stop=stop_override(body.stop),
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

    threading.Thread(target=work, name=f"opseval-run-{run_id}", daemon=True).start()
    return s.LaunchOut(
        accepted=True, run_id=run_id, message=f"run {run_id} started against {target}"
    )


def _set_nested(doc: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    """Set ``doc[a][b][c] = value``, or remove it (and emptied parents) when value is None."""
    *parents, leaf = path
    chain: list[dict[str, Any]] = [doc]
    for key in parents:
        nxt = chain[-1].get(key)
        if not isinstance(nxt, dict):
            if value is None:
                return
            nxt = chain[-1][key] = {}
        chain.append(nxt)
    if value is None:
        chain[-1].pop(leaf, None)
        for parent, key in zip(reversed(chain[:-1]), reversed(parents), strict=True):
            if parent.get(key) == {}:
                parent.pop(key)
    else:
        chain[-1][leaf] = value


def _suite_doc(body: s.SuiteIn, existing: dict[str, Any] | None) -> dict[str, Any]:
    """The suite file to write. Replacing a suite keeps every key the form does not manage
    (filters, exclude, other overrides): the console edits a suite, it does not rewrite it.

    For ``repeats`` and ``stop_at``, a field left out of the request keeps the file's value
    and an explicit ``null`` removes it.
    """
    doc: dict[str, Any] = dict(existing or {})
    doc.update(name=body.display_name, description=body.description, include=body.include)
    sent = body.model_fields_set
    if "gate" in sent:
        block = None if body.gate is None else to_yaml_block(gate_policy(body.gate))
        if block is None:
            doc.pop("gate", None)  # the plain standard gate needs no block
        else:
            doc["gate"] = block
    if "stop" in sent:
        block = stop_block(body.stop)
        if block:
            doc["stop"] = block
        else:
            doc.pop("stop", None)  # no policy: every scenario runs
    if "repeats" in sent or existing is None:
        _set_nested(doc, ("overrides", "execution", "repeats"), body.repeats)
    if "stop_at" in sent or existing is None:
        _set_nested(doc, ("defaults", "conversation", "stop_at"), body.stop_at)
    return doc


def stop_block(body: s.StopIn | None) -> dict[str, Any]:
    """The fields a request set: a run's overrides, or a suite's ``stop:`` block."""
    return {} if body is None else body.model_dump(exclude_none=True)


def stop_override(body: s.StopIn | None) -> StopPolicy | None:
    """Only the fields given, so each replaces that one field of the suite's policy."""
    block = stop_block(body)
    return StopPolicy.model_validate(block) if block else None


def cancel_run(ctx: ServerContext, run_id: str) -> s.CancelOut:
    """Ask a running run to stop before its next attempt (ADR-0035).

    The request is a file in the run folder, so this cancels a run started by the CLI as
    well as one started here. What the run did not do is recorded as cancelled.
    """
    from opseval.server.views import RunInProgress, run_detail

    try:
        detail = run_detail(ctx.store, run_id)
    except RunInProgress as running:
        if running.progress is not None and running.progress.stage == "finishing":
            # Every attempt has run: there is no "next attempt" left to stop before.
            raise ApiError(
                409, "RUN_FINISHING", "this run is writing its result; it is too late to cancel"
            ) from None
        detail = None
    else:
        if detail is None:
            raise ApiError(404, "UNKNOWN_RUN", f"no run {run_id!r}")
        state = detail.summary.state
        raise ApiError(
            409,
            "RUN_NOT_RUNNING",
            "this run has finished" if state == "finished" else "this run is no longer running",
        )
    if ctx.store.request_cancel(run_id, by="console"):
        return s.CancelOut(
            accepted=True,
            run_id=run_id,
            message="cancel requested: it stops before its next attempt",
        )
    return s.CancelOut(accepted=False, run_id=run_id, message="a cancel was already requested")


def gate_policy(body: s.GateIn) -> GatePolicy:
    """The ``gate:`` block for an edit, refused with 422 if any setting is not allowed."""
    try:
        return from_flat(body.preset, body.overrides)
    except ValueError as exc:  # a pydantic ValidationError is a ValueError
        raise ApiError(422, "INVALID_GATE", str(exc)) from exc


def preview_gate(ctx: ServerContext, body: s.GateIn) -> s.GateOut:
    """What an edited gate would resolve to, before it is saved."""
    from opseval.server.views import gate_out

    return gate_out(gate_policy(body), fail_on_flaky=ctx.settings.gate_fail_on_flaky)


def save_suite(ctx: ServerContext, body: s.SuiteIn) -> s.SuiteOut:
    """Validate the suite under a temporary name next to its home, then swap it in."""
    root, profile = ctx.need_writable()
    if not SAFE_NAME.match(body.file):
        raise ApiError(422, "BAD_NAME", "file name may use letters, digits, '.', '_' and '-' only")
    folder = root / "suites"
    path = folder / f"{body.file}.yaml"
    existing: dict[str, Any] | None = None
    if path.exists():
        if not body.overwrite:
            raise ApiError(
                409, "SUITE_EXISTS", f"suite {body.file!r} exists; send overwrite to replace it"
            )
        try:
            loaded = yaml.safe_load(path.read_text("utf-8"))
        except yaml.YAMLError:
            loaded = None  # an unreadable file is replaced whole, as the user asked
        existing = loaded if isinstance(loaded, dict) else None
    doc = _suite_doc(body, existing)
    try:
        Suite.model_validate(doc)
    except ValidationError as exc:
        raise ApiError(422, "INVALID_SUITE", str(exc)) from exc
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
        repeats=_nested_get(doc, "overrides", "execution", "repeats"),
        stop_at=_nested_get(doc, "defaults", "conversation", "stop_at"),
        gate=_gate_view(doc, ctx.settings.gate_fail_on_flaky),
        stop=_stop_view(doc),
    )


def _nested_get(doc: dict[str, Any], *path: str) -> Any:
    cur: Any = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _stop_view(doc: dict[str, Any]) -> s.StopOut | None:
    from opseval.server.views import stop_out

    return stop_out(StopPolicy.model_validate(doc["stop"])) if doc.get("stop") else None


def _gate_view(doc: dict[str, Any], fail_on_flaky: bool) -> s.GateOut:
    from opseval.server.views import gate_out

    return gate_out(GatePolicy.model_validate(doc.get("gate") or {}), fail_on_flaky=fail_on_flaky)


def _problem(path: str, fld: str, message: str) -> s.Problem:
    return s.Problem(path=path, field=fld, message=message)


def import_scenarios(ctx: ServerContext, body: s.ImportIn) -> s.ImportOut:
    """All-or-nothing: validate every file first, write only if all are valid and new."""
    root, profile = ctx.need_writable()
    if not SAFE_NAME.match(body.family):
        raise ApiError(422, "BAD_FAMILY", "family may use letters, digits, '.', '_' and '-' only")
    # The same root the rest of the server reads, so a flat catalogue (scenarios at the
    # top) is checked for clashes and its scenarios do not vanish behind a new folder.
    scenarios = scenario_root(root)
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
        found, found_problems = check_scenario_text(
            f.text,
            f.name,
            profile,
            known_scorers=REGISTRY,
            known_invariants=set(INVARIANTS),
        )
        problems.extend(_problem(p.path, p.field, p.message) for p in found_problems)
        if found is None:
            continue
        sc = found
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


# --- the scenario editor ---------------------------------------------------------------

_INDEX: dict[Path, tuple[tuple[tuple[str, int, int], ...], dict[str, Path]]] = {}
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _id_index(ctx: ServerContext) -> dict[str, Path]:
    """Valid scenario id -> file, parsed again only when a scenario file changed (the
    editor validates as you type: reparsing the catalogue on every pause is too slow)."""
    root, profile = ctx.need_catalogue()
    folder = scenario_root(root)
    files = sorted(folder.rglob("*.y*ml")) if folder.is_dir() else []
    signature = tuple(
        (str(f), f.stat().st_mtime_ns, f.stat().st_size)
        for f in files
        if not f.name.startswith(".")
    )
    cached = _INDEX.get(folder)
    if cached is not None and cached[0] == signature:
        return cached[1]
    cat, _ = load_catalogue(
        folder, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
    )
    index = {sid: Path(src).resolve() for sid, src in cat.sources.items()}
    _INDEX[folder] = (signature, index)
    return index


def _rel(root: Path, path: Path) -> str:
    """A path for display: relative to the catalogue when it is inside it."""
    resolved, base = path.resolve(), root.resolve()
    return resolved.relative_to(base).as_posix() if resolved.is_relative_to(base) else str(path)


def _scenario_file(ctx: ServerContext, rel: str) -> tuple[Path, Path]:
    """(catalogue root, file) for a catalogue-relative scenario file, valid or not: the
    files that fail validation are the ones that most need the editor."""
    root, _ = ctx.need_catalogue()
    path = (root / rel).resolve()
    inside = path.is_relative_to(scenario_root(root).resolve())
    if not inside or path.suffix not in (".yaml", ".yml") or not path.is_file():
        raise ApiError(404, "UNKNOWN_SCENARIO_FILE", f"no scenario file {rel!r}")
    return root, path


def _source(root: Path, path: Path, data: bytes, sid: str | None) -> s.ScenarioSourceOut:
    return s.ScenarioSourceOut(
        file=_rel(root, path), id=sid, text=data.decode("utf-8"), etag=_etag(data)
    )


def scenario_source(ctx: ServerContext, rel: str) -> s.ScenarioSourceOut:
    root, path = _scenario_file(ctx, rel)
    sid = next((k for k, v in _id_index(ctx).items() if v == path), None)
    return _source(root, path, path.read_bytes(), sid)


def scenario_source_by_id(ctx: ServerContext, sid: str) -> s.ScenarioSourceOut:
    root, _ = ctx.need_catalogue()
    path = _id_index(ctx).get(sid)
    if path is None:
        raise ApiError(404, "UNKNOWN_SCENARIO", f"no valid scenario {sid!r}")
    return _source(root, path, path.read_bytes(), sid)


def _suites_selecting(root: Path, sid: str) -> list[str]:
    """Suites that pick this id by filter, which a rename would leave selecting nothing."""
    out = []
    for f in sorted((root / "suites").glob("[!.]*.yaml")):
        try:
            raw = yaml.safe_load(f.read_text("utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        filters = raw.get("filters", []) if isinstance(raw, dict) else []
        if isinstance(filters, list) and f"id={sid}" in filters:
            out.append(f.stem)
    return out


def check_scenario(ctx: ServerContext, body: s.ScenarioTextIn) -> s.ScenarioCheckOut:
    """The loader's verdict on one scenario's text (catalogue.check_scenario_text), plus
    what only the catalogue can say: an id used by another file, a rename that would
    empty a suite, and, for a new file, whether its folder and name can be written."""
    root, profile = ctx.need_catalogue()
    where = body.file or (f"{body.family}/{body.file_name}" if body.file_name else "(new)")
    sc, problems = check_scenario_text(
        body.text, where, profile, known_scorers=REGISTRY, known_invariants=set(INVARIANTS)
    )
    out = [_problem(p.path, p.field, p.message) for p in problems]
    editing = _scenario_file(ctx, body.file)[1] if body.file else None
    if body.file is None and body.file_name is not None:
        family = body.family or "uncategorised"
        if not SAFE_NAME.match(family):
            out.append(_problem(where, "family", "letters, digits, '.', '_' and '-' only"))
        elif not SAFE_FILE.match(body.file_name):
            out.append(_problem(where, "file", "a .yaml file name: letters, digits, '.', '_', '-'"))
        elif (scenario_root(root) / family / body.file_name).exists():
            out.append(_problem(where, "file", f"{family}/{body.file_name} already exists"))
    if sc is not None:
        index = _id_index(ctx)
        other = index.get(sc.id)
        if other is not None and other != editing:
            out.append(
                _problem(where, "id", f"id {sc.id!r} is already used by {_rel(root, other)}")
            )
        before = next((k for k, v in index.items() if v == editing), None) if editing else None
        if before is not None and before != sc.id:
            for suite in _suites_selecting(root, before):
                out.append(
                    _problem(
                        where, "id", f"suite {suite!r} selects id={before}: renaming empties it"
                    )
                )
    warnings = []
    if sc is not None:
        here = Configured(
            judge=bool(ctx.settings.judge_base_url and ctx.settings.judge_model),
            simulator=bool(ctx.settings.simulator_base_url and ctx.settings.simulator_model),
        )
        warnings = [
            _problem(w.path, w.field, w.message)
            for w in scenario_warnings(sc, where, profile, here)
        ]
    return s.ScenarioCheckOut(
        ok=sc is not None and not out,
        id=sc.id if sc else None,
        title=sc.name if sc else None,
        problems=out,
        warnings=warnings,
    )


def _lock(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


def save_scenario_source(
    ctx: ServerContext, rel: str, body: s.ScenarioSaveIn
) -> s.ScenarioSourceOut:
    """Replace one scenario file with valid text, never over a newer version of it.

    The check of the version and the write happen under one lock per file, so of two
    saves of the same version the second is refused rather than silently replacing the
    first; validating (slow) happens before the lock. Line endings and permissions are
    kept, so a save shows in git as the lines that changed, not the whole file.
    """
    ctx.need_writable()
    root, path = _scenario_file(ctx, rel)
    check = check_scenario(ctx, s.ScenarioTextIn(text=body.text, file=rel))
    if not check.ok:
        raise ApiError(422, "INVALID_SCENARIO", "; ".join(p.message for p in check.problems))
    with _lock(path):
        current = path.read_bytes()
        if _etag(current) != body.etag:
            raise ApiError(
                409, "SCENARIO_CHANGED", "the file changed on disk since you opened it; reload it"
            )
        text = body.text.replace("\r\n", "\n")
        if b"\r\n" in current:
            text = text.replace("\n", "\r\n")
        data = text.encode("utf-8")
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}")
        try:
            temp.write_bytes(data)
            shutil.copymode(path, temp)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return _source(root, path, data, check.id)
