# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

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


@pytest.fixture
async def pg_url() -> AsyncIterator[str]:
    """A fresh, empty Postgres database for one test (server mode, ADR-0047).

    ``OPSEVAL_TEST_DATABASE_URL`` names a server the tests may create databases on (``make
    db-up`` starts one). Without it the test is skipped on a laptop, but in CI it fails: the
    database tests must never be skipped silently where they are the only proof.
    """
    admin = os.environ.get("OPSEVAL_TEST_DATABASE_URL")
    if not admin:
        if os.environ.get("CI"):
            pytest.fail("OPSEVAL_TEST_DATABASE_URL is not set: CI must run the database tests")
        pytest.skip("no OPSEVAL_TEST_DATABASE_URL: start one with `make db-up`")
    import asyncpg

    name = f"opseval_t_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    try:
        yield urlunparse(urlparse(admin)._replace(path=f"/{name}"))
    finally:
        conn = await asyncpg.connect(admin)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()
