# SPDX-License-Identifier: Apache-2.0
"""Test-tenant pool and exclusive leases (FR-RUN-08, FR-SAF-03).

Two attempts sharing a tenant would see each other's orders and refunds, and the mess
would be recorded as agent misbehaviour. So the rule these tests enforce is:

    **when in doubt, do not take the lease.**

Failing to get a tenant is `skipped_precondition` — mildly annoying. Two runs sharing one
produces *false agent failures* — much worse. A lease is therefore stolen only when its
heartbeat is provably older than the threshold, and an unreadable or undated lease counts
as held.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from opseval.errors import ConfigError
from opseval.models.tenant import Tenant, TenantPool, lane_count
from opseval.tenants import TenantLeases, load_pool, token_for


def pool(*ids: str, **kw: Any) -> TenantPool:
    return TenantPool(tenants=[Tenant(id=i, **kw) for i in (ids or ("tenant-a",))])


def leases(tmp_path: Path, p: TenantPool, holder: str = "run-1", **kw: Any) -> TenantLeases:
    return TenantLeases(p, tmp_path / "locks", holder=holder, **kw)


# --- lane arithmetic -------------------------------------------------------------------


class TestLaneCount:
    """FR-RUN-08: lanes = min(accounts, requested, cap)."""

    @pytest.mark.parametrize(
        ("accounts", "requested", "cap", "expected"),
        [
            (4, 4, 8, 4),
            (2, 8, 8, 2),  # accounts is the ceiling
            (8, 2, 8, 2),  # requested is the ceiling
            (8, 8, 3, 3),  # cap is the ceiling
            (1, 1, 1, 1),
            (5, None, 4, 4),  # unspecified means "as many as the pool allows"
            (3, None, 8, 3),
        ],
    )
    def test_the_minimum_wins(
        self, accounts: int, requested: int | None, cap: int, expected: int
    ) -> None:
        assert lane_count(accounts, requested, cap) == expected

    def test_it_never_drops_below_one(self) -> None:
        """Zero lanes would run nothing and report nothing, which is worse than slow."""
        assert lane_count(0, 0, 0) == 1
        assert lane_count(4, -1, 8) == 1

    def test_accounts_cap_a_larger_request(self) -> None:
        """A lane with no tenant to hold defeats the isolation the pool exists for."""
        assert lane_count(2, 100, 100) == 2


# --- the pool file ---------------------------------------------------------------------


class TestPoolFile:
    def write(self, path: Path, data: Any) -> Path:
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_a_valid_pool_loads(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path / "t.yaml",
            {
                "schema": "opseval/v1",
                "tenants": [
                    {"id": "a", "token_env": "A_TOKEN"},
                    {"id": "b", "target": "wss://b.example.com/ws"},
                ],
            },
        )
        loaded = load_pool(path)
        assert loaded.ids == ["a", "b"]

    def test_a_missing_file_is_a_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="cannot read"):
            load_pool(tmp_path / "nope.yaml")

    def test_bad_yaml_is_a_config_error(self, tmp_path: Path) -> None:
        path = tmp_path / "t.yaml"
        path.write_text("tenants: [\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_pool(path)

    def test_an_unknown_field_fails_at_load_time(self, tmp_path: Path) -> None:
        path = self.write(tmp_path / "t.yaml", {"tenants": [{"id": "a", "tokn_env": "X"}]})
        with pytest.raises(ConfigError):
            load_pool(path)

    def test_a_duplicate_id_is_rejected(self, tmp_path: Path) -> None:
        """Two tenants with one id would share a lease file and so share the tenant."""
        path = self.write(tmp_path / "t.yaml", {"tenants": [{"id": "a"}, {"id": "a"}]})
        with pytest.raises(ConfigError, match="duplicate tenant id"):
            load_pool(path)

    def test_an_empty_pool_is_rejected(self, tmp_path: Path) -> None:
        path = self.write(tmp_path / "t.yaml", {"tenants": []})
        with pytest.raises(ConfigError):
            load_pool(path)

    def test_an_id_that_would_escape_the_lock_dir_is_rejected(self) -> None:
        """The id becomes a filename, so `../` must not be possible."""
        for bad in ("../escape", "a/b", "", "a b"):
            with pytest.raises(ValueError):
                Tenant(id=bad)


class TestCredentials:
    def test_a_token_is_read_from_the_named_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TENANT_A_TOKEN", "secret-a")
        assert token_for(Tenant(id="a", token_env="TENANT_A_TOKEN")) == "secret-a"

    def test_a_tenant_without_a_token_env_has_none(self) -> None:
        assert token_for(Tenant(id="a")) is None

    def test_a_declared_but_unset_variable_is_a_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running against a tenant with no credential would fail for a reason that has
        nothing to do with the agent."""
        monkeypatch.delenv("TENANT_B_TOKEN", raising=False)
        with pytest.raises(ConfigError, match="which is not set"):
            token_for(Tenant(id="b", token_env="TENANT_B_TOKEN"))

    def test_a_tenant_cannot_declare_a_token_value(self) -> None:
        """Only the variable *name* is declarable, so a committed pool cannot leak. A
        `token:` key is rejected rather than ignored, because extra="forbid"."""
        assert "token_env" in Tenant.model_fields
        assert "token" not in Tenant.model_fields
        with pytest.raises(ValueError, match="token"):
            Tenant.model_validate({"id": "a", "token": "secret-in-a-committed-file"})


# --- exclusivity -----------------------------------------------------------------------


class TestExclusivity:
    def test_a_lease_is_granted(self, tmp_path: Path) -> None:
        lease = leases(tmp_path, pool("a")).acquire()
        assert lease is not None
        assert lease.tenant.id == "a"
        assert lease.path.is_file()

    def test_a_second_holder_cannot_take_the_same_tenant(self, tmp_path: Path) -> None:
        p = pool("a")
        first = leases(tmp_path, p, holder="run-1").acquire()
        second = leases(tmp_path, p, holder="run-2").acquire()
        assert first is not None
        assert second is None, "two runs must never hold one tenant"

    def test_lanes_get_different_tenants(self, tmp_path: Path) -> None:
        p = pool("a", "b", "c")
        held = [leases(tmp_path, p, holder=f"run-{i}").acquire() for i in range(3)]
        assert sorted(x.tenant.id for x in held if x) == ["a", "b", "c"]

    def test_the_pool_runs_out_rather_than_doubling_up(self, tmp_path: Path) -> None:
        p = pool("a", "b")
        held = [leases(tmp_path, p, holder=f"run-{i}").acquire() for i in range(3)]
        assert [x is not None for x in held] == [True, True, False]

    def test_releasing_frees_the_tenant(self, tmp_path: Path) -> None:
        p = pool("a")
        first = leases(tmp_path, p, holder="run-1").acquire()
        assert first is not None
        first.release()
        assert leases(tmp_path, p, holder="run-2").acquire() is not None

    def test_hold_releases_even_when_the_body_raises(self, tmp_path: Path) -> None:
        p = pool("a")
        with pytest.raises(RuntimeError), leases(tmp_path, p).hold() as lease:
            assert lease is not None
            raise RuntimeError("boom")
        assert leases(tmp_path, p, holder="run-2").acquire() is not None

    def test_release_does_not_free_someone_elses_lease(self, tmp_path: Path) -> None:
        """A stale object releasing a reclaimed lease would hand the tenant to a third."""
        p = pool("a")
        mine = leases(tmp_path, p, holder="run-1").acquire()
        assert mine is not None
        mine.path.write_text(
            json.dumps({"holder": "run-2", "heartbeat_at": time.time()}), encoding="utf-8"
        )
        mine.release()
        assert mine.path.is_file(), "run-2 still holds it"


# --- staleness: the dangerous part -----------------------------------------------------


class TestStaleRecovery:
    def stale_lease(self, tmp_path: Path, age: float, holder: str = "dead-run") -> Path:
        folder = tmp_path / "locks"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "a.lease"
        path.write_text(
            json.dumps({"holder": holder, "heartbeat_at": time.time() - age}), encoding="utf-8"
        )
        return path

    def test_a_provably_stale_lease_is_reclaimed(self, tmp_path: Path) -> None:
        self.stale_lease(tmp_path, age=1000)
        lease = leases(tmp_path, pool("a"), stale_after=300).acquire()
        assert lease is not None
        assert lease.holder == "run-1"

    def test_a_fresh_lease_is_not_reclaimed(self, tmp_path: Path) -> None:
        self.stale_lease(tmp_path, age=5)
        assert leases(tmp_path, pool("a"), stale_after=300).acquire() is None

    def test_just_inside_the_threshold_is_not_reclaimed(self, tmp_path: Path) -> None:
        """The comparison is `<=`, so the boundary resolves toward not stealing. The
        exact boundary is not testable against a wall clock - microseconds pass between
        writing the heartbeat and reading it - so this checks just inside it."""
        self.stale_lease(tmp_path, age=299)
        assert leases(tmp_path, pool("a"), stale_after=300).acquire() is None

    def test_an_unreadable_lease_counts_as_held(self, tmp_path: Path) -> None:
        """Cannot tell means do not take: guessing free is how two runs collide."""
        folder = tmp_path / "locks"
        folder.mkdir(parents=True)
        (folder / "a.lease").write_text("{not json", encoding="utf-8")
        assert leases(tmp_path, pool("a")).acquire() is None

    def test_a_lease_with_no_heartbeat_counts_as_held(self, tmp_path: Path) -> None:
        folder = tmp_path / "locks"
        folder.mkdir(parents=True)
        (folder / "a.lease").write_text(json.dumps({"holder": "x"}), encoding="utf-8")
        assert leases(tmp_path, pool("a")).acquire() is None

    def test_a_lease_with_a_non_numeric_heartbeat_counts_as_held(self, tmp_path: Path) -> None:
        folder = tmp_path / "locks"
        folder.mkdir(parents=True)
        (folder / "a.lease").write_text(
            json.dumps({"holder": "x", "heartbeat_at": "soon"}), encoding="utf-8"
        )
        assert leases(tmp_path, pool("a")).acquire() is None

    def test_an_unfamiliar_holder_alone_is_not_grounds_to_steal(self, tmp_path: Path) -> None:
        self.stale_lease(tmp_path, age=1, holder="somebody-else")
        assert leases(tmp_path, pool("a")).acquire() is None

    def test_a_heartbeat_keeps_a_lease_alive(self, tmp_path: Path) -> None:
        p = pool("a")
        mine = leases(tmp_path, p, holder="run-1").acquire()
        assert mine is not None
        time.sleep(0.05)
        mine.heartbeat()
        assert leases(tmp_path, p, holder="run-2", stale_after=0.04).acquire() is None

    def test_without_a_heartbeat_it_becomes_reclaimable(self, tmp_path: Path) -> None:
        p = pool("a")
        assert leases(tmp_path, p, holder="run-1").acquire() is not None
        time.sleep(0.06)
        assert leases(tmp_path, p, holder="run-2", stale_after=0.05).acquire() is not None

    def test_a_stale_lease_on_one_tenant_does_not_block_another(self, tmp_path: Path) -> None:
        self.stale_lease(tmp_path, age=5)  # 'a' is freshly held
        lease = leases(tmp_path, pool("a", "b"), stale_after=300).acquire()
        assert lease is not None and lease.tenant.id == "b"


class TestLockDirectory:
    def test_it_is_created_on_demand(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "locks"
        TenantLeases(pool("a"), target, holder="run-1").acquire()
        assert (target / "a.lease").is_file()

    def test_an_unwritable_location_is_a_config_error(self, tmp_path: Path) -> None:
        blocker = tmp_path / "locks"
        blocker.write_text("not a directory", encoding="utf-8")
        with pytest.raises((ConfigError, NotADirectoryError, FileExistsError)):
            TenantLeases(pool("a"), blocker, holder="run-1").acquire()

    def test_the_lease_file_is_not_world_readable(self, tmp_path: Path) -> None:
        """It names the holder, not a secret, but there is no reason to publish it."""
        lease = leases(tmp_path, pool("a")).acquire()
        assert lease is not None
        assert oct(os.stat(lease.path).st_mode)[-3:] == "600"


# --- a run that measured nothing must not say "safe to release" ------------------------


class TestNothingMeasuredBlocks:
    """Surfaced by the pool: exhaust it and every scenario skips. The exit code already
    said 2, but `gate.summary` said "Safe to release" and the PR comment headline read
    "🟢 Safe to release" — a claim about an agent nobody looked at."""

    def verdict(self, status: Any, crit: Any = None) -> Any:
        from opseval.models.results import TestVerdict, VerdictStatus
        from opseval.models.scenario import Criticality

        del VerdictStatus
        return TestVerdict(
            test_id="t-001",
            name="s",
            criticality=crit or Criticality.CRITICAL,
            status=status,
        )

    def test_all_skipped_blocks(self) -> None:
        from opseval.models.results import VerdictStatus
        from opseval.verdicts import exit_code, release_gate

        tests = [self.verdict(VerdictStatus.SKIPPED_PRECONDITION) for _ in range(3)]
        gate = release_gate(tests)
        assert gate.status == "block"
        assert "nothing was measured" in gate.summary
        assert exit_code(tests) == 2

    def test_all_parked_blocks(self) -> None:
        from opseval.models.results import VerdictStatus
        from opseval.verdicts import release_gate

        gate = release_gate([self.verdict(VerdictStatus.BLOCKED) for _ in range(2)])
        assert gate.status == "block"

    def test_one_measured_pass_is_still_a_pass(self) -> None:
        """Something was looked at, so the sentence is not vacuous."""
        from opseval.models.results import VerdictStatus
        from opseval.verdicts import release_gate

        gate = release_gate(
            [
                self.verdict(VerdictStatus.PASSED),
                self.verdict(VerdictStatus.SKIPPED_PRECONDITION),
            ]
        )
        assert gate.status == "pass"

    def test_a_measured_failure_still_blocks_for_its_own_reason(self) -> None:
        from opseval.models.results import VerdictStatus
        from opseval.verdicts import release_gate

        gate = release_gate([self.verdict(VerdictStatus.FAILED)])
        assert gate.status == "block"
        assert "nothing was measured" not in gate.summary

    def test_the_pr_headline_agrees_with_the_exit_code(self) -> None:
        from datetime import UTC, datetime

        from opseval.models.results import SuiteResult, VerdictStatus
        from opseval.reporting.markdown import markdown_summary
        from opseval.verdicts import exit_code, release_gate

        now = datetime(2026, 10, 5, tzinfo=UTC)
        tests = [self.verdict(VerdictStatus.SKIPPED_PRECONDITION) for _ in range(3)]
        result = SuiteResult(
            opseval_version="0",
            run_id="r",
            suite="smoke",
            target="x",
            profile="p",
            started_at=now,
            ended_at=now,
            tests=tests,
            gate=release_gate(tests),
            exit_code=exit_code(tests),
        )
        summary = markdown_summary(result)
        assert "Release blocked" in summary
        assert "Safe to release" not in summary


class TestTheScaffoldedPool:
    """``opseval init`` ships an example pool, so it has to be a loadable one."""

    def test_the_example_pool_loads_and_carries_no_secrets(self, tmp_path: Path) -> None:
        from opseval.scaffold import scaffold
        from opseval.tenants import load_pool

        scaffold(tmp_path)
        file = tmp_path / "tenants.yaml"
        pool = load_pool(file)
        assert pool.ids == ["tenant-a", "tenant-b"]
        # Every tenant names a variable; none carries a token. A committed pool file with
        # a credential in it is the obvious way to leak one.
        assert all(t.token_env for t in pool.tenants)
        assert "$schema=https://opsedia.ai/schemas/v1/tenants.json" in file.read_text("utf-8")

    def test_the_example_declares_variables_the_env_sample_mentions(self, tmp_path: Path) -> None:
        from opseval.scaffold import scaffold
        from opseval.tenants import load_pool

        scaffold(tmp_path)
        env = (tmp_path / ".env.example").read_text("utf-8")
        for tenant in load_pool(tmp_path / "tenants.yaml").tenants:
            assert tenant.token_env and tenant.token_env in env
