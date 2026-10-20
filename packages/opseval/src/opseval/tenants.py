# SPDX-License-Identifier: Apache-2.0
"""Exclusive test-tenant leases (FR-RUN-08, FR-SAF-03, ADR-0041).

Two attempts must never share a test tenant: they would see each other's orders, refunds
and state, and the resulting mess would be recorded as agent misbehaviour. The lock is
therefore the part of this feature that has to be right, and the rule it follows is:

    **When in doubt, do not take the lease.**

A scenario that cannot get a tenant is ``skipped_precondition`` — the harness could not
obtain a resource, which is not the agent's fault. That is a mildly annoying outcome. Two
runs sharing a tenant produces *false agent failures*, which is a much worse one. So a
lease is only ever stolen when its heartbeat is provably older than the stale threshold,
and never merely because the holder looks unfamiliar.

Leases are files created with ``O_EXCL`` (atomic on POSIX and Windows), holding the
holder and a heartbeat that is refreshed while the lease is held.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opseval.errors import ConfigError
from opseval.models.tenant import Tenant, TenantPool

if TYPE_CHECKING:
    from opseval.config import Settings

HEARTBEAT_SECONDS = 30.0
"""How often a held lease is refreshed, for as long as it is held."""
MIN_STALE_AFTER = 3 * HEARTBEAT_SECONDS
"""A stale threshold shorter than a few heartbeats would make every live lease look dead."""


def load_pool(path: str | Path) -> TenantPool:
    """Read and validate a tenant pool file. Raises :class:`ConfigError` (exit code 3)."""
    import yaml

    file = Path(path)
    try:
        raw = yaml.safe_load(file.read_text("utf-8"))
    except OSError as exc:
        raise ConfigError(f"{file}: cannot read the tenant pool ({exc})") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file}: not valid YAML ({exc})") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{file}: expected a mapping with a 'tenants' list")
    try:
        return TenantPool.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"{file}: {exc}") from exc


def default_lock_dir(pool: TenantPool) -> Path:
    """A machine-wide lock directory, keyed by *which accounts* the pool names.

    A path relative to the working directory would not do: two CI jobs checking the same
    repository out twice resolve it to two different directories, both believe the pool
    is free, and both lease the same tenant - the failure the lock exists to prevent,
    reappearing because of where the lock lives. The shared resource is the set of
    accounts, so that is what the directory is named after. Two unrelated pools that
    happen to share a tenant id do not collide; two checkouts of the same pool do.
    """
    digest = hashlib.sha256("\n".join(sorted(pool.ids)).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "opseval-tenant-locks" / digest


def token_for(tenant: Tenant, settings: Settings) -> str | None:
    """This tenant's token, from the variable it names.

    Resolved through :class:`~opseval.config.Settings`, not ``os.environ``: ``.env`` is
    read into ``Settings`` and never exported to the process environment, so a token set
    where ``opseval init`` says to put it would otherwise look unset.

    Raises :class:`ConfigError` when the variable is declared but empty: running a
    scenario against a tenant with no credential would fail for a reason that has nothing
    to do with the agent.
    """
    if tenant.token_env is None:
        return None
    value = settings.secret_named(tenant.token_env)
    if not value:
        raise ConfigError(
            f"tenant {tenant.id!r} declares token_env {tenant.token_env!r}, which is not set"
        )
    return value


def _body(holder: str, lease_id: str) -> str:
    return json.dumps({"holder": holder, "lease_id": lease_id, "heartbeat_at": time.time()})


def _read(path: Path) -> dict[str, Any] | None:
    """A lease's content, or None when it is missing or cannot be read."""
    try:
        held = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return held if isinstance(held, dict) else None


def _write_lease(path: Path, holder: str, lease_id: str) -> None:
    """Replace ``path`` atomically, so an interrupted write cannot leave it empty."""
    tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex}")
    try:
        tmp.write_text(_body(holder, lease_id), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with suppress(OSError):
            tmp.unlink()
        raise


class Lease:
    """An exclusive hold on one tenant, identified by its own ``lease_id``.

    The id, not the holder name, is what proves a lease file is still this one: a holder can
    take the same tenant again later, and only the id tells two of its leases apart.
    """

    def __init__(self, tenant: Tenant, path: Path, holder: str, lease_id: str) -> None:
        self.tenant = tenant
        self.path = path
        self.holder = holder
        self.lease_id = lease_id
        self.lost = False
        """Set when the lease file turned out to be someone else's: the tenant may have been
        shared, so attempts run under it are not judged (ADR-0041)."""

    def _ours(self) -> bool:
        held = _read(self.path)
        return held is not None and held.get("lease_id") == self.lease_id

    def heartbeat(self) -> None:
        """Refresh the lease so a live holder is never mistaken for a dead one.

        Only while the file is still this lease: a heartbeat must never re-create a released
        lease, nor stamp itself over another run's. Finding someone else's lease there means
        this one was lost, which the runner turns into a harness error, never a verdict.

        Written to a temporary file and ``os.replace``d into place, because a truncating
        write that is interrupted leaves an **empty** lease: unreadable, therefore held,
        therefore a tenant nobody can ever use again. A failed write does not release the
        lease; the worst case is that it looks stale later.
        """
        if not self.path.exists():
            return  # released: nothing to refresh
        if not self._ours():
            self.lost = True
            return
        with suppress(OSError):
            _write_lease(self.path, self.holder, self.lease_id)

    def release(self) -> None:
        if not self._ours():
            # Someone else holds it now (or it is gone). Deleting it would release theirs.
            return
        with suppress(OSError):
            self.path.unlink()


class TenantLeases:
    """Hands out exclusive leases from a pool, with stale-lease recovery."""

    def __init__(
        self,
        pool: TenantPool,
        lock_dir: str | Path,
        *,
        holder: str,
        stale_after: float,
    ) -> None:
        if stale_after < MIN_STALE_AFTER:
            raise ConfigError(
                f"a tenant lease may only go stale after at least {MIN_STALE_AFTER:g}s (three "
                f"heartbeats); {stale_after:g}s would make every live lease look dead"
            )
        self.pool = pool
        self.dir = Path(lock_dir)
        self.holder = holder
        self.stale_after = stale_after

    def _path(self, tenant_id: str) -> Path:
        return self.dir / f"{tenant_id}.lease"

    def _held_by_other(self, path: Path) -> bool:
        """Is this lease live? When the heartbeat cannot be trusted, the file's own
        modification time decides, and an unreadable age counts as live."""
        try:
            held: Any = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            # A crash between creating the lease and writing it leaves an empty file.
            # Without the mtime fallback that tenant would be held forever, so age the
            # file itself - but if even that is unreadable, treat it as held: guessing
            # free is how two runs end up sharing a tenant.
            return self._age(path) is None or not self._stale(self._age(path))
        at = held.get("heartbeat_at") if isinstance(held, dict) else None
        # ``bool`` is an ``int``, and ``json`` parses a bare ``NaN``: True would age to
        # 1970 and NaN makes every comparison False, so both would read as stale and get
        # a live tenant stolen. Neither is a timestamp.
        if not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at):
            age = self._age(path)
            return age is None or not self._stale(age)
        return not self._stale(time.time() - float(at))

    def _stale(self, age: float | None) -> bool:
        return age is not None and age > self.stale_after

    @staticmethod
    def _age(path: Path) -> float | None:
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return None

    def _try_take(self, tenant: Tenant) -> Lease | None:
        path = self._path(tenant.id)
        lease_id = uuid.uuid4().hex
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            judged = _read(path)
            if self._held_by_other(path):
                return None
            if not self._steal(path, judged):
                return None
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except OSError:
                # Someone else created it in the gap. They hold it; we move on.
                return None
        except OSError as exc:
            # Includes an unusable lock directory. A lock we cannot take is a harness
            # problem (exit 3), never an agent failure.
            raise ConfigError(f"cannot create tenant leases in {self.dir}: {exc}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_body(self.holder, lease_id))
        return Lease(tenant, path, self.holder, lease_id)

    def _steal(self, path: Path, judged: dict[str, Any] | None) -> bool:
        """Clear a provably stale lease, atomically, and only that one.

        Unlinking and re-creating is **not** safe: two processes can both see the same
        stale lease, both unlink, and both succeed at ``O_EXCL``. ``os.rename`` to a unique
        name is the atomic step instead: exactly one process can move a given file.

        Renaming moves whatever is at the path *now*, which may already be a fresh lease
        another reclaimer took a moment ago. So the moved file is checked against the one
        judged stale; if it is not that one, it is put back with a no-clobber link and the
        steal is abandoned. Moving a live lease away is the double-hold this module exists
        to prevent.
        """
        victim = path.with_name(f"{path.name}.stale.{uuid.uuid4().hex}")
        try:
            os.rename(path, victim)
        except OSError:
            return False  # Lost the race, or it vanished. Either way, not ours to clear.
        moved = _read(victim)
        same = judged is None and moved is None
        if judged is not None and moved is not None:
            same = moved.get("lease_id") == judged.get("lease_id") and moved.get(
                "heartbeat_at"
            ) == judged.get("heartbeat_at")
        if not same:
            with suppress(OSError):
                os.link(victim, path)  # restore; fails if the path was re-created meanwhile
            with suppress(OSError):
                victim.unlink()
            return False
        with suppress(OSError):
            victim.unlink()
        return True

    def acquire(self) -> Lease | None:
        """An exclusive lease on any free tenant, or ``None`` if every one is held."""
        for tenant in self.pool.tenants:
            lease = self._try_take(tenant)
            if lease is not None:
                return lease
        return None

    async def acquire_waiting(self, *, timeout: float, poll: float = 1.0) -> Lease | None:
        """Wait up to ``timeout`` for a free tenant before giving up.

        A leftover lease from a killed run would otherwise skip a scenario that could have
        run seconds later - and with every scenario skipped the gate blocks the release
        over a purely harness-side condition. Waiting briefly turns transient contention
        into a run; it never relaxes the lock itself.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            lease = self.acquire()
            if lease is not None or time.monotonic() >= deadline:
                return lease
            await asyncio.sleep(min(poll, max(0.0, deadline - time.monotonic())))


@asynccontextmanager
async def refreshing(lease: Lease | None, *, every: float | None = None) -> AsyncIterator[None]:
    """Keep ``lease``'s heartbeat fresh for as long as the block runs.

    Without this a scenario that takes longer than ``stale_after`` has its tenant
    declared stale and taken *while it is still talking to the agent*, which is exactly
    the double-hold the leases exist to prevent. Refreshing after each attempt is not
    enough: one long attempt never gets there.
    """
    if lease is None:
        yield
        return
    interval = HEARTBEAT_SECONDS if every is None else every

    # The write runs in a thread, and cancelling a task does not stop a thread: a heartbeat
    # already under way would land after the block, even after release() deleted the lease,
    # re-creating it and keeping a released tenant busy until it went stale. So each write is
    # shielded from the cancel and the block waits for it before returning.
    inflight: asyncio.Future[None] | None = None

    async def beat() -> None:
        nonlocal inflight
        while True:
            await asyncio.sleep(interval)
            inflight = asyncio.ensure_future(asyncio.to_thread(lease.heartbeat))
            await asyncio.shield(inflight)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if inflight is not None:
            await inflight


__all__ = [
    "HEARTBEAT_SECONDS",
    "MIN_STALE_AFTER",
    "Lease",
    "TenantLeases",
    "default_lock_dir",
    "load_pool",
    "refreshing",
    "token_for",
]
