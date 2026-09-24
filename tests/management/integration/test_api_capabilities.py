from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from fixtures.management.bundle import validate


def test_capabilities_response_is_schema_valid(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    response = client.get("/management/v1/capabilities", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    validate(body, "capabilities.response")
    assert body["capabilities"] == [
        {"capability_id": "guest.answer", "contract_version": "1.0", "state": "enabled"}
    ]
