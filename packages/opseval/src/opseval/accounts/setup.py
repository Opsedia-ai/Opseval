# SPDX-License-Identifier: Apache-2.0
"""Accounts mode as ``opseval serve`` sets it up (ADR-0050): the served project and task, the
session lifetimes, OIDC when configured, and where requests may come from."""

from __future__ import annotations

from datetime import timedelta

import httpx

from opseval.accounts.oidc import OidcClient
from opseval.accounts.service import Accounts, Served, resolve_served
from opseval.config import Settings
from opseval.db.run_store import DbRunStore
from opseval.errors import ConfigError
from opseval.server.access import AccessConfig, PublicUrl, parse_proxies


def oidc_client(settings: Settings, *, http: httpx.Client | None = None) -> OidcClient | None:
    """The OIDC client the settings describe, or ``None`` (passwords only). Half a
    configuration is refused rather than ignored."""
    if settings.oidc_issuer is None and settings.oidc_client_id is None:
        return None
    if not settings.oidc_issuer or not settings.oidc_client_id:
        raise ConfigError("OIDC needs both OPSEVAL_OIDC_ISSUER and OPSEVAL_OIDC_CLIENT_ID")
    if "openid" not in settings.oidc_scopes.split():
        raise ConfigError("OPSEVAL_OIDC_SCOPES must include openid")
    secret = settings.oidc_client_secret
    return OidcClient(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=secret.get_secret_value() if secret else None,
        allowlist=settings.allowlist(),
        scopes=settings.oidc_scopes,
        http=http,
    )


def access_config(
    settings: Settings,
    store: DbRunStore,
    *,
    project: str,
    task: str,
    public_url: str | None = None,
    trusted_proxies: list[str] | None = None,
    oidc_http: httpx.Client | None = None,
) -> AccessConfig:
    """Accounts mode for ``store``'s project and task. ``public_url``/``trusted_proxies``
    override the settings of the same name."""

    async def served(session):  # type: ignore[no-untyped-def]
        return await resolve_served(session, project, task)

    where: Served = store.run_in_session(served)
    url = public_url or settings.public_url
    proxies = trusted_proxies if trusted_proxies else settings.trusted_proxies
    accounts = Accounts(
        store,
        where,
        idle=timedelta(minutes=settings.session_idle_minutes),
        lifetime=timedelta(hours=settings.session_max_hours),
        oidc=oidc_client(settings, http=oidc_http),
    )
    return AccessConfig(
        accounts=accounts,
        public_url=PublicUrl.parse(url) if url else None,
        trusted_proxies=parse_proxies(proxies),
    )


__all__ = ["access_config", "oidc_client"]
