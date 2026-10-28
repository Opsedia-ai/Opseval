# SPDX-License-Identifier: Apache-2.0
"""Word error rate, for the STT health canary (ADR-0053, ADR-0054).

``WER = (substitutions + deletions + insertions) / words in the reference``, from a word-level
Levenshtein distance. Both sides are normalised first, so the canary measures what was heard
rather than how a provider likes to spell it:

1. Unicode NFKC (full-width and presentation forms become their plain letters, so Arabic
   presentation forms compare equal to the letters they stand for);
2. casefold;
3. Arabic diacritics (harakat, shadda, sukun: combining marks, category ``Mn``) and the tatweel
   (U+0640) are removed, because ASR output adds or drops them at will;
4. punctuation of every script (Unicode category ``P*``: ``.,!?`` as well as the Arabic comma
   ``،``, semicolon ``؛`` and question mark ``؟``) becomes a space;
5. split on whitespace.

Digits are not spelled out ("4" and "four" differ): a canary phrase is written without digits.
"""

from __future__ import annotations

import unicodedata

TATWEEL = "ـ"


def normalise(text: str) -> list[str]:
    """The words of ``text`` as the WER compares them."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept: list[str] = []
    for char in folded:
        category = unicodedata.category(char)
        if category == "Mn" or char == TATWEEL:
            continue
        kept.append(" " if category.startswith("P") else char)
    return "".join(kept).split()


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    """Word-level Levenshtein distance (substitution, deletion and insertion each cost 1)."""
    previous = list(range(len(hypothesis) + 1))
    for i, ref_word in enumerate(reference, start=1):
        current = [i]
        for j, hyp_word in enumerate(hypothesis, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (ref_word != hyp_word),  # substitution
                )
            )
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """WER of ``hypothesis`` against ``reference``, after :func:`normalise`.

    0.0 is a perfect transcript. It can exceed 1.0 (insertions). An empty reference has a WER
    of 0.0 against an empty hypothesis and 1.0 against anything else, so the value is defined
    for every input.
    """
    ref, hyp = normalise(reference), normalise(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


__all__ = ["edit_distance", "normalise", "word_error_rate"]
