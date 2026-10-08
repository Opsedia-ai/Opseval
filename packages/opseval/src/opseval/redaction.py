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


def strip_markers(value: str) -> str:
    """Text with redaction markers removed.

    For scorers that measure the *shape* of a message rather than its content — counting
    Arabic against Latin characters, say. A Latin-script marker dropped into an Arabic
    reply could otherwise push it under the threshold, which would be the harness marking
    the agent down for the harness's own edit.
    """
    return MARKER_RE.sub("", value)
