# SPDX-License-Identifier: Apache-2.0
"""The docs site's reference pages are generated from the code (ADR-0039)."""

from __future__ import annotations

from opseval.config import Settings
from opseval.scoring import REGISTRY


def test_every_setting_says_what_it_does() -> None:
    """The settings reference is read from these docstrings: a blank one is a blank row."""
    missing = [name for name, field in Settings.model_fields.items() if not field.description]
    assert missing == []


def test_every_scorer_says_what_it_checks() -> None:
    assert [sid for sid, cls in REGISTRY.items() if not cls.description.strip()] == []
