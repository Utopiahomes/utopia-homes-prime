from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from fixtures.management.bundle import validate


def test_identity_response_is_schema_valid(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    response = client.get("/management/v1/identity", headers=auth_headers())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    validate(body, "identity.response")
    assert body["synth_id"] == "stoin:synth:utopia-homes-prime"
    assert body["realm_id"] == "stoin:realm:utopia-homes"
    assert body["synth_class"] == "business-prime"


def test_identity_echoes_request_id(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    headers = auth_headers(request_id="c1a2b3d4-e5f6-4789-9abc-def012345678")
    response = client.get("/management/v1/identity", headers=headers)
    assert response.headers["x-request-id"] == "c1a2b3d4-e5f6-4789-9abc-def012345678"
    assert response.json()["contract_version"] == "1.0"
