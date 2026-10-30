# SPDX-License-Identifier: Apache-2.0
"""The blob stores (ADR-0051): a folder, and S3 signed in-house (SigV4) over httpx.

The signer is checked against AWS's published examples: the Signature Version 4 test suite
(``aws-sig-v4-test-suite``: get-vanilla, get-vanilla-query-order-key-case, post-vanilla) and
the S3 developer guide's header-signing examples (GET Object with a range, PUT Object).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from opseval.blobs import EMPTY_SHA256, FileBlobStore, S3BlobStore, blob_store, sign_v4
from opseval.config import SECRET_FIELDS, Settings, config_digest
from opseval.errors import AllowedHostError, BlobStoreError, ConfigError

SUITE = {
    "access_key": "AKIDEXAMPLE",
    "secret_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
    "region": "us-east-1",
    "service": "service",
    "amz_date": "20150830T123600Z",
}
HEADERS = {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"}


class TestSigV4Vectors:
    def test_get_vanilla(self) -> None:
        signed = sign_v4(
            method="GET",
            url="https://example.amazonaws.com/",
            headers=HEADERS,
            payload_sha256=EMPTY_SHA256,
            **SUITE,
        )
        assert signed.canonical_request == (
            "GET\n/\n\nhost:example.amazonaws.com\nx-amz-date:20150830T123600Z\n\n"
            f"host;x-amz-date\n{EMPTY_SHA256}"
        )
        assert signed.string_to_sign == (
            "AWS4-HMAC-SHA256\n20150830T123600Z\n20150830/us-east-1/service/aws4_request\n"
            "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63"
        )
        assert signed.authorization == (
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
            "SignedHeaders=host;x-amz-date, "
            "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
        )

    def test_get_vanilla_query_order_key_case(self) -> None:
        signed = sign_v4(
            method="GET",
            url="https://example.amazonaws.com/?Param2=value2&Param1=value1",
            headers=HEADERS,
            payload_sha256=EMPTY_SHA256,
            **SUITE,
        )
        assert "\nParam1=value1&Param2=value2\n" in signed.canonical_request
        assert signed.signature == (
            "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"
        )

    def test_post_vanilla(self) -> None:
        signed = sign_v4(
            method="POST",
            url="https://example.amazonaws.com/",
            headers=HEADERS,
            payload_sha256=EMPTY_SHA256,
            **SUITE,
        )
        assert signed.signature == (
            "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b"
        )

    S3 = {
        "access_key": "AKIAIOSFODNN7EXAMPLE",
        "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "region": "us-east-1",
        "service": "s3",
        "amz_date": "20130524T000000Z",
    }

    def test_s3_get_object_with_a_range(self) -> None:
        signed = sign_v4(
            method="GET",
            url="https://examplebucket.s3.amazonaws.com/test.txt",
            headers={
                "Host": "examplebucket.s3.amazonaws.com",
                "Range": "bytes=0-9",
                "x-amz-content-sha256": EMPTY_SHA256,
                "x-amz-date": "20130524T000000Z",
            },
            payload_sha256=EMPTY_SHA256,
            **self.S3,
        )
        assert signed.signature == (
            "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
        )

    def test_s3_put_object_encodes_the_key_once(self) -> None:
        body = b"Welcome to Amazon S3."
        digest = hashlib.sha256(body).hexdigest()
        signed = sign_v4(
            method="PUT",
            url="https://examplebucket.s3.amazonaws.com/test$file.text",
            headers={
                "Host": "examplebucket.s3.amazonaws.com",
                "Date": "Fri, 24 May 2013 00:00:00 GMT",
                "x-amz-content-sha256": digest,
                "x-amz-date": "20130524T000000Z",
                "x-amz-storage-class": "REDUCED_REDUNDANCY",
            },
            payload_sha256=digest,
            **self.S3,
        )
        assert signed.canonical_request.splitlines()[1] == "/test%24file.text"
        assert signed.signature == (
            "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
        )


class FakeS3:
    """An S3 endpoint in memory that checks every request's signature as AWS would."""

    def __init__(self, access: str = "AK", secret: str = "SK", region: str = "eu-west-1") -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.access, self.secret, self.region = access, secret, region
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        auth = request.headers.get("authorization", "")
        signed_names = auth.split("SignedHeaders=")[1].split(",")[0].split(";")
        body = request.content
        digest = request.headers["x-amz-content-sha256"]
        if hashlib.sha256(body).hexdigest() != digest:
            return httpx.Response(400, text="XAmzContentSHA256Mismatch")
        # The path as sent (already encoded once): decode before re-signing it.
        expected = sign_v4(
            method=request.method,
            url=f"{request.url.scheme}://{request.url.host}{_unquote(request.url.raw_path)}",
            headers={n: request.headers[n] for n in signed_names},
            payload_sha256=digest,
            access_key=self.access,
            secret_key=self.secret,
            region=self.region,
            service="s3",
            amz_date=request.headers["x-amz-date"],
        )
        if auth != expected.authorization:
            return httpx.Response(403, text="SignatureDoesNotMatch")
        key = request.url.raw_path.decode()
        if request.method == "PUT":
            self.objects[key] = (body, dict(request.headers))
            return httpx.Response(200)
        if request.method in ("GET", "HEAD"):
            if key not in self.objects:
                return httpx.Response(404)
            return httpx.Response(
                200, content=self.objects[key][0] if request.method == "GET" else b""
            )
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(405)


def _unquote(raw: bytes) -> str:
    from urllib.parse import unquote

    return unquote(raw.decode())


def s3(fake: FakeS3, **kw: object) -> S3BlobStore:
    return S3BlobStore(
        endpoint="https://s3.example.test",
        bucket="runs",
        region="eu-west-1",
        access_key=SecretStr("AK"),
        secret_key=SecretStr("SK"),
        allowlist={"s3.example.test"},
        client=httpx.Client(transport=httpx.MockTransport(fake)),
        **kw,  # type: ignore[arg-type]
    )


class TestS3:
    def test_put_get_exists_delete(self) -> None:
        fake = FakeS3()
        store = s3(fake, prefix="opseval/")
        key = "runs/r-1/records/a@1__attempt0.json"
        assert store.put(key, b'{"x": 1}', media_type="application/json") == "sse:AES256"
        (sent_key,) = fake.objects
        assert sent_key == "/runs/opseval/runs/r-1/records/a%401__attempt0.json"
        assert fake.objects[sent_key][1]["x-amz-server-side-encryption"] == "AES256"
        assert store.exists(key) and store.get(key) == b'{"x": 1}'
        store.delete(key)
        assert not store.exists(key)
        with pytest.raises(FileNotFoundError):
            store.get(key)
        store.delete(key)  # deleting what is gone is fine: retention retries

    def test_a_wrong_secret_is_a_harness_error_naming_only_the_host(self) -> None:
        store = S3BlobStore(
            endpoint="https://s3.example.test",
            bucket="runs",
            region="eu-west-1",
            access_key=SecretStr("AK"),
            secret_key=SecretStr("wrong"),
            allowlist={"s3.example.test"},
            client=httpx.Client(transport=httpx.MockTransport(FakeS3())),
        )
        with pytest.raises(BlobStoreError) as found:
            store.put("runs/r/k", b"x", media_type="text/plain")
        assert "s3.example.test" in str(found.value) and "403" in str(found.value)
        assert "wrong" not in str(found.value) and "runs/r/k" not in str(found.value)

    def test_without_server_side_encryption_it_says_none(self) -> None:
        fake = FakeS3()
        assert s3(fake, server_side_encryption=False).put("k", b"", media_type="x/y") == "none"

    def test_a_dead_endpoint_is_a_harness_error(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        store = S3BlobStore(
            endpoint="https://s3.example.test",
            bucket="b",
            region="r",
            access_key=SecretStr("a"),
            secret_key=SecretStr("s"),
            allowlist={"s3.example.test"},
            client=httpx.Client(transport=httpx.MockTransport(down)),
        )
        with pytest.raises(BlobStoreError, match="did not answer"):
            store.get("k")

    @pytest.mark.parametrize(
        ("endpoint", "allowed"),
        [
            ("https://s3.example.test", set()),  # not allowlisted
            ("http://s3.example.test", {"s3.example.test"}),  # plain text off loopback
        ],
    )
    def test_the_endpoint_is_allowlisted_and_uses_tls(
        self, endpoint: str, allowed: set[str]
    ) -> None:
        with pytest.raises(AllowedHostError):
            S3BlobStore(
                endpoint=endpoint,
                bucket="b",
                region="r",
                access_key=SecretStr("a"),
                secret_key=SecretStr("s"),
                allowlist=allowed,
            )

    def test_loopback_may_be_plain_http(self) -> None:
        S3BlobStore(
            endpoint="http://127.0.0.1:9000",
            bucket="b",
            region="r",
            access_key=SecretStr("a"),
            secret_key=SecretStr("s"),
            allowlist={"127.0.0.1"},
        )

    def test_keys_are_secrets_and_settings_build_the_store(self) -> None:
        assert {"s3_access_key_id", "s3_secret_access_key"} <= set(SECRET_FIELDS)
        settings = Settings(
            _env_file=None,
            blob_store="s3",
            s3_endpoint="https://s3.example.test",
            s3_bucket="runs",
            s3_access_key_id="AKID",
            s3_secret_access_key="topsecret",
            allowed_hosts="s3.example.test",
        )
        assert "topsecret" not in str(config_digest(settings))
        assert isinstance(blob_store(settings), S3BlobStore)
        with pytest.raises(ConfigError, match="OPSEVAL_S3_BUCKET"):
            blob_store(Settings(_env_file=None, blob_store="s3", s3_endpoint="https://x"))


class TestFileBlobStore:
    def test_round_trip_and_missing(self, tmp_path: Path) -> None:
        store = FileBlobStore(tmp_path)
        assert store.put("runs/r/a", b"data", media_type="x/y") == "none"
        assert store.get("runs/r/a") == b"data" and store.exists("runs/r/a")
        store.delete("runs/r/a")
        store.delete("runs/r/a")
        with pytest.raises(FileNotFoundError):
            store.get("runs/r/a")

    @pytest.mark.parametrize("key", ["../x", "/etc/passwd", "a//b", "a/./b", ""])
    def test_a_key_never_leaves_the_folder(self, tmp_path: Path, key: str) -> None:
        with pytest.raises(BlobStoreError):
            FileBlobStore(tmp_path / "blobs").put(key, b"x", media_type="x/y")

    def test_a_link_out_of_the_folder_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "blobs").mkdir()
        (tmp_path / "outside").mkdir()
        (tmp_path / "blobs" / "runs").symlink_to(tmp_path / "outside")
        with pytest.raises(BlobStoreError, match="leaves"):
            FileBlobStore(tmp_path / "blobs").put("runs/x", b"x", media_type="x/y")
