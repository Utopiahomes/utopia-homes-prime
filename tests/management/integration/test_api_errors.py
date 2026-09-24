from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from fixtures.management.bundle import validate

from utopia_homes_prime.management_adapter.api import create_app
from utopia_homes_prime.management_adapter.config import Config


def test_missing_authorization_returns_401(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    headers = auth_headers()
    del headers["Authorization"]
    response = client.get("/management/v1/health", headers=headers)
    assert response.status_code == 401
    body = response.json()
    validate(body, "error.response")
    assert body["error"]["code"] == "authentication_failed"
    assert body["error"]["retryable"] is False
    assert response.headers["x-request-id"] == headers["X-Request-ID"]
    assert "x-correlation-id" in response.headers


def test_missing_request_id_returns_400_and_never_echoes(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    headers = auth_headers()
    del headers["X-Request-ID"]
    response = client.get("/management/v1/identity", headers=headers)
    assert response.status_code == 400
    body = response.json()
    validate(body, "error.response")
    assert body["error"]["code"] == "invalid_request"
    assert "request_id" not in body["error"]
    assert "x-request-id" not in response.headers


def test_malformed_request_id_returns_400(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    headers = auth_headers(request_id="not-a-uuid")
    response = client.get("/management/v1/identity", headers=headers)
    assert response.status_code == 400
    assert "request_id" not in response.json()["error"]


def test_query_parameters_rejected(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    response = client.get("/management/v1/identity?foo=bar", headers=auth_headers())
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_wrong_method_returns_405(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    response = client.post("/management/v1/identity", headers=auth_headers())
    assert response.status_code == 405
    body = response.json()
    validate(body, "error.response")
    assert body["error"]["code"] == "method_not_allowed"


def test_accept_header_not_json_returns_400(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    # Note: httpx's TestClient always attaches a default `Accept: */*` if the caller doesn't
    # set one, so "missing Accept" can't be exercised through it — this instead sends an
    # explicit, incompatible Accept value, which exercises the same _accept_ok() rejection path.
    headers = auth_headers()
    headers["Accept"] = "text/html"
    response = client.get("/management/v1/identity", headers=headers)
    assert response.status_code == 400


def test_request_body_on_get_returns_400(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    headers = auth_headers()
    response = client.request("GET", "/management/v1/identity", headers=headers, content=b"{}")
    assert response.status_code == 400


def test_correlation_id_is_a_fresh_uuid_each_time(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    headers = auth_headers()
    del headers["Authorization"]
    first = client.get("/management/v1/identity", headers=headers).json()
    second = client.get("/management/v1/identity", headers=headers).json()
    assert first["error"]["correlation_id"] != second["error"]["correlation_id"]


def test_rate_limit_returns_429_with_retry_after(
    make_config: Callable[..., Config], auth_headers: Callable[..., dict[str, str]]
) -> None:
    app = create_app(config=make_config(rate_limit_per_minute=12))
    with TestClient(app) as client:
        headers = auth_headers()
        last_response = None
        for _ in range(13):
            last_response = client.get("/management/v1/identity", headers=headers)
        assert last_response is not None
        assert last_response.status_code == 429
        body = last_response.json()
        assert body["error"]["code"] == "rate_limited"
        assert body["error"]["retryable"] is True
        assert "retry-after" in last_response.headers
