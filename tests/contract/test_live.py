"""Real two-process (three, counting the fake legacy upstream) network HTTP contract tests —
mirrors the Management Contract adapter's contract-test layer, extended here to also prove the
idempotency five-branch table and the answer_validation_failed non-retry rule over a genuine
network round trip, not an in-process TestClient.
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest
from fixtures.keys import sign_token

pytestmark = pytest.mark.contract


def _headers(keypair, *, token=None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token or sign_token(keypair)}",
        "X-Request-ID": str(uuid.uuid4()),
        "Idempotency-Key": str(uuid.uuid4()),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _body() -> dict:
    return {
        "contract_version": "1.0",
        "session_id": str(uuid.uuid4()),
        "message": {"turn_id": str(uuid.uuid4()), "content": "What are your check-in times?"},
        "locale": "en-US",
    }


def test_healthz(spawned_provider):
    response = httpx.get(f"{spawned_provider.base_url}/healthz", timeout=5)
    assert response.status_code == 200


def test_happy_path_over_real_network(spawned_provider):
    headers = _headers(spawned_provider.keypair)
    response = httpx.post(
        f"{spawned_provider.base_url}/business/v1/guest/answer",
        json=_body(),
        headers=headers,
        timeout=15,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "answered"
    assert response.headers["X-Request-ID"] == headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"


def test_invalid_token_rejected_over_real_network(spawned_provider):
    headers = _headers(spawned_provider.keypair, token="not-a-real-token")
    response = httpx.post(
        f"{spawned_provider.base_url}/business/v1/guest/answer",
        json=_body(),
        headers=headers,
        timeout=15,
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_idempotency_replay_over_real_network(spawned_provider):
    headers = _headers(spawned_provider.keypair)
    body = _body()

    first = httpx.post(
        f"{spawned_provider.base_url}/business/v1/guest/answer",
        json=body,
        headers=headers,
        timeout=15,
    )
    assert first.status_code == 200

    retry_headers = dict(headers)
    retry_headers["Authorization"] = f"Bearer {sign_token(spawned_provider.keypair)}"
    retry_headers["X-Request-ID"] = str(uuid.uuid4())
    second = httpx.post(
        f"{spawned_provider.base_url}/business/v1/guest/answer",
        json=body,
        headers=retry_headers,
        timeout=15,
    )
    assert second.status_code == 200
    assert second.json()["response_id"] == first.json()["response_id"]


def test_idempotency_conflict_over_real_network(spawned_provider):
    headers = _headers(spawned_provider.keypair)
    body = _body()
    first = httpx.post(
        f"{spawned_provider.base_url}/business/v1/guest/answer",
        json=body,
        headers=headers,
        timeout=15,
    )
    assert first.status_code == 200

    different_body = _body()
    different_body["message"] = {"turn_id": str(uuid.uuid4()), "content": "A different question."}
    conflict_headers = dict(headers)
    conflict_headers["Authorization"] = f"Bearer {sign_token(spawned_provider.keypair)}"
    conflict_headers["X-Request-ID"] = str(uuid.uuid4())
    second = httpx.post(
        f"{spawned_provider.base_url}/business/v1/guest/answer",
        json=different_body,
        headers=conflict_headers,
        timeout=15,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"


def test_request_in_progress_over_real_network(spawned_provider_with_slow_upstream):
    """A concurrent second request under the same Idempotency-Key, while the first is still
    in-flight against a deliberately slow (2s) legacy upstream, must see request_in_progress."""
    import concurrent.futures

    headers = _headers(spawned_provider_with_slow_upstream.keypair)
    body = _body()

    def first_call():
        return httpx.post(
            f"{spawned_provider_with_slow_upstream.base_url}/business/v1/guest/answer",
            json=body,
            headers=headers,
            timeout=15,
        )

    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(first_call)
        time.sleep(0.3)  # let the first request be admitted before the second arrives
        second_headers = dict(headers)
        second_headers["Authorization"] = (
            f"Bearer {sign_token(spawned_provider_with_slow_upstream.keypair)}"
        )
        second_headers["X-Request-ID"] = str(uuid.uuid4())
        second = httpx.post(
            f"{spawned_provider_with_slow_upstream.base_url}/business/v1/guest/answer",
            json=body,
            headers=second_headers,
            timeout=15,
        )
        first = future.result()

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "request_in_progress"


def test_answer_validation_failed_is_not_auto_retried_by_a_conformant_client(spawned_provider):
    """Cross-checks fixtures/consumer/{retry-policy,answer-validation-failed-non-retry-test}.json
    against a live 501-char-content call: the response is 503, but retryable=false, so a
    conformant client's retry logic (not exercised here — this just proves the server-side half)
    must never auto-retry it despite the misleading HTTP status."""
    headers = _headers(spawned_provider.keypair)
    body = _body()
    body["message"] = {"turn_id": str(uuid.uuid4()), "content": "x" * 501}

    response = httpx.post(
        f"{spawned_provider.base_url}/business/v1/guest/answer",
        json=body,
        headers=headers,
        timeout=15,
    )
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "answer_validation_failed"
    assert error["retryable"] is False
