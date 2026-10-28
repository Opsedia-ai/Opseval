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

from dataclasses import dataclass

from opseval.errors import ConfigError
from opseval.models.scenario import Persona


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


@dataclass(frozen=True)
class Resolution:
    pack: LanguagePack
    fallback_from: str | None = None
    """The dialect asked for, when ``pack`` only stands in for it (it has no pack of its own)."""


PACKS: list[LanguagePack] = []
_EXACT: dict[tuple[str, str | None], LanguagePack] = {}
_FALLBACK: dict[tuple[str, str], LanguagePack] = {}


def _norm(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _sub(code: str, name: str) -> str:
    """A dialect key under ``code``: ``ar-EG``, ``EG`` and ``eg`` are the same key."""
    n = _norm(name)
    return n[len(code) + 1 :] if n.startswith(code + "-") else n


def register(pack: LanguagePack) -> None:
    """Add a pack. All or nothing: a key already taken raises before anything is written."""
    code = _norm(pack.code)
    tag_code, _, tag_sub = _norm(pack.tag).partition("-")
    if tag_code != code:
        raise ValueError(f"pack {pack.tag!r}: tag does not start with its code {pack.code!r}")
    keys: list[tuple[str, str | None]] = [(code, tag_sub or None)]
    keys += [(code, _sub(code, n)) for n in (pack.dialect, *pack.aliases) if n]
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


def _known(code: str | None = None) -> str:
    tags = [p.tag for p in PACKS if code is None or _norm(p.code) == code]
    return ", ".join(tags)


def lookup(language: str, dialect: str | None) -> Resolution:
    """The pack for a persona, and whether it only stands in for the dialect asked for.

    Raises :class:`UnknownLanguage` when no pack matches; never falls back to English.
    """
    lang = _norm(language)
    code, _, from_tag = lang.partition("-")
    asked: list[tuple[str, str, str]] = [(from_tag, "language", language)] if from_tag else []
    if dialect is not None and dialect.strip():
        asked.append((_sub(code, dialect), "dialect", dialect))
    # A code is known when any pack has it: a regional pack (fr-CA) needs no base pack (fr).
    if not code or not any(_norm(p.code) == code for p in PACKS):
        raise UnknownLanguage(
            f"no language pack for {language!r} (packs: {_known()}); add a pack or use one "
            "of these",
            field="language",
        )
    if not asked and (code, None) not in _EXACT:
        raise UnknownLanguage(
            f"no base pack for {language!r}, only regional ones (packs: {_known(code)}); "
            "name one of these",
            field="language",
        )
    found: list[Resolution] = []
    for sub, fld, raw in asked:
        if (code, sub) in _EXACT:
            found.append(Resolution(_EXACT[(code, sub)]))
        elif (code, sub) in _FALLBACK:
            found.append(Resolution(_FALLBACK[(code, sub)], fallback_from=raw))
        else:
            raise UnknownLanguage(
                f"no {code!r} pack for dialect {raw!r} (packs: {_known(code)}); add a pack "
                "or use one of these",
                field=fld,
            )
    if not found:
        return Resolution(_EXACT[(code, None)])
    if len({id(r.pack) for r in found}) > 1:
        raise UnknownLanguage(
            f"language {language!r} and dialect {dialect!r} name different packs "
            f"({found[0].pack.tag} and {found[1].pack.tag}); give one of them",
            field="dialect",
        )
    # The same pack named twice: exactly once is enough, so no warning.
    return next((r for r in found if r.fallback_from is None), found[0])


def resolve(language: str, dialect: str | None) -> LanguagePack:
    """The pack for a persona. Raises :class:`UnknownLanguage`; never falls back to English."""
    return lookup(language, dialect).pack


@dataclass(frozen=True)
class Spoken:
    """The language the simulated customer writes in a scenario, resolved once (ADR-0059)."""

    language: str
    dialect: str | None
    tag: str | None
    """The pack's tag, or ``None`` when no pack is used (scripted turns with no pack, or a
    dialect a pack only stands in for)."""

    @property
    def label(self) -> str:
        """``ar (egyptian)``: how a judge is told the language. Unchanged for every name a
        persona could give before ADR-0059's tags, so judge prompts and digests are too."""
        return f"{self.language} ({self.dialect})" if self.dialect else self.language


def effective_language(persona: Persona, *, scripted: bool) -> Spoken:
    """What the customer writes: an LLM persona writes the pack it resolves to (a declared
    fallback included, so ``ar`` + ``levantine`` is MSA), named canonically however the persona
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
    return Spoken(found.pack.code, found.pack.dialect, found.pack.tag)


register(
    LanguagePack(
        tag="en",
        code="en",
        dialect=None,
        instruction=(
            "Write every message in plain, everyday English, the way a real customer types "
            "into a chat box: short sentences, no bullet points, no formal letter style."
        ),
        stable=True,
        # Until en-US and en-GB have packs of their own (ADR-0059, step 2).
        fallback_for=(
            *("US", "GB", "AU", "CA", "IE", "NZ"),
            *("american", "british", "uk", "australian", "canadian", "irish"),
        ),
    )
)
register(
    LanguagePack(
        tag="ar",
        code="ar",
        dialect=None,
        instruction=(
            "Write every message in Modern Standard Arabic (الفصحى), simple and polite, the way a "
            "customer writes in a support chat. Use Arabic script only."
        ),
        aliases=("msa",),
        # Arabic dialects with no pack yet are written in MSA, the standard written form,
        # as they were before ADR-0059, now with a warning (MENA step).
        fallback_for=(
            "levantine",
            "lebanese",
            "jordanian",
            "syrian",
            "palestinian",
            "iraqi",
            "maghrebi",
            "moroccan",
            "algerian",
            "tunisian",
            "libyan",
            "sudanese",
            "yemeni",
            "LB",
            "JO",
            "SY",
            "PS",
            "IQ",
            "MA",
            "DZ",
            "TN",
            "LY",
            "SD",
            "YE",
        ),
    )
)
register(
    LanguagePack(
        tag="ar-EG",
        code="ar",
        dialect="egyptian",
        instruction=(
            "Write every message in colloquial Egyptian Arabic (العامية المصرية), NOT formal "
            "Arabic. Use Arabic script and everyday Cairene expressions. It is normal to mix in "
            "an English word for technical or product terms."
        ),
        examples=("لو سمحت الطلب وصلني بايظ", "عايز فلوسي ترجع", "تمام كده، موافق"),
        aliases=("eg", "egypt", "masri"),
    )
)
register(
    LanguagePack(
        # No single region: the pack covers Saudi Arabia and the UAE, so it takes a private-use
        # subtag (as ar-x-codeswitch does) and stands in for the Gulf regions until each has a
        # pack of its own.
        tag="ar-x-gulf",
        code="ar",
        dialect="gulf",
        instruction=(
            "Write every message in colloquial Gulf Arabic (اللهجة الخليجية), as spoken in Saudi "
            "Arabia and the UAE, NOT formal Arabic. Use Arabic script and everyday Gulf "
            "expressions."
        ),
        examples=("السلام عليكم، الطلب وصلني خربان", "أبي فلوسي ترجع", "تمام، موافق"),
        aliases=("khaleeji", "saudi", "emirati"),
        fallback_for=("SA", "AE", "KW", "QA", "BH", "OM", "kuwaiti", "qatari", "bahraini", "omani"),
    )
)


__all__ = [
    "PACKS",
    "LanguagePack",
    "Resolution",
    "Spoken",
    "UnknownLanguage",
    "effective_language",
    "lookup",
    "register",
    "resolve",
]
