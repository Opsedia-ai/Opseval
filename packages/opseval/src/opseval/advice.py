# SPDX-License-Identifier: Apache-2.0
"""Warnings about a valid scenario: it loads, but it will measure less than it looks (FR-AUT).

An error stops a file from loading. A warning does not, and never changes ``ok`` or an exit
code: it says what the scenario will *not* tell you, so nobody reads a green run as proof of
something it never checked. Two kinds:

- about the file itself (a dialect simulated with a nearer pack, scripted turns in a language
  with no pack, one repeat, a lowered pass threshold, a planted injection or a declared
  expectation that no scorer grades, trace-backed checks with no trace source, the commit
  opt-in, quarantine);
- about this machine (judge checks with no judge configured, an LLM persona with no simulator).
  These are checked only when the caller says what is configured.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from opseval.catalogue import LoadProblem
from opseval.datasets import Expansion
from opseval.langpacks import UnknownLanguage, lookup, narrower
from opseval.models.profile import SutProfile
from opseval.models.scenario import Scenario
from opseval.scoring import REGISTRY, effective_specs


@dataclass(frozen=True)
class Configured:
    """What this machine has configured, for the warnings that depend on it."""

    judge: bool
    simulator: bool


def scenario_warnings(
    scenario: Scenario, where: str, profile: SutProfile, configured: Configured | None = None
) -> list[LoadProblem]:
    out: list[LoadProblem] = []

    def warn(field: str, message: str) -> None:
        out.append(LoadProblem(where, field, message))

    specs = effective_specs(scenario, profile)
    needs = {spec.id: getattr(REGISTRY.get(spec.id), "requires", None) for spec in specs}
    if scenario.is_blocked:
        # Nothing else applies to a scenario that never runs.
        warn("tags", "tagged 'blocked': quarantined, it is reported but never run")
        return out
    for index, spec in enumerate(specs):
        cls = REGISTRY.get(spec.id)
        if spec.params and cls is not None and cls.Params is None:
            warn(
                f"scorers.{index}.params",
                f"{spec.id} does not declare the params it takes (a plugin without a Params "
                "model), so a typo in them is not caught here",
            )
    _language(scenario, warn)
    if scenario.execution.repeats == 1:
        warn(
            "execution.repeats",
            "one attempt cannot show flakiness: a pass proves only that it passed once",
        )
    if scenario.execution.consistency_threshold < 1:
        warn(
            "execution.consistency_threshold",
            f"it passes when {scenario.execution.consistency_threshold:.0%} of attempts pass, "
            "so some attempts may fail and it still passes",
        )
    if scenario.conversation.allow_commit:
        warn(
            "conversation.allow_commit",
            "with --allow-commit on the run, this scenario performs the irreversible outcome: "
            "point that run at a test tenant",
        )
    if scenario.conversation.injections and "security.no_injection_takeover" not in needs:
        warn(
            "conversation.injections",
            "payloads are planted but nothing grades them: add security.no_injection_takeover",
        )
    _ungraded(scenario, needs, warn)
    traced = sorted(sid for sid, req in needs.items() if req == "trace")
    if traced and profile.traces is None:
        warn(
            "scorers",
            f"{', '.join(traced)} read traces, and the profile has no trace source: they will "
            "use frame evidence (degraded) or be not measured",
        )
    if configured is not None:
        judged = sorted(sid for sid, req in needs.items() if req == "llm")
        if judged and not configured.judge:
            warn(
                "scorers",
                f"no judge is configured here: {', '.join(judged)} will be not measured "
                "(set OPSEVAL_JUDGE_BASE_URL and OPSEVAL_JUDGE_MODEL)",
            )
        if scenario.conversation.llm_persona and not configured.simulator:
            warn(
                "conversation",
                "an LLM persona and no simulator configured here: it will be skipped "
                "(set OPSEVAL_SIMULATOR_BASE_URL and OPSEVAL_SIMULATOR_MODEL)",
            )
    return out


def _language(scenario: Scenario, warn: Callable[[str, str], None]) -> None:
    """What the customer will not write (ADR-0059): a dialect a nearer pack stands in for, or,
    for scripted turns, a language with no pack at all (it loads: the turns are sent as
    written, but an LLM persona would be refused)."""
    persona = scenario.persona
    try:
        found = lookup(persona.language, persona.dialect)
    except UnknownLanguage as exc:
        if not scenario.conversation.llm_persona:
            warn(
                f"persona.{exc.field}",
                f"no language pack for {persona.language!r}"
                + (f" ({persona.dialect!r})" if persona.dialect else "")
                + ": fine for scripted turns, which are sent as written; an LLM persona "
                "would be refused",
            )
        return  # for an LLM persona, a load error the loader reports
    if found.fallback_from is not None and scenario.conversation.llm_persona:
        field = "persona.dialect" if found.fallback_from == persona.dialect else "persona.language"
        warn(
            field,
            f"no pack for {found.fallback_from!r} yet: the customer writes with the "
            f"{found.pack.tag} pack instead, so this does not test that dialect",
        )
    if found.fallback_from is None and scenario.conversation.llm_persona:
        for fld, alias, tag in narrower(persona.language, persona.dialect, found.pack):
            warn(
                f"persona.{fld}",
                f"{alias!r} still names the {found.pack.tag} pack, as it did before, so saved "
                f"scenarios keep their meaning; the {tag} pack is more specific (preview) and "
                f"is used only when named ({tag})",
            )


def _ungraded(
    scenario: Scenario, needs: dict[str, str | None], warn: Callable[[str, str], None]
) -> None:
    """An expectation the scenario declares and none of its checks reads (ADR-0048): written
    down, it looks checked, so an agent that breaks it would pass unnoticed. ``scorers:
    default`` covers each of these; an explicit list may not."""
    exp = scenario.expectations
    tools = exp.expected_tools
    graders: list[tuple[str, bool, set[str]]] = [
        ("expectations.expected_tools.must_call", bool(tools.must_call), {"tools.expected_called"}),
        (
            "expectations.expected_tools.must_not_call",
            bool(tools.must_not_call),
            {"tools.forbidden_not_called"},
        ),
        (
            "expectations.expected_tools.forbidden_before",
            bool(tools.forbidden_before),
            {"tools.forbidden_not_called", "tools.approval_gated"},
        ),
        ("expectations.expected_tools.ordering", bool(tools.ordering), {"tools.ordering"}),
        ("expectations.kind", exp.kind == "refusal", {"judge.goal_achievement"}),
        ("expectations.policy", bool(exp.policy), {"security.policy_adherence"}),
    ]
    for field, declared, by in graders:
        if declared and not by & needs.keys():
            warn(field, f"declared but nothing grades it: add {' or '.join(sorted(by))}")
    if exp.expected_outcome and "llm" not in needs.values():
        warn(
            "expectations.expected_outcome",
            "only the judges read it, and this scenario runs none: add judge.goal_achievement",
        )


NEAR_CAP = 0.8
"""An expansion at this share of ``OPSEVAL_MAX_EXPANSION`` is named: a few more rows refuse it."""


def expansion_warnings(expansion: Expansion) -> list[LoadProblem]:
    """What a dataset or matrix scenario will not do as written (ADR-0060): a column no
    placeholder reads (it changes the ids, never the conversation), or a count close to the
    cap, where a few more rows stop the scenario loading."""
    out: list[LoadProblem] = []
    where = expansion.file
    if expansion.unused_columns:
        names = ", ".join(map(repr, expansion.unused_columns))
        out.append(
            LoadProblem(
                where,
                "dataset",
                f"column(s) {names} of {expansion.dataset} are used by no placeholder: they "
                "change no conversation, only the expanded ids",
            )
        )
    if expansion.count >= NEAR_CAP * expansion.limit:
        out.append(
            LoadProblem(
                where,
                "dataset" if expansion.dataset else "matrix",
                f"expands to {expansion.count} scenarios, near the limit of {expansion.limit} "
                "(OPSEVAL_MAX_EXPANSION): over it, the scenario stops loading",
            )
        )
    return out


__all__ = ["Configured", "expansion_warnings", "scenario_warnings"]
