"""Spawns the adapter as a real, separate OS process and talks to it over real HTTP — the
"contract tests run over network HTTP between two processes" requirement (§14, §18 criterion
16), which has no precedent in the sibling monolith repo (its HTTP tests all use an in-process
TestClient). Every fixture here uses ephemeral, test-only key material — nothing production.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from fixtures.management.keys import Ed25519KeyPair, generate_test_keypair

TEST_ARTIFACT_DIGEST = "sha256:" + "0" * 64


@dataclass(frozen=True, slots=True)
class SpawnedAdapter:
    base_url: str
    keypair: Ed25519KeyPair
    rate_limit_per_minute: int


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _OutputDrain:
    """Continuously reads the child process's stdout in a background thread.

    Without this, the child's stdout pipe fills up once enough log lines accumulate (uvicorn
    logs each request, plus our own access_log line) and the child's next blocking write() stalls
    its single-threaded event loop forever — observed here as every request hanging after the
    ~11th, regardless of the adapter's own logic. Draining continuously avoids that entirely
    while still keeping the output around for a startup-failure error message.
    """

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


def _spawn(*, rate_limit_per_minute: int = 120) -> Iterator[SpawnedAdapter]:
    keypair = generate_test_keypair()
    port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "PORT": str(port),
            "MANAGEMENT_ADAPTER_ENVIRONMENT": "test",
            "MANAGEMENT_ADAPTER_DEPLOYMENT_ID": "stoin:deployment:utopia-homes-management:test",
            "MANAGEMENT_ADAPTER_RUNTIME_ID": "stoin:runtime:utopia-homes-management:contract-test",
            "MANAGEMENT_ADAPTER_RELEASE_ID": "homes-management:release:contract-test.1",
            "MANAGEMENT_ADAPTER_SOFTWARE_VERSION": "0.1.0",
            "MANAGEMENT_ADAPTER_ARTIFACT_DIGEST": TEST_ARTIFACT_DIGEST,
            "MANAGEMENT_ADAPTER_DEPLOYED_AT": "2026-09-15T19:45:00Z",
            "MANAGEMENT_ADAPTER_CAPABILITIES_JSON": json.dumps(
                [{"capability_id": "guest.answer", "contract_version": "1.0", "state": "enabled"}]
            ),
            "MANAGEMENT_ADAPTER_JWT_PUBLIC_KEYS_JSON": json.dumps(
                [{"kid": keypair.kid, "public_key_pem": keypair.public_key_pem, "status": "active"}]
            ),
            "MANAGEMENT_ADAPTER_RATE_LIMIT_PER_MINUTE": str(rate_limit_per_minute),
        }
    )

    process = subprocess.Popen(
        [sys.executable, "-m", "utopia_homes_prime.management_adapter.runtime"],
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
            raise RuntimeError(f"management adapter process exited early:\n{drain.snapshot()}")
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
        raise RuntimeError(
            f"management adapter did not become ready within 10 seconds:\n{drain.snapshot()}"
        )

    try:
        yield SpawnedAdapter(
            base_url=base_url, keypair=keypair, rate_limit_per_minute=rate_limit_per_minute
        )
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.fixture
def spawned_adapter() -> Iterator[SpawnedAdapter]:
    yield from _spawn()


@pytest.fixture
def spawned_adapter_rate_limit_12() -> Iterator[SpawnedAdapter]:
    """A second adapter instance with the §13 minimum rate limit, for the 429 test only —
    keeping this off the default fixture means every other contract test isn't rate-limited by
    incidental request volume."""
    yield from _spawn(rate_limit_per_minute=12)
