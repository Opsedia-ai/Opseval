# SPDX-License-Identifier: Apache-2.0
"""Where a run came from (ADR-0058 E2).

Adds two nullable columns to ``runs``: ``origin`` (``ci`` for a run a CI job reported with
``opseval run --server``; null for every other run, including all those written before) and
``ci``, what the CI system said (provider, repository, ref, commit, the job's URL) and the
catalogue versions the job pulled. A check keeps them together: a ``ci`` run has its metadata,
and nothing else has any.

Adding nullable columns rewrites nothing, but it does take the table's lock, so the upgrade
waits at most ``migrate.LOCK_TIMEOUT`` for it rather than queue behind a long transaction. The
downgrade drops both columns, and with them the CI metadata of every reported run (the runs
themselves stay).

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError

from opseval.db import migrate
from opseval.errors import ConfigError

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

LOCK_NOT_AVAILABLE = "55P03"
SET_LOCK_TIMEOUT = sa.text("SELECT set_config('lock_timeout', :t, true)")
ORIGIN_RULE = "(origin IS NULL AND ci IS NULL) OR (origin IN ('ci') AND ci IS NOT NULL)"
"""Copied, not imported: a migration describes the schema as it was when it was written."""


def upgrade() -> None:
    bind = op.get_bind()
    before = bind.scalar(sa.text("SELECT current_setting('lock_timeout')"))
    bind.execute(SET_LOCK_TIMEOUT, {"t": migrate.LOCK_TIMEOUT})
    try:
        op.add_column("runs", sa.Column("origin", sa.String(16), nullable=True))
        op.add_column("runs", sa.Column("ci", postgresql.JSONB(), nullable=True))
        op.create_check_constraint("ck_runs_origin", "runs", ORIGIN_RULE)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
            raise
        raise ConfigError(
            "migration 0007 could not lock the runs table within "
            f"{migrate.LOCK_TIMEOUT}: a run is being written. Nothing was changed; run "
            "`opseval db upgrade` again when the database is quieter"
        ) from exc
    bind.execute(SET_LOCK_TIMEOUT, {"t": before})


def downgrade() -> None:
    op.drop_constraint("ck_runs_origin", "runs", type_="check")
    op.drop_column("runs", "ci")
    op.drop_column("runs", "origin")
