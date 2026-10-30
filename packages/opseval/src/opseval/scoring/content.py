# SPDX-License-Identifier: Apache-2.0
"""``content.*``: deterministic checks on what was said, with no LLM (ADR-0061).

"The reply contains the order id" or "the JSON answer has ``status: approved``" needs no
judge: a comparison decides it, costs nothing and never varies. The rules they share:

- they read the redacted text and payloads (ADR-0033), as the judges do;
- they are on the ``output`` axis;
- ``infra_error`` only for a harness reason (bad params, a guard the harness itself set),
  never for an answer they did not like. An agent that said nothing did not say what was
  required: that fails; and a check that passes because there was nothing to look at says
  so (``vacuous``, ADR-0024).
"""

from __future__ import annotations

import json
import re
import unicodedata
from re import _parser as sre  # type: ignore[attr-defined]
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from opseval.models.profile import FrameKind, SutProfile
from opseval.models.record import RunRecord
from opseval.models.results import Axis, ScoreResult
from opseval.models.scenario import ScorerSpec
from opseval.paths import dig
from opseval.scoring.base import NoParams, Scorer, ScoringContext, register
from opseval.scoring.deterministic import _declared_checkpoint, _occurrence, _unknown_checkpoint

Scope = Literal["any_turn", "last_turn", "every_turn"]
Speaker = Literal["agent", "customer"]
Needle = Annotated[str, Field(min_length=1, max_length=1000)]

MAX_PATTERN = 500
"""The longest ``content.regex`` pattern."""
MAX_UNBOUNDED = 3
"""Unbounded quantifiers (``*``, ``+``, ``{n,}``) one pattern may have."""
MAX_REGEX_INPUT = 10_000
"""The longest turn ``content.regex`` runs on, in characters. A longer turn is *not
measured* (``REGEX_INPUT_TOO_LONG``), never cut: a match past the cut would be a false fail."""

_ARABIC_TATWEEL = "ـ"


# --- text --------------------------------------------------------------------------------


class _TextParams(NoParams):
    case_sensitive: bool = False
    """Whether case must match (Unicode case folding when false)."""
    ignore_diacritics: bool = False
    """Ignore combining marks (Arabic harakat, accents) and the Arabic tatweel on both
    sides: ``مَرْحَبًا`` matches ``مرحبا``, ``café`` matches ``cafe``."""
    scope: Scope = "any_turn"
    """Which turns: ``any_turn`` (somewhere in the conversation), ``last_turn`` (the
    speaker's last turn) or ``every_turn`` (each of the speaker's turns)."""
    speaker: Speaker = "agent"
    """Whose turns are read: the ``agent`` or the simulated ``customer``."""


def normalise(text: str, *, case_sensitive: bool, ignore_diacritics: bool) -> str:
    """NFKC, whitespace runs as one space, then case folding and diacritics as asked. Both
    the needle and the turn go through it, so the comparison is like for like."""
    text = unicodedata.normalize("NFKC", text)
    if ignore_diacritics:
        decomposed = unicodedata.normalize("NFD", text)
        text = "".join(
            ch for ch in decomposed if unicodedata.category(ch) != "Mn" and ch != _ARABIC_TATWEEL
        )
        text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if case_sensitive else text.casefold()


def _turns(record: RunRecord, speaker: Speaker) -> list[tuple[int, str]]:
    """``(turn number in the whole conversation, text)`` for one speaker: the numbering the
    judges use, so a turn named here is the turn a judge would quote."""
    return [
        (n, text) for n, (who, text) in enumerate(record.transcript(), start=1) if who == speaker
    ]


def _scoped(turns: list[tuple[int, str]], scope: Scope) -> list[tuple[int, str]]:
    return turns[-1:] if scope == "last_turn" else turns


class ContainsParams(_TextParams):
    all_of: list[Needle] | None = Field(default=None, min_length=1)
    """Every one of these must appear."""
    any_of: list[Needle] | None = Field(default=None, min_length=1)
    """At least one of these must appear."""

    @model_validator(mode="after")
    def _something(self) -> ContainsParams:
        if not self.all_of and not self.any_of:
            raise ValueError("give all_of or any_of (or both)")
        return self


def _holds(text: str, params: ContainsParams, needles: dict[str, str]) -> tuple[bool, list[str]]:
    """Whether ``text`` (normalised) meets the params, and the needles it contains."""
    found = [raw for raw, norm in needles.items() if norm in text]
    ok = True
    if params.all_of:
        ok = all(needles[n] in text for n in params.all_of)
    if params.any_of:
        ok = ok and any(needles[n] in text for n in params.any_of)
    return ok, found


def _contains(record: RunRecord, params: ContainsParams) -> tuple[bool, dict[str, Any]]:
    """What ``content.contains`` decides: (holds, details). Not-contains is its negation."""

    def norm(text: str) -> str:
        return normalise(
            text, case_sensitive=params.case_sensitive, ignore_diacritics=params.ignore_diacritics
        )

    needles = {n: norm(n) for n in [*(params.all_of or []), *(params.any_of or [])]}
    turns = _scoped(_turns(record, params.speaker), params.scope)
    texts = [(n, norm(t)) for n, t in turns]
    found: dict[str, list[int]] = {}
    for n, text in texts:
        for raw, needle in needles.items():
            if needle in text:
                found.setdefault(raw, []).append(n)
    if params.scope == "every_turn":
        holds = bool(texts) and all(_holds(text, params, needles)[0] for _, text in texts)
    else:
        # any_turn: each needle somewhere in the (scoped) turns, not necessarily one turn.
        holds = bool(texts)
        if params.all_of:
            holds = holds and all(n in found for n in params.all_of)
        if params.any_of:
            holds = holds and any(n in found for n in params.any_of)
    details: dict[str, Any] = {
        "turns_checked": [n for n, _ in texts],
        "found": found,
    }
    missing = [n for n in [*(params.all_of or [])] if n not in found]
    if missing:
        details["missing"] = missing
    return holds, details


def _silent(params: _TextParams) -> str:
    return f"the {params.speaker} sent no text"


@register
class ContentContains(Scorer):
    id = "content.contains"
    axis = Axis.OUTPUT
    Params = ContainsParams
    description = (
        "What was said contains the given text (params: all_of / any_of, case_sensitive, "
        "ignore_diacritics, scope any_turn|last_turn|every_turn, speaker agent|customer)."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        try:
            params = ContainsParams.model_validate(spec.params)
        except ValidationError as exc:
            return self.infra(spec, _bad(exc))
        holds, details = _contains(record, params)
        if not details["turns_checked"]:
            if params.speaker == "customer":
                return self.infra(spec, "NOT_MEASURABLE: the simulated customer sent no text")
            # The agent said nothing, so it did not say what was required: its failure.
            return self.result(spec, 0.0, note=_silent(params), **details)
        return self.result(spec, 1.0 if holds else 0.0, **details)


@register
class ContentNotContains(Scorer):
    id = "content.not_contains"
    axis = Axis.OUTPUT
    Params = ContainsParams
    description = (
        "Fails exactly when content.contains with the same params would pass: any_of names "
        "text that must not appear. No text at all is a vacuous pass, named as one."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        try:
            params = ContainsParams.model_validate(spec.params)
        except ValidationError as exc:
            return self.infra(spec, _bad(exc))
        holds, details = _contains(record, params)
        if not details["turns_checked"]:
            if params.speaker == "customer":
                return self.infra(spec, "NOT_MEASURABLE: the simulated customer sent no text")
            # conversation.agent_replied fails the silence; this check had nothing to read.
            return self.result(spec, 1.0, vacuous=True, note=_silent(params))
        details.pop("missing", None)
        return self.result(spec, 0.0 if holds else 1.0, **details)


# --- regex -------------------------------------------------------------------------------


def _shape(tree: Any, inside_repeat: bool = False) -> tuple[int, bool]:
    """(unbounded quantifiers, a quantifier nested in another) for a parsed pattern."""
    unbounded, nested = 0, False
    for op, av in tree:
        name = str(op)
        children: list[Any] = []
        repeat = False
        if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            low, high, sub = av
            repeat = high > 1
            if high == sre.MAXREPEAT:
                unbounded += 1
            if repeat and inside_repeat:
                nested = True
            children = [sub]
        elif name == "SUBPATTERN":
            children = [av[-1]]
        elif name == "BRANCH":
            children = list(av[1])
        elif name in ("ASSERT", "ASSERT_NOT"):
            children = [av[1]]
        elif name == "ATOMIC_GROUP":
            children = [av]
        elif name == "GROUPREF_EXISTS":
            children = [a for a in av[1:] if a is not None]
        for child in children:
            u, n = _shape(child, inside_repeat or repeat)
            unbounded += u
            nested = nested or n
    return unbounded, nested


def guard_pattern(pattern: str, flags: int = 0) -> re.Pattern[str]:
    """Compile ``pattern``, refusing the shapes that backtrack catastrophically on Python's
    ``re``: a quantifier inside a quantified group (``(a+)+``, ``(\\w*)*``, ``(x+){2,}``) and
    more than :data:`MAX_UNBOUNDED` unbounded quantifiers. A simple structural guard, not a
    proof: with the per-turn length cap it keeps a typo from hanging a run; server mode needs
    the stronger choice ADR-0061 leaves open. Raises ``ValueError`` with the reason."""
    if len(pattern) > MAX_PATTERN:
        raise ValueError(f"a pattern is at most {MAX_PATTERN} characters")
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"not a valid regular expression: {exc}") from exc
    unbounded, nested = _shape(sre.parse(pattern, flags))
    if nested:
        raise ValueError(
            "a quantifier inside a quantified group ((a+)+, (\\w*)*) can take exponential time "
            "on Python's re: rewrite it without the nesting"
        )
    if unbounded > MAX_UNBOUNDED:
        raise ValueError(
            f"more than {MAX_UNBOUNDED} unbounded quantifiers (*, +, {{n,}}): bound some, "
            "such as {1,40}, or split the check"
        )
    return compiled


class RegexParams(_TextParams):
    pattern: str = Field(min_length=1, max_length=MAX_PATTERN)
    """A Python regular expression, searched in each turn's text (compiled when the scenario
    loads; nested quantifiers and more than three unbounded ones are refused)."""
    must_match: bool = True
    """True: the pattern must be found (per ``scope``). False: it must not be found."""
    case_sensitive: bool = True
    """Whether case must match (``re.IGNORECASE`` when false). Default true: a pattern says
    exactly what it means."""
    ignore_diacritics: bool = False
    """Normalise the text as ``content.contains`` does (NFKC, whitespace runs as one space)
    and strip combining marks and the Arabic tatweel before searching."""

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        guard_pattern(value)
        return value

    def compiled(self) -> re.Pattern[str]:
        return guard_pattern(self.pattern, 0 if self.case_sensitive else re.IGNORECASE)


@register
class ContentRegex(Scorer):
    id = "content.regex"
    axis = Axis.OUTPUT
    Params = RegexParams
    description = (
        "A regular expression is found (must_match: true) or not found (false) in what was "
        "said (params: pattern, scope, speaker, case_sensitive, ignore_diacritics)."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        try:
            params = RegexParams.model_validate(spec.params)
            compiled = params.compiled()
        except (ValidationError, ValueError) as exc:
            return self.infra(spec, _bad(exc))
        turns = _scoped(_turns(record, params.speaker), params.scope)
        if not turns:
            if params.speaker == "customer":
                return self.infra(spec, "NOT_MEASURABLE: the simulated customer sent no text")
            if params.must_match:
                return self.result(spec, 0.0, note=_silent(params))
            return self.result(spec, 1.0, vacuous=True, note=_silent(params))
        long = [n for n, t in turns if len(t) > MAX_REGEX_INPUT]
        if long:
            return self.infra(
                spec,
                f"REGEX_INPUT_TOO_LONG: turn(s) {long} are over {MAX_REGEX_INPUT} characters, "
                "the most content.regex searches (a guard against slow patterns)",
            )
        matched: list[int] = []
        for n, text in turns:
            if params.ignore_diacritics:
                text = normalise(text, case_sensitive=True, ignore_diacritics=True)
            if compiled.search(text):
                matched.append(n)
        checked = [n for n, _ in turns]
        every = params.scope == "every_turn"
        found = len(matched) == len(turns) if every else bool(matched)
        ok = found if params.must_match else not matched
        return self.result(spec, 1.0 if ok else 0.0, turns_checked=checked, matched=matched)


# --- JSON --------------------------------------------------------------------------------


class JsonPathParams(NoParams):
    path: str = Field(min_length=1)
    """Dotted path into the JSON (``data.refunds.0.id``: objects by key, lists by index)."""
    equals: Any
    """The value it must have: any JSON value, null included. ``1`` and ``true`` differ."""
    source: Literal["message", "result", "checkpoint"] = "result"
    """Where the JSON is: the agent's ``message`` text parsed as JSON, a ``result`` frame's
    payload, or a ``checkpoint`` payload (name it in ``checkpoint``)."""
    checkpoint: str | None = Field(default=None, min_length=1)
    """The checkpoint whose payload is read, with ``source: checkpoint`` (its approved
    occurrence, else the last)."""
    scope: Scope = "last_turn"
    """For messages and results: the ``last_turn`` (default), ``any_turn`` (one of them) or
    ``every_turn``."""

    @model_validator(mode="after")
    def _checkpoint(self) -> JsonPathParams:
        if self.source == "checkpoint" and self.checkpoint is None:
            raise ValueError("source checkpoint needs params.checkpoint")
        if self.source != "checkpoint" and self.checkpoint is not None:
            raise ValueError("params.checkpoint is read only with source: checkpoint")
        return self


def json_equal(a: Any, b: Any) -> bool:
    """JSON equality: ``true`` is not ``1``, ``1`` is ``1.0``, objects ignore key order."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and bool(a == b)


_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n\s*```$", re.DOTALL)


def _parse_message(text: str) -> tuple[bool, Any]:
    """The agent's text as JSON: the whole text, or one fenced block (```json ... ```)."""
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        return True, json.loads(body)
    except ValueError:
        return False, None


@register
class ContentJsonPathEquals(Scorer):
    id = "content.json_path_equals"
    axis = Axis.OUTPUT
    Params = JsonPathParams
    description = (
        "A value in the agent's JSON equals the expected one (params: path, equals, source "
        "message|result|checkpoint, checkpoint, scope). A message that is not JSON fails."
    )

    @classmethod
    def check_params(cls, params: BaseModel, profile: SutProfile) -> list[str]:
        if getattr(params, "source", None) != "checkpoint":
            return []
        return _declared_checkpoint(params, profile)

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        try:
            params = JsonPathParams.model_validate(spec.params)
        except ValidationError as exc:
            return self.infra(spec, _bad(exc))
        sources: list[tuple[str, bool, Any]] = []  # (where, is JSON, value)
        if params.source == "checkpoint":
            name = params.checkpoint or ""
            unknown = _unknown_checkpoint(name, ctx)
            if unknown:
                return self.infra(spec, unknown)
            chosen = _occurrence(record, name)
            if chosen is None:
                return self.result(spec, 0.0, note=f"checkpoint {name!r} was never raised")
            sources = [(f"checkpoint {name} #{chosen.occurrence}", True, chosen.payload)]
        elif params.source == "result":
            results = [f for f in record.received() if f.kind is FrameKind.RESULT]
            sources = [(f"frame {f.seq}", True, f.payload) for f in results]
        else:
            turns = _turns(record, "agent")
            sources = [(f"turn {n}", *_parse_message(t)) for n, t in turns]
        if params.source != "checkpoint":
            sources = sources[-1:] if params.scope == "last_turn" else sources
        if not sources:
            what = "result frame" if params.source == "result" else "message"
            return self.result(spec, 0.0, note=f"the agent sent no {what}")
        checked = []
        for where, is_json, value in sources:
            row: dict[str, Any] = {"at": where}
            if not is_json:
                row["problem"] = "not JSON"
            else:
                try:
                    observed = dig(value, params.path)
                except KeyError:
                    row["problem"] = f"{params.path!r} missing"
                else:
                    row["observed"] = observed
                    row["ok"] = json_equal(observed, params.equals)
            row.setdefault("ok", False)
            checked.append(row)
        oks = [r["ok"] for r in checked]
        passed = all(oks) if params.scope == "every_turn" else any(oks)
        return self.result(spec, 1.0 if passed else 0.0, expected=params.equals, checked=checked)


def _bad(exc: ValidationError | ValueError) -> str:
    if isinstance(exc, ValidationError):
        err = exc.errors()[0]
        where = ".".join(str(p) for p in err["loc"])
        return f"BAD_PARAMS: {where + ': ' if where else ''}{err['msg']}"
    return f"BAD_PARAMS: {exc}"


__all__ = [
    "ContentContains",
    "ContentJsonPathEquals",
    "ContentNotContains",
    "ContentRegex",
    "guard_pattern",
    "json_equal",
    "normalise",
]
