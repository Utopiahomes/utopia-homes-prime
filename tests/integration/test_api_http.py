"""Real-HTTP-shaped proof (via FastAPI TestClient) that the wiring in api.py actually produces
what the unit-level vector replays (tests/unit/) prove the individual pieces should produce.
Covers the header-format vectors (headers.*.json), the transport byte-boundary and duplicate-key
vectors (transport.*.json), and the relational exchange vectors (exchange.*.json) that can only
be checked against a live request/response pair, not a single document in isolation.
"""

from __future__ import annotations

import uuid

from vector_helpers import load_json, vectors_in


def test_happy_path_schema_valid_response(client, valid_headers, valid_body, fake_upstream):
    from guest_answer_provider import schema_validation

    response = client.post("/business/v1/guest/answer", json=valid_body, headers=valid_headers)
    assert response.status_code == 200
    schema_validation.validate_response(response.json())


def test_release_headers_present_on_success(client, valid_headers, valid_body, fake_upstream):
    """headers.003 (release_id format) + exchange.005 (both present on success)."""
    response = client.post("/business/v1/guest/answer", json=valid_body, headers=valid_headers)
    assert response.status_code == 200
    assert response.headers["X-Utopia-Business-Release"]
    assert response.headers["X-Utopia-Knowledge-Release"]


def test_cache_control_no_store_on_every_response(client, valid_headers, valid_body, fake_upstream):
    """headers.006"""
    ok = client.post("/business/v1/guest/answer", json=valid_body, headers=valid_headers)
    assert ok.headers["Cache-Control"] == "no-store"

    bad_headers = dict(valid_headers)
    del bad_headers["X-Request-ID"]
    err = client.post("/business/v1/guest/answer", json=valid_body, headers=bad_headers)
    assert err.headers["Cache-Control"] == "no-store"


def test_no_set_cookie_on_responses(client, valid_headers, valid_body, fake_upstream):
    """headers.007"""
    response = client.post("/business/v1/guest/answer", json=valid_body, headers=valid_headers)
    assert "set-cookie" not in {k.lower() for k in response.headers}


def test_request_id_echoed_unchanged(client, valid_headers, valid_body, fake_upstream):
    """exchange.001/.002: the response's X-Request-ID must equal the inbound value exactly,
    including case — this is the relational half headers.001 (format) alone doesn't cover."""
    response = client.post("/business/v1/guest/answer", json=valid_body, headers=valid_headers)
    assert response.headers["X-Request-ID"] == valid_headers["X-Request-ID"]


def test_correlation_id_distinct_from_request_id(client, valid_headers, valid_body):
    """exchange.003/.004: correlation_id must never coincidentally equal X-Request-ID."""
    response = client.post("/business/v1/guest/answer", json=valid_body, headers=valid_headers)
    assert response.status_code != 200  # no fake_upstream fixture -> temporarily_unavailable
    body = response.json()
    assert body["error"]["correlation_id"] != valid_headers["X-Request-ID"]


def test_missing_x_request_id_rejected(client, valid_headers, valid_body):
    """headers.001 case: missing -> invalid_request."""
    headers = dict(valid_headers)
    del headers["X-Request-ID"]
    response = client.post("/business/v1/guest/answer", json=valid_body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_uppercase_x_request_id_rejected(client, valid_headers, valid_body):
    """headers.001 case 4: uppercase UUID rejected (RC2 §3 canonical lowercase only)."""
    headers = dict(valid_headers)
    headers["X-Request-ID"] = str(uuid.uuid4()).upper()
    response = client.post("/business/v1/guest/answer", json=valid_body, headers=headers)
    assert response.status_code == 400


def test_missing_idempotency_key_rejected(client, valid_headers, valid_body):
    """headers.002 case: missing -> invalid_request."""
    headers = dict(valid_headers)
    del headers["Idempotency-Key"]
    response = client.post("/business/v1/guest/answer", json=valid_body, headers=headers)
    assert response.status_code == 400


def test_query_params_rejected(client, valid_headers, valid_body):
    """headers.005"""
    response = client.post(
        "/business/v1/guest/answer?foo=bar", json=valid_body, headers=valid_headers
    )
    assert response.status_code == 400


def test_missing_accept_header_rejected(client, valid_headers, valid_body):
    """headers.008. Uses content= (raw bytes) rather than json=, since httpx's json= helper
    injects its own Content-Type/Accept-adjacent defaults that would silently defeat a header
    deletion from the test's own headers dict."""
    import json as json_module

    headers = dict(valid_headers)
    del headers["Accept"]
    response = client.post(
        "/business/v1/guest/answer", content=json_module.dumps(valid_body), headers=headers
    )
    assert response.status_code == 400


def test_wrong_accept_header_rejected(client, valid_headers, valid_body):
    import json as json_module

    headers = dict(valid_headers)
    headers["Accept"] = "text/html"
    response = client.post(
        "/business/v1/guest/answer", content=json_module.dumps(valid_body), headers=headers
    )
    assert response.status_code == 400


def test_missing_content_type_rejected(client, valid_headers, valid_body):
    """headers.009"""
    import json as json_module

    headers = dict(valid_headers)
    del headers["Content-Type"]
    response = client.post(
        "/business/v1/guest/answer", content=json_module.dumps(valid_body), headers=headers
    )
    assert response.status_code == 400


def test_malformed_authorization_framing_rejected(client, valid_headers, valid_body):
    """headers.010"""
    headers = dict(valid_headers)
    headers["Authorization"] = "Basic not-bearer"
    response = client.post("/business/v1/guest/answer", json=valid_body, headers=headers)
    assert response.status_code == 401


def test_duplicate_top_level_member_rejected(client, valid_headers):
    """transport.003"""
    raw = (
        b'{"contract_version":"1.0","contract_version":"1.1","session_id":"'
        + str(uuid.uuid4()).encode()
        + b'","message":{"turn_id":"'
        + str(uuid.uuid4()).encode()
        + b'","content":"Hello"},"locale":"en-US"}'
    )
    response = client.post("/business/v1/guest/answer", content=raw, headers=valid_headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_duplicate_nested_member_rejected(client, valid_headers):
    """transport.004"""
    raw = (
        b'{"contract_version":"1.0","session_id":"'
        + str(uuid.uuid4()).encode()
        + b'","message":{"turn_id":"'
        + str(uuid.uuid4()).encode()
        + b'","turn_id":"'
        + str(uuid.uuid4()).encode()
        + b'","content":"Hello"},"locale":"en-US"}'
    )
    response = client.post("/business/v1/guest/answer", content=raw, headers=valid_headers)
    assert response.status_code == 400


def test_request_body_at_64kib_accepted_by_schema_layer(client, valid_headers, fake_upstream):
    """transport.001: proves the byte cap doesn't reject at exactly 64 KiB. Uses the vendored
    vector's own padded raw_body so the padding technique matches the bundle's own proof."""
    vector = load_json(next(iter(vectors_in("transport", "transport.001.json"))))
    raw = vector["raw_body"].encode("utf-8")
    assert len(raw) == vector["byte_length"] == 65536
    response = client.post("/business/v1/guest/answer", content=raw, headers=valid_headers)
    assert response.status_code != 413


def test_request_body_over_64kib_rejected(client, valid_headers):
    """transport.002"""
    vector = load_json(next(iter(vectors_in("transport", "transport.002.json"))))
    raw = vector["raw_body"].encode("utf-8")
    assert len(raw) == vector["byte_length"] == 65537
    response = client.post("/business/v1/guest/answer", content=raw, headers=valid_headers)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
