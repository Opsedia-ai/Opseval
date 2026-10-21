# SPDX-License-Identifier: Apache-2.0
"""Load and validate scenarios, suites and SUT profiles (fail closed)."""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from opseval.errors import ConfigError
from opseval.models.gate import GatePolicy
from opseval.models.profile import SutProfile
from opseval.models.scenario import RESERVED_STOP_AT, PolicyAction, Scenario, Suite
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
    known_scorers: Iterable[str],
    known_invariants: set[str],
) -> list[str]:
    """Cross-check a scenario against the active profile and scorer catalogue."""
    errors: list[str] = []
    known = set(known_scorers)
    conv = scenario.conversation
    if conv.stop_at not in RESERVED_STOP_AT and conv.stop_at not in profile.checkpoints:
        errors.append(
            f"conversation.stop_at: {conv.stop_at!r} is not a checkpoint of profile "
            f"{profile.name!r} (known: {sorted(profile.checkpoints)})"
        )
    if conv.stop_at == "commit_gate" and profile.commit_gate is None:
        errors.append(
            "conversation.stop_at: the default 'commit_gate' needs a profile with a commit_gate; "
            f"profile {profile.name!r} declares none, so set stop_at: goal_reached"
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
        for spec in scenario.scorers:
            if spec.id not in known:
                errors.append(
                    f"scorers: unknown scorer id {spec.id!r} (see `opseval list-scorers`)"
                )
            if spec.id == "security.policy_adherence" and not scenario.expectations.policy:
                errors.append(
                    "scorers: 'security.policy_adherence' grades the agent against "
                    "expectations.policy, which is empty; list the rules it must not break, "
                    "or drop the scorer"
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
    try:
        return Scenario.model_validate(merged), []
    except ValidationError as exc:
        return None, _problems_from(path, exc)


def reference_problems(
    scenario: Scenario,
    where: str,
    profile: SutProfile,
    known_scorers: Iterable[str],
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
    known_scorers: Iterable[str],
    known_invariants: set[str],
) -> tuple[Scenario | None, list[LoadProblem]]:
    """One scenario file's text through exactly the checks `opseval validate` applies to
    it: YAML (with the line), a mapping, the schema (each problem at its field path), and
    its references against the profile. The one place the console's editor and import
    validate, so they cannot drift from the loader."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = f"line {mark.line + 1}" if mark is not None else ""
        return None, [LoadProblem(where, line, f"invalid YAML: {exc}")]
    if not isinstance(raw, dict):
        return None, [LoadProblem(where, "", "a scenario file must contain a mapping")]
    try:
        scenario = Scenario.model_validate(raw)
    except ValidationError as exc:
        return None, _problems_from(Path(where), exc)
    return scenario, reference_problems(scenario, where, profile, known_scorers, known_invariants)


def _match_filter(scenario: Scenario, flt: str) -> bool:
    key, _, value = flt.partition("=")
    if not value:
        raise ConfigError(f"invalid filter {flt!r}; use tag=…, id=… or criticality=…")
    if key == "tag":
        return value in scenario.tags
    if key == "id":
        return scenario.id == value
    if key == "criticality":
        return scenario.criticality.value == value
    raise ConfigError(f"unknown filter key {key!r}; use tag, id or criticality")


def suite_files(path: Path, suite: Suite) -> list[Path]:
    """The scenario files a suite file selects, before its filters: what ``include`` matches
    under the catalogue root, less ``exclude``, never outside the root or in ``suites/``."""
    root = path.parent.parent if path.parent.name == "suites" else path.parent
    root = root.resolve()
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
    known_scorers: Iterable[str],
    known_invariants: set[str],
    filters: list[str] | None = None,
) -> tuple[Catalogue, str]:
    """Load a scenario file, a directory, or a suite file. Returns (catalogue, suite name)."""
    path = Path(target)
    if not path.exists():
        raise ConfigError(f"{path}: no such file or directory")
    known = list(known_scorers)
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
    elif path.is_dir():
        files = sorted(p for p in path.rglob("*.yaml") if "suites" not in p.parts)
    else:
        files = [path]

    cat = Catalogue(gate=suite_gate, stop=suite_stop)
    seen: dict[str, str] = {}
    for f in files:
        scenario, problems = _load_scenario(f, defaults, overrides)
        cat.problems.extend(problems)
        if scenario is None:
            continue
        cat.problems.extend(reference_problems(scenario, str(f), profile, known, known_invariants))
        if scenario.id in seen:
            cat.problems.append(
                LoadProblem(
                    str(f), "id", f"duplicate id {scenario.id!r} (also in {seen[scenario.id]})"
                )
            )
            continue
        seen[scenario.id] = str(f)
        if all(_match_filter(scenario, flt) for flt in all_filters):
            cat.scenarios.append(scenario)
            cat.sources[scenario.id] = str(f)
    # A scenario with problems must not run: drop it (it becomes a load error).
    bad_files = {p.path for p in cat.problems}
    cat.scenarios = sorted(
        (s for s in cat.scenarios if cat.sources[s.id] not in bad_files), key=lambda s: s.id
    )
    return cat, suite_name
