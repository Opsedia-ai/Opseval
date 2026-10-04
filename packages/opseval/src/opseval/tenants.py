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

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from opseval.errors import ConfigError
from opseval.models.tenant import Tenant, TenantPool

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


def token_for(tenant: Tenant) -> str | None:
    """This tenant's token, read from the environment it names.

    Raises :class:`ConfigError` when the variable is declared but unset: running a
    scenario against a tenant with no credential would fail for a reason that has nothing
    to do with the agent.
    """
    if tenant.token_env is None:
        return None
    value = os.environ.get(tenant.token_env)
    if not value:
        raise ConfigError(
            f"tenant {tenant.id!r} declares token_env {tenant.token_env!r}, which is not set"
        )
    return value


class Lease:
    """An exclusive hold on one tenant."""

    def __init__(self, tenant: Tenant, path: Path, holder: str) -> None:
        self.tenant = tenant
        self.path = path
        self.holder = holder

    def heartbeat(self) -> None:
        """Refresh the lease so a live holder is never mistaken for a dead one.

        A failed write does not release the lease; the worst case is that it looks stale
        later, which only matters if this process has already died.
        """
        with suppress(OSError):
            self.path.write_text(
                json.dumps({"holder": self.holder, "heartbeat_at": time.time()}),
                encoding="utf-8",
            )

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
        """Is this lease live? Unreadable or undated counts as live, not free."""
        try:
            held: Any = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            # Cannot tell. Treat it as held: guessing free is how two runs end up
            # sharing a tenant.
            return True
        at = held.get("heartbeat_at") if isinstance(held, dict) else None
        if not isinstance(at, (int, float)):
            return True
        return (time.time() - float(at)) <= self.stale_after

    def _try_take(self, tenant: Tenant) -> Lease | None:
        path = self._path(tenant.id)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if self._held_by_other(path):
                return None
            # Provably stale. Remove it and retry once; if someone else won the race,
            # the next O_EXCL fails and we move on to another tenant.
            try:
                path.unlink()
            except OSError:
                return None
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except OSError:
                return None
        except OSError as exc:
            raise ConfigError(f"cannot create tenant leases in {self.dir}: {exc}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"holder": self.holder, "heartbeat_at": time.time()}, handle)
        return Lease(tenant, path, self.holder)

    def acquire(self) -> Lease | None:
        """An exclusive lease on any free tenant, or ``None`` if every one is held."""
        for tenant in self.pool.tenants:
            lease = self._try_take(tenant)
            if lease is not None:
                return lease
        return None

    @contextmanager
    def hold(self) -> Iterator[Lease | None]:
        lease = self.acquire()
        try:
            yield lease
        finally:
            if lease is not None:
                lease.release()


__all__ = [
    "HEARTBEAT_SECONDS",
    "STALE_AFTER_SECONDS",
    "Lease",
    "TenantLeases",
    "load_pool",
    "token_for",
]
