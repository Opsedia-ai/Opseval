# SPDX-License-Identifier: Apache-2.0
"""Connecting to the server-mode database (ADR-0047).

The URL comes from ``OPSEVAL_DATABASE_URL`` through :class:`~opseval.config.Settings`, like
every other setting, and its host is checked against the allowlist before anything connects.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from opseval.config import Settings, check_database_url_allowed
from opseval.errors import ConfigError

DRIVER = "postgresql+asyncpg"


def async_url(url: str) -> str:
    """The URL with the asyncpg driver, and libpq's ``sslmode`` spelt as asyncpg's ``ssl``.

    ``postgresql://`` is what every hosted Postgres hands out, so it is accepted as is.
    """
    parsed = urlparse(url)
    query = [("ssl" if key == "sslmode" else key, value) for key, value in parse_qsl(parsed.query)]
    return urlunparse(parsed._replace(scheme=DRIVER, query=urlencode(query)))


def database_url(settings: Settings) -> str:
    """The configured URL, refused unless its host may receive the catalogue and runs."""
    if settings.database_url is None:
        raise ConfigError(
            "no database: set OPSEVAL_DATABASE_URL=postgresql://user:password@host:5432/opseval "
            "(server mode, ADR-0047)"
        )
    url = settings.database_url.get_secret_value()
    check_database_url_allowed(url, settings.allowlist())
    return async_url(url)


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(database_url(settings), pool_pre_ping=True)


def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
