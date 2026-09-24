"""§7/§18 criterion 18: missing/malformed X-Request-ID -> 400 with no fabricated substitute;
a valid one is echoed exactly, in both the body and the header.
"""

from __future__ import annotations

import httpx
import pytest
from fixtures.management.tokens import make_token

pytestmark = pytest.mark.contract


def _valid_headers(spawned_adapter, request_id: str | None) -> dict[str, str]:  # type: ignore[no-untyped-def]
    token = make_token(spawned_adapter.keypair.private_key, spawned_adapter.keypair.kid)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return headers


def _get(base_url: str, headers: dict[str, str]) -> httpx.Response:
    with httpx.Client(base_url=base_url, timeout=5) as client:
        return client.get("/management/v1/identity", headers=headers)


def test_missing_request_id_returns_400_without_fabricated_id(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    response = _get(spawned_adapter.base_url, _valid_headers(spawned_adapter, None))
    assert response.status_code == 400
    assert "x-request-id" not in response.headers
    assert "request_id" not in response.json()["error"]


def test_malformed_request_id_returns_400(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    response = _get(spawned_adapter.base_url, _valid_headers(spawned_adapter, "not-a-uuid"))
    assert response.status_code == 400
    assert "x-request-id" not in response.headers


def test_valid_request_id_echoed_exactly(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    request_id = "9F86D081-884C-4D65-9861-5C4111111111"  # exercises uppercase-hex acceptance
    response = _get(spawned_adapter.base_url, _valid_headers(spawned_adapter, request_id))
    assert response.status_code == 200
    assert response.headers["x-request-id"] == request_id
