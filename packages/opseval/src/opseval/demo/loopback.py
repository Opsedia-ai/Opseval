# SPDX-License-Identifier: Apache-2.0
"""A minimal HTTP/1.1 server on loopback, for the voice demo's fake speech providers and its
audio-http agent. Standard library only: the core has no web server dependency, and the demo
must run with nothing but ``pip install opseval``. One request per connection."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

MAX_BODY = 32 * 1024 * 1024


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/octet-stream"
    headers: dict[str, str] = field(default_factory=dict)


Handler = Callable[[Request], Awaitable[Response]]
REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Server Error", 503: "Busy"}


async def _handle(
    handler: Handler, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if length > MAX_BODY:
            raise ValueError("body too large")
        body = await reader.readexactly(length) if length else b""
        response = await handler(Request(method, path.split("?")[0], headers, body))
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
        response = Response(status=400, body=b"bad request", content_type="text/plain")
    except ConnectionError:
        writer.close()
        return
    extra = "".join(f"{k}: {v}\r\n" for k, v in response.headers.items())
    writer.write(
        (
            f"HTTP/1.1 {response.status} {REASONS.get(response.status, 'Status')}\r\n"
            f"content-type: {response.content_type}\r\n"
            f"content-length: {len(response.body)}\r\n"
            f"{extra}connection: close\r\n\r\n"
        ).encode("latin-1")
        + response.body
    )
    with contextlib.suppress(ConnectionError):
        await writer.drain()
    writer.close()
    with contextlib.suppress(ConnectionError):
        await writer.wait_closed()


@asynccontextmanager
async def serve_http(handler: Handler, port: int = 0) -> AsyncIterator[str]:
    """Serve ``handler`` on 127.0.0.1; yields the base URL (``http://127.0.0.1:<port>``)."""

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle(handler, reader, writer)

    server = await asyncio.start_server(on_connect, "127.0.0.1", port, limit=MAX_BODY)
    bound = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{bound}"
    finally:
        server.close()
        await server.wait_closed()


def multipart_file(request: Request, name: str = "file") -> bytes | None:
    """The bytes of one file field of a ``multipart/form-data`` body, or ``None``."""
    ctype = request.headers.get("content-type", "")
    marker = "boundary="
    if marker not in ctype:
        return None
    boundary = ("--" + ctype.split(marker, 1)[1].split(";")[0].strip('"')).encode("latin-1")
    for part in request.body.split(boundary):
        head, sep, data = part.partition(b"\r\n\r\n")
        if sep and f'name="{name}"'.encode() in head:
            return data[:-2] if data.endswith(b"\r\n") else data
    return None


__all__ = ["Request", "Response", "multipart_file", "serve_http"]
