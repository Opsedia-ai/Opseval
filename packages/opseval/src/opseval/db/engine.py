# SPDX-License-Identifier: Apache-2.0
"""Connecting to the server-mode database (ADR-0047).

The URL comes from ``OPSEVAL_DATABASE_URL`` through :class:`~opseval.config.Settings`, like
every other setting. Its host and TLS are checked against the allowlist before anything
connects, as the driver will read them, so the check cannot be shown one host and connect to
another.
"""

from __future__ import annotations

import ssl
from urllib.parse import urlparse

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from opseval.config import Settings, check_database_host
from opseval.errors import AllowedHostError, ConfigError

DRIVER = "postgresql+asyncpg"
SCHEMES = ("postgresql", "postgres", "postgresql+asyncpg")
PARAMETERS = {"ssl", "sslmode", "sslrootcert"}
"""The only URL parameters accepted. asyncpg reads every other query key as a connection
argument, including ``host``, which would override the host the allowlist checked."""


def _modes(url: URL) -> list[str]:
    modes: list[str] = []
    for key in ("ssl", "sslmode"):
        value = url.query.get(key)
        modes += [value] if isinstance(value, str) else list(value or ())
    return modes


def checked_url(raw: str, allowlist: set[str]) -> tuple[URL, ssl.SSLContext | str | None]:
    """The URL to connect with, and the ``ssl`` argument for asyncpg, once the database may
    receive data. Errors name the scheme and host only: the URL carries a password."""
    scheme = urlparse(raw).scheme
    if scheme not in SCHEMES:
        raise AllowedHostError(
            f"database URL scheme {scheme or 'missing'!r} is not supported; "
            "use postgresql://user:password@host:5432/db"
        )
    try:
        url = make_url(raw)
    except ArgumentError as exc:
        raise AllowedHostError("the database URL cannot be parsed") from exc
    unknown = sorted(set(url.query) - PARAMETERS)
    if unknown:
        raise AllowedHostError(
            f"database URL parameter(s) {unknown} are not supported; only ssl (or sslmode) "
            "and sslrootcert"
        )
    host = (url.host or "").lower()
    if host != (urlparse(raw).hostname or "").lower():
        # Two parsers reading two hosts from one URL ("user:pa@ss@host", a stray "/" in the
        # password): refuse rather than guess which one would be connected to.
        raise AllowedHostError(
            "the database URL is ambiguous (its host reads differently to different parsers); "
            "percent-encode the user and password"
        )
    modes = _modes(url)
    check_database_host(host, modes, allowlist)
    plain = url.set(drivername=DRIVER, query={})
    if not modes:
        return plain, None
    mode = modes[-1]
    if mode not in ("verify-ca", "verify-full"):
        return plain, mode  # loopback only: check_database_host refused it anywhere else
    root = url.query.get("sslrootcert")
    context = ssl.create_default_context(cafile=root if isinstance(root, str) else None)
    context.check_hostname = mode == "verify-full"
    return plain, context


def make_engine(settings: Settings) -> AsyncEngine:
    if settings.database_url is None:
        raise ConfigError(
            "no database: set OPSEVAL_DATABASE_URL=postgresql://user:password@host:5432/opseval "
            "(server mode, ADR-0047)"
        )
    try:
        url, tls = checked_url(settings.database_url.get_secret_value(), settings.allowlist())
    except FileNotFoundError as exc:
        raise ConfigError(f"sslrootcert: no such file: {exc.filename}") from exc
    args: dict[str, object] = {} if tls is None else {"ssl": tls}
    # Every statement has a limit: a lock or a stuck query fails as a harness error instead
    # of hanging a run or the console (review of #93).
    args["server_settings"] = {
        "statement_timeout": str(int(settings.storage_timeout_seconds * 1000))
    }
    args["timeout"] = settings.storage_timeout_seconds  # connecting, too
    return create_async_engine(url, pool_pre_ping=True, connect_args=args)


def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
