# SPDX-License-Identifier: Apache-2.0
"""Credential values never reach a run record, and nothing else is touched (FR-REC-03)."""

from __future__ import annotations

import pytest

from opseval.redaction import MARKER_RE, redact_text, redact_value, strip_markers

# (kind, text containing it, the exact secret that must not survive)
SECRETS = [
    ("bearer_token", "Authorization: Bearer abcdefghijklmnop1234", "abcdefghijklmnop1234"),
    (
        "jwt",
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
    ),
    ("openai_key", "use sk-proj-abcdefghij1234567890 please", "sk-proj-abcdefghij1234567890"),
    ("anthropic_key", "key sk-ant-api03-abcdefghij1234567890", "sk-ant-api03-abcdefghij1234567890"),
    (
        "github_token",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ),
    ("slack_token", "xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij"),
    ("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    # A real Google API key is exactly 39 characters: AIza + 35.
    ("google_api_key", "AIza" + "S" * 35, "AIza" + "S" * 35),
    ("stripe_key", "sk_live_abcdefghij1234567890", "sk_live_abcdefghij1234567890"),
    ("basic_auth", "Authorization: Basic QWxhZGRpbjpvcGVuc2VzYW1l", "QWxhZGRpbjpvcGVuc2VzYW1l"),
    ("labelled_secret", "api_key=hunter2hunter2", "hunter2hunter2"),
    ("labelled_secret", 'password: "correct-horse-battery"', "correct-horse-battery"),
]


@pytest.mark.parametrize(("kind", "text", "secret"), SECRETS)
def test_a_credential_value_never_survives(kind: str, text: str, secret: str) -> None:
    out, kinds = redact_text(text)
    assert kinds, f"{kind} was not detected in {text!r}"
    assert MARKER_RE.search(out), out
    # The point of the exercise: the secret itself is gone.
    assert secret not in out, f"{kind}: {secret!r} survived in {out!r}"


def test_a_private_key_block_goes_whole() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\nabc\n-----END RSA PRIVATE KEY-----"
    out, kinds = redact_text(f"here it is:\n{pem}\nthanks")
    assert kinds == ["private_key"]
    assert "MIIEpAIBAAKCAQEA" not in out
    assert out.startswith("here it is:") and out.endswith("thanks")


# --- precision: a false positive deletes evidence, which is the worse failure ---------

ORDINARY = [
    "Hi, order 10482 arrived damaged. I'd like my money back to my original card please.",
    "My email is aya.nasser@example.com and my phone is +966 50 123 4567.",
    "Refund 249.99 SAR to card ending 4242, reference REF-2026-00123456.",
    "السلام عليكم، الطلب رقم ٥٥١٢٠ وصلني بايظ، عايز فلوسي ترجع على الكارت",
    "The tracking number is 1Z999AA10123456784 and the invoice is INV-98765432.",
    "I spoke to Sarah yesterday about my subscription password reset email.",
    "Please confirm you have processed this as a full refund, not store credit.",
    "secret santa gifts were ordered on the 14th",  # the word, not an assignment
]


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_conversation_is_left_exactly_alone(text: str) -> None:
    out, kinds = redact_text(text)
    assert out == text and kinds == [], f"redacted real content: {out!r}"


def test_personal_data_is_deliberately_kept() -> None:
    """PII is the subject matter of a support conversation. Removing it would make the
    transcript unfaithful and leave judges unable to grade what the agent did with it."""
    text = "my email is aya@example.com, card ending 4242, order 10482"
    assert redact_text(text) == (text, [])


# --- structured payloads ---------------------------------------------------------------


def test_nested_payloads_are_walked() -> None:
    payload = {
        "headers": {"note": "use Bearer abcdefghijklmnop1234 for this"},
        "items": ["fine", {"deep": "sk-proj-abcdefghij1234567890"}],
        "count": 3,
    }
    out, kinds = redact_value(payload)
    assert sorted(set(kinds)) == ["bearer_token", "openai_key"]
    assert "abcdefghijklmnop1234" not in str(out)
    assert "sk-proj-abcdefghij1234567890" not in str(out)
    assert out["count"] == 3 and out["items"][0] == "fine"  # untouched


def test_non_strings_pass_through_unchanged() -> None:
    for value in (3, 3.5, True, None):
        assert redact_value(value) == (value, [])


# --- the marker must not become a score ------------------------------------------------


def test_markers_can_be_stripped_for_shape_based_scorers() -> None:
    """`output.language_script` counts Arabic against Latin characters. A Latin marker
    dropped into an Arabic reply must not push it under the threshold — that would be the
    harness marking the agent down for the harness's own edit."""
    arabic = "تم استرداد المبلغ على الكارت"
    out, _ = redact_text(f"{arabic} Bearer abcdefghijklmnop1234")
    assert MARKER_RE.search(out)
    assert strip_markers(out).strip() == arabic


def test_redaction_is_idempotent() -> None:
    once, _ = redact_text("Bearer abcdefghijklmnop1234")
    twice, kinds = redact_text(once)
    assert twice == once and kinds == []


# --- the record and the scorers --------------------------------------------------------


def _record_with(text: str) -> object:
    """A minimal record carrying one agent message, built the way the driver does."""
    from datetime import UTC, datetime

    from opseval.models.record import FrameRecord, RedactionInfo, RunRecord, TerminalState
    from opseval.redaction import redact_text

    clean, kinds = redact_text(text)
    return RunRecord(
        run_id="r",
        test_id="t",
        attempt_index=0,
        seed=1,
        target="ws://x",
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        terminal_state=TerminalState.GOAL_REACHED,
        frames=(
            FrameRecord(
                seq=0,
                at=datetime.now(UTC),
                direction="received",
                kind="message",
                payload={},
                text=clean,
            ),
        ),
        redaction=RedactionInfo(applied=True, found=tuple(kinds)),
    )


def test_an_old_record_says_it_was_never_checked_rather_than_clean() -> None:
    """An empty `found` with `applied=False` must not read as "nothing was there"."""
    from opseval.models.record import RedactionInfo

    old = RedactionInfo()
    assert old.applied is False and old.found == ()


def test_the_record_reports_what_was_taken_out() -> None:
    record = _record_with("your key is sk-proj-abcdefghij1234567890")
    assert record.redaction.applied is True
    assert record.redaction.found == ("openai_key",)
    assert "sk-proj-abcdefghij1234567890" not in record.frames[0].text


async def test_a_leaked_credential_still_reaches_the_security_judge() -> None:
    """The failure mode this feature could have introduced: removing the secret so the
    judge cannot see the leak, and the agent passes the check meant to catch it."""
    from opseval.scoring.judges import Judge

    record = _record_with("Sure — the admin token is Bearer abcdefghijklmnop1234")
    transcript = Judge.digest(Judge, record, None)  # type: ignore[arg-type]
    assert "abcdefghijklmnop1234" not in transcript  # the value is gone
    assert "[redacted:bearer_token]" in transcript  # the signal is not
    assert "treat the marker as proof" in transcript  # and the judge is told so


async def test_redaction_cannot_mark_an_arabic_agent_down() -> None:
    """A Latin-script marker inside an Arabic reply must not flip output.language_script."""
    from opseval.models.scenario import ScorerSpec
    from opseval.scoring.deterministic import LanguageScript

    arabic = "تم استرداد المبلغ بالكامل على نفس البطاقة"
    spec = ScorerSpec(id="output.language_script", params={"script": "arabic"})
    clean = await LanguageScript().score(_record_with(arabic), spec, None)  # type: ignore[arg-type]
    leaked = await LanguageScript().score(
        _record_with(f"{arabic} Bearer abcdefghijklmnop1234"), spec, None
    )  # type: ignore[arg-type]
    assert clean.score == 1.0
    assert leaked.score == clean.score, "the harness's own edit changed the agent's score"
