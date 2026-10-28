# SPDX-License-Identifier: Apache-2.0
"""A task links only its own items, of the right kind (ADR-0049).

``0002`` (as released) linked ``tasks.profile_item_id`` and ``tasks.default_suite_item_id``
to any catalogue item. This revision keys both by ``(item, task)`` to a new unique
``catalogue_items (id, task_id)``, so a task can only name its own items, and installs a
trigger (``opseval_task_link_kinds``) that checks the profile is a ``profile`` and the suite a
``suite``. A link the new rule forbids (another task's item, or the wrong kind) is invalid, so
it is cleared before the keys are added, and the count is printed.

The upgrade waits at most ``migrate.LOCK_TIMEOUT`` for each lock: on a busy database it fails
fast, naming why, instead of queueing behind a push while every read queues behind it.

The downgrade removes exactly that and restores ``0002``'s plain ``ON DELETE SET NULL`` keys;
it loses nothing, so it is never refused.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sys

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

from opseval.db import migrate
from opseval.db.schema import LINK_COLUMNS, drop_link_kinds, install_link_kinds
from opseval.errors import ConfigError

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

LOCK_NOT_AVAILABLE = "55P03"
SET_LOCK_TIMEOUT = sa.text("SELECT set_config('lock_timeout', :t, true)")
"""``SET LOCAL lock_timeout``, with the value as a parameter."""


def _fk(column: str) -> str:
    return f"fk_tasks_{column}_catalogue_items"


def _clear_invalid(column: str, kind: str) -> int:
    """Null every link the new key or the trigger would refuse; how many there were.
    ``column`` is one of ``LINK_COLUMNS``, never input, so the f-string is not an injection."""
    result = op.get_bind().execute(
        sa.text(
            f"UPDATE tasks AS t SET {column} = NULL WHERE t.{column} IS NOT NULL "  # noqa: S608  # nosec B608
            f"AND NOT EXISTS (SELECT 1 FROM catalogue_items AS i WHERE i.id = t.{column} "
            "AND i.task_id = t.id AND i.kind = :kind)"
        ),
        {"kind": kind},
    )
    return int(result.rowcount or 0)


def _upgrade() -> None:
    # The links 0002 allowed but the new rule forbids are invalid: cleared, and counted.
    for column, kind in LINK_COLUMNS.items():
        cleared = _clear_invalid(column, kind)
        if cleared:
            print(
                f"opseval migration 0003: cleared {cleared} tasks.{column} link(s) to an item "
                f"of another task or not of kind {kind}",
                file=sys.stderr,
            )
    # A task links only its own items, and only of the right kind. The key is (item, task),
    # so another task's item cannot be named; NO ACTION, because Postgres before 15 cannot
    # null one column of a composite key, and an item is only ever deleted with its task.
    op.create_unique_constraint("uq_catalogue_items_id", "catalogue_items", ["id", "task_id"])
    for column in LINK_COLUMNS:
        op.drop_constraint(_fk(column), "tasks", type_="foreignkey")
        op.create_foreign_key(
            _fk(column), "tasks", "catalogue_items", [column, "id"], ["id", "task_id"]
        )
    install_link_kinds(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    # SET LOCAL lasts to the end of the migration transaction, so the old value is put back
    # for whatever revision runs after this one.
    before = bind.scalar(sa.text("SELECT current_setting('lock_timeout')"))
    bind.execute(SET_LOCK_TIMEOUT, {"t": migrate.LOCK_TIMEOUT})
    try:
        _upgrade()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
            raise
        raise ConfigError(
            "migration 0003 could not lock the tasks and catalogue tables within "
            f"{migrate.LOCK_TIMEOUT}: something else (a push, a pull) holds them. Nothing was "
            "changed; run `opseval db upgrade` again when the database is quieter"
        ) from exc
    bind.execute(SET_LOCK_TIMEOUT, {"t": before})


def downgrade() -> None:
    drop_link_kinds(op.get_bind())
    for column in LINK_COLUMNS:
        op.drop_constraint(_fk(column), "tasks", type_="foreignkey")
        op.create_foreign_key(
            _fk(column), "tasks", "catalogue_items", [column], ["id"], ondelete="SET NULL"
        )
    op.drop_constraint("uq_catalogue_items_id", "catalogue_items", type_="unique")
