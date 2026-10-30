# SPDX-License-Identifier: Apache-2.0
"""The blob store: where server mode keeps a run's files (ADR-0051).

A :class:`BlobStore` keeps opaque objects by key. What an object *is* (its run, its kind, its
hash, whether it is committed) is recorded in the database's ``blobs`` table, never guessed
from the store: an object without a row is not Opseval's to delete.

- :class:`FileBlobStore` (the default) is a folder, written atomically.
- :class:`S3BlobStore` speaks the S3 API (AWS, MinIO, R2, any compatible store) over httpx,
  with request signing (AWS Signature Version 4) written here: no boto, no vendor SDK
  (ADR-0003). The endpoint is allowlisted and must use TLS off loopback; the keys are secrets.

Every failure is a :class:`~opseval.errors.BlobStoreError` (a harness error, never the
agent's), except a missing object on :meth:`BlobStore.get`, which is ``FileNotFoundError`` so
a reader can say "missing" rather than "broken".
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import quote, urlparse

import httpx
from pydantic import SecretStr

from opseval.config import Settings, check_url_allowed
from opseval.errors import BlobStoreError, ConfigError

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


@runtime_checkable
class BlobStore(Protocol):
    """Opaque objects by key. ``put`` returns the encryption the store applied at rest
    (``none`` or ``sse:AES256``), recorded on the blob's row. ``location`` names where the
    objects are (an absolute folder, or ``host/bucket/prefix``): recorded on each row, so a
    reader started on another location says so instead of reporting the object missing."""

    name: str
    region: str | None
    location: str

    def put(self, key: str, data: bytes, *, media_type: str) -> str: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...


def _check_key(key: str) -> str:
    parts = key.split("/")
    if not key or key.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise BlobStoreError(f"not a blob key: {key!r}")
    return key


class FileBlobStore:
    """Objects as files under one folder. Never outside it, links included."""

    name = "file"
    region: str | None = None
    at_rest = "none"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.location = str(self.root)

    def _path(self, key: str) -> Path:
        path = self.root / _check_key(key)
        root = self.root.resolve()
        if root not in path.resolve().parents:
            raise BlobStoreError(f"blob key {key!r} leaves the blob folder")
        return path

    def put(self, key: str, data: bytes, *, media_type: str) -> str:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as exc:
            raise BlobStoreError(f"cannot write blob {key}: {type(exc).__name__}") from exc
        return "none"

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise BlobStoreError(f"cannot read blob {key}: {type(exc).__name__}") from exc

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise BlobStoreError(f"cannot delete blob {key}: {type(exc).__name__}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


# --- AWS Signature Version 4 -------------------------------------------------------------------


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(text: str, *, keep_slash: bool) -> str:
    return quote(text, safe="-_.~" + ("/" if keep_slash else ""))


@dataclass(frozen=True)
class Signed:
    """What signing produced, kept for tests: each step is checked against AWS's vectors."""

    canonical_request: str
    string_to_sign: str
    signature: str
    authorization: str


def sign_v4(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    amz_date: str,
) -> Signed:
    """Sign a request (AWS Signature Version 4, header form). ``headers`` are the headers to
    sign, ``host`` and ``x-amz-date`` included; ``amz_date`` is ``YYYYMMDDTHHMMSSZ``.

    The path is URI-encoded once, as S3 expects (``/`` kept): an S3 key is never normalised.
    """
    parsed = urlparse(url)
    path = _uri_encode(parsed.path or "/", keep_slash=True)
    pairs = []
    for part in filter(None, parsed.query.split("&")):
        name, _, value = part.partition("=")
        pairs.append((_uri_encode(name, keep_slash=False), _uri_encode(value, keep_slash=False)))
    query = "&".join(f"{k}={v}" for k, v in sorted(pairs))
    canon = {k.lower().strip(): " ".join(v.strip().split()) for k, v in headers.items()}
    names = sorted(canon)
    canonical_headers = "".join(f"{n}:{canon[n]}\n" for n in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join(
        [method, path, query, canonical_headers, signed_headers, payload_sha256]
    )
    day = amz_date[:8]
    scope = f"{day}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), day)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return Signed(canonical_request, string_to_sign, signature, authorization)


class S3BlobStore:
    """Objects in an S3 bucket, path-style (``<endpoint>/<bucket>/<key>``), so MinIO and R2
    work as AWS does. Errors name the host and the HTTP status, never a key or a URL."""

    name = "s3"

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        access_key: SecretStr,
        secret_key: SecretStr,
        allowlist: set[str],
        prefix: str = "",
        server_side_encryption: bool = True,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        check_url_allowed(endpoint, allowlist, secure_schemes=("https",))
        if not bucket or "/" in bucket:
            raise ConfigError("OPSEVAL_S3_BUCKET must be a bucket name")
        parsed = urlparse(endpoint)
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise ConfigError(
                "OPSEVAL_S3_ENDPOINT must not carry a user or password; the keys go in "
                "OPSEVAL_S3_ACCESS_KEY_ID and OPSEVAL_S3_SECRET_ACCESS_KEY"
            )
        self.endpoint = endpoint.rstrip("/")
        self.host = host_header(endpoint)
        self.location = f"{self.host}/{bucket}/{prefix}"
        self.bucket = bucket
        self.region: str | None = region
        self._region = region
        self._access, self._secret = access_key, secret_key
        self.prefix = prefix
        self.sse = server_side_encryption
        self.at_rest = "sse:AES256" if server_side_encryption else "none"
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def _url(self, key: str) -> str:
        return f"{self.endpoint}/{self.bucket}/{self.prefix}{_check_key(key)}"

    def _request(
        self, method: str, key: str, body: bytes = b"", extra: dict[str, str] | None = None
    ) -> httpx.Response:
        url = self._url(key)
        digest = hashlib.sha256(body).hexdigest()
        amz_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "host": self.host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": digest,
            **(extra or {}),
        }
        signed = sign_v4(
            method=method,
            url=url,
            headers=headers,
            payload_sha256=digest,
            access_key=self._access.get_secret_value(),
            secret_key=self._secret.get_secret_value(),
            region=self._region,
            service="s3",
            amz_date=amz_date,
        )
        # The Host header is sent exactly as signed: httpx would otherwise derive its own, and
        # a default port (":443") kept by one side and dropped by the other breaks the
        # signature (review of #93).
        sent = dict(headers)
        sent["authorization"] = signed.authorization
        # The URL as signed: httpx must not re-encode the path differently.
        raw = urlparse(url)
        target = httpx.URL(url).copy_with(
            raw_path=(_uri_encode(raw.path, keep_slash=True)).encode("ascii")
        )
        try:
            return self._client.request(method, target, headers=sent, content=body or None)
        except httpx.HTTPError as exc:
            raise BlobStoreError(
                f"the blob store at {self.host} did not answer ({type(exc).__name__})"
            ) from exc

    def _fail(self, what: str, response: httpx.Response) -> BlobStoreError:
        return BlobStoreError(
            f"the blob store at {self.host} refused to {what} an object "
            f"(HTTP {response.status_code})"
        )

    def put(self, key: str, data: bytes, *, media_type: str) -> str:
        extra = {"content-type": media_type}
        if self.sse:
            extra["x-amz-server-side-encryption"] = "AES256"
        response = self._request("PUT", key, data, extra)
        if response.status_code != 200 and self.sse and _refuses_sse(response):
            raise BlobStoreError(
                f"the blob store at {self.host} does not support server-side encryption "
                f"(HTTP {response.status_code}): enable it on the store (MinIO needs a KMS), "
                "or set OPSEVAL_S3_SERVER_SIDE_ENCRYPTION=false (audio stays sealed by "
                "Opseval either way)"
            )
        if response.status_code != 200:
            raise self._fail("store", response)
        return "sse:AES256" if self.sse else "none"

    def get(self, key: str) -> bytes:
        response = self._request("GET", key)
        if response.status_code == 404:
            raise FileNotFoundError(key)
        if response.status_code != 200:
            raise self._fail("return", response)
        return response.content

    def delete(self, key: str) -> None:
        response = self._request("DELETE", key)
        if response.status_code not in (200, 204, 404):
            raise self._fail("delete", response)

    def exists(self, key: str) -> bool:
        response = self._request("HEAD", key)
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise self._fail("find", response)
        return True


def _refuses_sse(response: httpx.Response) -> bool:
    """The store turned down the ``x-amz-server-side-encryption`` header (MinIO without a KMS
    answers ``NotImplemented``; others name the header)."""
    if response.status_code == 501:
        return True
    body = response.text[:2000] if response.content else ""
    return response.status_code == 400 and (
        "NotImplemented" in body or "ServerSideEncryption" in body or "x-amz-server-side" in body
    )


def host_header(endpoint: str) -> str:
    """The ``Host`` header for ``endpoint``, as it is signed and sent: the host lower-cased,
    the port kept unless it is the scheme's default (``:443`` for https, ``:80`` for http)."""
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"  # IPv6
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("OPSEVAL_S3_ENDPOINT has an invalid port") from exc
    default = {"https": 443, "http": 80}.get(parsed.scheme)
    return host if port is None or port == default else f"{host}:{port}"


def blob_dir(settings: Settings) -> Path:
    """The folder ``OPSEVAL_BLOB_STORE=file`` uses, absolute. An explicit relative folder is
    refused: it would move with the working directory, and a server started elsewhere would
    find the run's files "missing"."""
    if settings.blob_dir is None:
        return Path(settings.reports_dir).expanduser().resolve() / ".blobs"
    folder = Path(settings.blob_dir).expanduser()
    if not folder.is_absolute():
        raise ConfigError(
            f"OPSEVAL_BLOB_DIR must be an absolute path (got {settings.blob_dir!r}): a "
            "relative folder would move with the working directory"
        )
    return folder.resolve()


def blob_store(settings: Settings) -> BlobStore:
    """The blob store the settings name, checked before anything is written."""
    if settings.blob_store == "file":
        return FileBlobStore(blob_dir(settings))
    missing = [
        name
        for name, value in (
            ("OPSEVAL_S3_ENDPOINT", settings.s3_endpoint),
            ("OPSEVAL_S3_BUCKET", settings.s3_bucket),
            ("OPSEVAL_S3_ACCESS_KEY_ID", settings.s3_access_key_id),
            ("OPSEVAL_S3_SECRET_ACCESS_KEY", settings.s3_secret_access_key),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"OPSEVAL_BLOB_STORE=s3 needs {', '.join(missing)}")
    return S3BlobStore(
        endpoint=settings.s3_endpoint or "",
        bucket=settings.s3_bucket or "",
        region=settings.s3_region,
        access_key=settings.s3_access_key_id or SecretStr(""),
        secret_key=settings.s3_secret_access_key or SecretStr(""),
        allowlist=settings.allowlist(),
        prefix=settings.s3_prefix,
        server_side_encryption=settings.s3_server_side_encryption,
        timeout=settings.s3_timeout_seconds,
    )


__all__ = [
    "EMPTY_SHA256",
    "BlobStore",
    "FileBlobStore",
    "S3BlobStore",
    "Signed",
    "blob_dir",
    "blob_store",
    "host_header",
    "sign_v4",
]
