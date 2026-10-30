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
from typing import Literal

from opseval.errors import ConfigError
from opseval.models.scenario import Persona

Script = Literal[
    "arabic", "latin", "han", "kana", "hangul", "cyrillic", "devanagari", "hebrew", "greek", "thai"
]
"""The Unicode scripts ``output.language_script`` counts (ADR-0059, v2)."""

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
    scripts: tuple[Script, ...] = ("latin",)
    """The Unicode scripts the dialect is normally typed in, the default of
    ``output.language_script`` (ADR-0059). Code-switching packs list more than one."""
    mixes: tuple[str, ...] = ()
    """The languages a code-switching pack mixes within a sentence (``ar``, ``en``): judges are
    told the switches are on purpose, as for ``voice.code_switching`` (ADR-0055)."""
    narrower: tuple[tuple[str, str], ...] = ()
    """``(alias, tag)``: an alias kept so saved scenarios keep their pack, and the more
    specific pack that now exists for it (``saudi`` stays Gulf; ``ar-SA`` is its own).
    ``advice`` names the narrower pack; nothing resolves to it silently."""


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


def by_tag(tag: str | None) -> LanguagePack | None:
    """The pack with this exact tag (case and ``_`` ignored), or ``None``."""
    if tag is None:
        return None
    return next((p for p in PACKS if _norm(p.tag) == _norm(tag)), None)


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
        return (*base, self.tag) if self.name and self.tag else base

    @property
    def pack(self) -> LanguagePack | None:
        return by_tag(self.tag)


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
    pack = found.pack
    name = None if pack.tag in LEGACY_LABELS or not pack.name else f"{pack.name}, {pack.tag}"
    return Spoken(pack.code, pack.dialect, pack.tag, name)


def voice_language(persona: Persona, *, scripted: bool) -> str:
    """The language tag a voice run speaks and hears in: the pack's tag when one is used, so
    ``OPSEVAL_TTS_VOICE_MAP`` can map ``ar-SA`` however the persona spelled it, else the
    persona's language as given. Speech providers fall back to the primary subtag (``ar``)."""
    return effective_language(persona, scripted=scripted).tag or persona.language


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
        # Until en-US and en-GB have packs of their own (ADR-0059, step 2).
        fallback_for=(
            *("US", "GB", "AU", "CA", "IE", "NZ"),
            *("american", "british", "uk", "australian", "canadian", "irish"),
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
        "want), وش (what), ليش (why), الحين (now) and زين (fine), and it is normal to mix in "
        "an English word for an app or product term." + _ARABIC_SCRIPT,
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
        "Arabic. Use common words such as بدي (I want), شو (what), هسا (now), كثير (a lot) "
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
        mixes=("ar", "en"),
    )
)


__all__ = [
    "LEGACY_LABELS",
    "PACKS",
    "LanguagePack",
    "Resolution",
    "Script",
    "Spoken",
    "UnknownLanguage",
    "by_tag",
    "effective_language",
    "lookup",
    "narrower",
    "register",
    "resolve",
    "voice_language",
]
