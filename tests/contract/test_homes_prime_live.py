"""Real-network proof for the Stage 2 candidate: website-shaped client -> Homes Prime provider (a
separate OS process, default network transport) -> fake Shared Model Execution (real TCP server in
this process). Exercises what an in-process transport cannot: real streaming reads, real header
casing, no ambient proxy use, and a genuinely dropped connection."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from contract.conftest import _free_port, _OutputDrain
from fixtures.fake_sme import FakeSharedModelExecution, success
from fixtures.homes_prime import (
    EXECUTION_ISSUER,
    GENERATE,
    HARBOR_CAPACITY_DRAFT,
    REVIEW,
    SUPPORTED,
    HomesPrimeHarness,
    build_homes_prime_env,
)
from fixtures.keys import sign_token

pytestmark = pytest.mark.contract


@dataclass
class LiveFake:
    fake: FakeSharedModelExecution
    url: str
    drop_next_connections: int = 0


@contextlib.contextmanager
def _serve(fake: FakeSharedModelExecution) -> Iterator[LiveFake]:
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    live: LiveFake

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _dispatch(self) -> None:
            length = int(self.headers.get("content-length", 0))
            body = self.rfile.read(length)
            headers = {name.lower(): value for name, value in self.headers.items()}
            future = asyncio.run_coroutine_threadsafe(
                fake.handle(self.command, self.path, headers, body), loop
            )
            status, response_headers, response_body = future.result(timeout=30)
            if live.drop_next_connections > 0:
                live.drop_next_connections -= 1
                self.close_connection = True
                self.connection.shutdown(2)
                return
            self.send_response(status)
            for name, value in response_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        do_POST = _dispatch
        do_GET = _dispatch

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    live = LiveFake(fake, f"http://127.0.0.1:{server.server_port}/execution/v1/inference")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield live
    finally:
        server.shutdown()
        thread.join(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)


@contextlib.contextmanager
def _spawn(harness: HomesPrimeHarness) -> Iterator[str]:
    port = _free_port()
    env = dict(os.environ)
    env.update(harness.env)
    env["PORT"] = str(port)
    process = subprocess.Popen(
        [sys.executable, "-m", "utopia_homes_prime.runtime"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    drain = _OutputDrain(process.stdout)
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"provider exited early:\n{drain.snapshot()}")
        try:
            if httpx.get(f"{base_url}/healthz", timeout=0.5).status_code == 200:
                break
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    else:
        process.terminate()
        raise RuntimeError(f"provider did not become ready:\n{drain.snapshot()}")
    try:
        yield base_url
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            process.kill()
            process.wait()


def _ask(base_url: str, harness: HomesPrimeHarness) -> httpx.Response:
    return httpx.post(
        f"{base_url}/business/v1/guest/answer",
        json={
            "contract_version": "1.0",
            "session_id": str(uuid.uuid4()),
            "message": {"turn_id": str(uuid.uuid4()), "content": "How many guests fit?"},
            "locale": "en-US",
        },
        headers={
            "Authorization": f"Bearer {sign_token(harness.guest_keypair)}",
            "X-Request-ID": str(uuid.uuid4()),
            "Idempotency-Key": str(uuid.uuid4()),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=20,
    )


def test_grounded_answer_over_real_network(tmp_path):
    fake = FakeSharedModelExecution(public_key_pem="", issuer=EXECUTION_ISSUER)
    with _serve(fake) as live:
        harness = build_homes_prime_env(tmp_path, execution_url=live.url)
        fake.public_key_pem = harness.execution_keypair.public_pem
        fake.script(GENERATE, success(HARBOR_CAPACITY_DRAFT))
        fake.script(REVIEW, success(SUPPORTED))
        with _spawn(harness) as base_url:
            response = _ask(base_url, harness)

    assert response.status_code == 200, response.text
    assert response.json()["sources"][0]["source_id"] == "public-source:harbor-light-page"
    assert response.headers["X-Utopia-Preview-Mode"] == "homes-prime-candidate"
    assert fake.violations == []
    assert fake.dispatches == {GENERATE: 1, REVIEW: 1}


def test_dropped_connection_recovers_by_replay_over_real_network(tmp_path):
    fake = FakeSharedModelExecution(public_key_pem="", issuer=EXECUTION_ISSUER)
    with _serve(fake) as live:
        harness = build_homes_prime_env(tmp_path, execution_url=live.url)
        fake.public_key_pem = harness.execution_keypair.public_pem
        fake.script(GENERATE, success(HARBOR_CAPACITY_DRAFT))
        fake.script(REVIEW, success(SUPPORTED))
        live.drop_next_connections = 1
        with _spawn(harness) as base_url:
            response = _ask(base_url, harness)

    assert response.status_code == 200, response.text
    assert fake.violations == []
    assert fake.dispatches == {GENERATE: 1, REVIEW: 1}
    generate_attempts = [a for a in fake.attempts if a.profile_id == GENERATE]
    assert len(generate_attempts) == 2
    assert len({a.idempotency_key for a in generate_attempts}) == 1


def test_unreachable_execution_service_fails_closed_over_real_network(tmp_path):
    harness = build_homes_prime_env(
        tmp_path, execution_url=f"http://127.0.0.1:{_free_port()}/execution/v1/inference"
    )
    with _spawn(harness) as base_url:
        started = time.monotonic()
        response = _ask(base_url, harness)
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "temporarily_unavailable"
    assert elapsed < 15
