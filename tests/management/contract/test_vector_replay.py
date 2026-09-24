"""Cross-checks live response *shape* (field names) against the vendored vectors, as an
independent oracle alongside raw schema validation — catches a field-name drift that could
otherwise coincidentally still satisfy the schema.
"""

from __future__ import annotations

import httpx
import pytest
from fixtures.management.bundle import iter_vector_files
from fixtures.management.tokens import make_token

pytestmark = pytest.mark.contract


def _headers(spawned_adapter) -> dict[str, str]:  # type: ignore[no-untyped-def]
    token = make_token(spawned_adapter.keypair.private_key, spawned_adapter.keypair.kid)
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-Request-ID": "5a6a3c4e-1b2c-4d5e-89ab-1234567890ab",
    }


def _find_vector(*, positive: bool, predicate) -> dict:  # type: ignore[no-untyped-def]
    for vector in iter_vector_files(positive=positive):
        if predicate(vector["document"]):
            return vector
    raise LookupError("no matching vendored vector found")


def test_live_authentication_failure_shape_matches_vendored_error_vector(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    vector = _find_vector(
        positive=True,
        predicate=lambda doc: doc.get("error", {}).get("code") == "authentication_failed",
    )

    headers = _headers(spawned_adapter)
    del headers["Authorization"]
    with httpx.Client(base_url=spawned_adapter.base_url, timeout=5) as client:
        response = client.get("/management/v1/identity", headers=headers)
    assert response.status_code == 401

    live_keys = set(response.json()["error"].keys())
    vector_keys = set(vector["document"]["error"].keys())
    # The live body may additionally include request_id (present here, since ours was valid);
    # every other field the vector declares must be present in the live body too.
    assert vector_keys - {"request_id"} <= live_keys


def test_live_transitional_health_shape_matches_vendored_positive_vector(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    vector = _find_vector(
        positive=True,
        predicate=lambda doc: (
            doc.get("status") == "unknown"
            and doc.get("reason_codes") == ["health_coverage_limited"]
            and doc.get("degraded_capabilities") == []
        ),
    )

    with httpx.Client(base_url=spawned_adapter.base_url, timeout=5) as client:
        response = client.get("/management/v1/health", headers=_headers(spawned_adapter))
    assert response.status_code == 200

    live_body = response.json()
    assert set(vector["document"].keys()) <= set(live_body.keys())
    assert live_body["status"] == vector["document"]["status"]
    assert live_body["reason_codes"] == vector["document"]["reason_codes"]
