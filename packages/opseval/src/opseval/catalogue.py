# SPDX-License-Identifier: Apache-2.0
"""Load and validate scenarios, suites and SUT profiles (fail closed)."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from opseval.datasets import Expansion, Limits, case_label, expand
from opseval.errors import ConfigError
from opseval.langpacks import UnknownLanguage, effective_language, lookup, script_expectation
from opseval.models.gate import GatePolicy
from opseval.models.profile import SutProfile
from opseval.models.scenario import (
    DATASET_FOLDER,
    RESERVED_STOP_AT,
    PolicyAction,
    Scenario,
    Suite,
)
from opseval.models.stop import StopPolicy


@dataclass(frozen=True)
class LoadProblem:
    path: str
    field: str
    message: str

    def __str__(self) -> str:
        where = f"{self.path}: {self.field}" if self.field else self.path
        return f"{where}: {self.message}"


@dataclass
class Catalogue:
    scenarios: list[Scenario] = field(default_factory=list)
    problems: list[LoadProblem] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)  # scenario id -> file
    expansions: dict[str, Expansion] = field(default_factory=dict)
    """Authored id -> what its ``dataset:``/``matrix:`` expanded to (ADR-0060); a scenario
    without either is not listed. ``sources`` names the expanded ids."""
    gate: GatePolicy | None = None  # the suite's gate policy, when a suite was loaded
    stop: StopPolicy | None = None  # the suite's stop policy, when a suite was loaded


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file: {exc}") from exc


def _problems_from(path: Path, exc: ValidationError) -> list[LoadProblem]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        out.append(LoadProblem(str(path), loc, err["msg"]))
    return out


BUILTIN_PREFIX = "builtin:"

KnownScorers = Iterable[str] | Mapping[str, Any]
"""Scorer ids, or the registry itself (id → class), which also lets params be checked."""


def builtin_profiles() -> dict[str, Path]:
    """Profiles shipped with Opseval, usable as ``--profile builtin:<name>``."""
    folder = Path(str(files("opseval") / "profiles"))
    return {f.stem: f for f in sorted(folder.glob("*.yaml"))}


def load_profile(path: str | Path) -> SutProfile:
    """Load a SUT profile from a file, or a built-in one via ``builtin:<name>``."""
    raw = str(path)
    if raw.startswith(BUILTIN_PREFIX):
        name = raw[len(BUILTIN_PREFIX) :]
        known = builtin_profiles()
        if name not in known:
            raise ConfigError(f"unknown built-in profile {name!r}; available: {sorted(known)}")
        path = known[name]
    p = Path(path)
    data = _read_yaml(p)
    try:
        return SutProfile.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(str(x) for x in _problems_from(p, exc))
        raise ConfigError(f"invalid SUT profile: {details}") from exc


def check_references(
    scenario: Scenario,
    profile: SutProfile,
    known_scorers: KnownScorers,
    known_invariants: set[str],
) -> list[str]:
    """Cross-check a scenario against the active profile and scorer catalogue.

    ``known_scorers`` is the scorer ids, or the registry itself (id → class): given the
    registry, each scorer's ``params`` are checked against the model it declares (ADR-0043),
    so a typo fails here rather than becoming *not measured* at run time.
    """
    errors: list[str] = []
    known = set(known_scorers)
    classes = known_scorers if isinstance(known_scorers, Mapping) else {}
    conv = scenario.conversation
    if conv.stop_at not in RESERVED_STOP_AT and conv.stop_at not in profile.checkpoints:
        errors.append(
            f"conversation.stop_at: {conv.stop_at!r} is not a checkpoint of profile "
            f"{profile.name!r} (known: {sorted(profile.checkpoints)})"
        )
    if (
        conv.stop_at == "commit_gate"
        and profile.commit_gate is None
        and "stop_at" in conv.model_fields_set
    ):
        # Left out, stop_at means "stop before the irreversible step, if there is one": with
        # no commit gate the conversation runs to its goal (ADR-0043). Written out, it asks
        # for a gate this profile does not have.
        errors.append(
            "conversation.stop_at: 'commit_gate' needs a profile with a commit_gate; "
            f"profile {profile.name!r} declares none, so set stop_at: goal_reached (or leave "
            "it out)"
        )
    for name, policy in conv.checkpoint_policy.items():
        if name not in profile.checkpoints:
            errors.append(f"conversation.checkpoint_policy.{name}: unknown checkpoint")
        if policy.expected_target_phase and policy.expected_target_phase not in profile.phases:
            errors.append(
                f"conversation.checkpoint_policy.{name}.expected_target_phase: unknown phase "
                f"{policy.expected_target_phase!r}"
            )
        if policy.via.value == "user_message" and not profile.reject_via_message_allowed:
            errors.append(
                f"conversation.checkpoint_policy.{name}.via: profile forbids chat rejects"
            )
        if conv.scripted_turns and policy.action is PolicyAction.SCRIPT:
            errors.append(
                f"conversation.checkpoint_policy.{name}.action: 'script' needs an LLM persona; "
                "scripted conversations must use approve, reject or sequence"
            )
    if conv.llm_persona:
        try:
            # Never a silent fallback: an LLM persona with no pack would write something
            # else, and the persona judge would then blame the agent or the run (ADR-0059).
            # Scripted turns are sent verbatim and need no pack (advice warns instead).
            lookup(scenario.persona.language, scenario.persona.dialect)
        except UnknownLanguage as exc:
            errors.append(f"persona.{exc.field}: {exc}")
    errors += _language_checks(scenario)
    tz = scenario.persona.timezone
    if tz and profile.timezones and tz not in profile.timezones:
        errors.append(
            f"persona.timezone: {tz!r} not accepted by the profile (accepted: {profile.timezones})"
        )
    tools = scenario.expectations.expected_tools
    for tool in [*tools.must_call, *tools.must_not_call]:
        if tool not in profile.tools:
            errors.append(f"expectations.expected_tools: unknown tool {tool!r}")
    for order in tools.ordering:
        for tool in (order.before, order.after):
            if tool not in profile.tools:
                errors.append(f"expectations.expected_tools.ordering: unknown tool {tool!r}")
    for rule in tools.forbidden_before:
        if rule.tool not in profile.tools:
            errors.append(
                f"expectations.expected_tools.forbidden_before: unknown tool {rule.tool!r}"
            )
        if rule.checkpoint not in profile.checkpoints:
            errors.append(
                f"expectations.expected_tools.forbidden_before: unknown checkpoint "
                f"{rule.checkpoint!r}"
            )
    for inv in scenario.expectations.invariants:
        if inv not in known_invariants:
            errors.append(f"expectations.invariants: unknown invariant {inv!r}")
    if isinstance(scenario.scorers, list):
        for index, spec in enumerate(scenario.scorers):
            if spec.id not in known:
                errors.append(
                    f"scorers: unknown scorer id {spec.id!r} (see `opseval list-scorers`)"
                )
            cls = classes.get(spec.id)
            model = getattr(cls, "Params", None)
            if cls is not None and isinstance(model, type) and issubclass(model, BaseModel):
                try:
                    params = model.model_validate(spec.params)
                except ValidationError as exc:
                    for err in exc.errors():
                        where = ".".join(str(part) for part in err["loc"])
                        field = f"scorers.{index}.params" + (f".{where}" if where else "")
                        errors.append(f"{field}: {spec.id}: {err['msg']}")
                else:
                    for problem in cls.check_params(params, profile):
                        where, _, message = problem.partition(": ")
                        errors.append(f"scorers.{index}.params.{where}: {spec.id}: {message}")
            if spec.id == "security.policy_adherence" and not scenario.expectations.policy:
                errors.append(
                    "scorers: 'security.policy_adherence' grades the agent against "
                    "expectations.policy, which is empty; list the rules it must not break, "
                    "or drop the scorer"
                )
    return errors


LANGUAGE_SCRIPT = "output.language_script"


def _language_checks(scenario: Scenario) -> list[str]:
    """What the persona's pack decides and the scenario contradicts or leaves undecided
    (ADR-0059): a code-switching pack and ``voice.code_switching`` naming different mixes
    (the judges would be told one and the simulator the other), and ``output.language_script``
    with no ``script`` when no pack gives one (it could only ever be *not measured*)."""
    errors: list[str] = []
    scripted = not scenario.conversation.llm_persona
    pack = effective_language(scenario.persona, scripted=scripted).pack
    voice = scenario.voice
    if pack is not None and pack.mixes and voice is not None and voice.code_switching:
        said = [lang.strip().lower() for lang in voice.code_switching]
        if sorted(said) != sorted(m.lower() for m in pack.mixes):
            errors.append(
                f"voice.code_switching: {list(voice.code_switching)} disagrees with the "
                f"{pack.tag} pack, which mixes {list(pack.mixes)}; give the same languages, "
                "or leave voice.code_switching out to use the pack's"
            )
    if isinstance(scenario.scorers, list):
        for index, spec in enumerate(scenario.scorers):
            if (
                spec.id != LANGUAGE_SCRIPT
                or spec.params.get("script")
                or spec.params.get("scripts")
            ):
                continue
            if script_expectation(scenario.persona, scripted=scripted) is None:
                errors.append(
                    f"scorers.{index}.params.script: {LANGUAGE_SCRIPT}: no language pack for "
                    f"{scenario.persona.language!r} gives a default script, so it could never "
                    "be measured; give params.script (or params.scripts)"
                )
    return errors


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _load_scenario(
    path: Path, defaults: dict[str, Any], overrides: dict[str, Any]
) -> tuple[Scenario | None, list[LoadProblem]]:
    try:
        raw = _read_yaml(path)
    except ConfigError as exc:
        return None, [LoadProblem(str(path), "", str(exc))]
    if not isinstance(raw, dict):
        return None, [LoadProblem(str(path), "", "a scenario file must contain a mapping")]
    # precedence: suite overrides > scenario value > suite defaults > schema default
    merged = _deep_merge(_deep_merge(defaults, raw), overrides)
    return _validate_authored(merged, path)


LOADER_ONLY = "expanded_from"


def _validate_authored(
    raw: dict[str, Any], path: Path
) -> tuple[Scenario | None, list[LoadProblem]]:
    if LOADER_ONLY in raw:
        return None, [
            LoadProblem(
                str(path),
                LOADER_ONLY,
                "set by the loader on the scenarios a dataset or matrix expands to; remove it",
            )
        ]
    try:
        return Scenario.model_validate(raw), []
    except ValidationError as exc:
        return None, _problems_from(path, exc)


def suite_root(path: Path) -> Path:
    """The catalogue folder of a suite file: the parent of its ``suites/`` folder, else its
    own folder. Resolved first, so ``full.yaml`` run from inside ``suites/`` is the same."""
    path = path.resolve()
    return path.parent.parent if path.parent.name == "suites" else path.parent


def dataset_root(target: Path) -> Path:
    """The catalogue folder whose ``datasets/`` a load reads (ADR-0060), decided once for the
    whole load from what was loaded, never per scenario file and never by a folder's name
    alone: the nearest folder, starting at the loaded folder (or a single file's own folder),
    that holds ``profile.yaml`` or ``datasets/``. Without one, the loaded folder (the parent
    of a loaded ``scenarios`` folder, the ``opseval init`` layout) or a file's own folder.

    A suite's is its catalogue folder (:func:`suite_root`). The server, the editor and import
    pass their catalogue folder instead, so they and the loader agree."""
    path = target.resolve()
    start = path if path.is_dir() else path.parent
    for folder in (start, *start.parents):
        if (folder / "profile.yaml").is_file() or (folder / DATASET_FOLDER).is_dir():
            return folder
    if path.is_dir() and path.name == "scenarios":
        return path.parent
    return start


@dataclass
class ScenarioCheck:
    """One authored scenario through the loader: the scenario as written (with its base id),
    what it expands to (itself, for a plain one) and every problem, at its field."""

    scenario: Scenario | None
    expanded: list[Scenario]
    problems: list[LoadProblem]
    expansion: Expansion | None = None


def check_expanded(
    template: Scenario,
    where: str,
    profile: SutProfile,
    known_scorers: KnownScorers,
    known_invariants: set[str],
    *,
    root: Path | None,
    limits: Limits | None = None,
) -> ScenarioCheck:
    """Expand ``template`` and check every expansion's references against the profile. A
    problem one expansion shares with the others is reported once."""
    cached: list[Limits] = []

    def lim() -> Limits:
        if not cached:
            cached.append(limits or Limits.from_env())
        return cached[0]

    cases, problems, info = expand(template, root=root, limits=lim)
    out = [LoadProblem(where, fld, msg) for fld, msg in problems]
    if info is not None:
        info.file = where
    seen: set[tuple[str, str]] = set()
    for case in cases:
        for msg in check_references(case, profile, known_scorers, known_invariants):
            fld, _, rest = msg.partition(": ")
            if (fld, rest) in seen:
                continue
            seen.add((fld, rest))
            if case.expanded_from is not None:
                ef = case.expanded_from
                label = case_label(ef.row, ef.dataset, ef.matrix)
                rest = f"{label}: {rest}" if label else rest
            out.append(LoadProblem(where, fld, rest))
    return ScenarioCheck(template, [] if out else cases, out, info)


def reference_problems(
    scenario: Scenario,
    where: str,
    profile: SutProfile,
    known_scorers: KnownScorers,
    known_invariants: set[str],
) -> list[LoadProblem]:
    """`check_references`, as problems with the field split from the message."""
    out = []
    for msg in check_references(scenario, profile, known_scorers, known_invariants):
        fld, _, rest = msg.partition(": ")
        out.append(LoadProblem(where, fld, rest))
    return out


def check_scenario_text(
    text: str,
    where: str,
    profile: SutProfile,
    *,
    known_scorers: KnownScorers,
    known_invariants: set[str],
    root: Path | None = None,
    limits: Limits | None = None,
) -> tuple[Scenario | None, list[LoadProblem]]:
    """One scenario file's text through exactly the checks `opseval validate` applies to
    it: YAML (with the line), a mapping, the schema (each problem at its field path), its
    expansion (ADR-0060; ``root`` is the catalogue folder its dataset is under) and its
    references against the profile. The one place the console's editor and import
    validate, so they cannot drift from the loader."""
    found = check_scenario_source(
        text,
        where,
        profile,
        known_scorers=known_scorers,
        known_invariants=known_invariants,
        root=root,
        limits=limits,
    )
    return found.scenario, found.problems


def check_scenario_source(
    text: str,
    where: str,
    profile: SutProfile,
    *,
    known_scorers: KnownScorers,
    known_invariants: set[str],
    root: Path | None = None,
    limits: Limits | None = None,
) -> ScenarioCheck:
    """:func:`check_scenario_text`, with what the scenario expands to."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = f"line {mark.line + 1}" if mark is not None else ""
        return ScenarioCheck(None, [], [LoadProblem(where, line, f"invalid YAML: {exc}")])
    if not isinstance(raw, dict):
        return ScenarioCheck(
            None, [], [LoadProblem(where, "", "a scenario file must contain a mapping")]
        )
    scenario, problems = _validate_authored(raw, Path(where))
    if scenario is None:
        return ScenarioCheck(None, [], problems)
    return check_expanded(
        scenario, where, profile, known_scorers, known_invariants, root=root, limits=limits
    )


def _match_filter(scenario: Scenario, flt: str) -> bool:
    key, _, value = flt.partition("=")
    if not value:
        raise ConfigError(f"invalid filter {flt!r}; use tag=…, id=… or criticality=…")
    if key == "tag":
        return value in scenario.tags
    if key == "id":
        # An authored id selects every scenario its dataset or matrix expands to (ADR-0060).
        return value in (scenario.id, scenario.base_id)
    if key == "criticality":
        return scenario.criticality.value == value
    raise ConfigError(f"unknown filter key {key!r}; use tag, id or criticality")


def suite_files(path: Path, suite: Suite) -> list[Path]:
    """The scenario files a suite file selects, before its filters: what ``include`` matches
    under the catalogue root, less ``exclude``, never outside the root or in ``suites/``."""
    root = suite_root(path)
    chosen: set[Path] = set()
    for pattern in suite.include:
        for f in root.glob(pattern):
            resolved = f.resolve()
            if resolved.is_file() and resolved.is_relative_to(root):
                chosen.add(resolved)
    for pattern in suite.exclude:
        chosen -= {f.resolve() for f in root.glob(pattern)}
    suites_dir = (root / "suites").resolve()
    return sorted(f for f in chosen if not f.is_relative_to(suites_dir))


def load_catalogue(
    target: str | Path,
    profile: SutProfile,
    *,
    known_scorers: KnownScorers,
    known_invariants: set[str],
    filters: list[str] | None = None,
    limits: Limits | None = None,
    root: Path | None = None,
) -> tuple[Catalogue, str]:
    """Load a scenario file, a directory, or a suite file. Returns (catalogue, suite name).

    A scenario with ``dataset:`` or ``matrix:`` becomes its expansions (ADR-0060); ``limits``
    (default: from settings, read only if something expands) caps them. ``root`` is the
    catalogue folder their ``datasets/`` paths are under (default: :func:`dataset_root`)."""
    path = Path(target)
    if not path.exists():
        raise ConfigError(f"{path}: no such file or directory")
    # The registry stays a mapping, so reference checks can see each scorer's params model.
    known = known_scorers if isinstance(known_scorers, Mapping) else list(known_scorers)
    suite_name = path.stem
    defaults: dict[str, Any] = {}
    overrides: dict[str, Any] = {}
    all_filters = list(filters or [])
    suite_gate: GatePolicy | None = None
    suite_stop: StopPolicy | None = None
    files: list[Path]

    raw = _read_yaml(path) if path.is_file() else None
    if isinstance(raw, dict) and "include" in raw and "id" not in raw:
        try:
            suite = Suite.model_validate(raw)
        except ValidationError as exc:
            details = "; ".join(str(x) for x in _problems_from(path, exc))
            raise ConfigError(f"invalid suite: {details}") from exc
        suite_name = suite.name
        defaults, overrides = suite.defaults, suite.overrides
        suite_gate = suite.gate
        suite_stop = suite.stop
        all_filters += suite.filters
        files = suite_files(path, suite)
        data_root = root or suite_root(path)
    elif path.is_dir():
        files = sorted(p for p in path.rglob("*.yaml") if "suites" not in p.parts)
        data_root = root or dataset_root(path)
    else:
        files = [path]
        data_root = root or dataset_root(path)

    cat = Catalogue(gate=suite_gate, stop=suite_stop)
    seen: dict[str, str] = {}
    for f in files:
        template, problems = _load_scenario(f, defaults, overrides)
        cat.problems.extend(problems)
        if template is None:
            continue
        if template.id in seen:
            cat.problems.append(
                LoadProblem(
                    str(f), "id", f"duplicate id {template.id!r} (also in {seen[template.id]})"
                )
            )
            continue
        seen[template.id] = str(f)
        found = check_expanded(
            template,
            str(f),
            profile,
            known,
            known_invariants,
            root=data_root,
            limits=limits,
        )
        cat.problems.extend(found.problems)
        if found.expansion is not None:
            cat.expansions[template.id] = found.expansion
        for scenario in found.expanded:
            if all(_match_filter(scenario, flt) for flt in all_filters):
                cat.scenarios.append(scenario)
                cat.sources[scenario.id] = str(f)
    # A scenario with problems must not run: drop it (it becomes a load error).
    bad_files = {p.path for p in cat.problems}
    cat.scenarios = sorted(
        (s for s in cat.scenarios if cat.sources[s.id] not in bad_files), key=lambda s: s.id
    )
    return cat, suite_name
