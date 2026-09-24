from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from fixtures.management.keys import Ed25519KeyPair
from fixtures.management.tokens import make_token

from utopia_homes_prime.management_adapter.api import create_app
from utopia_homes_prime.management_adapter.config import Config


@pytest.fixture
def client(make_config: Callable[..., Config]) -> Iterator[TestClient]:
    app = create_app(config=make_config())
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers(keypair: Ed25519KeyPair) -> Callable[..., dict[str, str]]:
    def _headers(*, request_id: str = "5a6a3c4e-1b2c-4d5e-89ab-1234567890ab") -> dict[str, str]:
        token = make_token(keypair.private_key, keypair.kid)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-Request-ID": request_id,
        }

    return _headers
