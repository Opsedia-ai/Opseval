# SPDX-License-Identifier: Apache-2.0
"""Schema migrations, run by ``opseval db upgrade`` (Alembic, configured in code).

The migrations ship inside the package, so an installed Opseval can upgrade its own database
without a checkout or an ``alembic.ini``. The version table is ``opseval_schema_version``, so
Opseval can share a Postgres server, or even a database, with other tools.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

VERSION_TABLE = "opseval_schema_version"
MIGRATIONS = Path(__file__).parent / "migrations"


def config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["connection"] = connection
    return cfg


def head() -> str:
    """The newest migration this version of Opseval knows."""
    revision = ScriptDirectory.from_config(config()).get_current_head()
    if revision is None:  # pragma: no cover - the package always ships migrations
        raise RuntimeError("no migrations found")
    return revision


LOCK_TIMEOUT = "10s"
"""How long a migration that alters busy tables (``0003``) waits for each lock before it fails,
rather than queueing every read behind it."""

UPGRADE_LOCK = 0x0B5E7A1  # any constant; one per application sharing the database


def _upgrade(connection: Connection, revision: str) -> None:
    # A web and a worker container started together both upgrade: the second waits here,
    # then finds the schema current, instead of failing on CREATE TABLE.
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": UPGRADE_LOCK})
    command.upgrade(config(connection), revision)


def _current(connection: Connection) -> str | None:
    context = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
    return context.get_current_revision()


async def upgrade(engine: AsyncEngine, revision: str = "head") -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade, revision)


async def current(engine: AsyncEngine) -> str | None:
    """The database's schema revision; ``None`` for a database never upgraded."""
    async with engine.connect() as conn:
        return await conn.run_sync(_current)
