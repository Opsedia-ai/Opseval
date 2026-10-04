# SPDX-License-Identifier: Apache-2.0
"""Exclusive test-tenant leases (FR-RUN-08, FR-SAF-03, ADR-0029).

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
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opseval.errors import ConfigError
from opseval.models.tenant import Tenant, TenantPool

if TYPE_CHECKING:
    from opseval.config import Settings

STALE_AFTER_SECONDS = 300.0
"""A lease whose heartbeat is older than this may be reclaimed. Deliberately generous:
reclaiming a live lease is the failure this whole module exists to prevent."""

HEARTBEAT_SECONDS = 30.0


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


def _write_lease(path: Path, holder: str) -> None:
    """Replace ``path`` atomically, so an interrupted write cannot leave it empty."""
    tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex}")
    try:
        tmp.write_text(
            json.dumps({"holder": holder, "heartbeat_at": time.time()}), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        with suppress(OSError):
            tmp.unlink()
        raise


class Lease:
    """An exclusive hold on one tenant."""

    def __init__(self, tenant: Tenant, path: Path, holder: str) -> None:
        self.tenant = tenant
        self.path = path
        self.holder = holder

    def heartbeat(self) -> None:
        """Refresh the lease so a live holder is never mistaken for a dead one.

        Written to a temporary file and ``os.replace``d into place, because a truncating
        write that is interrupted leaves an **empty** lease: unreadable, therefore held,
        therefore a tenant nobody can ever use again.

        A failed write does not release the lease; the worst case is that it looks stale
        later, which only matters if this process has already died.
        """
        with suppress(OSError):
            _write_lease(self.path, self.holder)

    def release(self) -> None:
        try:
            held = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if held.get("holder") != self.holder:
            # Someone else holds it now. Deleting it would release *their* lease.
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
        stale_after: float = STALE_AFTER_SECONDS,
    ) -> None:
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
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if self._held_by_other(path):
                return None
            if not self._steal(path):
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
            json.dump({"holder": self.holder, "heartbeat_at": time.time()}, handle)
        return Lease(tenant, path, self.holder)

    def _steal(self, path: Path) -> bool:
        """Clear a provably stale lease, atomically.

        Unlinking and re-creating is **not** safe: two processes can both see the same
        stale lease, both unlink, and both succeed at ``O_EXCL`` - the double-hold this
        module exists to prevent. ``os.rename`` to a unique name is the atomic step
        instead: exactly one process can move a given file, so exactly one process gets
        to clear it. The winner still has to win ``O_EXCL`` afterwards, so a third
        process creating the lease in the gap costs us the tenant rather than the lock.
        """
        victim = path.with_name(f"{path.name}.stale.{uuid.uuid4().hex}")
        try:
            os.rename(path, victim)
        except OSError:
            return False  # Lost the race, or it vanished. Either way, not ours to clear.
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

    @contextmanager
    def hold(self) -> Iterator[Lease | None]:
        lease = self.acquire()
        try:
            yield lease
        finally:
            if lease is not None:
                lease.release()


@asynccontextmanager
async def refreshing(
    lease: Lease | None, *, every: float = HEARTBEAT_SECONDS
) -> AsyncIterator[None]:
    """Keep ``lease``'s heartbeat fresh for as long as the block runs.

    Without this a scenario that takes longer than ``stale_after`` has its tenant
    declared stale and taken *while it is still talking to the agent*, which is exactly
    the double-hold the leases exist to prevent. Refreshing after each attempt is not
    enough: one long attempt never gets there.
    """
    if lease is None:
        yield
        return

    async def beat() -> None:
        while True:
            await asyncio.sleep(every)
            await asyncio.to_thread(lease.heartbeat)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


__all__ = [
    "HEARTBEAT_SECONDS",
    "STALE_AFTER_SECONDS",
    "Lease",
    "TenantLeases",
    "default_lock_dir",
    "load_pool",
    "refreshing",
    "token_for",
]
