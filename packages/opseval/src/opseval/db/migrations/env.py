# SPDX-License-Identifier: Apache-2.0
"""Alembic environment: migrations run only through ``opseval.db.migrate``, on a connection
it hands over, so there is no second way to reach the database or to read its URL."""

from __future__ import annotations

from alembic import context

from opseval.db.migrate import VERSION_TABLE
from opseval.db.schema import Base

connection = context.config.attributes.get("connection")
if connection is None:  # pragma: no cover - a direct `alembic` call
    raise RuntimeError("run migrations with `opseval db upgrade`, not the alembic command")

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    version_table=VERSION_TABLE,
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
