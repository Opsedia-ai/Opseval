# SPDX-License-Identifier: Apache-2.0
"""The gate policy: what each outcome *does* to the release (ADR-0032).

Opseval decides what happened: a score passed, failed or could not be measured; a
timeout was the agent's or the harness's. Those facts are fixed in code, so every report
means the same thing. A team decides what to *do* about each fact: block the release,
warn, or ignore it. That is this policy, set per suite in YAML (``gate:``) and recorded in
every result, so a green run under a lenient policy can never pass for a strict one.

Two things are not configurable, because configuring them would break the one rule:

- A suite where nothing was judged always blocks: it has proven nothing.
- Exit code 3 (every attempt was a harness failure) is always 3.

``standard`` is exactly the behaviour before policies existed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Action = Literal["block", "warn", "ignore"]
NeverBlock = Literal["warn", "ignore"]
NeverIgnore = Literal["block", "warn"]
Preset = Literal["strict", "standard", "lenient"]
ExitMode = Literal["strict", "gate"]
Undeclared = Literal["critical", "normal"]

RANK: dict[str, int] = {"ignore": 0, "warn": 1, "block": 2}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ByCriticality(_Strict):
    blocker: Action
    critical: Action
    normal: Action


class ByCriticalityIn(_Strict):
    blocker: Action | None = None
    critical: Action | None = None
    normal: Action | None = None


class ResolvedGatePolicy(_Strict):
    """Every setting concrete: what a run was actually gated with."""

    preset: Preset
    failed: ByCriticality
    """A failed scenario, by its criticality."""
    skipped: ByCriticality
    """Skipped because a dependency or the simulator was unavailable."""
    commit_guard: NeverBlock
    """Skipped by the commit guard. Never blocks: that would push teams to --allow-commit."""
    quarantined: Action
    """A scenario tagged ``blocked`` by its author."""
    inconclusive: NeverIgnore
    """Too few valid attempts to judge. Never ignored: it is a scenario that was not judged."""
    flaky: Action
    unmeasured: Action
    """An agent-facing check that could not run in an attempt that was still judged."""
    degraded: Action
    """A score made on weaker evidence than its scorer wants."""
    vacuous: Action
    """A pass because the situation the check looks for never arose."""
    load_errors: NeverIgnore
    """Scenario files that could not be loaded."""
    undeclared_criticality: Undeclared
    """How to treat scenarios when no scenario in the run declares a criticality."""
    exit_code: ExitMode
    """``strict``: exit codes say what happened (any failure 1, anything unjudged 2).
    ``gate``: they follow this policy's decision (pass 0, blocked by the agent 1, blocked
    by what could not be judged 2). Exit 3 is the same in both."""


class GatePolicy(_Strict):
    """The ``gate:`` block of a suite: a preset, and any settings that differ from it."""

    preset: Preset = "standard"
    failed: ByCriticalityIn | None = None
    skipped: ByCriticalityIn | None = None
    commit_guard: NeverBlock | None = None
    quarantined: Action | None = None
    inconclusive: NeverIgnore | None = None
    flaky: Action | None = None
    unmeasured: Action | None = None
    degraded: Action | None = None
    vacuous: Action | None = None
    load_errors: NeverIgnore | None = None
    undeclared_criticality: Undeclared | None = None
    exit_code: ExitMode | None = None


def _dump(value: object) -> object:
    return value.model_dump() if isinstance(value, BaseModel) else value


def _all(action: Action) -> ByCriticality:
    return ByCriticality(blocker=action, critical=action, normal=action)


PRESETS: dict[Preset, ResolvedGatePolicy] = {
    "standard": ResolvedGatePolicy(
        preset="standard",
        failed=ByCriticality(blocker="block", critical="block", normal="warn"),
        skipped=_all("warn"),
        commit_guard="warn",
        quarantined="warn",
        inconclusive="block",
        flaky="warn",
        unmeasured="warn",
        degraded="ignore",
        vacuous="ignore",
        load_errors="warn",
        undeclared_criticality="critical",
        exit_code="strict",
    ),
    "strict": ResolvedGatePolicy(
        preset="strict",
        failed=_all("block"),
        skipped=_all("block"),
        commit_guard="warn",
        quarantined="block",
        inconclusive="block",
        flaky="block",
        unmeasured="block",
        degraded="warn",
        vacuous="warn",
        load_errors="block",
        undeclared_criticality="critical",
        exit_code="strict",
    ),
    "lenient": ResolvedGatePolicy(
        preset="lenient",
        failed=ByCriticality(blocker="block", critical="block", normal="ignore"),
        skipped=_all("ignore"),
        commit_guard="ignore",
        quarantined="ignore",
        inconclusive="warn",
        flaky="ignore",
        unmeasured="warn",
        degraded="ignore",
        vacuous="ignore",
        load_errors="warn",
        undeclared_criticality="critical",
        exit_code="gate",
    ),
}


def resolve(
    policy: GatePolicy | None = None,
    *,
    preset: Preset | None = None,
    fail_on_flaky: bool = False,
) -> ResolvedGatePolicy:
    """The policy a run is gated with.

    ``preset`` (``opseval run --gate``) replaces the suite's policy whole: a release branch
    asking for ``strict`` gets exactly ``strict``. Otherwise the suite's settings are
    applied over its preset. ``fail_on_flaky`` is the older ``OPSEVAL_GATE_FAIL_ON_FLAKY``.
    """
    if preset is not None:
        resolved = PRESETS[preset]
    else:
        policy = policy or GatePolicy()
        base = PRESETS[policy.preset]
        update: dict[str, object] = {
            k: v for k, v in policy.model_dump(exclude_none=True).items() if k != "preset"
        }
        for group in ("failed", "skipped"):
            override: ByCriticalityIn | None = getattr(policy, group)
            if override is not None:
                base_group: ByCriticality = getattr(base, group)
                update[group] = base_group.model_copy(update=override.model_dump(exclude_none=True))
        # Validated again, so the merge cannot produce a value the model would refuse.
        resolved = ResolvedGatePolicy.model_validate(
            {**base.model_dump(), **{k: _dump(v) for k, v in update.items()}}
        )
    if fail_on_flaky and resolved.flaky != "block":
        resolved = resolved.model_copy(update={"flaky": "block"})
    return resolved


def _flat(p: ResolvedGatePolicy) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in p.model_dump().items():
        if key == "preset":
            continue
        if isinstance(value, dict):
            out.update({f"{key}.{k}": v for k, v in value.items()})
        else:
            out[key] = value
    return out


def changes_from(p: ResolvedGatePolicy, base: Preset | None = None) -> list[str]:
    """Settings that differ from a preset (its own, by default), as ``key=value``."""
    ref = _flat(PRESETS[base or p.preset])
    return [f"{k}={v}" for k, v in _flat(p).items() if ref[k] != v]


def looser_than_standard(p: ResolvedGatePolicy) -> list[str]:
    """Settings that let more through than ``standard`` does. Named in every output."""
    std = _flat(PRESETS["standard"])
    looser = []
    for key, value in _flat(p).items():
        ref = std[key]
        if key == "exit_code":
            weaker = value == "gate" and ref == "strict"
        elif key == "undeclared_criticality":
            weaker = value == "normal" and ref == "critical"
        else:
            weaker = RANK[value] < RANK[ref]
        if weaker:
            looser.append(f"{key}={value}")
    return looser


def describe(p: ResolvedGatePolicy) -> str:
    """One line for reports: ``standard`` or ``standard + failed.normal=block``."""
    changes = changes_from(p)
    return p.preset if not changes else f"{p.preset} + " + ", ".join(changes)
