# SPDX-License-Identifier: Apache-2.0
"""Sign-in rate limiting, per account and per address, with growing back-off (ADR-0050).

A few failures are free; past them each further failure doubles the wait, up to
:data:`MAX_WAIT`. A success clears the account's count (not the address's: one good password
must not reset an address that is guessing others).

**Check and count are one step.** :meth:`LoginLimiter.begin` reserves the attempt, counted as
a failure, under the lock and before the (slow) password check; :meth:`succeeded` gives it back.
A burst of parallel sign-ins therefore cannot all pass the check before any of them is counted:
at most the allowance gets through.

**In memory, per process.** Behind a load balancer with several ``opseval serve`` processes,
each counts on its own, so an attacker gets the allowance once per process. That is named in
``docs/ATTENTION.md``; a shared limiter (the database or Redis) is the fix when it matters.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

ACCOUNT_FREE = 5
ADDRESS_FREE = 20
WINDOW = 15 * 60.0
"""Failures older than this are forgotten."""
MAX_WAIT = 15 * 60.0
MAX_ENTRIES = 10_000
"""Oldest entries are dropped past this, so a flood of addresses cannot exhaust memory."""


@dataclass
class _Count:
    failures: int
    last: float
    until: float


class LoginLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._counts: OrderedDict[str, _Count] = OrderedDict()

    def _get(self, key: str, now: float) -> _Count | None:
        count = self._counts.get(key)
        if count is not None and now - count.last > WINDOW and now >= count.until:
            del self._counts[key]
            return None
        return count

    def _waits(self, address: str, email: str, now: float) -> float:
        waits = [
            c.until - now
            for key in (f"a:{address}", f"e:{email}")
            if (c := self._get(key, now)) is not None
        ]
        return max([0.0, *waits])

    def wait(self, address: str, email: str) -> float:
        """Seconds until a sign-in from ``address`` for ``email`` may be tried; 0 if now."""
        now = self._clock()
        with self._lock:
            return self._waits(address, email, now)

    def begin(self, address: str, email: str) -> float:
        """Reserve one attempt: 0 when it may go ahead (it is counted as a failure until
        :meth:`succeeded` says otherwise), else the seconds to wait (nothing is counted)."""
        now = self._clock()
        with self._lock:
            wait = self._waits(address, email, now)
            if wait > 0:
                return wait
            for key, free in ((f"a:{address}", ADDRESS_FREE), (f"e:{email}", ACCOUNT_FREE)):
                count = self._get(key, now) or _Count(0, now, now)
                count.failures += 1
                count.last = now
                # Reaching the allowance already makes the next attempt wait.
                over = count.failures - free + 1
                if over > 0:
                    count.until = now + min(2.0 ** (over - 1), MAX_WAIT)
                self._counts[key] = count
                self._counts.move_to_end(key)
            while len(self._counts) > MAX_ENTRIES:
                self._counts.popitem(last=False)
            return 0.0

    def succeeded(self, address: str, email: str) -> None:
        """The reserved attempt was right: forget the account's failures, and give the
        address back the one attempt (not its other failures)."""
        now = self._clock()
        with self._lock:
            self._counts.pop(f"e:{email}", None)
            count = self._get(f"a:{address}", now)
            if count is not None:
                count.failures = max(0, count.failures - 1)
                if count.failures < ADDRESS_FREE:
                    count.until = min(count.until, now)


__all__ = ["ACCOUNT_FREE", "ADDRESS_FREE", "MAX_WAIT", "LoginLimiter"]
