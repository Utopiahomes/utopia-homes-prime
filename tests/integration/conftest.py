from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient
from fixtures.env import DEFAULT_SNAPSHOT_DIGEST, build_test_environment
from fixtures.keys import sign_token

from guest_answer_provider.api import create_app
from guest_answer_provider.config import Config


@pytest.fixture()
def fake_upstream(monkeypatch) -> str:
    """Not a real network server — patches httpx.AsyncClient.post so the legacy upstream call
    resolves in-process. Used by integration tests that only need the wiring proven, not a real
    second process (that's what tests/contract/ is for)."""

    async def fake_post(self, url, *, headers=None, json=None, timeout=None, follow_redirects=None):
        request = httpx.Request("POST", url, headers=headers)
        return httpx.Response(
            200,
            request=request,
            json={
                "answer": f"Answer to: {json['question']}",
                "source": "faq-1",
                "version": 1,
                "snapshot_digest": DEFAULT_SNAPSHOT_DIGEST,
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return DEFAULT_SNAPSHOT_DIGEST


@pytest.fixture()
def test_environment():
    return build_test_environment()


@pytest.fixture()
def config(test_environment) -> Config:
    return Config.from_environment(test_environment.env)


@pytest.fixture()
def client(config):
    app = create_app(config=config)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def make_token(test_environment) -> Callable[..., str]:
    def _make(**kwargs) -> str:
        return sign_token(test_environment.keypair, **kwargs)

    return _make


@pytest.fixture()
def valid_headers(make_token) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {make_token()}",
        "X-Request-ID": str(uuid.uuid4()),
        "Idempotency-Key": str(uuid.uuid4()),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


@pytest.fixture()
def valid_body() -> dict:
    return {
        "contract_version": "1.0",
        "session_id": str(uuid.uuid4()),
        "message": {"turn_id": str(uuid.uuid4()), "content": "What are your check-in times?"},
        "locale": "en-US",
    }
