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

import asyncio
import json
import re
import sys
import unicodedata
from collections.abc import Callable, Iterator
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
"""The longest turn ``content.regex`` searches, in characters, after any normalisation. A
longer turn is *not measured* (``REGEX_INPUT_TOO_LONG``), never cut: a match past the cut
would be a false fail."""
REGEX_TIMEOUT_SECONDS = 5.0
"""How long one ``content.regex`` search may run (in its own process, started fresh) before
it is killed and the score is *not measured* (``REGEX_TIMEOUT``)."""

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


def _contains(record: RunRecord, params: ContainsParams) -> tuple[bool, dict[str, Any]]:
    """What ``content.contains`` decides: (holds, details)."""

    def norm(text: str) -> str:
        return normalise(
            text, case_sensitive=params.case_sensitive, ignore_diacritics=params.ignore_diacritics
        )

    all_of = {n: norm(n) for n in params.all_of or []}
    any_of = {n: norm(n) for n in params.any_of or []}
    texts = [(n, norm(t)) for n, t in _scoped(_turns(record, params.speaker), params.scope)]
    found: dict[str, list[int]] = {}
    for n, text in texts:
        for raw, needle in (all_of | any_of).items():
            if needle in text:
                found.setdefault(raw, []).append(n)

    def meets(seen: Callable[[str], bool]) -> bool:
        return all(seen(n) for n in all_of) and (not any_of or any(seen(n) for n in any_of))

    def in_turn(turn: int) -> Callable[[str], bool]:
        return lambda raw: turn in found.get(raw, [])

    if params.scope == "every_turn":
        holds = bool(texts) and all(meets(in_turn(n)) for n, _ in texts)
    else:
        # any_turn: each needle somewhere in the (scoped) turns, not necessarily one turn.
        holds = bool(texts) and meets(lambda raw: raw in found)
    details: dict[str, Any] = {"turns_checked": [n for n, _ in texts], "found": found}
    missing = [n for n in all_of if n not in found]
    if missing:
        details["missing"] = missing
    return holds, details


def _forbidden(record: RunRecord, params: ContainsParams) -> dict[str, Any]:
    """What ``content.not_contains`` reads: every string in ``all_of`` and ``any_of`` is
    forbidden, and one forbidden string in one examined turn fails it, whatever the scope
    (``last_turn`` examines the last turn; ``any_turn`` and ``every_turn`` examine them all),
    as ``content.regex`` with ``must_match: false`` does."""
    texts = [
        (
            n,
            normalise(
                t, case_sensitive=params.case_sensitive, ignore_diacritics=params.ignore_diacritics
            ),
        )
        for n, t in _scoped(_turns(record, params.speaker), params.scope)
    ]
    forbidden = {
        raw: normalise(
            raw, case_sensitive=params.case_sensitive, ignore_diacritics=params.ignore_diacritics
        )
        for raw in [*(params.all_of or []), *(params.any_of or [])]
    }
    found: dict[str, list[int]] = {}
    for n, text in texts:
        for raw, needle in forbidden.items():
            if needle in text:
                found.setdefault(raw, []).append(n)
    return {"turns_checked": [n for n, _ in texts], "found": found}


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
        "None of the listed text appears (all_of and any_of are both forbidden strings): one of "
        "them in one examined turn fails it, whatever the scope. No text at all is a vacuous "
        "pass, named as one."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        try:
            params = ContainsParams.model_validate(spec.params)
        except ValidationError as exc:
            return self.infra(spec, _bad(exc))
        details = _forbidden(record, params)
        if not details["turns_checked"]:
            if params.speaker == "customer":
                return self.infra(spec, "NOT_MEASURABLE: the simulated customer sent no text")
            # conversation.agent_replied fails the silence; this check had nothing to read.
            return self.result(spec, 1.0, vacuous=True, note=_silent(params))
        return self.result(spec, 0.0 if details["found"] else 1.0, **details)


# --- regex -------------------------------------------------------------------------------

_REPEATS = ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")


class _Shape:
    """Walks a parsed pattern and refuses the shapes that backtrack badly on Python's ``re``."""

    def __init__(self) -> None:
        self.unbounded = 0

    def check(self, tree: Any) -> None:
        self._adjacent(self.walk(tree, quantified=False))
        if self.unbounded > MAX_UNBOUNDED:
            raise ValueError(
                f"more than {MAX_UNBOUNDED} unbounded quantifiers (*, +, {{n,}}): bound some, "
                "such as {1,40}, or split the check"
            )

    @staticmethod
    def _adjacent(tokens: list[str]) -> None:
        """Two unbounded quantifiers need a literal character between them: ``.*.*x`` or
        ``\\s+\\w+`` can try every split of the text between them."""
        open_run = False
        for kind in tokens:
            if kind == "U":
                if open_run:
                    raise ValueError(
                        "two unbounded quantifiers (*, +, {n,}) with no literal character "
                        "between them (.*.*, \\s+\\w+) can take polynomial time on Python's re: "
                        "put a literal between them or bound one, such as {1,40}"
                    )
                open_run = True
            elif kind == "L":
                open_run = False

    def walk(self, tree: Any, *, quantified: bool) -> list[str]:
        """Tokens for one sequence: ``U`` an unbounded quantifier, ``L`` a literal character,
        ``O`` anything else. Raises ``ValueError`` on a refused shape."""
        tokens: list[str] = []
        for op, av in tree:
            name = str(op)
            if name in _REPEATS:
                _low, high, sub = av
                if high > 1:
                    if quantified:
                        raise ValueError(
                            "a quantifier inside a quantified group ((a+)+, (\\w*)*) can take "
                            "exponential time on Python's re: rewrite it without the nesting"
                        )
                    if high == sre.MAXREPEAT:
                        self.unbounded += 1
                    self.walk(sub, quantified=True)
                    tokens.append("U" if high == sre.MAXREPEAT else "O")
                else:
                    tokens += self.walk(sub, quantified=quantified)
            elif name == "SUBPATTERN":
                tokens += self.walk(av[-1], quantified=quantified)
            elif name == "ATOMIC_GROUP":
                tokens += self.walk(av, quantified=quantified)
            elif name in ("BRANCH", "GROUPREF_EXISTS"):
                if quantified:
                    raise ValueError(
                        "alternation inside a quantified group ((a|ab)*, (?:x|y)+) can take "
                        "exponential time on Python's re: use a character class ([xy]+) or "
                        "split the check"
                    )
                branches = av[1] if name == "BRANCH" else [b for b in av[1:] if b is not None]
                inner = [self.walk(b, quantified=False) for b in branches]
                for branch in inner:
                    self._adjacent(branch)
                tokens.append("U" if any("U" in b for b in inner) else "O")
            elif name in ("ASSERT", "ASSERT_NOT"):
                self._adjacent(self.walk(av[1], quantified=quantified))
                tokens.append("O")
            elif name == "LITERAL":
                tokens.append("L")
            else:
                tokens.append("O")
        return tokens


def guard_pattern(pattern: str, flags: int = 0) -> re.Pattern[str]:
    """Compile ``pattern``, refusing the shapes that backtrack badly on Python's ``re``:

    - a quantifier inside a quantified group (``(a+)+``, ``(\\w*)*``, ``(x+){2,}``);
    - alternation inside a quantified group (``(a|ab)*``, ``(?:foo|bar)+``);
    - two unbounded quantifiers with no literal character between them (``.*.*``,
      ``\\s+\\w+``), and more than :data:`MAX_UNBOUNDED` in all.

    A structural guard, not a proof: the search also runs in a separate process that is
    killed at :data:`REGEX_TIMEOUT_SECONDS` (:func:`search_turns`). Raises ``ValueError``."""
    if len(pattern) > MAX_PATTERN:
        raise ValueError(f"a pattern is at most {MAX_PATTERN} characters")
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"not a valid regular expression: {exc}") from exc
    _Shape().check(sre.parse(pattern, flags))
    return compiled


def _codepoints(tree: Any) -> Iterator[int]:
    """Every literal code point a parsed pattern names, in classes and ranges too."""
    for op, av in tree:
        name = str(op)
        if name in ("LITERAL", "NOT_LITERAL"):
            yield av
        elif name == "RANGE":
            yield from range(av[0], av[1] + 1)
        elif name == "IN":
            yield from _codepoints(av)
        elif name in _REPEATS:
            yield from _codepoints(av[2])
        elif name == "SUBPATTERN":
            yield from _codepoints(av[-1])
        elif name == "ATOMIC_GROUP":
            yield from _codepoints(av)
        elif name == "BRANCH":
            for branch in av[1]:
                yield from _codepoints(branch)
        elif name in ("ASSERT", "ASSERT_NOT"):
            yield from _codepoints(av[1])
        elif name == "GROUPREF_EXISTS":
            for branch in av[1:]:
                if branch is not None:
                    yield from _codepoints(branch)


def _is_mark(code: int) -> bool:
    return chr(code) == _ARABIC_TATWEEL or unicodedata.category(chr(code)) == "Mn"


def strip_marks(text: str) -> str:
    """``text`` without combining marks or the Arabic tatweel (NFD, then NFC). Nothing else
    changes, so a pattern's whitespace and escapes mean what they say."""
    decomposed = unicodedata.normalize("NFD", text)
    kept = "".join(ch for ch in decomposed if not _is_mark(ord(ch)))
    return unicodedata.normalize("NFC", kept)


class RegexParams(_TextParams):
    pattern: str = Field(min_length=1, max_length=MAX_PATTERN)
    """A Python regular expression, searched in each turn's text (compiled when the scenario
    loads; nested quantifiers, alternation under a quantifier and unbounded quantifiers with
    nothing literal between them are refused)."""
    must_match: bool = True
    """True: the pattern must be found (per ``scope``). False: it must not be found in any
    examined turn."""
    case_sensitive: bool = True
    """Whether case must match (``re.IGNORECASE`` when false). Default true: a pattern says
    exactly what it means."""
    ignore_diacritics: bool = False
    """Strip combining marks (Arabic harakat, accents) and the Arabic tatweel from the text
    before searching; a pattern that itself names one is refused, since it could never
    match."""

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        guard_pattern(value)
        return value

    @model_validator(mode="after")
    def _marks(self) -> RegexParams:
        if not self.ignore_diacritics:
            return self
        named = any(_is_mark(c) for c in _codepoints(sre.parse(self.pattern)))
        decomposed = unicodedata.normalize("NFD", self.pattern)
        if named or any(_is_mark(ord(ch)) for ch in decomposed):
            raise ValueError(
                "ignore_diacritics strips combining marks and the tatweel from the text, so a "
                "pattern that names one (or a letter that carries one, such as é) could never "
                "match: write the pattern without them"
            )
        return self

    def flags(self) -> int:
        return 0 if self.case_sensitive else re.IGNORECASE


_CHILD = (
    "import json, re, sys\n"
    "job = json.load(sys.stdin)\n"
    "found = re.compile(job['pattern'], job['flags'])\n"
    "print(json.dumps([i for i, t in enumerate(job['texts']) if found.search(t)]))\n"
)


class RegexTimeout(Exception):
    """The search ran past :data:`REGEX_TIMEOUT_SECONDS` and was killed."""


async def search_turns(
    pattern: str, flags: int, texts: list[str], *, timeout: float | None = None
) -> list[int]:
    """The indexes of ``texts`` the pattern is found in, searched in a child Python process
    that is killed at the deadline.

    A separate process, not a thread: CPython's ``re`` holds the GIL for the whole search, so
    a thread can neither be stopped nor even let a deadline fire on the event loop until the
    search ends. Raises :class:`RegexTimeout`, or ``OSError`` when the child cannot run."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _CHILD,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    job = json.dumps({"pattern": pattern, "flags": flags, "texts": texts}).encode()
    try:
        async with asyncio.timeout(REGEX_TIMEOUT_SECONDS if timeout is None else timeout):
            out, err = await proc.communicate(job)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RegexTimeout from None
    if proc.returncode != 0:
        raise OSError(f"the regex worker exited {proc.returncode}: {err.decode()[-200:]}")
    found = json.loads(out)
    if not isinstance(found, list) or not all(isinstance(i, int) for i in found):
        raise OSError("the regex worker answered with something other than indexes")
    return found


@register
class ContentRegex(Scorer):
    id = "content.regex"
    axis = Axis.OUTPUT
    Params = RegexParams
    description = (
        "A regular expression is found (must_match: true) or not found in any examined turn "
        "(false) in what was said (params: pattern, scope, speaker, case_sensitive, "
        "ignore_diacritics). Searched in a separate process with a deadline."
    )

    async def score(self, record: RunRecord, spec: ScorerSpec, ctx: ScoringContext) -> ScoreResult:
        try:
            params = RegexParams.model_validate(spec.params)
        except ValidationError as exc:
            return self.infra(spec, _bad(exc))
        turns = _scoped(_turns(record, params.speaker), params.scope)
        if not turns:
            if params.speaker == "customer":
                return self.infra(spec, "NOT_MEASURABLE: the simulated customer sent no text")
            if params.must_match:
                return self.result(spec, 0.0, note=_silent(params))
            return self.result(spec, 1.0, vacuous=True, note=_silent(params))
        searched = [(n, strip_marks(t) if params.ignore_diacritics else t) for n, t in turns]
        # The cap applies to the text actually searched, after any normalisation.
        long = [n for n, t in searched if len(t) > MAX_REGEX_INPUT]
        if long:
            return self.infra(
                spec,
                f"REGEX_INPUT_TOO_LONG: turn(s) {long} are over {MAX_REGEX_INPUT} characters, "
                "the most content.regex searches (a guard against slow patterns)",
            )
        try:
            hits = await search_turns(params.pattern, params.flags(), [t for _, t in searched])
        except RegexTimeout:
            # The author's pattern was too slow on this text: not measured, never the agent's.
            return self.infra(
                spec,
                f"REGEX_TIMEOUT: the search ran past {REGEX_TIMEOUT_SECONDS:g}s and was stopped; "
                "simplify the pattern",
            )
        except OSError as exc:
            return self.infra(spec, f"REGEX_ERROR: {exc}")
        matched = [searched[i][0] for i in hits]
        checked = [n for n, _ in searched]
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
