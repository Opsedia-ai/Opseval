# SPDX-License-Identifier: Apache-2.0
"""Language packs: how the simulated customer should write (ADR-0059).

A persona names its language as a BCP-47 tag (``ar-EG``), or as ``language`` plus
``dialect`` (``ar`` + ``egyptian``), as before. Matching ignores case and reads ``_`` as
``-``. Every alias that resolved before still resolves to the same pack, so no saved scenario
changes meaning.

Nothing falls back silently. A language with no pack, or a dialect no pack names, is
:class:`UnknownLanguage`, a ``ConfigError``: the catalogue loader refuses the scenario, so the
simulated customer can never write English for a French scenario. A pack may *declare* the
dialects it stands in for (``fallback_for``); those load, and ``advice.scenario_warnings``
says the customer is simulated with the nearer pack.

This module lives outside ``opseval.simulator`` so the catalogue (and the offline paths that
import it) can check a persona without importing the simulator. Community packs need a review
by native speakers before a pack is marked stable.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from opseval.errors import ConfigError
from opseval.models.scenario import Persona

Script = Literal[
    "arabic", "latin", "han", "kana", "hangul", "cyrillic", "devanagari", "hebrew", "greek", "thai"
]
"""The Unicode scripts ``output.language_script`` counts (ADR-0059, v2)."""

SCRIPT_PREFIXES: dict[str, tuple[str, ...]] = {
    "arabic": ("ARABIC",),
    "latin": ("LATIN", "FULLWIDTH LATIN"),
    "han": ("CJK",),
    "kana": ("HIRAGANA", "KATAKANA", "HALFWIDTH KATAKANA"),
    "hangul": ("HANGUL", "HALFWIDTH HANGUL"),
    "cyrillic": ("CYRILLIC",),
    "devanagari": ("DEVANAGARI",),
    "hebrew": ("HEBREW",),
    "greek": ("GREEK",),
    "thai": ("THAI",),
}
"""Each :data:`Script`, by the start of a letter's Unicode name. The one script table: the
voice bridge's off-script annotation and ``output.language_script`` both read it."""

Keyboard = Literal["qwerty", "azerty", "qwertz", "ime"]
"""The typing-noise models a pack can name (:attr:`LanguagePack.keyboard`)."""

SCRIPT_GROUPS: dict[str, tuple[Script, ...]] = {"japanese": ("han", "kana")}
"""Names for scripts written together: Japanese mixes kanji (``han``) and kana in a sentence."""


def is_letter(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "L"


def script_of(ch: str) -> str | None:
    """The :data:`Script` a letter is in, ``other`` for a script not listed, ``None`` for a
    character that is not a letter (digits, punctuation, marks, spaces)."""
    if not is_letter(ch):
        return None
    name = unicodedata.name(ch, "")
    return next((k for k, p in SCRIPT_PREFIXES.items() if name.startswith(p)), "other")


def letter_scripts(text: str) -> Counter[str]:
    """How many letters of each script ``text`` has (``other`` for scripts not listed).
    Digits, punctuation, marks and spaces are not letters, so they never count."""
    counts: Counter[str] = Counter()
    for ch in text:
        script = script_of(ch)
        if script is not None:
            counts[script] += 1
    return counts


def in_scripts(
    text: str,
    scripts: tuple[str, ...],
    *,
    primary: str | None = None,
    primary_share: float = 0.0,
) -> bool:
    """Whether the expected scripts, together, have more letters than any other script, and,
    with ``primary``, whether that script has at least ``primary_share`` of the letters (a
    code-switching reply must still be mostly Arabic). A message with no letters is not in the
    script: a blank reply is not an answer in it."""
    counts = letter_scripts(text)
    want = sum(counts[s] for s in scripts)
    other = max((n for s, n in counts.items() if s not in scripts), default=0)
    if want <= other:
        return False
    if primary is not None and primary_share > 0:
        return counts[primary] >= primary_share * sum(counts.values())
    return True


def primary_subtag(language: str) -> str:
    """``ar`` for ``ar-EG``, ``AR_eg`` or ``ar``."""
    return language.strip().split("-")[0].split("_")[0].lower()


LEGACY_LABELS = frozenset({"en", "ar", "ar-EG", "ar-x-gulf"})
"""Packs that existed before the human-readable label: judges are told ``ar (egyptian)`` for
them, byte for byte as before, so no saved grade or rubric digest moves."""


class UnknownLanguage(ConfigError):
    """No language pack matches a persona's language or dialect. Maps to exit code 3."""

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field
        """The persona field at fault: ``language`` or ``dialect``."""


@dataclass(frozen=True)
class LanguagePack:
    tag: str
    """The pack's BCP-47 tag (``ar-EG``; a private-use subtag where no region fits)."""
    code: str
    """The base language subtag (``ar``)."""
    dialect: str | None
    """The dialect name a persona writes with ``language: <code>``; ``None`` for the base."""
    instruction: str
    examples: tuple[str, ...] = ()
    stable: bool = False
    aliases: tuple[str, ...] = ()
    """Other names for this pack's dialect, after ``language: <code>``."""
    fallback_for: tuple[str, ...] = ()
    """Dialects (names or region subtags) this pack stands in for, with a warning."""
    name: str = ""
    """A human-readable name, such as ``Arabic (Saudi)``: what a judge and a reader are told."""
    scripts: tuple[Script, ...] = ()
    """The Unicode scripts the dialect is normally typed in, the default of
    ``output.language_script`` (ADR-0059). **Required**: :func:`register` refuses a pack
    without one, because a guessed default (Latin) would fail every reply of a correct agent in
    another script. Code-switching packs list more than one."""
    primary_script: Script | None = None
    """For a code-switching pack, the script most of each message is in (``arabic``)."""
    primary_share: float = 0.0
    """With ``primary_script``: the share of a reply's letters that must be in it for
    ``output.language_script`` (``0.5``: an all-English reply to an Arabic–English customer
    fails, as it should). ``0`` means no such floor."""
    mixes: tuple[str, ...] = ()
    """The languages a code-switching pack mixes within a sentence (``ar``, ``en``): judges are
    told the switches are on purpose, as for ``voice.code_switching`` (ADR-0055)."""
    narrower: tuple[tuple[str, str], ...] = ()
    """``(alias, tag)``: an alias kept so saved scenarios keep their pack, and the more
    specific pack that now exists for it (``saudi`` stays Gulf; ``ar-SA`` is its own).
    ``advice`` names the narrower pack; nothing resolves to it silently."""
    provider_language: str | None = None
    """The BCP-47 tag speech providers are given (``ar-SA`` for the Gulf pack, say). Unset:
    the pack's tag, unless it has a private-use subtag (``ar-x-gulf``), then the base
    language (``ar``): a provider is never handed a tag it cannot know."""
    default_for_code: bool = False
    """The pack also answers the bare language code: ``fr`` is the ``fr-FR`` pack (ADR-0059,
    US/EU step). Only one pack per code may; a code with none (``zh``) must name a pack."""
    written_form: str = ""
    """What the judges are told about the written form, beside the label (the character set,
    and the forms that count as the same language): ``zh-HK`` writes Traditional characters,
    and written Cantonese and Standard Written Chinese are both acceptable. It joins the
    rubric. Empty for every pack before this field, so their prompts and digests are unchanged."""
    script_cannot_tell: str = ""
    """What ``output.language_script`` cannot see for this pack (Simplified versus Traditional
    characters are both Unicode Han): ``advice`` warns when the scenario checks the script
    with no ``judge.language_consistency`` to grade the rest."""
    keyboard: Keyboard = "qwerty"
    """The typing-noise model (:mod:`opseval.simulator.typing_noise`): the Latin layout a
    neighbouring-key slip is drawn from (``azerty`` for French, ``qwertz`` for German), or
    ``ime`` for a script typed through an input method (Chinese): Han characters are never
    mistyped, because an IME's errors are whole wrong characters, which Opseval does not
    model; Latin words in the message still are, on QWERTY."""


@dataclass(frozen=True)
class Resolution:
    pack: LanguagePack
    fallback_from: str | None = None
    """The dialect asked for, when ``pack`` only stands in for it (it has no pack of its own)."""
    kept_pair: tuple[str, ...] = ()
    """The packs ``language`` and ``dialect`` name now, when they name different ones and the
    pair is kept on the pack it resolved to before the MENA step (ar-SA + saudi stays Gulf).
    ``advice`` names them; nothing resolves to them silently."""


PACKS: list[LanguagePack] = []
_EXACT: dict[tuple[str, str | None], LanguagePack] = {}
_FALLBACK: dict[tuple[str, str], LanguagePack] = {}


def _norm(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _sub(code: str, name: str) -> str:
    """A dialect key under ``code``: ``ar-EG``, ``EG`` and ``eg`` are the same key."""
    n = _norm(name)
    return n[len(code) + 1 :] if n.startswith(code + "-") else n


def _check_pack(pack: LanguagePack) -> None:
    if not pack.scripts:
        raise ValueError(
            f"pack {pack.tag!r}: scripts is required (the scripts the dialect is typed in); "
            "output.language_script reads it, and no default is safe"
        )
    unknown = [s for s in pack.scripts if s not in SCRIPT_PREFIXES]
    if unknown:
        raise ValueError(f"pack {pack.tag!r}: unknown scripts {unknown}")
    if pack.primary_script is not None and pack.primary_script not in pack.scripts:
        raise ValueError(f"pack {pack.tag!r}: primary_script is not one of its scripts")
    if not 0 <= pack.primary_share <= 1 or (pack.primary_share and pack.primary_script is None):
        raise ValueError(f"pack {pack.tag!r}: primary_share needs a primary_script, in [0, 1]")
    if pack.provider_language is not None and "-x-" in f"-{_norm(pack.provider_language)}-":
        raise ValueError(f"pack {pack.tag!r}: provider_language must not be private use")
    if pack.keyboard not in ("qwerty", "azerty", "qwertz", "ime"):
        raise ValueError(f"pack {pack.tag!r}: unknown keyboard {pack.keyboard!r}")


def register(pack: LanguagePack) -> None:
    """Add a pack. All or nothing: a key already taken, or a pack with no ``scripts``, raises
    before anything is written."""
    _check_pack(pack)
    code = _norm(pack.code)
    tag_code, _, tag_sub = _norm(pack.tag).partition("-")
    if tag_code != code:
        raise ValueError(f"pack {pack.tag!r}: tag does not start with its code {pack.code!r}")
    keys: list[tuple[str, str | None]] = [(code, tag_sub or None)]
    keys += [(code, _sub(code, n)) for n in (pack.dialect, *pack.aliases) if n]
    if pack.default_for_code:
        keys.append((code, None))
    exact = list(dict.fromkeys(keys))
    fallback = list(dict.fromkeys((code, _sub(code, n)) for n in pack.fallback_for))
    for key in exact:
        if key in _EXACT or key in _FALLBACK:
            raise ValueError(f"pack {pack.tag!r}: {key} is already taken")
    for fkey in fallback:
        if fkey in _EXACT or fkey in _FALLBACK or fkey in exact:
            raise ValueError(f"pack {pack.tag!r}: fallback {fkey[1]!r} is already taken")
    for key in exact:
        _EXACT[key] = pack
    for fkey in fallback:
        _FALLBACK[fkey] = pack
    PACKS.append(pack)


def by_tag(tag: str | None) -> LanguagePack | None:
    """The pack with this exact tag (case and ``_`` ignored), or ``None``."""
    if tag is None:
        return None
    return next((p for p in PACKS if _norm(p.tag) == _norm(tag)), None)


def _known(code: str | None = None) -> str:
    tags = [p.tag for p in PACKS if code is None or _norm(p.code) == code]
    return ", ".join(tags)


V = TypeVar("V")


def _find(
    language: str,
    dialect: str | None,
    exact: Mapping[tuple[str, str | None], V],
    fallback: Mapping[tuple[str, str], V],
    known: str,
) -> list[tuple[V, str | None]]:
    """What ``language`` and ``dialect`` each name in one table: ``(value, fallback_from)``,
    the base pack alone when neither names a dialect. Raises :class:`UnknownLanguage`."""
    code, _, from_tag = _norm(language).partition("-")
    asked: list[tuple[str, str, str]] = [(from_tag, "language", language)] if from_tag else []
    if dialect is not None and dialect.strip():
        asked.append((_sub(code, dialect), "dialect", dialect))
    # A code is known when any pack has it: a regional pack (fr-CA) needs no base pack (fr).
    if not code or not any(k[0] == code for k in (*exact, *fallback)):
        raise UnknownLanguage(
            f"no language pack for {language!r} (packs: {known}); add a pack or use one of these",
            field="language",
        )
    if not asked:
        if (code, None) not in exact:
            raise UnknownLanguage(
                f"no base pack for {language!r}, only regional ones (packs: {known}); "
                "name one of these",
                field="language",
            )
        return [(exact[(code, None)], None)]
    found: list[tuple[V, str | None]] = []
    for sub, fld, raw in asked:
        if (code, sub) in exact:
            found.append((exact[(code, sub)], None))
        elif (code, sub) in fallback:
            found.append((fallback[(code, sub)], raw))
        else:
            raise UnknownLanguage(
                f"no {code!r} pack for dialect {raw!r} (packs: {known}); add a pack "
                "or use one of these",
                field=fld,
            )
    return found


def _one(found: list[tuple[V, str | None]]) -> tuple[V, str | None] | None:
    """The single pack named, preferring an exact name over a fallback; ``None`` when the two
    fields name different packs."""
    if len({id(v) for v, _ in found}) > 1:
        return None
    # The same pack named twice: exactly once is enough, so no warning.
    return next((f for f in found if f[1] is None), found[0])


def lookup(language: str, dialect: str | None) -> Resolution:
    """The pack for a persona, and whether it only stands in for the dialect asked for.

    Two fields naming different packs is refused, except a pair that loaded before the MENA
    step (``ar-SA`` + ``saudi``, ``ar-LB`` + ``levantine``): it keeps the pack it resolved to
    then (:func:`legacy_lookup`), and :attr:`Resolution.kept_pair` names the packs it now
    also names, so ``advice`` can say which tag to use. Raises :class:`UnknownLanguage`
    when no pack matches; never falls back to English.
    """
    code = _norm(language).partition("-")[0]
    found = _find(language, dialect, _EXACT, _FALLBACK, _known(code if code else None))
    one = _one(found)
    if one is not None:
        return Resolution(one[0], fallback_from=one[1])
    named = tuple(dict.fromkeys(p.tag for p, _ in found))
    try:
        tag, fallback_from = legacy_lookup(language, dialect)
    except UnknownLanguage:
        tag = None
    pack = by_tag(tag)
    if pack is None:
        raise UnknownLanguage(
            f"language {language!r} and dialect {dialect!r} name different packs "
            f"({' and '.join(named)}); give one of them",
            field="dialect",
        )
    return Resolution(
        pack, fallback_from=fallback_from, kept_pair=tuple(t for t in named if t != pack.tag)
    )


def narrower(language: str, dialect: str | None, pack: LanguagePack) -> list[tuple[str, str, str]]:
    """``(field, alias, tag)`` for each kept alias the persona used that now has a more
    specific pack (``saudi`` and ``ar-SA``): ``advice`` names it, nothing switches to it."""
    code, _, from_tag = _norm(language).partition("-")
    used = [("language", from_tag)] if from_tag else []
    if dialect is not None and dialect.strip():
        used.append(("dialect", _sub(code, dialect)))
    return [(fld, alias, tag) for fld, sub in used for alias, tag in pack.narrower if sub == alias]


def resolve(language: str, dialect: str | None) -> LanguagePack:
    """The pack for a persona. Raises :class:`UnknownLanguage`; never falls back to English."""
    return lookup(language, dialect).pack


# --- before the MENA step -------------------------------------------------------------------
#
# Frozen: the four packs and their names exactly as they resolved before the MENA step (step 1
# of ADR-0059). Two things read it, and nothing else may: a pair of fields that loaded then and
# would now name two packs keeps its old pack (:func:`lookup`), and a run recorded before the
# resolved pack was written into the record is graded against what its simulator was told then
# (:func:`graded_language`), so ``rescore`` keeps its rubric. Never edit these tables.

_LEGACY_PACKS: dict[str, tuple[str, str | None]] = {
    "en": ("en", None),
    "ar": ("ar", None),
    "ar-EG": ("ar", "egyptian"),
    "ar-x-gulf": ("ar", "gulf"),
}
"""Tag → (code, dialect) of each pack before the MENA step."""

_LEGACY_EXACT: dict[tuple[str, str | None], str] = {
    ("en", None): "en",
    ("ar", None): "ar",
    ("ar", "msa"): "ar",
    ("ar", "eg"): "ar-EG",
    ("ar", "egyptian"): "ar-EG",
    ("ar", "egypt"): "ar-EG",
    ("ar", "masri"): "ar-EG",
    ("ar", "x-gulf"): "ar-x-gulf",
    ("ar", "gulf"): "ar-x-gulf",
    ("ar", "khaleeji"): "ar-x-gulf",
    ("ar", "saudi"): "ar-x-gulf",
    ("ar", "emirati"): "ar-x-gulf",
}
_LEGACY_FALLBACK: dict[tuple[str, str], str] = {
    **{
        ("en", _norm(n)): "en"
        for n in (
            *("US", "GB", "AU", "CA", "IE", "NZ"),
            *("american", "british", "uk", "australian", "canadian", "irish"),
        )
    },
    **{
        ("ar", _norm(n)): "ar"
        for n in (
            *("levantine", "lebanese", "jordanian", "syrian", "palestinian", "iraqi"),
            *("maghrebi", "moroccan", "algerian", "tunisian", "libyan", "sudanese", "yemeni"),
            *("LB", "JO", "SY", "PS", "IQ", "MA", "DZ", "TN", "LY", "SD", "YE"),
        )
    },
    **{
        ("ar", _norm(n)): "ar-x-gulf"
        for n in (
            *("SA", "AE", "KW", "QA", "BH", "OM"),
            *("kuwaiti", "qatari", "bahraini", "omani"),
        )
    },
}


def legacy_lookup(language: str, dialect: str | None) -> tuple[str, str | None]:
    """``(tag, fallback_from)`` as :func:`lookup` resolved a persona before the MENA step.
    Raises :class:`UnknownLanguage` where it did (two packs named, or none)."""
    found = _find(language, dialect, _LEGACY_EXACT, _LEGACY_FALLBACK, ", ".join(_LEGACY_PACKS))
    # Tags are interned strings here, so compare by value, not identity.
    if len({v for v, _ in found}) > 1:
        raise UnknownLanguage(
            f"language {language!r} and dialect {dialect!r} named different packs",
            field="dialect",
        )
    return next((f for f in found if f[1] is None), found[0])


@dataclass(frozen=True)
class Spoken:
    """The language the simulated customer writes in a scenario, resolved once (ADR-0059)."""

    language: str
    dialect: str | None
    tag: str | None
    """The pack's tag, or ``None`` when no pack is used (scripted turns with no pack, or a
    dialect a pack only stands in for)."""
    name: str | None = None
    """The pack's human-readable name and tag (``Arabic (Saudi), ar-SA``); ``None`` for the
    packs in :data:`LEGACY_LABELS` and when no pack is used."""
    written_form: str | None = None
    """The pack's :attr:`LanguagePack.written_form`, when it has one: told to the judges
    beside the label, and part of the rubric."""

    @property
    def label(self) -> str:
        """How a judge is told the language: ``Arabic (Saudi), ar-SA`` for a pack with a
        human-readable label; ``ar (egyptian)`` otherwise. Unchanged for every name a persona
        could give before ADR-0059's tags, so judge prompts and digests are too."""
        if self.name:
            return self.name
        return f"{self.language} ({self.dialect})" if self.dialect else self.language

    @property
    def rubric(self) -> tuple[str, ...]:
        """What a judge's rubric digest holds about the language: the pack tag too for a
        labelled pack (ADR-0059), exactly the pre-ADR-0059 pair for the others."""
        base = (self.language, self.dialect or "")
        if not (self.name and self.tag):
            return base
        form = (f"written_form={self.written_form}",) if self.written_form else ()
        return (*base, self.tag, *form)

    @property
    def pack(self) -> LanguagePack | None:
        return by_tag(self.tag)


def _spoken(pack: LanguagePack) -> Spoken:
    if pack.tag in LEGACY_LABELS or not pack.name:
        return Spoken(pack.code, pack.dialect, pack.tag)
    return Spoken(
        pack.code, pack.dialect, pack.tag, f"{pack.name}, {pack.tag}", pack.written_form or None
    )


def effective_language(persona: Persona, *, scripted: bool) -> Spoken:
    """What the customer writes: an LLM persona writes the pack it resolves to (a declared
    fallback included, so ``ar`` + ``yemeni`` is MSA), named canonically however the persona
    spelled it; scripted turns are sent verbatim, so they are in the declared language, named
    canonically only when it is exactly a pack.

    Judges grade against this, so the persona judge holds the customer to what the simulator
    was told to write, and two spellings of one pack give the same rubric digest.
    """
    try:
        found = lookup(persona.language, persona.dialect)
    except UnknownLanguage:
        # Only scripted turns load without a pack (an LLM persona is refused at load).
        return Spoken(persona.language, persona.dialect or None, None)
    if scripted and found.fallback_from is not None:
        return Spoken(persona.language, persona.dialect or None, None)
    return _spoken(found.pack)


PACK_KEY = "pack"
"""The key in ``RunRecord.persona`` that holds the tag of the pack resolved at run time
(``None``: no pack was used). A record without it was written before the MENA step."""


def legacy_language(persona: Persona, *, scripted: bool) -> Spoken:
    """:func:`effective_language` as it was before the MENA step, from the frozen tables."""
    try:
        tag, fallback_from = legacy_lookup(persona.language, persona.dialect)
    except UnknownLanguage:
        return Spoken(persona.language, persona.dialect or None, None)
    if scripted and fallback_from is not None:
        return Spoken(persona.language, persona.dialect or None, None)
    code, dialect = _LEGACY_PACKS[tag]
    return Spoken(code, dialect, tag)


def graded_language(
    persona: Persona, *, scripted: bool, recorded: Mapping[str, Any] | None
) -> Spoken:
    """The language a recorded attempt is graded against: the pack resolved at run time and
    written into ``RunRecord.persona`` (:data:`PACK_KEY`), so a later change to the packs
    cannot re-grade a saved run; for a record written before that (it has a persona and no
    pack), the resolution of the time, so ``rescore`` keeps its rubric. No recorded persona
    (a record built by hand): the packs as they are now."""
    if not recorded:
        return effective_language(persona, scripted=scripted)
    if PACK_KEY not in recorded:
        return legacy_language(persona, scripted=scripted)
    tag = recorded[PACK_KEY]
    pack = by_tag(tag) if isinstance(tag, str) else None
    if pack is None:
        return Spoken(persona.language, persona.dialect or None, None)
    return _spoken(pack)


def expected_scripts(
    persona: Persona, *, scripted: bool, spoken: Spoken | None = None
) -> tuple[LanguagePack, bool] | None:
    """The pack ``output.language_script`` takes its default scripts from, and whether it is
    only a stand-in (a declared fallback, or the language's base pack for a dialect or region
    no pack names): the pack in use, else the one a declared fallback names, else the base
    pack of the language code (``ar`` for scripted ``ar-OM``). ``None`` when nothing gives
    one (``fr`` with no pack): the scenario must say ``script``."""
    spoken = spoken or effective_language(persona, scripted=scripted)
    if spoken.pack is not None:
        return spoken.pack, False
    try:
        return lookup(persona.language, persona.dialect).pack, True
    except UnknownLanguage:
        pass
    base = _EXACT.get((primary_subtag(persona.language), None))
    return (base, True) if base is not None else None


@dataclass(frozen=True)
class ScriptExpectation:
    """What ``output.language_script`` holds a reply to (ADR-0059)."""

    scripts: tuple[str, ...]
    primary: str | None = None
    """The script that must have at least ``primary_share`` of the letters, if any."""
    primary_share: float = 0.0
    source: Literal["params", "pack", "stand-in"] = "params"
    """``params`` (given), ``pack`` (the pack in use), ``stand-in`` (a pack that only stands
    in for the persona's language: ``advice`` says so)."""
    pack: str | None = None


def script_expectation(
    persona: Persona | None,
    *,
    scripted: bool,
    script: str | None = None,
    scripts: Sequence[str] | None = None,
    min_primary_share: float | None = None,
    spoken: Spoken | None = None,
) -> ScriptExpectation | None:
    """The scripts a reply must be in: ``script`` (one script, or a group such as
    ``japanese``) or ``scripts`` (a list) when given, else the persona's pack's (see
    :func:`expected_scripts`). ``None`` when neither gives any (or no persona is given): a load
    error, never a guess.

    ``min_primary_share`` sets a floor on the first expected script's share of the letters;
    left out, the pack's own (``ar-x-codeswitch``: half Arabic) applies when the scripts come
    from the pack, none when they are given."""
    if script is not None or scripts:
        listed = list(SCRIPT_GROUPS.get(script, (script,))) if script is not None else []
        names = tuple(dict.fromkeys([*listed, *(scripts or ())]))
        share = min_primary_share or 0.0
        return ScriptExpectation(names, names[0] if share else None, share)
    found = expected_scripts(persona, scripted=scripted, spoken=spoken) if persona else None
    if found is None:
        return None
    pack, stand_in = found
    share = pack.primary_share if min_primary_share is None else min_primary_share
    primary = (pack.primary_script or pack.scripts[0]) if share else None
    return ScriptExpectation(
        tuple(pack.scripts), primary, share, "stand-in" if stand_in else "pack", pack.tag
    )


def voice_language(persona: Persona, *, scripted: bool) -> str:
    """The language tag a voice run speaks and hears in: the pack's tag when one is used, so
    ``OPSEVAL_TTS_VOICE_MAP`` can map ``ar-SA`` however the persona spelled it, else the
    persona's language as given. Speech providers get :func:`provider_language` of it."""
    return effective_language(persona, scripted=scripted).tag or persona.language


def provider_language(language: str) -> str:
    """The BCP-47 tag a speech provider is given for a voice language: the pack's declared
    ``provider_language``, else the tag itself, except that a private-use tag (``ar-x-gulf``)
    becomes its base language (``ar``). A provider never sees a private-use subtag."""
    pack = by_tag(language)
    if pack is not None and pack.provider_language:
        return pack.provider_language
    parts = _norm(language).split("-")
    if "x" in parts:
        return primary_subtag(language)
    return language


# --- the packs ------------------------------------------------------------------------------
#
# Every instruction and example below was written for Opseval: generic customer-service phrasing
# (order status, refunds, delivery, opening hours), nothing taken from any other system. Only
# ``en`` is stable. The Arabic packs are previews until a native speaker has reviewed them and
# an STT check has passed (docs/guide/languages.md; ADR-0059): an unstable pack runs, with a
# warning in the guided form.

_ARABIC_SCRIPT = (
    " Use Arabic script, the way most customers type in a chat app; do not switch to Latin "
    "letters (Arabizi) or to formal Arabic."
)


def _arabic(
    tag: str,
    dialect: str | None,
    name: str,
    instruction: str,
    *examples: str,
    aliases: tuple[str, ...] = (),
    fallback_for: tuple[str, ...] = (),
    narrower: tuple[tuple[str, str], ...] = (),
    scripts: tuple[Script, ...] = ("arabic",),
    primary_script: Script | None = None,
    primary_share: float = 0.0,
    mixes: tuple[str, ...] = (),
) -> LanguagePack:
    return LanguagePack(
        tag=tag,
        code="ar",
        dialect=dialect,
        name=name,
        instruction=instruction,
        examples=examples,
        aliases=aliases,
        fallback_for=fallback_for,
        narrower=narrower,
        scripts=scripts,
        primary_script=primary_script,
        primary_share=primary_share,
        mixes=mixes,
    )


register(
    LanguagePack(
        tag="en",
        code="en",
        dialect=None,
        name="English",
        instruction=(
            "Write every message in plain, everyday English, the way a real customer types "
            "into a chat box: short sentences, no bullet points, no formal letter style."
        ),
        stable=True,
        scripts=("latin",),
        # en-US and en-GB have packs of their own since the US/EU step (ADR-0059); the other
        # regions keep plain English, with a warning, until a pack of theirs is reviewed.
        fallback_for=(
            *("AU", "CA", "IE", "NZ"),
            *("australian", "canadian", "irish"),
        ),
    )
)
register(
    _arabic(
        "ar",
        None,
        "Arabic (Modern Standard)",
        "Write every message in Modern Standard Arabic (الفصحى), simple and polite, the way a "
        "customer writes in a support chat. Use Arabic script only.",
        aliases=("msa",),
        # Arabic dialects with no pack yet are written in MSA, the standard written form,
        # with a warning. The MENA step gave most of the old ones a pack of their own.
        fallback_for=("maghrebi", "libyan", "sudanese", "yemeni", "LY", "SD", "YE"),
    )
)
register(
    _arabic(
        "ar-EG",
        "egyptian",
        "Arabic (Egyptian)",
        "Write every message in colloquial Egyptian Arabic (العامية المصرية), NOT formal "
        "Arabic. Use Arabic script and everyday Cairene expressions. It is normal to mix in "
        "an English word for technical or product terms.",
        "لو سمحت الطلب وصلني بايظ",
        "عايز فلوسي ترجع",
        "تمام كده، موافق",
        aliases=("eg", "egypt", "masri"),
    )
)
register(
    _arabic(
        # No single region: the pack covers Saudi Arabia and the UAE, so it takes a private-use
        # subtag (as ar-x-codeswitch does). Qatar, Bahrain and Oman stay its fallbacks: their
        # chat writing is close to it, and a pack of their own waits for a native reviewer.
        "ar-x-gulf",
        "gulf",
        "Arabic (Gulf)",
        "Write every message in colloquial Gulf Arabic (اللهجة الخليجية), as spoken in Saudi "
        "Arabia and the UAE, NOT formal Arabic. Use Arabic script and everyday Gulf "
        "expressions.",
        "السلام عليكم، الطلب وصلني خربان",
        "أبي فلوسي ترجع",
        "تمام، موافق",
        # `saudi` and `emirati` named this pack before ar-SA and ar-AE existed; they still do,
        # so no saved scenario changes meaning, and advice names the narrower pack.
        aliases=("khaleeji", "saudi", "emirati"),
        narrower=(("saudi", "ar-SA"), ("emirati", "ar-AE")),
        fallback_for=("QA", "BH", "OM", "qatari", "bahraini", "omani"),
    )
)

# Gulf, one pack per country the roadmap names.
register(
    _arabic(
        "ar-SA",
        "ksa",
        "Arabic (Saudi)",
        "Write every message in everyday Saudi Arabic (اللهجة السعودية), the urban register "
        "most Saudi customers type in a support chat (Najdi and Hijazi speakers alike), NOT "
        "formal Arabic. Keep messages short, use common Saudi words such as أبغى or أبي (I "
        "want), وش (what, Najdi) or إيش (what, Hijazi), ليش (why), الحين (now) and زين "
        "(fine), and it is normal to mix in an English word for an app or product term."
        + _ARABIC_SCRIPT,
        "السلام عليكم، طلبي للحين ما وصل",
        "أبغى أسترجع المبلغ لو سمحت",
        "وش آخر موعد للتوصيل؟",
        aliases=("saudi-arabia",),
    )
)
register(
    _arabic(
        "ar-AE",
        "uae",
        "Arabic (Emirati)",
        "Write every message in everyday Emirati Arabic (اللهجة الإماراتية), NOT formal "
        "Arabic. Keep messages short and use common Emirati words such as أبا or أبي (I want), "
        "شو (what), الحين (now), وايد (a lot) and باكر (tomorrow); an English word for a "
        "product, an app or a payment term is normal." + _ARABIC_SCRIPT,
        "مرحبا، الطلبية متأخرة وايد",
        "أبا أعرف شو صار على الاسترجاع",
        "متى تفتحون باكر؟",
    )
)
register(
    _arabic(
        "ar-KW",
        "kuwaiti",
        "Arabic (Kuwaiti)",
        "Write every message in everyday Kuwaiti Arabic (اللهجة الكويتية), NOT formal Arabic. "
        "Keep messages short and use common Kuwaiti words such as أبي (I want), شنو (what), "
        "شلون (how), الحين (now) and وايد (a lot)." + _ARABIC_SCRIPT,
        "هلا، شلون أتابع طلبي؟",
        "الطلب وصلني ناقص، أبي حل",
        "شنو مواعيد الدوام عندكم؟",
    )
)

# Levant: Lebanese and Jordanian have packs, as the roadmap names them; a general Levantine
# pack covers the common urban core and stands in for Syria and Palestine.
register(
    _arabic(
        "ar-x-levant",
        "levantine",
        "Arabic (Levantine)",
        "Write every message in everyday Levantine Arabic (اللهجة الشامية), the common urban "
        "register of Damascus, Beirut and Amman, NOT formal Arabic. Use common words such as "
        "بدي (I want), شو (what), هلق (now), كتير (a lot) and منيح (fine)." + _ARABIC_SCRIPT,
        "مرحبا، بدي اعرف وين صار طلبي",
        "الغرض وصل مكسور، بدي رجّع المصاري",
        "شو أوقات الدوام عندكن؟",
        aliases=("shami",),
        fallback_for=("SY", "PS", "syrian", "palestinian"),
    )
)
register(
    _arabic(
        "ar-LB",
        "lebanese",
        "Arabic (Lebanese)",
        "Write every message in everyday Lebanese Arabic (اللهجة اللبنانية), NOT formal "
        "Arabic. Use common words such as بدي (I want), شو (what), هلق (now), كتير (a lot) "
        "and مش (not); French greetings and thanks written in Arabic letters, such as بونجور "
        "and ميرسي, are natural. Many Lebanese customers type in Latin letters (Arabizi); "
        "this pack does not." + _ARABIC_SCRIPT,
        "بونجور، طلبيتي بعدها ما وصلت",
        "بدي رجّع الغرض، كيف بعمل؟",
        "ميرسي كتير",
    )
)
register(
    _arabic(
        "ar-JO",
        "jordanian",
        "Arabic (Jordanian)",
        "Write every message in everyday Jordanian Arabic (اللهجة الأردنية), NOT formal "
        "Arabic. Use common words such as بدي (I want), شو (what), هسا (now), كتير (a lot) "
        "and منيح (fine)." + _ARABIC_SCRIPT,
        "مرحبا، وين وصل طلبي؟",
        "بدي أرجّع الغرض وآخذ مصاريّ",
        "هسا بتفتحوا ولا لأ؟",
    )
)
register(
    _arabic(
        "ar-IQ",
        "iraqi",
        "Arabic (Iraqi)",
        "Write every message in everyday Iraqi Arabic (اللهجة العراقية), the Baghdadi "
        "register, NOT formal Arabic. Use common words such as أريد (I want), شنو (what), "
        "شلون (how), هسه (now), هواية (a lot) and شوكت (when)." + _ARABIC_SCRIPT,
        "هلو، طلبي ليش تأخر هواية؟",
        "أريد أرجّع فلوسي",
        "شوكت يوصل الطلب؟",
    )
)

# Maghreb: one pack per country, since Moroccan, Algerian and Tunisian differ a lot. French
# words are part of everyday speech in all three; `maghrebi` alone stays an MSA fallback,
# because no one of them may stand in for the others.
register(
    _arabic(
        "ar-MA",
        "moroccan",
        "Arabic (Moroccan Darija)",
        "Write every message in Moroccan Darija (الدارجة المغربية), NOT formal Arabic. Use "
        "common words such as بغيت (I want), شنو or آش (what), فين (where), دابا (now), بزاف "
        "(a lot) and واخا (OK); French shopping words written in Arabic letters, such as "
        "الكوموند (the order), are natural. Many Moroccans type Darija in Latin letters with "
        "digits for some sounds; this pack does not." + _ARABIC_SCRIPT,
        "السلام، فين وصلات الكوموند ديالي؟",
        "بغيت نرجع الفلوس ديالي",
        "واخا، شكرا بزاف",
        aliases=("darija",),
    )
)
register(
    _arabic(
        "ar-DZ",
        "algerian",
        "Arabic (Algerian)",
        "Write every message in everyday Algerian Arabic (الدارجة الجزائرية), NOT formal "
        "Arabic. Use common words such as نحب (I want), واش (what), وقتاش (when), دروك (now) "
        "and بزاف (a lot); French words written in Arabic letters, such as ميرسي, are "
        "natural." + _ARABIC_SCRIPT,
        "السلام، وقتاش توصل الكوموند؟",
        "نحب نرجع دراهمي",
        "صحيت، ميرسي بزاف",
    )
)
register(
    _arabic(
        "ar-TN",
        "tunisian",
        "Arabic (Tunisian)",
        "Write every message in everyday Tunisian Arabic (الدارجة التونسية), NOT formal "
        "Arabic. Use common words such as نحب (I want), شنوة (what), توا (now), برشا (a lot), "
        "باهي (OK) and متاعي (my); French words written in Arabic letters are natural."
        + _ARABIC_SCRIPT,
        "عسلامة، الكوموند متاعي ما وصلتش",
        "نحب نرجّع الفلوس",
        "باهي، يعيشك",
    )
)

# Code-switching as typed in Gulf customer chats: Arabic with English terms in Latin letters.
register(
    _arabic(
        "ar-x-codeswitch",
        "codeswitch",
        "Arabic–English code-switching (Gulf)",
        "Write every message in everyday Gulf Arabic in Arabic script, mixing in English words "
        "and short phrases in Latin letters the way bilingual customers in the Gulf type in a "
        "support chat: order and app terms (order, refund, tracking, app, card) and words "
        "such as ok, please and thanks. Most of each message stays Arabic. Switch naturally, "
        "mid-sentence, and never translate yourself.",
        "هلا، الـ order حقي للحين ما وصل",
        "أبي refund لو سمحت",
        "ok تمام، thanks",
        aliases=("code-switching", "arabic-english"),
        scripts=("arabic", "latin"),
        # Most of each message stays Arabic: an all-English reply is not code-switching.
        primary_script="arabic",
        primary_share=0.5,
        mixes=("ar", "en"),
    )
)


# --- the US/EU step (ADR-0059) --------------------------------------------------------------
#
# Written for Opseval like the packs above: generic customer-service phrasing, nothing from
# any other system. All previews. ``fr``, ``de``, ``es`` and ``it`` alone are the pack of the
# country the ADR's roadmap means (``default_for_code``); another region (``fr-CA``,
# ``es-MX``) has no pack and is refused, never written as the European variety silently.

_CHAT = (
    " Keep messages short, the way a customer types into a chat box: no bullet points, no "
    "letter style, no greeting formula longer than a word or two."
)

register(
    LanguagePack(
        tag="en-US",
        code="en",
        dialect="american",
        name="English (United States)",
        instruction=(
            "Write every message in everyday American English: US spelling (color, center, "
            "canceled), US words (zip code, cell phone, check for a restaurant bill), dates "
            "written month first, and a friendly, direct tone." + _CHAT
        ),
        examples=(
            "hi, my package still hasn't shown up",
            "can I get a refund on my card instead?",
            "ok sounds good, go ahead",
        ),
        scripts=("latin",),
        written_form=(
            "American English. US and British spelling or vocabulary are one language: a "
            "difference between them is not a change of language or dialect."
        ),
    )
)
register(
    LanguagePack(
        tag="en-GB",
        code="en",
        dialect="british",
        aliases=("uk",),
        name="English (United Kingdom)",
        instruction=(
            "Write every message in everyday British English: UK spelling (colour, centre, "
            "cancelled), UK words (postcode, mobile, parcel, bin), dates written day first, "
            "and a polite, slightly understated tone ('sorry to bother you', 'cheers')." + _CHAT
        ),
        examples=(
            "hiya, my parcel still hasn't arrived",
            "could I have a refund to my card instead please?",
            "brilliant, cheers, go ahead",
        ),
        scripts=("latin",),
        written_form=(
            "British English. US and British spelling or vocabulary are one language: a "
            "difference between them is not a change of language or dialect."
        ),
    )
)
register(
    LanguagePack(
        tag="fr-FR",
        code="fr",
        dialect=None,
        default_for_code=True,
        name="French (France)",
        instruction=(
            "Write every message in everyday French as typed in France. Address the agent "
            "politely with 'vous', as French customers do with a company, open with "
            "'Bonjour' and thank with 'merci'. Keep the accents (é, è, à, ç) most of the "
            "time, as people do on a computer; a casual customer may drop one now and then. "
            "Use French words for shopping terms (commande, remboursement, livraison, colis)."
            + _CHAT
        ),
        examples=(
            "Bonjour, ma commande n'est toujours pas arrivée",
            "Je voudrais être remboursé sur ma carte, s'il vous plaît",
            "D'accord, merci, vous pouvez y aller",
        ),
        scripts=("latin",),
        keyboard="azerty",
    )
)
register(
    LanguagePack(
        tag="de-DE",
        code="de",
        dialect=None,
        default_for_code=True,
        name="German (Germany)",
        instruction=(
            "Write every message in everyday German as typed in Germany. Address the agent "
            "with the polite 'Sie' (Sie, Ihnen, Ihr), as German customers do with a company; "
            "'Hallo' or 'Guten Tag' to open and 'danke' to thank are normal. Write the "
            "umlauts and ß (ä, ö, ü, ß), and capitalise nouns. Use German words for "
            "shopping terms (Bestellung, Rückerstattung, Lieferung, Paket)." + _CHAT
        ),
        examples=(
            "Hallo, meine Bestellung ist immer noch nicht da",
            "Ich hätte gern eine Rückerstattung auf meine Karte",
            "Okay, danke, machen Sie das bitte",
        ),
        scripts=("latin",),
        keyboard="qwertz",
    )
)
register(
    LanguagePack(
        tag="es-ES",
        code="es",
        dialect=None,
        default_for_code=True,
        name="Spanish (Spain)",
        instruction=(
            "Write every message in everyday Spanish as typed in Spain (castellano). A "
            "customer may use 'usted' or the friendly 'tú' with a support chat; pick one and "
            "keep it. Use Spain's forms, such as 'vosotros' for 'you all' and words like "
            "'móvil' and 'vale'. Write the accents and ñ most of the time; opening question "
            "and exclamation marks (¿, ¡) are often left out in chat. Use Spanish words for "
            "shopping terms (pedido, reembolso, envío, paquete)." + _CHAT
        ),
        examples=(
            "Hola, mi pedido todavía no ha llegado",
            "Quiero que me devolváis el dinero a la tarjeta",
            "Vale, gracias, adelante",
        ),
        scripts=("latin",),
    )
)
register(
    LanguagePack(
        tag="it-IT",
        code="it",
        dialect=None,
        default_for_code=True,
        name="Italian (Italy)",
        instruction=(
            "Write every message in everyday Italian as typed in Italy. Address the agent "
            "politely with 'Lei' (Lei, Le, Suo), as Italian customers usually do with a "
            "company; 'Buongiorno' or 'Salve' to open and 'grazie' to thank are normal. "
            "Write the accented vowels (è, à, ò, ù, perché); a casual customer may type an "
            "apostrophe instead now and then. Use Italian words for shopping terms (ordine, "
            "rimborso, consegna, pacco)." + _CHAT
        ),
        examples=(
            "Buongiorno, il mio ordine non è ancora arrivato",
            "Vorrei il rimborso sulla carta, per favore",
            "Va bene, grazie, proceda pure",
        ),
        scripts=("latin",),
    )
)

# --- the Chinese step (ADR-0059) ------------------------------------------------------------
#
# Two packs, kept apart on purpose: Mandarin in Simplified characters (mainland China) and
# Cantonese in Traditional characters (Hong Kong). Neither stands in for the other, and ``zh``
# alone names neither, so a persona must say which. ``output.language_script`` sees both as
# Unicode Han and cannot tell Simplified from Traditional: the judges are told the written form
# (``written_form``) and grade it. Typing noise never touches Han characters (``ime``).

_HAN_ONLY = (
    "output.language_script counts Simplified and Traditional characters alike (both are "
    "Unicode Han); only judge.language_consistency, told the pack's written form, grades "
    "which one the agent wrote"
)

register(
    LanguagePack(
        tag="zh-CN",
        code="zh",
        dialect="mandarin",
        aliases=("hans-cn",),
        name="Chinese (Mandarin, Simplified)",
        instruction=(
            "Write every message in everyday Mandarin Chinese as typed in mainland China, in "
            "Simplified characters (简体字) only, never Traditional ones. Write the way a "
            "customer types on a phone into a support chat: short, polite but direct, with "
            "Chinese punctuation (，。？). Common words such as 您好, 请问, 订单, 退款, 快递 "
            "and 谢谢 are natural, and an English brand or app name may stay in Latin letters."
        ),
        examples=(
            "您好，我的订单怎么还没到？",
            "请问可以退款到我的银行卡吗",
            "好的，谢谢，就这样办吧",
        ),
        scripts=("han",),
        written_form=(
            "Mandarin Chinese in Simplified characters (简体字). Traditional characters "
            "(繁體字) are a different written form and do not match it; written Cantonese "
            "does not either. An English brand, app or product name in Latin letters is fine."
        ),
        script_cannot_tell=_HAN_ONLY,
        keyboard="ime",
    )
)
register(
    LanguagePack(
        tag="zh-HK",
        code="zh",
        dialect="cantonese",
        aliases=("hant-hk",),
        name="Chinese (Cantonese, Hong Kong)",
        instruction=(
            "Write every message in written Cantonese as typed in Hong Kong, in Traditional "
            "characters (繁體字) only, never Simplified ones. Use the colloquial Cantonese a "
            "customer types in a chat, with Cantonese words such as 嘅 (of), 咗 (done), 喺 "
            "(at), 唔 (not), 係 (is), 冇 (have not) and 幾時 (when), not formal written "
            "Chinese (書面語) and not Mandarin. Hong Kong customers often drop an English "
            "word into a sentence (order, refund, app, card, ok); that is natural, but most "
            "of each message stays Chinese."
        ),
        examples=(
            "你好，我張單到而家都未到",
            "可唔可以幫我 refund 返落張卡度？",
            "好呀，唔該晒",
        ),
        scripts=("han",),
        written_form=(
            "Hong Kong Chinese in Traditional characters (繁體字). Written Cantonese (with "
            "Cantonese-only words such as 嘅, 咗, 喺, 唔, 係) and Standard Written Chinese "
            "(書面語) are both acceptable written forms, and a message may mix them; English "
            "words for products, apps or payments are common and fine. Simplified characters "
            "(简体字) are not the Hong Kong written form and do not match it."
        ),
        script_cannot_tell=_HAN_ONLY,
        keyboard="ime",
    )
)


__all__ = [
    "LEGACY_LABELS",
    "Keyboard",
    "PACKS",
    "PACK_KEY",
    "SCRIPT_GROUPS",
    "SCRIPT_PREFIXES",
    "ScriptExpectation",
    "LanguagePack",
    "Resolution",
    "Script",
    "Spoken",
    "UnknownLanguage",
    "by_tag",
    "effective_language",
    "expected_scripts",
    "graded_language",
    "in_scripts",
    "legacy_language",
    "legacy_lookup",
    "letter_scripts",
    "lookup",
    "narrower",
    "primary_subtag",
    "provider_language",
    "register",
    "resolve",
    "script_expectation",
    "script_of",
    "voice_language",
]
