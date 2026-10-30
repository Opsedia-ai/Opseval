# SPDX-License-Identifier: Apache-2.0
"""Catalogue items of kind ``dataset`` (ADR-0060).

A scenario's ``dataset:`` names a CSV or JSONL file under ``datasets/``; a push stores it as an
item of its own, versioned and labelled like scenarios, suites and profiles, so a pulled label
runs the rows it was validated with. This revision only widens ``ck_catalogue_items_kind``.

The upgrade waits at most ``migrate.LOCK_TIMEOUT`` for its lock, as ``0003`` does.

The downgrade puts the old constraint back, under its old name. It refuses, changing nothing,
while any dataset item exists: deleting them would leave the scenarios that name them unable to
load.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

from opseval.db import migrate
from opseval.errors import ConfigError

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

LOCK_NOT_AVAILABLE = "55P03"
SET_LOCK_TIMEOUT = sa.text("SELECT set_config('lock_timeout', :t, true)")
NAME = "ck_catalogue_items_kind"
"""The models' name (naming convention ``ck_<table>_<name>`` over ``kind``)."""
RELEASED = "ck_catalogue_items_ck_catalogue_items_kind"
"""The name ``0001`` gave it: its full name went through the naming convention again."""
BEFORE = ("scenario", "suite", "profile")
AFTER = ("scenario", "suite", "profile", "dataset")
"""Copied, not imported: a migration describes the schema as it was when it was written."""


def _one_of(values: tuple[str, ...]) -> str:
    return f"kind IN ({', '.join(repr(v) for v in values)})"


def _replace(values: tuple[str, ...], name: str) -> None:
    # Either name may be there (a database made from the models has the short one); the
    # names are constants, never input.
    for old in (NAME, RELEASED):
        op.execute(f'ALTER TABLE catalogue_items DROP CONSTRAINT IF EXISTS "{old}"')
    op.create_check_constraint(op.f(name), "catalogue_items", _one_of(values))


def upgrade() -> None:
    bind = op.get_bind()
    before = bind.scalar(sa.text("SELECT current_setting('lock_timeout')"))
    bind.execute(SET_LOCK_TIMEOUT, {"t": migrate.LOCK_TIMEOUT})
    try:
        _replace(AFTER, NAME)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
            raise
        raise ConfigError(
            "migration 0004 could not lock the catalogue within "
            f"{migrate.LOCK_TIMEOUT}: something else (a push, a pull) holds it. Nothing was "
            "changed; run `opseval db upgrade` again when the database is quieter"
        ) from exc
    bind.execute(SET_LOCK_TIMEOUT, {"t": before})


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM catalogue_items WHERE kind = 'dataset'")
    )
    if count:
        raise RuntimeError(
            f"{count} dataset item(s) are stored, which revision 0003 cannot hold; nothing was "
            "downgraded. Delete the tasks that use datasets first, or stay at 0004"
        )
    _replace(BEFORE, RELEASED)
