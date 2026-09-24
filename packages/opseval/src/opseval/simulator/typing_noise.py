# SPDX-License-Identifier: Apache-2.0
"""Seeded, keyboard-aware typing noise.

Realistic customers mistype. Opseval adds reproducible typos, but never touches the
things that must stay exact: digits (order numbers, amounts), URLs, e-mail addresses,
currency tokens, quoted text and planted ``[INJECT]…[/INJECT]`` payloads.
"""

from __future__ import annotations

import random
import re

# Neighbouring keys on a QWERTY keyboard (lower case).
QWERTY_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
# Neighbouring keys on the common Arabic PC layout (rows as typed left to right).
ARABIC_ROWS = ("ضصثقفغعهخحجد", "شسيبلاتنمكط", "ئءؤرلاىةوزظ")

# Letters Arabic writers commonly swap when typing fast.
ARABIC_SWAPS = {"ة": "ه", "ه": "ة", "ى": "ي", "ي": "ى", "أ": "ا", "إ": "ا", "آ": "ا", "ؤ": "و"}

PROTECTED = re.compile(
    r"\[INJECT\].*?\[/INJECT\]"  # planted payloads
    r"|\"[^\"]*\"|“[^”]*”|«[^»]*»"  # quoted text
    r"|https?://\S+|www\.\S+"  # URLs
    r"|\S+@\S+\.\S+"  # e-mail addresses
    r"|\b(?:SAR|AED|EGP|GBP|USD|EUR|KWD|QAR)\b|ريال|درهم|جنيه|دينار"  # currency
    r"|[0-9٠-٩][0-9٠-٩.,:/-]*",  # numbers of any kind
    re.DOTALL,
)


def _neighbours(rows: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            near = []
            if c > 0:
                near.append(row[c - 1])
            if c < len(row) - 1:
                near.append(row[c + 1])
            for rr in (r - 1, r + 1):
                if 0 <= rr < len(rows) and c < len(rows[rr]):
                    near.append(rows[rr][c])
            out[ch] = "".join(dict.fromkeys(near))
    return out


LATIN_NEAR = _neighbours(QWERTY_ROWS)
ARABIC_NEAR = _neighbours(ARABIC_ROWS)


def _mutate_word(word: str, rng: random.Random) -> str:
    letters = [i for i, ch in enumerate(word) if ch.isalpha()]
    if len(letters) < 3:
        return word
    i = rng.choice(letters[1:])  # keep the first letter: humans rarely miss it
    ch = word[i]
    op = rng.random()
    if ch in ARABIC_SWAPS and op < 0.5:
        return word[:i] + ARABIC_SWAPS[ch] + word[i + 1 :]
    near = ARABIC_NEAR.get(ch) or LATIN_NEAR.get(ch.lower())
    if op < 0.45 and near:  # hit a neighbouring key
        sub = rng.choice(near)
        sub = sub.upper() if ch.isupper() else sub
        return word[:i] + sub + word[i + 1 :]
    if op < 0.7:  # drop a letter
        return word[:i] + word[i + 1 :]
    if op < 0.85 and i + 1 < len(word) and word[i + 1].isalpha():  # swap two letters
        return word[:i] + word[i + 1] + word[i] + word[i + 2 :]
    return word[:i] + ch + word[i:]  # double a letter


def add_typing_noise(text: str, rate: float, seed: int) -> str:
    """Return ``text`` with roughly ``rate`` of eligible words mistyped (deterministic)."""
    if rate <= 0 or not text:
        return text
    # Reproducible noise, not cryptography.
    rng = random.Random(seed)  # noqa: S311  # nosec B311
    out: list[str] = []
    last = 0
    for match in PROTECTED.finditer(text):
        out.append(_noisy_segment(text[last : match.start()], rate, rng))
        out.append(match.group(0))
        last = match.end()
    out.append(_noisy_segment(text[last:], rate, rng))
    return "".join(out)


def _noisy_segment(segment: str, rate: float, rng: random.Random) -> str:
    parts = re.split(r"(\s+)", segment)
    return "".join(
        _mutate_word(p, rng) if p and not p.isspace() and rng.random() < rate else p for p in parts
    )
