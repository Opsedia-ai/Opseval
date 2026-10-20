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

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from opseval.catalogue import load_profile
from opseval.config import AllowedHostError, Settings
from opseval.demo.refund_agent import run_agent
from opseval.errors import ConfigError
from opseval.models.tenant import Tenant, TenantPool, lane_count
from opseval.runner import RunOptions, build_transport_factory, launch
from opseval.tenants import TenantLeases, _identity, _read, load_pool, token_for


@pytest.fixture(autouse=True)
def _fast_stale(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Most lock mechanics are tested with sub-second stale windows to stay fast. The real
    floor (three heartbeats) has its own test, which keeps it."""
    if "keeps_the_real_floor" not in request.node.name:
        monkeypatch.setattr("opseval.tenants.MIN_STALE_AFTER", 0.0)


def pool(*ids: str, **kw: Any) -> TenantPool:
    return TenantPool(tenants=[Tenant(id=i, **kw) for i in (ids or ("tenant-a",))])


def leases(tmp_path: Path, p: TenantPool, holder: str = "run-1", **kw: Any) -> TenantLeases:
    kw.setdefault("stale_after", 300.0)
    return TenantLeases(p, tmp_path / "locks", holder=holder, **kw)


def lease_file(tmp_path: Path, tenant_id: str = "x") -> Path:
    """The path `leases()` would use for this tenant, with its directory created."""
    directory = tmp_path / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{tenant_id}.lease"


def stale(path: Path, *, holder: str = "dead", age: float) -> Path:
    path.write_text(
        json.dumps({"holder": holder, "heartbeat_at": time.time() - age}), encoding="utf-8"
    )
    return path


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
        assert token_for(Tenant(id="a", token_env="TENANT_A_TOKEN"), Settings()) == "secret-a"

    def test_a_token_set_in_dot_env_is_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`opseval init` tells the operator to put tenant tokens in `.env`. A `.env`
        value reaches Settings but is **never exported to os.environ**, so reading the
        process environment directly would report every one of them unset."""
        monkeypatch.delenv("TENANT_A_TOKEN", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("TENANT_A_TOKEN=from-dot-env\n", encoding="utf-8")
        assert token_for(Tenant(id="a", token_env="TENANT_A_TOKEN"), Settings()) == "from-dot-env"

    def test_the_real_environment_wins_over_dot_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("TENANT_A_TOKEN=stale\n", encoding="utf-8")
        monkeypatch.setenv("TENANT_A_TOKEN", "live")
        assert token_for(Tenant(id="a", token_env="TENANT_A_TOKEN"), Settings()) == "live"

    def test_a_tenant_without_a_token_env_has_none(self) -> None:
        assert token_for(Tenant(id="a"), Settings()) is None

    def test_a_declared_but_unset_variable_is_a_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running against a tenant with no credential would fail for a reason that has
        nothing to do with the agent."""
        monkeypatch.delenv("TENANT_B_TOKEN", raising=False)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ConfigError, match="which is not set"):
            token_for(Tenant(id="b", token_env="TENANT_B_TOKEN"), Settings())

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
        TenantLeases(pool("a"), target, holder="run-1", stale_after=300).acquire()
        assert (target / "a.lease").is_file()

    def test_an_unwritable_location_is_a_config_error(self, tmp_path: Path) -> None:
        """Only ConfigError, because only ConfigError exits 3. A raw OSError escapes
        `cli._execute`, which catches ConfigError alone, and Typer exits **1** - a
        harness problem reported as the agent failing."""
        blocker = tmp_path / "locks"
        blocker.write_text("not a directory", encoding="utf-8")
        with pytest.raises(ConfigError, match="cannot create tenant leases"):
            TenantLeases(
                pool("a"), blocker / "under-a-file", holder="run-1", stale_after=300
            ).acquire()

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
        assert "no scenario was judged" in gate.summary  # the rule main enforces (ADR-0030)
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
        assert "no scenario was judged" not in gate.summary

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


# --- regressions found in review of the original pool PR -------------------------------


class TestTheStealIsAtomic:
    """Reclaiming a stale lease by unlink-then-create lets two processes both win."""

    def test_two_racers_cannot_both_steal_the_same_stale_lease(self, tmp_path: Path) -> None:
        a = leases(tmp_path, pool("x"), holder="run-a")
        b = leases(tmp_path, pool("x"), holder="run-b")
        stale(lease_file(tmp_path), holder="dead", age=10_000)

        # Both have already decided the lease is stale: the interleaving is the one where
        # each checks before either acts, which is what makes unlink-then-create unsafe.
        assert a._held_by_other(lease_file(tmp_path)) is False
        assert b._held_by_other(lease_file(tmp_path)) is False

        judged = _identity(lease_file(tmp_path))
        assert judged is not None
        first = a._steal(lease_file(tmp_path), judged)
        second = b._steal(lease_file(tmp_path), judged)
        # Exactly one process gets to clear it. os.rename is the atomic step.
        assert [first, second] == [True, False]

    def test_a_stale_lease_is_still_reclaimable(self, tmp_path: Path) -> None:
        stale(lease_file(tmp_path), holder="dead", age=10_000)
        lease = leases(tmp_path, pool("x")).acquire()
        assert lease is not None and lease.tenant.id == "x"

    def test_stealing_leaves_no_debris(self, tmp_path: Path) -> None:
        stale(lease_file(tmp_path), holder="dead", age=10_000)
        leases(tmp_path, pool("x")).acquire()
        assert sorted(p.name for p in (tmp_path / "locks").iterdir()) == ["x.lease"]


class TestACrashedWriteDoesNotRetireATenant:
    def test_an_empty_lease_ages_out_by_its_own_mtime(self, tmp_path: Path) -> None:
        """A kill between creating the lease and writing it leaves an empty file. With
        no timestamp to age, "unreadable means held" would retire that tenant forever."""
        path = lease_file(tmp_path)
        path.write_text("", encoding="utf-8")
        os.utime(path, (time.time() - 10_000, time.time() - 10_000))
        lease = leases(tmp_path, pool("x")).acquire()
        assert lease is not None

    def test_a_freshly_emptied_lease_is_still_held(self, tmp_path: Path) -> None:
        """The mtime fallback must not become a licence to steal a live lease."""
        lease_file(tmp_path).write_text("", encoding="utf-8")
        assert leases(tmp_path, pool("x")).acquire() is None

    def test_the_heartbeat_is_never_a_partial_file(self, tmp_path: Path) -> None:
        """Written to a temp file and replaced, so an interrupted write cannot truncate
        the lease to nothing."""
        lease = leases(tmp_path, pool("x")).acquire()
        assert lease is not None
        lease.heartbeat()
        assert json.loads(lease.path.read_text("utf-8"))["holder"] == lease.holder
        assert [p.name for p in (tmp_path / "locks").iterdir()] == ["x.lease"]


class TestAGarbageHeartbeatIsNotAnInvitation:
    @pytest.mark.parametrize("value", ["NaN", "true", '"2026-01-01"', "null", "Infinity"])
    def test_a_heartbeat_that_is_not_a_timestamp_leaves_the_lease_held(
        self, tmp_path: Path, value: str
    ) -> None:
        """`bool` is an `int` and `json` parses bare `NaN`: True ages to 1970 and every
        NaN comparison is False, so both would read as stale and get a live tenant
        stolen. A fresh file with an unusable heartbeat stays held."""
        lease_file(tmp_path).write_text(
            f'{{"holder": "other", "heartbeat_at": {value}}}', encoding="utf-8"
        )
        assert leases(tmp_path, pool("x")).acquire() is None


class TestTheLockDirIsNotRelativeToTheCwd:
    def test_two_checkouts_of_one_pool_share_a_lock_directory(self) -> None:
        """A path relative to the working directory would let two CI jobs checking the
        same repository out twice both lease the same account."""
        from opseval.tenants import default_lock_dir

        here, there = default_lock_dir(pool("a", "b")), default_lock_dir(pool("b", "a"))
        assert here == there
        assert here.is_absolute()

    def test_different_pools_do_not_collide(self) -> None:
        from opseval.tenants import default_lock_dir

        assert default_lock_dir(pool("a")) != default_lock_dir(pool("a", "b"))


class TestTheHeartbeatIsActuallyPeriodic:
    async def test_a_long_attempt_keeps_its_tenant(self, tmp_path: Path) -> None:
        """One refresh per finished attempt is not enough: a scenario that runs longer
        than `stale_after` never reaches it, and has its tenant taken mid-conversation."""
        from opseval.tenants import refreshing

        mine = leases(tmp_path, pool("x"), holder="run-a", stale_after=0.05)
        lease = mine.acquire()
        assert lease is not None
        rival = leases(tmp_path, pool("x"), holder="run-b", stale_after=0.05)
        async with refreshing(lease, every=0.01):
            await asyncio.sleep(0.2)  # Several multiples of stale_after.
            assert rival.acquire() is None, "a live holder was mistaken for a dead one"

    async def test_the_refresher_stops_with_the_block(self, tmp_path: Path) -> None:
        """A task left running past the lease would keep a released tenant looking busy."""
        from opseval.tenants import refreshing

        lease = leases(tmp_path, pool("x")).acquire()
        assert lease is not None
        async with refreshing(lease, every=0.01):
            await asyncio.sleep(0.05)
        settled = json.loads(lease.path.read_text("utf-8"))["heartbeat_at"]
        await asyncio.sleep(0.05)  # Several refresh intervals with nobody refreshing.
        assert json.loads(lease.path.read_text("utf-8"))["heartbeat_at"] == settled

    async def test_a_slow_heartbeat_never_lands_after_the_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancelling a task does not stop the thread writing the heartbeat: the block must
        wait for it, or the write lands after release() and re-creates the lease."""
        import threading
        import time

        from opseval import tenants
        from opseval.tenants import refreshing

        lease = leases(tmp_path, pool("x")).acquire()
        assert lease is not None
        started = threading.Event()
        real = tenants._write_lease

        def slow(path: Path, holder: str, lease_id: str) -> None:
            started.set()
            time.sleep(0.2)
            real(path, holder, lease_id)

        monkeypatch.setattr(tenants, "_write_lease", slow)
        async with refreshing(lease, every=0.01):
            await asyncio.to_thread(started.wait, 2)
        lease.release()
        await asyncio.sleep(0.3)
        assert not lease.path.exists(), "a late heartbeat re-created the released lease"

    def test_a_heartbeat_never_recreates_a_released_lease(self, tmp_path: Path) -> None:
        lease = leases(tmp_path, pool("x")).acquire()
        assert lease is not None
        lease.release()
        lease.heartbeat()
        assert not lease.path.exists()

    async def test_no_lease_is_not_an_error(self) -> None:
        from opseval.tenants import refreshing

        async with refreshing(None):
            pass


class TestWaitingForATenant:
    async def test_a_lease_freed_a_moment_later_is_used(self, tmp_path: Path) -> None:
        """A leftover lease from a killed run should not skip a scenario that could have
        run a second later - with every scenario skipped the gate blocks the release."""
        holder = leases(tmp_path, pool("x"), holder="run-a")
        held = holder.acquire()
        assert held is not None

        async def free_it_soon() -> None:
            await asyncio.sleep(0.05)
            held.release()

        task = asyncio.create_task(free_it_soon())
        got = await leases(tmp_path, pool("x"), holder="run-b").acquire_waiting(
            timeout=5.0, poll=0.01
        )
        await task
        assert got is not None

    async def test_waiting_still_gives_up(self, tmp_path: Path) -> None:
        """Bounded: it never relaxes the lock, it only absorbs transient contention."""
        leases(tmp_path, pool("x"), holder="run-a").acquire()
        got = await leases(tmp_path, pool("x"), holder="run-b").acquire_waiting(
            timeout=0.05, poll=0.01
        )
        assert got is None


# --- what launch() refuses before the first frame --------------------------------------


def write_pool(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tenants.yaml"
    path.write_text(body, encoding="utf-8")
    return path


class TestLaunchChecksThePoolUpFront:
    """ "Authoring mistakes must fail at load time." Discovering an unset tenant token
    halfway through kills the run after records are already on disk - and with one lane
    the pool's later tenants may never be drawn, so it would not be discovered at all."""

    def run_options(self, cat_dir: Path) -> RunOptions:
        return RunOptions(target="ws://127.0.0.1:9", profile=cat_dir / "profile.yaml", repeats=1)

    async def test_an_unset_tenant_token_fails_before_anything_runs(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("T_B", raising=False)
        file = write_pool(
            tmp_path,
            "tenants:\n  - id: a\n    token_env: T_A\n  - id: b\n    token_env: T_B\n",
        )
        monkeypatch.setenv("T_A", "token-a")
        settings = Settings(
            allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "r"), tenants_file=str(file)
        )  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="T_B.*not set"):
            await launch(cat_dir / "suites" / "smoke.yaml", self.run_options(cat_dir), settings)
        # Nothing was started: no run folder, so no records to explain away.
        assert not (tmp_path / "r").exists()

    async def test_a_tenant_target_off_the_allowlist_fails_before_anything_runs(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        file = write_pool(
            tmp_path, "tenants:\n  - id: a\n    target: wss://elsewhere.example.com/ws\n"
        )
        settings = Settings(
            allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "r"), tenants_file=str(file)
        )  # type: ignore[arg-type]
        with pytest.raises(AllowedHostError):
            await launch(cat_dir / "suites" / "smoke.yaml", self.run_options(cat_dir), settings)

    async def test_lanes_without_a_pool_is_refused(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rather than silently running sequentially and taking four times as long."""
        monkeypatch.chdir(tmp_path)
        settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "r"), lanes=4)  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="needs --tenants"):
            await launch(cat_dir / "suites" / "smoke.yaml", self.run_options(cat_dir), settings)

    async def test_one_lane_without_a_pool_is_fine(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--lanes 1` asks for nothing a pool is needed for."""
        monkeypatch.chdir(tmp_path)
        settings = Settings(allowed_hosts="127.0.0.1", reports_dir=str(tmp_path / "r"), lanes=1)  # type: ignore[arg-type]
        async with run_agent() as url:
            result = await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1),
                settings,
            )
        assert result.exit_code == 0


class TestMintedTokensCannotCarryATenant:
    """The quiet failure: a profile that mints its own tokens knows nothing about
    tenants, so every lane would authenticate as the *same* account while believing it
    was isolated - the shared-state corruption the pool exists to prevent, recorded as
    agent misbehaviour."""

    async def test_a_minting_profile_with_a_credential_free_pool_is_refused(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        profile = yaml.safe_load((cat_dir / "profile.yaml").read_text("utf-8"))
        profile["token"] = {"ttl_seconds": 300, "claims": {"sub": "fixed"}}
        (tmp_path / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
        file = write_pool(tmp_path, "tenants:\n  - id: a\n  - id: b\n")
        settings = Settings(
            allowed_hosts="127.0.0.1",
            reports_dir=str(tmp_path / "r"),
            tenants_file=str(file),
            token_signing_key="k" * 32,
        )  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="cannot tell one tenant from another"):
            await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target="ws://127.0.0.1:9", profile=tmp_path / "profile.yaml", repeats=1),
                settings,
            )

    async def test_a_tenant_token_beats_a_minted_one(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a token_env each tenant authenticates as itself, so the combination is
        allowed - the tenant's own credential wins over both minting and the static one."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("T_A", "token-a")
        monkeypatch.setenv("T_B", "token-b")
        profile = yaml.safe_load((cat_dir / "profile.yaml").read_text("utf-8"))
        profile["token"] = {"ttl_seconds": 300}
        (tmp_path / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
        file = write_pool(
            tmp_path, "tenants:\n  - id: a\n    token_env: T_A\n  - id: b\n    token_env: T_B\n"
        )
        settings = Settings(
            allowed_hosts="127.0.0.1",
            reports_dir=str(tmp_path / "r"),
            tenants_file=str(file),
            tenant_lock_dir=str(tmp_path / "locks"),
            static_token="static-should-lose",
            token_signing_key="k" * 32,
        )  # type: ignore[arg-type]
        factory = build_transport_factory(
            RunOptions(target="ws://127.0.0.1:9", profile=tmp_path / "profile.yaml"), settings
        )
        loaded = load_profile(tmp_path / "profile.yaml")
        seen = {
            t.id: factory(loaded, t).tokens.current()  # type: ignore[union-attr]
            for t in load_pool(file).tenants
        }
        assert seen["a"] != seen["b"]
        assert "token-a" in seen["a"] and "token-b" in seen["b"]


class TestLanesAndRunControl:
    """Lanes on today's runner (ADR-0035): a stop policy stops new scenarios from starting,
    never ones already running, and what it left out is skipped, never dropped."""

    async def test_a_stop_in_one_lane_keeps_the_next_scenario_from_starting(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.config import Settings
        from opseval.demo.refund_agent import run_agent
        from opseval.models.results import VerdictStatus
        from opseval.models.stop import StopPolicy
        from opseval.runner import RunOptions, launch

        monkeypatch.setenv("T_A", "a")
        monkeypatch.setenv("T_B", "b")
        file = write_pool(
            tmp_path, "tenants:\n  - id: a\n    token_env: T_A\n  - id: b\n    token_env: T_B\n"
        )
        settings = Settings(
            reports_dir=str(tmp_path / "r"),
            allowed_hosts="127.0.0.1",
            tenants_file=str(file),
            lanes=2,
            tenant_lock_dir=str(tmp_path / "locks"),
        )
        async with run_agent(bug="skip-approval") as url:
            result = await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(
                    target=url,
                    profile=cat_dir / "profile.yaml",
                    repeats=1,
                    stop=StopPolicy(stop_on_critical_failure=True),
                ),
                settings,
            )
        kinds = {t.test_id: (t.status, t.skip_kind) for t in result.tests}
        # rf-001 and rf-002 (both critical) ran side by side and failed; rf-003 never started.
        assert kinds["rf-001"][0] is VerdictStatus.FAILED
        assert kinds["rf-002"][0] is VerdictStatus.FAILED
        assert kinds["rf-003"] == (VerdictStatus.SKIPPED_PRECONDITION, "stopped")
        assert [t.test_id for t in result.tests] == ["rf-001", "rf-002", "rf-003"]
        assert result.stopped is not None and result.exit_code == 1
        assert list((tmp_path / "locks").glob("*")) == [], "every lease was released"


class TestReviewOf62:
    """The holes found in review: each could let two runs share one tenant, or let a run
    misreport what happened."""

    def test_a_steal_never_moves_a_fresh_lease_away(self, tmp_path: Path) -> None:
        path = stale(lease_file(tmp_path, "x"), age=10_000)
        judged = _identity(path)
        assert judged is not None
        # Between our staleness check and our rename, another reclaimer took the tenant.
        fresh = leases(tmp_path, pool("x"), holder="run-b")
        path.unlink()
        theirs = fresh.acquire()
        assert theirs is not None
        mine = leases(tmp_path, pool("x"), holder="run-a")
        assert mine._steal(path, judged) is False
        assert _read(path)["lease_id"] == theirs.lease_id, "their lease was put back"
        assert list(path.parent.glob("*.stale.*")) == []

    def test_a_heartbeat_never_stamps_over_another_runs_lease(self, tmp_path: Path) -> None:
        a = leases(tmp_path, pool("x"), holder="run-a").acquire()
        assert a is not None
        a.path.unlink()  # reclaimed (as stale) ...
        b = leases(tmp_path, pool("x"), holder="run-b").acquire()  # ... and taken by run b
        assert b is not None
        a.heartbeat()
        assert a.lost is True
        assert _read(a.path)["lease_id"] == b.lease_id, "b's lease is untouched"
        a.release()
        assert a.path.exists(), "a cannot release b's lease"

    def test_keeps_the_real_floor_on_stale_after(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="at least 90s"):
            leases(tmp_path, pool("x"), stale_after=20)

    def test_progress_shows_every_running_scenario(self, tmp_path: Path) -> None:
        from opseval.models.results import TestVerdict, VerdictStatus
        from opseval.models.scenario import Criticality
        from opseval.progress import ProgressLog
        from opseval.storage import FileStore

        store = FileStore(tmp_path)
        log = ProgressLog(store, "r", ["a", "b"])
        log.started("a", 1)
        log.started("b", 1)
        log.ended(
            TestVerdict(
                test_id="b", name="b", criticality=Criticality.NORMAL, status=VerdictStatus.PASSED
            )
        )
        progress = store.load_progress("r")
        assert progress is not None and progress.current == "a", "a is still running"

    def test_secret_named_reads_the_env_file_this_settings_was_built_with(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.config import Settings

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("T_X", raising=False)
        (tmp_path / ".env").write_text("T_X=from-default\n", encoding="utf-8")
        (tmp_path / "ci.env").write_text("T_X=from-ci\n", encoding="utf-8")
        assert Settings(_env_file="ci.env").secret_named("T_X") == "from-ci"  # type: ignore[call-arg]
        assert Settings(_env_file=None).secret_named("T_X") is None  # type: ignore[call-arg]
        assert Settings().secret_named("T_X") == "from-default"


class TestTheRunnerWithAPoolAfterReview:
    def settings(self, tmp_path: Path, file: Path, **kw: Any) -> Any:
        from opseval.config import Settings

        return Settings(
            reports_dir=str(tmp_path / "r"),
            allowed_hosts="127.0.0.1",
            tenants_file=str(file),
            tenant_lock_dir=str(tmp_path / "locks"),
            **kw,
        )

    @pytest.mark.parametrize(
        "body",
        [
            "tenants:\n  - id: a\n  - id: b\n",
            "tenants:\n  - id: a\n    token_env: T\n  - id: b\n    token_env: T\n",
        ],
        ids=["no-identity", "same-identity"],
    )
    async def test_tenants_that_are_one_account_are_refused(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
    ) -> None:
        from opseval.runner import RunOptions, launch

        monkeypatch.setenv("T", "t")
        file = write_pool(tmp_path, body)
        opts = RunOptions(target="ws://127.0.0.1:9", profile=cat_dir / "profile.yaml", repeats=1)
        with pytest.raises(ConfigError, match="not isolated|one account under two names"):
            await launch(cat_dir / "suites" / "smoke.yaml", opts, self.settings(tmp_path, file))

    async def test_an_unreachable_tenant_target_stops_the_run(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.demo.refund_agent import run_agent
        from opseval.runner import RunOptions, launch

        monkeypatch.setenv("T_A", "a")
        monkeypatch.setenv("T_B", "b")
        file = write_pool(
            tmp_path,
            "tenants:\n  - id: a\n    token_env: T_A\n"
            "  - id: b\n    token_env: T_B\n    target: ws://127.0.0.1:9\n",
        )
        async with run_agent() as url:
            opts = RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1)
            with pytest.raises(
                ConfigError, match="tenant target ws://127.0.0.1:9 is not reachable"
            ):
                await launch(cat_dir / "suites" / "smoke.yaml", opts, self.settings(tmp_path, file))

    async def test_one_failing_lane_stops_the_others_and_frees_every_lease(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.demo.refund_agent import run_agent
        from opseval.runner import RunOptions, launch
        from opseval.storage import FileStore

        monkeypatch.setenv("T_A", "a")
        monkeypatch.setenv("T_B", "b")
        file = write_pool(
            tmp_path, "tenants:\n  - id: a\n    token_env: T_A\n  - id: b\n    token_env: T_B\n"
        )
        real = FileStore.save_record

        def broken_disk(self: FileStore, record: Any) -> Path:
            if record.test_id == "rf-001":
                raise OSError(28, "No space left on device")
            return real(self, record)

        monkeypatch.setattr(FileStore, "save_record", broken_disk)
        async with run_agent() as url:
            opts = RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=1)
            with pytest.raises(OSError, match="No space left"):
                await launch(
                    cat_dir / "suites" / "smoke.yaml", opts, self.settings(tmp_path, file, lanes=2)
                )
        assert list((tmp_path / "locks").glob("*.lease")) == [], "every lease was released"

    async def test_the_lease_is_refreshed_while_scoring_too(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not only during the conversation: scoring between attempts can take minutes."""
        from opseval.demo.refund_agent import run_agent
        from opseval.runner import RunOptions, launch
        from opseval.scoring import ScoringEngine
        from opseval.tenants import Lease

        monkeypatch.setattr("opseval.tenants.HEARTBEAT_SECONDS", 0.02)
        monkeypatch.setenv("T_A", "a")
        file = write_pool(tmp_path, "tenants:\n  - id: a\n    token_env: T_A\n")
        scoring = {"now": False, "beats": 0}
        real_score = ScoringEngine.score
        real_beat = Lease.heartbeat

        async def slow_score(self: Any, *args: Any, **kw: Any) -> Any:
            scoring["now"] = True
            await asyncio.sleep(0.2)
            scoring["now"] = False
            return await real_score(self, *args, **kw)

        def counted(self: Lease) -> None:
            if scoring["now"]:
                scoring["beats"] += 1
            real_beat(self)

        monkeypatch.setattr(ScoringEngine, "score", slow_score)
        monkeypatch.setattr(Lease, "heartbeat", counted)
        async with run_agent() as url:
            opts = RunOptions(
                target=url, profile=cat_dir / "profile.yaml", repeats=1, only_ids=["rf-001"]
            )
            await launch(cat_dir / "suites" / "smoke.yaml", opts, self.settings(tmp_path, file))
        assert scoring["beats"] > 0


class TestReviewOf63:
    """The second review: each hole could let a lease be shared, or a lost one be judged."""

    def test_an_empty_fresh_lease_is_not_the_empty_stale_one(self, tmp_path: Path) -> None:
        """Two unreadable files used to compare equal (both None), so a steal could move a
        lease another reclaimer had just created and not yet written."""
        path = lease_file(tmp_path, "x")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        os.utime(path, (time.time() - 10_000, time.time() - 10_000))
        judged = _identity(path)
        assert judged is not None
        path.unlink()
        path.write_text("", encoding="utf-8")  # someone else's, created a moment ago
        mine = leases(tmp_path, pool("x"), holder="run-a")
        assert mine._steal(path, judged) is False
        assert path.exists(), "their lease was put back"
        assert list(path.parent.glob("*.stale.*")) == []

    def test_a_failed_restore_never_deletes_a_live_lease(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = stale(lease_file(tmp_path, "x"), age=10_000)
        judged = _identity(path)
        assert judged is not None
        os.utime(path, None)  # it changed after we judged it

        def no_link(src: Any, dst: Any) -> None:
            raise OSError("link failed")

        monkeypatch.setattr(os, "link", no_link)
        mine = leases(tmp_path, pool("x"), holder="run-a")
        assert mine._steal(path, judged) is False
        assert len(list(path.parent.glob("*.stale.*"))) == 1, "kept, not deleted"

    def test_a_lease_that_vanished_mid_hold_is_lost(self, tmp_path: Path) -> None:
        a = leases(tmp_path, pool("x"), holder="run-a").acquire()
        assert a is not None
        a.path.unlink()  # stolen, and not (yet) re-created
        a.heartbeat()
        assert a.lost is True
        assert not a.path.exists(), "a heartbeat never re-creates a lease"

    def test_a_released_lease_is_not_lost(self, tmp_path: Path) -> None:
        a = leases(tmp_path, pool("x"), holder="run-a").acquire()
        assert a is not None
        a.release()
        a.heartbeat()
        assert (a.lost, a.check()) == (False, True)
        assert not a.path.exists()

    def test_check_sees_a_steal_the_heartbeat_has_not(self, tmp_path: Path) -> None:
        a = leases(tmp_path, pool("x"), holder="run-a").acquire()
        assert a is not None
        assert a.check() is True
        a.path.unlink()
        leases(tmp_path, pool("x"), holder="run-b").acquire()
        assert a.check() is False and a.lost is True

    def test_rescore_applies_the_lost_lease_rule(self) -> None:
        """The rule lives in attempt_verdict, so a re-scored record is not judged either."""
        from datetime import UTC, datetime

        from opseval.models.record import RunRecord, TerminalState
        from opseval.models.results import Axis, ScoreResult, ScoreStatus
        from opseval.verdicts import TENANT_LEASE_LOST, attempt_verdict

        now = datetime(2026, 10, 20, tzinfo=UTC)
        rec = RunRecord(
            run_id="r",
            test_id="t",
            attempt_index=0,
            seed=1,
            target="ws://127.0.0.1:1",
            started_at=now,
            ended_at=now,
            terminal_state=TerminalState.STOPPED_AT_CHECKPOINT,
            notes=(TENANT_LEASE_LOST,),
        )
        passing = ScoreResult(
            scorer_id="s", axis=Axis.OUTPUT, status=ScoreStatus.PASSED, score=1.0, threshold=1.0
        )
        verdict = attempt_verdict(rec, [passing], "a.json")
        assert verdict.passed is None
        assert "tenant lease lost" in (verdict.invalid_reason or "")

    def test_targets_are_published_without_credentials(self) -> None:
        from opseval.runner import _shown

        assert _shown("wss://user:pw@agent.example:8443/ws?token=abc") == (
            "wss://agent.example:8443/ws"
        )
        assert _shown("http://127.0.0.1:9000") == "http://127.0.0.1:9000"

    def test_pool_targets_fall_back_to_the_default(self) -> None:
        from opseval.runner import _tenant_targets

        p = TenantPool.model_validate(
            {
                "tenants": [
                    {"id": "a", "token_env": "A"},
                    {"id": "b", "token_env": "B", "target": "ws://127.0.0.1:2"},
                ]
            }
        )
        assert _tenant_targets(p, "ws://127.0.0.1:1") == {"ws://127.0.0.1:1", "ws://127.0.0.1:2"}
        assert _tenant_targets(None, "ws://x") == set()

    async def test_a_scenario_stops_on_a_lost_lease_and_is_not_judged(
        self, cat_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opseval.models.results import VerdictStatus
        from opseval.tenants import Lease

        def taken(self: Lease) -> bool:
            self.lost = True
            return False

        monkeypatch.setattr(Lease, "check", taken)
        monkeypatch.setenv("T_A", "a")
        file = write_pool(tmp_path, "tenants:\n  - id: a\n    token_env: T_A\n")
        settings = Settings(
            reports_dir=str(tmp_path / "r"),
            allowed_hosts="127.0.0.1",
            tenants_file=str(file),
            tenant_lock_dir=str(tmp_path / "locks"),
        )
        async with run_agent() as url:
            result = await launch(
                cat_dir / "suites" / "smoke.yaml",
                RunOptions(target=url, profile=cat_dir / "profile.yaml", repeats=3),
                settings,
            )
        for t in result.tests:
            assert len(t.attempts) == 1, "no more attempts on a tenant another run holds"
            assert (t.status, t.skip_kind) == (VerdictStatus.SKIPPED_PRECONDITION, "dependency")
            assert "tenant lease lost after 1 of 3" in (t.reason or "")
            assert t.attempts[0].passed is None
        assert result.exit_code != 0
