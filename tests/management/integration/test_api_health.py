from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from fixtures.management.bundle import validate


def test_health_response_is_schema_valid_and_transitional(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    response = client.get("/management/v1/health", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    validate(body, "health.response")
    assert body["status"] == "unknown"
    assert body["degraded_capabilities"] == []
    assert body["reason_codes"] == ["health_coverage_limited"]
    assert "retry_after_seconds" not in body
