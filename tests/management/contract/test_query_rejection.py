"""§7: requests MUST NOT contain query parameters; any query parameter -> 400 invalid_request."""

from __future__ import annotations

import httpx
import pytest
from fixtures.management.tokens import make_token

pytestmark = pytest.mark.contract

_ENDPOINTS = [
    "/management/v1/identity",
    "/management/v1/health",
    "/management/v1/version",
    "/management/v1/capabilities",
]


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_any_query_parameter_is_rejected(spawned_adapter, endpoint: str) -> None:  # type: ignore[no-untyped-def]
    token = make_token(spawned_adapter.keypair.private_key, spawned_adapter.keypair.kid)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-Request-ID": "5a6a3c4e-1b2c-4d5e-89ab-1234567890ab",
    }
    with httpx.Client(base_url=spawned_adapter.base_url, timeout=5) as client:
        response = client.get(endpoint, params={"unexpected": "value"}, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
