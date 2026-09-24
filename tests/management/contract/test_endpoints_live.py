"""Live-endpoint replay against the vendored schemas plus invariants I-01/I-02/I-03/I-05/I-06/I-07
(§14's provider-side obligation, run over a real network hop rather than in-process).
"""

from __future__ import annotations

import httpx
import pytest
from fixtures.management.bundle import (
    check_i01_sorted,
    check_i02_unique_capability_ids,
    check_i03_release_id_match,
    check_i04_i06_i07,
    check_i05_transitional,
    validate,
)
from fixtures.management.tokens import make_token

pytestmark = pytest.mark.contract


def _client(spawned_adapter) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(base_url=spawned_adapter.base_url, timeout=5)


def _headers(
    spawned_adapter, request_id: str = "5a6a3c4e-1b2c-4d5e-89ab-1234567890ab"
) -> dict[str, str]:  # type: ignore[no-untyped-def]
    token = make_token(spawned_adapter.keypair.private_key, spawned_adapter.keypair.kid)
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-Request-ID": request_id,
    }


def test_all_four_endpoints_return_schema_valid_bodies(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    with _client(spawned_adapter) as client:
        for path, schema_name in [
            ("/management/v1/identity", "identity.response"),
            ("/management/v1/health", "health.response"),
            ("/management/v1/version", "version.response"),
            ("/management/v1/capabilities", "capabilities.response"),
        ]:
            response = client.get(path, headers=_headers(spawned_adapter))
            assert response.status_code == 200
            validate(response.json(), schema_name)


def test_identity_returns_exact_homes_ids(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    with _client(spawned_adapter) as client:
        body = client.get("/management/v1/identity", headers=_headers(spawned_adapter)).json()
    assert body["synth_id"] == "stoin:synth:utopia-homes-prime"
    assert body["realm_id"] == "stoin:realm:utopia-homes"


def test_version_returns_exact_managed_synth_id(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    with _client(spawned_adapter) as client:
        body = client.get("/management/v1/version", headers=_headers(spawned_adapter)).json()
    assert body["managed_synth"]["synth_id"] == "stoin:synth:utopia-homes-prime"


def test_invariants_hold_across_the_live_pair(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    with _client(spawned_adapter) as client:
        health = client.get("/management/v1/health", headers=_headers(spawned_adapter)).json()
        version = client.get("/management/v1/version", headers=_headers(spawned_adapter)).json()
        capabilities = client.get(
            "/management/v1/capabilities", headers=_headers(spawned_adapter)
        ).json()

    check_i01_sorted(health)
    check_i01_sorted(capabilities)
    check_i01_sorted(version)
    check_i02_unique_capability_ids(capabilities)
    check_i03_release_id_match(health, version)
    check_i04_i06_i07(health, capabilities)
    check_i05_transitional(health, capabilities)


def test_identity_stable_across_repeated_requests(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    """§18 criterion 7: identity remains stable across requests to the same running process."""
    with _client(spawned_adapter) as client:
        first = client.get("/management/v1/identity", headers=_headers(spawned_adapter)).json()
        second = client.get("/management/v1/identity", headers=_headers(spawned_adapter)).json()
    for key in ("synth_id", "realm_id", "synth_class", "display_name"):
        assert first[key] == second[key]
