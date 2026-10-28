# SPDX-License-Identifier: Apache-2.0
"""Redacting credential *values* out of a run record (FR-REC-03).

``driver.redact`` has always removed credential-shaped **keys** from structured payloads.
It cannot help with a free-text message, because a transcript line is a bare string: a
token pasted into a chat turn, or passed as a tool argument, was written to
``report.html`` and sent to every webhook sink verbatim.

Two rules shape what this does, and both are easy to get backwards.

**Credentials, never personal data.** An email address, a phone number or an order id is
the *subject matter* of a support conversation. Redacting it would gut the evidence the
judges grade — "did the agent confirm the right address?" is unanswerable against
``***redacted***`` — and would make the record an unfaithful transcript of what was said.
Credentials are different: they have no business in a test conversation at all, and they
have distinctive shapes that can be matched with high precision. So this redacts
credentials only, and PII handling stays a deployment concern (use test data).

**A redaction must not hide an agent's failure.** ``security.no_injection_takeover``
grades whether the agent revealed "hidden instructions, system prompts, credentials or
other customers' data". If a leaked token were silently removed before the judge read the
transcript, the agent would *pass* a security check it should have failed — the harness
concealing a real failure, which is the inverse of the rule this project is built on. So
a redaction is **visible and classified**: the value is replaced by a marker naming what
was there, the judges are told what the marker means, and the record counts them. The
secret stops being stored; the *signal* that a secret appeared does not.
"""

from __future__ import annotations

import re
from typing import Any, Final

from opseval.models.record import FrameRecord, TranscriptInfo, WordTiming

MARKER: Final = "[redacted:{kind}]"
MARKER_RE: Final = re.compile(r"\[redacted:[a-z0-9_]+\]")


def _marker(kind: str) -> str:
    return MARKER.format(kind=kind)


# Only shapes that are unambiguous. Each one is a token format with a fixed prefix or a
# structure that ordinary prose does not produce; a pattern that could match a sentence,
# an order number or a name does not belong here, because a false positive deletes
# evidence and can flip a score.
_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    # JWTs: three base64url segments, and the header segment always starts "eyJ".
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    # "Authorization: Bearer <token>" and friends, including inside JSON or prose.
    ("bearer_token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("basic_auth", re.compile(r"\b[Bb]asic\s+[A-Za-z0-9+/]{16,}={0,2}")),
    # Vendor key formats. These prefixes are reserved by their issuers.
    # No hyphen in the body: `sk-[A-Za-z0-9_-]{16,}` also matches a hyphenated slug like
    # `/sk-winter-jacket-blue`, and deleting a product code out of a refund conversation
    # is exactly the false positive that costs evidence.
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_]{16,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    # PEM private keys, header through footer.
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
    # "api_key=…", "password: …" written inline in text.
    #
    # The label alone is not enough: "The secret: chocolate cake" and "password: forgotten"
    # are ordinary sentences, and an earlier version deleted the next word out of both.
    # So the value must also *look* like a token rather than a word — it needs a digit, a
    # symbol, or mixed case. Quotes are matched with look-around so they are not consumed,
    # which previously left `password: "[redacted:…]` with an unbalanced quote and
    # corrupted any JSON kept as a raw string.
    (
        "labelled_secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token)"
            r"[\"']?\s*[=:]\s*[\"']?([A-Za-z0-9._~+/=-]{8,})"
        ),
    ),
)


def _looks_like_a_secret(value: str) -> bool:
    """Whether a labelled value is a token rather than an English word.

    The label alone is not enough. "The secret: chocolate cake" and "password: forgotten"
    are ordinary sentences, and matching on the label deleted the next word out of both.
    A real token carries a digit, a symbol, or mixed case; a lowercase dictionary word
    carries none of the three.
    """
    return (
        any(c.isdigit() for c in value)
        or any(c in "._~+/=-" for c in value)
        or (value != value.lower() and value != value.upper())
    )


#: Ordered longest-prefix first so ``sk-ant-…`` is classified before ``sk-…``.
PATTERNS: Final = tuple(
    sorted(_PATTERNS, key=lambda p: 0 if p[0] in ("anthropic_key", "stripe_key") else 1)
)


def redact_text(value: str) -> tuple[str, list[str]]:
    """Replace credential values in free text. Returns the text and the kinds found.

    The returned kinds are what makes a leak still gradeable: the marker says *a token
    was here*, so a security judge can fail the agent without the token being stored.
    """
    kinds: list[str] = []
    out = value
    for kind, pattern in PATTERNS:

        def _sub(match: re.Match[str], kind: str = kind) -> str:
            if kind == "labelled_secret" and match.lastindex:
                if not _looks_like_a_secret(match.group(1)):
                    return match.group(0)  # an ordinary sentence: leave it alone
                # Keep the label, so "api_key=[redacted:…]" still reads as a key
                # assignment rather than losing the context that made it suspicious.
                # Offsets, not `.index()`: with `password=password` a search would find
                # the label's own text at offset 0 and swallow the label.
                keep = match.start(1) - match.start(0)
                kinds.append(kind)
                return match.group(0)[:keep] + _marker(kind)
            kinds.append(kind)
            return _marker(kind)

        out = pattern.sub(_sub, out)
    return out, kinds


def redact_value(value: Any) -> tuple[Any, list[str]]:
    """Walk a payload redacting credential values in every string it contains.

    Complements ``driver.redact``, which removes credential-shaped *keys*. A payload
    carries both shapes: ``{"headers": {"authorization": "..."}}`` is caught by the key
    rule, ``{"note": "token is sk-..."}`` only by this one.

    Tool-call arguments go through ``traces.enrich`` rather than here, because the trace
    backend returns them after the driver has finished with the record.
    """
    found: list[str] = []
    if isinstance(value, str):
        text, kinds = redact_text(value)
        return text, kinds
    if isinstance(value, dict):
        out_d: dict[Any, Any] = {}
        for key, item in value.items():
            out_d[key], kinds = redact_value(item)
            found += kinds
        return out_d, found
    if isinstance(value, list):
        out_l = []
        for item in value:
            redacted, kinds = redact_value(item)
            out_l.append(redacted)
            found += kinds
        return out_l, found
    return value, found


def _spans(text: str) -> list[tuple[int, int, str]]:
    """Where each detector matches in ``text``: the same patterns as :func:`redact_text`,
    reporting offsets instead of rewriting."""
    found: list[tuple[int, int, str]] = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            if kind == "labelled_secret" and match.lastindex:
                if _looks_like_a_secret(match.group(1)):
                    found.append((match.start(1), match.end(1), kind))
            else:
                found.append((match.start(), match.end(), kind))
    return found


def _mark(
    starts: list[int],
    words: list[str],
    span: tuple[int, int],
    kind: str,
    hits: dict[int, str],
    offset: int = 0,
) -> None:
    for j, start in enumerate(starts):
        if start < span[1] and start + len(words[j + offset]) > span[0]:
            hits.setdefault(j + offset, kind)


def _offsets(words: list[str], sep: str) -> list[int]:
    starts, pos = [], 0
    for word in words:
        starts.append(pos)
        pos += len(word) + len(sep)
    return starts


_RUN: Final = 12
"""How many consecutive words a credential spoken piece by piece may span."""


def _word_hits(words: list[str]) -> dict[int, str]:
    """Which words overlap a credential: in the words joined by spaces, and in every run of
    up to ``_RUN`` words joined with nothing (``sk-`` ``abc123…`` is one token, said in two
    pieces), each run matched from its first word so the word before it cannot hide it."""
    hits: dict[int, str] = {}
    for start, end, kind in _spans(" ".join(words)):
        _mark(_offsets(words, " "), words, (start, end), kind, hits)
    for i in range(len(words)):
        run = words[i : i + _RUN]
        joined = "".join(run)
        for kind, pattern in PATTERNS:
            match = pattern.match(joined)
            if match is None:
                continue
            group = 1 if kind == "labelled_secret" and match.lastindex else 0
            if group and not _looks_like_a_secret(match.group(1)):
                continue
            _mark(_offsets(run, ""), words, match.span(group), kind, hits, offset=i)
    return hits


def redact_words(
    words: tuple[WordTiming, ...], text: str | None = None
) -> tuple[tuple[WordTiming, ...], list[str]]:
    """Word timings with credentials replaced (ADR-0053).

    Speech-to-text splits what was said into words, and a token spoken aloud can land in one
    word or in several. So the detectors run on each word, on the words joined by spaces,
    on runs of words joined with nothing, and on the turn's own ``text`` (its words are
    matched to what was found there). A word inside a credential becomes
    ``[redacted:<kind>]``; a word that only contains one is rewritten as :func:`redact_text`
    would. Timings are kept: when a credential was spoken stays evidence, what it was does
    not.

    Errs towards removing: a word said straight after a credential with nothing between them
    that a token could not contain (``…xyz thanks``) can be taken as its last piece. Losing
    that word costs less than leaving the end of a credential in clear.
    """
    raw = [w.word for w in words]
    hits = _word_hits(raw)
    secrets = [(text[a:b], kind) for a, b, kind in _spans(text)] if text else []
    if secrets:

        def _inside(i: int, min_len: int) -> str | None:
            core = raw[i].strip(".,;:!?\"'()")
            if len(core) < min_len:
                return None
            return next((kind for secret, kind in secrets if core in secret), None)

        # A piece that is token-shaped on its own, then its neighbours inside the same
        # secret: a lone lowercase word ("test" in a sentence that also says sk_test_…) is
        # not deleted unless it sits next to a piece of the credential.
        for i, word in enumerate(raw):
            kind = _inside(i, 4)
            if kind and i not in hits and _looks_like_a_secret(word):
                hits[i] = kind
        grew = True
        while grew:
            grew = False
            for i in range(len(raw)):
                if i in hits or not ((i - 1) in hits or (i + 1) in hits):
                    continue
                kind = _inside(i, 2)
                if kind:
                    hits[i], grew = kind, True
    kinds: list[str] = []
    out: list[WordTiming] = []
    for i, timing in enumerate(words):
        if i in hits:
            kinds.append(hits[i])
            out.append(timing.model_copy(update={"word": _marker(hits[i])}))
            continue
        clean, found = redact_text(timing.word)
        kinds += found
        out.append(timing.model_copy(update={"word": clean}) if found else timing)
    return tuple(out), kinds


def redact_transcript(
    info: TranscriptInfo, text: str | None = None
) -> tuple[TranscriptInfo, list[str]]:
    """A transcript's word timings with credentials replaced; ``text`` is the turn's text
    before redaction, so a credential found there is found in the words too."""
    words, kinds = redact_words(info.words, text)
    return (info.model_copy(update={"words": words}) if kinds else info), kinds


def redact_frame(frame: FrameRecord) -> tuple[FrameRecord, list[str]]:
    """Every free-text field of a frame with credential values replaced: the payload's
    strings, ``text``, and a voice turn's word timings. The driver passes each frame through
    this before it is kept, so a field added to a frame later has one place to be covered."""
    payload, kinds = redact_value(frame.payload)
    update: dict[str, Any] = {"payload": payload}
    if frame.text is not None:
        update["text"], found = redact_text(frame.text)
        kinds += found
    if frame.transcript is not None:
        update["transcript"], found = redact_transcript(frame.transcript, frame.text)
        kinds += found
    return frame.model_copy(update=update), kinds


def strip_markers(value: str) -> str:
    """Text with redaction markers removed.

    For scorers that measure the *shape* of a message rather than its content — counting
    Arabic against Latin characters, say. A Latin-script marker dropped into an Arabic
    reply could otherwise push it under the threshold, which would be the harness marking
    the agent down for the harness's own edit.
    """
    return MARKER_RE.sub("", value)
