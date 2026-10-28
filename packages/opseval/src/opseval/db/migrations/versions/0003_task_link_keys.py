# SPDX-License-Identifier: Apache-2.0
"""A task links only its own items, of the right kind (ADR-0049).

``0002`` (as released) linked ``tasks.profile_item_id`` and ``tasks.default_suite_item_id``
to any catalogue item. This revision keys both by ``(item, task)`` to a new unique
``catalogue_items (id, task_id)``, so a task can only name its own items, and installs a
trigger (``opseval_task_link_kinds``) that checks the profile is a ``profile`` and the suite a
``suite``. A link the new rule forbids (another task's item, or the wrong kind) is invalid, so
it is cleared before the keys are added, and the count is printed.

The downgrade removes exactly that and restores ``0002``'s plain ``ON DELETE SET NULL`` keys.
When the downgrade goes on below ``0002``, it first refuses anything ``0002``'s own downgrade
would silently drop (see ``_guard_below_tasks``).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import context, op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

LINKS = {"profile_item_id": "profile", "default_suite_item_id": "suite"}
"""A task's links to its own items, and the kind each must have."""
LINK_KINDS = "opseval_task_link_kinds"
LINK_KINDS_FUNCTION = """
CREATE FUNCTION opseval_task_link_kinds() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM catalogue_items WHERE id = NEW.profile_item_id AND kind <> 'profile')
  THEN
    RAISE EXCEPTION '%: tasks.profile_item_id must name an item of kind profile',
      'ck_tasks_profile_item_kind'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_tasks_profile_item_kind';
  END IF;
  IF EXISTS (
    SELECT 1 FROM catalogue_items WHERE id = NEW.default_suite_item_id AND kind <> 'suite'
  ) THEN
    RAISE EXCEPTION '%: tasks.default_suite_item_id must name an item of kind suite',
      'ck_tasks_default_suite_item_kind'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_tasks_default_suite_item_kind';
  END IF;
  RETURN NEW;
END
$$
"""
"""The kind half of the links: a foreign key cannot say it, and a trigger is cheap here (a
task's links change once per push). An item's kind never changes, so tasks are enough."""


def _fk(column: str) -> str:
    return f"fk_tasks_{column}_catalogue_items"


def _clear_invalid(column: str, kind: str) -> int:
    """Null every link the new key or the trigger would refuse; how many there were.
    ``column`` is one of ``LINKS``, never input, so the f-string is not an injection."""
    result = op.get_bind().execute(
        sa.text(
            f"UPDATE tasks AS t SET {column} = NULL WHERE t.{column} IS NOT NULL "  # noqa: S608  # nosec B608
            f"AND NOT EXISTS (SELECT 1 FROM catalogue_items AS i WHERE i.id = t.{column} "
            "AND i.task_id = t.id AND i.kind = :kind)"
        ),
        {"kind": kind},
    )
    return int(result.rowcount or 0)


def upgrade() -> None:
    # The links 0002 allowed but the new rule forbids are invalid: cleared, and counted.
    for column, kind in LINKS.items():
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
    for column in LINKS:
        op.drop_constraint(_fk(column), "tasks", type_="foreignkey")
        op.create_foreign_key(
            _fk(column), "tasks", "catalogue_items", [column, "id"], ["id", "task_id"]
        )
    op.execute(LINK_KINDS_FUNCTION)
    op.execute(
        f"CREATE TRIGGER {LINK_KINDS} BEFORE INSERT OR UPDATE OF "
        f"{', '.join(LINKS)} ON tasks FOR EACH ROW EXECUTE FUNCTION {LINK_KINDS}()"
    )


LOST = """
SELECT o.slug || '/' || p.slug, t.slug, t.kind, t.modality, t.region,
       t.archived_at IS NOT NULL, t.default_suite_item_id IS NOT NULL
FROM tasks AS t
JOIN projects AS p ON p.id = t.project_id
JOIN organizations AS o ON o.id = p.organization_id
WHERE t.slug <> 'default' OR t.kind <> 'chat' OR t.modality <> 'text'
   OR t.region IS NOT NULL OR t.archived_at IS NOT NULL OR t.default_suite_item_id IS NOT NULL
ORDER BY 1, 2
"""


def _lost(row: Sequence[Any]) -> str:
    project, slug, kind, modality, region, archived, suite = row
    if slug != "default":
        return f"{project}: task {slug!r} and its catalogue"
    settings = [f"kind {kind}"] if kind != "chat" else []
    settings += [f"modality {modality}"] if modality != "text" else []
    settings += [f"region {region}"] if region is not None else []
    settings += ["archived"] if archived else []
    settings += ["a default suite"] if suite else []
    return f"{project}: the default task's {', '.join(settings)}"


def _stays_at_or_above_0002() -> bool:
    """Whether this downgrade stops at ``0002`` or later. A base, ``0001``, relative or
    unknown destination counts as going below: the guard then runs, which can only refuse."""
    destination = context.get_revision_argument()
    if not isinstance(destination, str) or destination.startswith(("-", "+")):
        return False
    try:
        below = context.script.revision_map.iterate_revisions(destination, "base")
        return any(r.revision == "0002" for r in below)
    except Exception:  # noqa: BLE001 - an unresolvable destination is treated as "below"
        return False


def _guard_below_tasks() -> None:
    # 0002's downgrade (unchanged since it was released) refuses only a non-default task; it
    # would drop a default task's kind, modality, region, archive or default suite. The old
    # schema has no place for them, so a downgrade past 0002 is refused here, never lossy.
    lost = [_lost(row) for row in op.get_bind().execute(sa.text(LOST))]
    if lost:
        listed = "\n".join(f"  - {line}" for line in lost)
        raise RuntimeError(
            f"the schema before tasks cannot hold these, so nothing was downgraded:\n{listed}"
        )


def downgrade() -> None:
    if not _stays_at_or_above_0002():
        _guard_below_tasks()
    op.execute(f"DROP TRIGGER {LINK_KINDS} ON tasks")
    op.execute(f"DROP FUNCTION {LINK_KINDS}()")
    for column in LINKS:
        op.drop_constraint(_fk(column), "tasks", type_="foreignkey")
        op.create_foreign_key(
            _fk(column), "tasks", "catalogue_items", [column], ["id"], ondelete="SET NULL"
        )
    op.drop_constraint("uq_catalogue_items_id", "catalogue_items", type_="unique")
