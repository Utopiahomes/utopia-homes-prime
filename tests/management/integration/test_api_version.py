from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from fixtures.management.bundle import validate


def test_version_response_is_schema_valid(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    response = client.get("/management/v1/version", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    validate(body, "version.response")
    assert body["managed_synth"]["synth_id"] == "stoin:synth:utopia-homes-prime"
    assert body["supported_management_contracts"] == ["1.0"]


def test_version_release_id_matches_health(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    version_body = client.get("/management/v1/version", headers=auth_headers()).json()
    health_body = client.get("/management/v1/health", headers=auth_headers()).json()
    assert (
        version_body["management_provider"]["release_id"]
        == health_body["management_provider_release_id"]
    )
