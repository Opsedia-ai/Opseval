# SPDX-License-Identifier: Apache-2.0
"""Language packs: how the simulated customer should write.

Community packs can add languages and dialects; review by native speakers is required
before a pack is marked stable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguagePack:
    code: str
    dialect: str | None
    instruction: str
    examples: tuple[str, ...] = ()
    stable: bool = False


PACKS: dict[tuple[str, str | None], LanguagePack] = {}


def register(pack: LanguagePack, *aliases: str) -> None:
    PACKS[(pack.code, pack.dialect)] = pack
    for alias in aliases:
        PACKS[(pack.code, alias)] = pack


register(
    LanguagePack(
        code="en",
        dialect=None,
        instruction=(
            "Write every message in plain, everyday English, the way a real customer types "
            "into a chat box: short sentences, no bullet points, no formal letter style."
        ),
        stable=True,
    )
)
register(
    LanguagePack(
        code="ar",
        dialect=None,
        instruction=(
            "Write every message in Modern Standard Arabic (الفصحى), simple and polite, the way a "
            "customer writes in a support chat. Use Arabic script only."
        ),
    )
)
register(
    LanguagePack(
        code="ar",
        dialect="egyptian",
        instruction=(
            "Write every message in colloquial Egyptian Arabic (العامية المصرية), NOT formal "
            "Arabic. Use Arabic script and everyday Cairene expressions. It is normal to mix in "
            "an English word for technical or product terms."
        ),
        examples=("لو سمحت الطلب وصلني بايظ", "عايز فلوسي ترجع", "تمام كده، موافق"),
    ),
    "eg",
    "egypt",
    "masri",
)
register(
    LanguagePack(
        code="ar",
        dialect="gulf",
        instruction=(
            "Write every message in colloquial Gulf Arabic (اللهجة الخليجية), as spoken in Saudi "
            "Arabia and the UAE, NOT formal Arabic. Use Arabic script and everyday Gulf "
            "expressions."
        ),
        examples=("السلام عليكم، الطلب وصلني خربان", "أبي فلوسي ترجع", "تمام، موافق"),
    ),
    "khaleeji",
    "saudi",
    "emirati",
)


def resolve(language: str, dialect: str | None) -> LanguagePack:
    """Pick the pack for a persona, falling back to the base language, then English."""
    key = (language.lower(), dialect.lower() if dialect else None)
    if key in PACKS:
        return PACKS[key]
    if (key[0], None) in PACKS:
        return PACKS[(key[0], None)]
    return PACKS[("en", None)]
