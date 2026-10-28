# SPDX-License-Identifier: Apache-2.0
"""Resolving a run or an attempt from a URL: the one check every route shares.

An id from a URL becomes a folder or file name under the reports folder, so it is matched
against :data:`RUN_ID` (and never contains ``..``) before it touches the disk.
"""

from __future__ import annotations

import re

from opseval.models.record import RunRecord
from opseval.server.actions import ApiError, ServerContext
from opseval.storage import UnknownLayout

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def safe_id(value: str) -> bool:
    """Whether a run or test id from a URL can name something under the reports folder."""
    return bool(RUN_ID.match(value)) and ".." not in value


def attempt(ctx: ServerContext, run_id: str, test_id: str, index: int) -> RunRecord:
    """An attempt's record, read once, or the :class:`ApiError` that says why not: 404 for
    no such attempt, 409 for a newer run layout, 500 for a record that cannot be read (a
    harness problem, never "no such attempt")."""
    if index < 0 or not safe_id(test_id) or not safe_id(run_id):
        raise ApiError(404, "UNKNOWN_ATTEMPT", "no such attempt")
    try:
        record = ctx.store.load_record(run_id, test_id, index)
    except UnknownLayout as exc:
        raise ApiError(409, "UNKNOWN_LAYOUT", str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise ApiError(
            500, "UNREADABLE_RECORD", f"cannot read {test_id} attempt {index}: {type(exc).__name__}"
        ) from exc
    if record is None:
        raise ApiError(
            404, "UNKNOWN_ATTEMPT", f"no record of {test_id} attempt {index} in {run_id}"
        )
    return record


__all__ = ["RUN_ID", "attempt", "safe_id"]
