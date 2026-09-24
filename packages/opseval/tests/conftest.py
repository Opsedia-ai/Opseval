# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opseval.catalogue import load_profile
from opseval.config import Settings
from opseval.demo import catalogue_dir
from opseval.models.profile import SutProfile
from opseval.models.scenario import Scenario


@pytest.fixture
def cat_dir() -> Path:
    return catalogue_dir()


@pytest.fixture
def profile(cat_dir: Path) -> SutProfile:
    return load_profile(cat_dir / "profile.yaml")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "reports"))


def make_scenario(**overrides: Any) -> Scenario:
    """A minimal valid scripted scenario for the reference refund agent."""
    base: dict[str, Any] = {
        "id": "t-001",
        "name": "test scenario",
        "goal": "Get a refund to the original card.",
        "persona": {"role": "customer", "language": "en"},
        "conversation": {
            "scripted_turns": ["Order 10482 arrived damaged, refund to my original card please."],
            "stop_at": "supervisor_review",
            "checkpoint_policy": {"proposal_approval": {"action": "approve"}},
        },
        "expectations": {"expected_tools": {"must_call": ["lookup_order", "check_eligibility"]}},
        "scorers": [{"id": "output.terminal_state"}, {"id": "tools.expected_called"}],
        "execution": {"repeats": 1, "turn_timeout_seconds": 5, "attempt_timeout_seconds": 20},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return Scenario.model_validate(base)
