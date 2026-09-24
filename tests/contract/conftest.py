"""Spawns the provider as a real, separate OS process and talks to it over real HTTP — the same
two-process network proof the Management Contract adapter's contract tests established, extended
here with a real (thread-based) fake legacy upstream HTTP server so the whole chain — consumer
request -> provider process -> legacy upstream process -> provider process -> response — is real
network traffic end to end, not an in-process mock.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fixtures.keys import TestKeypair, generate_test_keypair

DEFAULT_SNAPSHOT_DIGEST = hashlib.sha256(b"contract-test-snapshot").hexdigest()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _OutputDrain:
    def __init__(self, stream: object) -> None:
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self._thread.start()

    def _drain(self, stream: object) -> None:
        for line in stream:  # type: ignore[attr-defined]
            with self._lock:
                self._lines.append(line)

    def snapshot(self) -> str:
        with self._lock:
            return "".join(self._lines)


@dataclass(frozen=True, slots=True)
class FakeLegacyUpstream:
    url: str
    snapshot_digest: str
    delay_seconds: float = 0.0


def _make_fake_legacy_upstream(*, delay_seconds: float = 0.0) -> Iterator[FakeLegacyUpstream]:
    snapshot_digest = DEFAULT_SNAPSHOT_DIGEST

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if delay_seconds:
                time.sleep(delay_seconds)
            length = int(self.headers.get("content-length", 0))
            payload = json.loads(self.rfile.read(length))
            body = json.dumps(
                {
                    "answer": f"Answer to: {payload['question']}",
                    "source": "faq-1",
                    "version": 1,
                    "snapshot_digest": snapshot_digest,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield FakeLegacyUpstream(
            url=f"http://127.0.0.1:{port}/fake-upstream",
            snapshot_digest=snapshot_digest,
            delay_seconds=delay_seconds,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def fake_legacy_upstream() -> Iterator[FakeLegacyUpstream]:
    yield from _make_fake_legacy_upstream()


@pytest.fixture
def slow_fake_legacy_upstream() -> Iterator[FakeLegacyUpstream]:
    """A 2-second-delayed upstream, used to prove request_in_progress is reachable for real."""
    yield from _make_fake_legacy_upstream(delay_seconds=2.0)


@dataclass(frozen=True, slots=True)
class SpawnedProvider:
    base_url: str
    keypair: TestKeypair
    snapshot_digest: str


def _spawn(*, upstream: FakeLegacyUpstream) -> Iterator[SpawnedProvider]:
    keypair = generate_test_keypair()
    port = _free_port()
    jwt_keys = [
        {
            "kid": keypair.kid,
            "public_key_pem": keypair.public_pem,
            "environment": "preview",
            "issuer": keypair.issuer,
            "subject": keypair.subject,
            "audience": keypair.audience,
            "capabilities": ["guest.answer"],
            "status": "active",
        }
    ]
    env = dict(os.environ)
    env.update(
        {
            "PORT": str(port),
            "GUEST_ANSWER_PROVIDER_ENVIRONMENT": "preview",
            "GUEST_ANSWER_PROVIDER_JWT_PUBLIC_KEYS_JSON": json.dumps(jwt_keys),
            "GUEST_ANSWER_PROVIDER_BUSINESS_RELEASE_ID": "homes-business:release:contract-test.1",
            "GUEST_ANSWER_PROVIDER_KNOWLEDGE_RELEASE_ID": "homes-knowledge:release:contract-test.1",
            "GUEST_ANSWER_PROVIDER_IDEMPOTENCY_IN_PROGRESS_CEILING_SECONDS": "5",
            "LUCY_PUBLIC_ENABLED": "true",
            "LUCY_PUBLIC_API_URL": upstream.url,
            "LUCY_PUBLIC_API_TOKEN": "x" * 40,
            "LUCY_PUBLIC_SITE_HOSTNAME": "www.example.com",
            "LUCY_PUBLIC_SNAPSHOT_DIGEST": upstream.snapshot_digest,
        }
    )

    process = subprocess.Popen(
        [sys.executable, "-m", "utopia_homes_prime.runtime"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    drain = _OutputDrain(process.stdout)
    base_url = f"http://127.0.0.1:{port}"

    deadline = time.time() + 10
    ready = False
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"provider process exited early:\n{drain.snapshot()}")
        try:
            response = httpx.get(f"{base_url}/healthz", timeout=0.5)
            if response.status_code == 200:
                ready = True
                break
        except httpx.TransportError:
            pass
        time.sleep(0.1)

    if not ready:
        process.terminate()
        raise RuntimeError(f"provider did not become ready within 10 seconds:\n{drain.snapshot()}")

    try:
        yield SpawnedProvider(
            base_url=base_url, keypair=keypair, snapshot_digest=upstream.snapshot_digest
        )
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.fixture
def spawned_provider(fake_legacy_upstream) -> Iterator[SpawnedProvider]:
    yield from _spawn(upstream=fake_legacy_upstream)


@pytest.fixture
def spawned_provider_with_slow_upstream(slow_fake_legacy_upstream) -> Iterator[SpawnedProvider]:
    yield from _spawn(upstream=slow_fake_legacy_upstream)
