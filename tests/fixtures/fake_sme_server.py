"""Runs FakeSharedModelExecution behind a real loopback TCP/HTTP server, not an in-process httpx
transport substitution (fake_sme.py's own `.transport()` / `_FakeTransport`).

The two fakes prove different things. The in-process transport is what every other integration
test uses, and is enough to prove sme_wire.py/sme_client.py build and parse the wire format
correctly. It never puts bytes on a socket, so it can't catch a bug that only shows up over real
HTTP: header casing/encoding as actually transmitted, a body that's genuinely chunked across TCP
reads, a real connection close/timeout. This module exists to close that gap with one test, and is
deliberately shaped as a standalone async server (start/stop around a real host:port), not a
pytest-only fixture, so it can also be run directly -- see `__main__` below -- as the "genuinely
separate Tiamat process" side of the two-process proof this Stage 2 work is building toward: Homes
in one process, this fake Tiamat in another, talking over a real loopback socket.

A hand-rolled minimal HTTP/1.1 server, not a library server, because FakeSharedModelExecution's
idempotency/long-poll state (one asyncio.Task per execution record, observed by every concurrent
request replaying that Idempotency-Key) must run on a single event loop shared across requests for
the same logical execution. A threaded per-request server would give each request its own loop and
break that -- so this stays on asyncio.start_server, in-process, single loop, real sockets.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fixtures.fake_sme import PATH, FakeSharedModelExecution

_MAX_HEADERS_BYTES = 65_536
_REASON_PHRASES: dict[int, str] = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | None:
    request_line = await reader.readline()
    if not request_line:
        return None  # peer closed the connection cleanly between requests
    method, path, _version = request_line.decode("latin-1").strip().split(" ", 2)

    headers: dict[str, str] = {}
    total = 0
    while True:
        line = await reader.readline()
        total += len(line)
        if total > _MAX_HEADERS_BYTES:
            raise ValueError("request headers exceed the test server's bound")
        stripped = line.decode("latin-1").rstrip("\r\n")
        if not stripped:
            break
        name, _, value = stripped.partition(":")
        headers[name.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    body = await reader.readexactly(length) if length else b""
    return method, path, headers, body


def _status_line(status: int) -> str:
    return f"HTTP/1.1 {status} {_REASON_PHRASES.get(status, 'Unknown')}"


async def _serve_connection(
    fake: FakeSharedModelExecution, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        while True:
            try:
                parsed = await _read_request(reader)
            except (ValueError, asyncio.IncompleteReadError):
                return
            if parsed is None:
                return
            method, path, headers, body = parsed
            status, response_headers, response_body = await fake.handle(
                method, path if path != PATH else PATH, headers, body
            )
            response_headers = dict(response_headers)
            response_headers["content-length"] = str(len(response_body))
            lines = [_status_line(status)]
            lines.extend(f"{name}: {value}" for name, value in response_headers.items())
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + response_body)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        with contextlib.suppress(OSError):
            writer.close()


@asynccontextmanager
async def run(fake: FakeSharedModelExecution, *, host: str = "127.0.0.1") -> AsyncIterator[str]:
    """Starts the server on an OS-assigned loopback port and yields its execution endpoint URL
    (scheme://host:port + PATH, ready to pass as SharedModelExecutionClient's `endpoint_url`)."""
    server = await asyncio.start_server(
        lambda r, w: _serve_connection(fake, r, w), host=host, port=0
    )
    port = server.sockets[0].getsockname()[1]
    async with server:
        serve_task = asyncio.ensure_future(server.serve_forever())
        try:
            yield f"http://{host}:{port}{PATH}"
        finally:
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await serve_task


if __name__ == "__main__":
    # Manual/cross-process use: `python -m fixtures.fake_sme_server` from tests/ starts a fake
    # Tiamat that stays up until killed, printing its endpoint URL. Requires a caller-supplied
    # public key, since FakeSharedModelExecution verifies real signed requests; wire one in here
    # (or extend this block) before pointing a separate Homes process at the printed URL.
    raise SystemExit(
        "fake_sme_server.run() is an async context manager for use from a test or a short driver "
        "script that supplies FakeSharedModelExecution(public_key_pem=..., issuer=...); there is "
        "no standalone CLI yet."
    )
