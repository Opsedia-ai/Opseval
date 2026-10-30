# SPDX-License-Identifier: Apache-2.0
"""Who may reach ``opseval serve``, and from where (ADR-0031, ADR-0050).

Two modes, chosen by configuration and never by the bind address:

- **Shared-token mode** (ADR-0031): file mode with no database and no public URL. Loopback
  only, one bearer token, the caller may do anything. Unchanged.
- **Accounts mode** (ADR-0050): a database or a public URL is configured. Sessions and API
  keys authenticate; the shared token is off. Every route names what it needs and
  :func:`opseval.accounts.roles.decide` alone says yes or no.

This module is light on purpose: shared-token mode must work with only the ``serve`` extra,
so the database, argon2 and JOSE are imported only when accounts mode is configured.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from opseval.accounts.roles import Denial
from opseval.errors import ConfigError

if TYPE_CHECKING:
    from starlette.requests import Request

    from opseval.accounts.service import Accounts

LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
Network = ipaddress.IPv4Network | ipaddress.IPv6Network
HSTS = "max-age=63072000; includeSubDomains"


def host_name(header: str) -> str:
    """The host part of a ``Host`` header: ``[::1]:8000`` -> ``::1``, ``a:80`` -> ``a``."""
    header = header.strip().lower()
    if header.startswith("["):
        return header[1 : header.find("]")] if "]" in header else ""
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


@dataclass(frozen=True)
class PublicUrl:
    scheme: str
    host: str
    origin: str
    """``scheme://host[:port]``, as a browser sends it in ``Origin``."""

    @classmethod
    def parse(cls, url: str) -> PublicUrl:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not host:
            raise ConfigError(f"public URL {url!r}: use https://host[:port]")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
            raise ConfigError(
                f"public URL {url!r}: only scheme, host and port (opseval serve answers at /)"
            )
        if parsed.scheme != "https" and host not in LOOPBACK_NAMES:
            raise ConfigError(f"public URL {url!r} must use https (plain http only on loopback)")
        netloc = f"[{host}]" if ":" in host else host
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        return cls(parsed.scheme, host, f"{parsed.scheme}://{netloc}")


def parse_proxies(text: str | list[str] | tuple[str, ...]) -> list[Network]:
    items = text.split(",") if isinstance(text, str) else list(text)
    out: list[Network] = []
    for raw in (i.strip() for i in items):
        if not raw:
            continue
        try:
            out.append(ipaddress.ip_network(raw, strict=False))
        except ValueError as exc:
            raise ConfigError(f"trusted proxy {raw!r} is not an address or CIDR") from exc
    return out


@dataclass(frozen=True)
class View:
    """The request as the server believes it: the client's address, and the origin a
    same-origin write must carry."""

    address: str
    origin: str
    https: bool


@dataclass
class AccessConfig:
    """Accounts mode, as ``opseval serve`` configured it."""

    accounts: Accounts
    public_url: PublicUrl | None = None
    trusted_proxies: list[Network] = field(default_factory=list)

    @property
    def secure(self) -> bool:
        return self.public_url is not None and self.public_url.scheme == "https"


def _trusted(address: str, proxies: list[Network]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in proxies)


def _last(value: str | None) -> str | None:
    """The last entry of a comma list: the one the nearest proxy added."""
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts[-1] if parts else None


def _forwarded(header: str | None) -> dict[str, str]:
    """The last element of an RFC 7239 ``Forwarded`` header, as lowercase keys."""
    last = _last(header)
    if last is None:
        return {}
    out: dict[str, str] = {}
    for pair in last.split(";"):
        key, sep, value = pair.partition("=")
        if sep:
            out[key.strip().lower()] = value.strip().strip('"')
    return out


def _client(peer: str, chain: str | None, proxies: list[Network]) -> str:
    """The client address: the right-most ``X-Forwarded-For`` entry that is not one of our
    proxies (anything left of it was written by the client and proves nothing)."""
    hops = [h.strip() for h in (chain or "").split(",") if h.strip()]
    for hop in reversed(hops):
        if not _trusted(hop, proxies):
            return hop
    return hops[0] if hops else peer


def view(request: Request, config: AccessConfig | None) -> View | Denial:
    """Check where a request came from and what it calls itself (``Host``), or refuse it
    403 ``HOST_NOT_ALLOWED``. Forwarded headers count only from a trusted proxy."""
    peer = request.client.host if request.client else ""
    host = request.headers.get("host", "")
    https = request.url.scheme == "https"
    if config is None or config.public_url is None:
        if host_name(host) not in LOOPBACK_NAMES:
            return Denial(403, "HOST_NOT_ALLOWED", "opseval serve answers on loopback names only")
        return View(peer, f"{'https' if https else 'http'}://{host.strip().lower()}", https)
    public = config.public_url
    address = peer
    if config.trusted_proxies and _trusted(peer, config.trusted_proxies):
        fwd = _forwarded(request.headers.get("forwarded"))
        proto = fwd.get("proto") or _last(request.headers.get("x-forwarded-proto"))
        host = fwd.get("host") or _last(request.headers.get("x-forwarded-host")) or host
        chain = fwd.get("for") or request.headers.get("x-forwarded-for")
        address = _client(peer, chain, config.trusted_proxies).strip("[]")
        https = (proto or "").lower() == "https"
    if host_name(host) != public.host:
        return Denial(403, "HOST_NOT_ALLOWED", f"this server answers as {public.host} only")
    if public.scheme == "https" and not https:
        return Denial(
            403,
            "HOST_NOT_ALLOWED",
            "plain http is refused: reach this server at its https address",
        )
    return View(address, public.origin, https)


def check_bind(
    host: str,
    *,
    accounts: bool,
    public_url: PublicUrl | None,
    tls: bool,
    proxies: list[Network],
) -> None:
    """Refuse (``ConfigError``, exit 3) a way of serving that would put plain HTTP, or a
    shared token, on a network (ADR-0050): the bind rule makes it impossible, not merely
    discouraged."""
    loopback = host_name(host) in LOOPBACK_NAMES
    if not accounts and not loopback:
        raise ConfigError(
            f"opseval serve binds to loopback only, not {host!r}, without accounts: it can "
            "start runs with your agent token and LLM keys. To reach it from elsewhere, use "
            "an SSH tunnel (ADR-0031), or serve with a database and --public-url https://… "
            "(ADR-0050)."
        )
    if proxies and public_url is None:
        raise ConfigError("--trusted-proxy needs --public-url: the host the proxy serves")
    if public_url is not None and public_url.scheme == "https" and not (tls or proxies):
        raise ConfigError(
            f"--public-url {public_url.origin} is https: give --tls-cert and --tls-key, or "
            "--trusted-proxy for the proxy that terminates TLS"
        )
    if not loopback and (public_url is None or public_url.scheme != "https"):
        raise ConfigError(
            f"binding {host!r} serves beyond this machine: it needs --public-url https://… "
            "and either --tls-cert/--tls-key or --trusted-proxy"
        )


__all__ = [
    "HSTS",
    "LOOPBACK_NAMES",
    "AccessConfig",
    "PublicUrl",
    "View",
    "check_bind",
    "host_name",
    "parse_proxies",
    "view",
]
