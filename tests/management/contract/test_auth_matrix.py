"""Real-network JWT auth matrix (§18 criteria 5, 6, 19). Every failure variant must produce a
byte-identical error body (except correlation_id) — never revealing which check failed.
"""

from __future__ import annotations

import time

import httpx
import pytest
from fixtures.management.tokens import make_hs256_confusion_token, make_token

pytestmark = pytest.mark.contract


def _get(base_url: str, headers: dict[str, str]) -> httpx.Response:
    with httpx.Client(base_url=base_url, timeout=5) as client:
        return client.get("/management/v1/identity", headers=headers)


def _headers(
    token: str, request_id: str = "5a6a3c4e-1b2c-4d5e-89ab-1234567890ab"
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-Request-ID": request_id,
    }


def test_valid_token_succeeds(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    token = make_token(spawned_adapter.keypair.private_key, spawned_adapter.keypair.kid)
    response = _get(spawned_adapter.base_url, _headers(token))
    assert response.status_code == 200


@pytest.mark.parametrize(
    "build_headers_kwargs",
    [
        {"iss": "wrong-issuer"},
        {"sub": "wrong-subject"},
        {"aud": "wrong-audience"},
        {"scope": "wrong-scope"},
        {"jti": "not-a-uuid"},
        {"omit_claims": ("iss",)},
        {"omit_claims": ("sub",)},
        {"omit_claims": ("aud",)},
        {"omit_claims": ("iat",)},
        {"omit_claims": ("nbf",)},
        {"omit_claims": ("exp",)},
        {"omit_claims": ("jti",)},
    ],
)
def test_every_claim_failure_returns_identical_generic_401(
    spawned_adapter,
    build_headers_kwargs: dict[str, object],  # type: ignore[no-untyped-def]
) -> None:
    token = make_token(
        spawned_adapter.keypair.private_key, spawned_adapter.keypair.kid, **build_headers_kwargs
    )
    response = _get(spawned_adapter.base_url, _headers(token))
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "authentication_failed"
    assert body["error"]["message"] == "Authentication failed."
    assert body["error"]["retryable"] is False
    assert (
        "request_id" not in body["error"]
        or body["error"]["request_id"] == _headers(token)["X-Request-ID"]
    )


def test_all_failure_bodies_are_identical_except_correlation_id(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    kid = spawned_adapter.keypair.kid
    private_key = spawned_adapter.keypair.private_key
    variants = [
        make_token(private_key, kid, iss="wrong"),
        make_token(private_key, kid, sub="wrong"),
        make_token(private_key, kid, aud="wrong"),
        make_token(private_key, kid, scope="wrong"),
        make_token(private_key, "unknown-kid"),
        make_hs256_confusion_token(kid, spawned_adapter.keypair.public_key_pem.encode()),
    ]
    bodies = []
    for token in variants:
        response = _get(spawned_adapter.base_url, _headers(token))
        assert response.status_code == 401
        body = response.json()
        del body["error"]["correlation_id"]
        bodies.append(body)
    assert all(body == bodies[0] for body in bodies)


def test_expired_token_rejected(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    now = int(time.time())
    token = make_token(
        spawned_adapter.keypair.private_key,
        spawned_adapter.keypair.kid,
        iat=now - 600,
        nbf=now - 600,
        exp=now - 300,
    )
    response = _get(spawned_adapter.base_url, _headers(token))
    assert response.status_code == 401


def test_premature_token_rejected(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    now = int(time.time())
    token = make_token(
        spawned_adapter.keypair.private_key,
        spawned_adapter.keypair.kid,
        iat=now + 3600,
        nbf=now + 3600,
        exp=now + 3900,
    )
    response = _get(spawned_adapter.base_url, _headers(token))
    assert response.status_code == 401


def test_lifetime_exceeding_300_seconds_rejected(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    now = int(time.time())
    token = make_token(
        spawned_adapter.keypair.private_key, spawned_adapter.keypair.kid, iat=now, exp=now + 301
    )
    response = _get(spawned_adapter.base_url, _headers(token))
    assert response.status_code == 401


def test_accepted_at_exp_plus_30_boundary(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    # The live process uses real wall-clock time, so build a token whose exp+30 instant is a
    # couple of seconds in the future and sleep to it, rather than trying to fake the clock.
    now = int(time.time())
    iat = now
    exp = iat + 5  # short-lived so the test doesn't need a long sleep
    token = make_token(
        spawned_adapter.keypair.private_key, spawned_adapter.keypair.kid, iat=iat, nbf=iat, exp=exp
    )

    time.sleep(max(0.0, (exp + 30) - time.time()))
    response = _get(spawned_adapter.base_url, _headers(token))
    assert response.status_code == 200


def test_rejected_after_exp_plus_30_boundary(spawned_adapter) -> None:  # type: ignore[no-untyped-def]
    now = int(time.time())
    iat = now
    exp = iat + 5
    token = make_token(
        spawned_adapter.keypair.private_key, spawned_adapter.keypair.kid, iat=iat, nbf=iat, exp=exp
    )

    time.sleep(max(0.0, (exp + 31) - time.time()))
    response = _get(spawned_adapter.base_url, _headers(token))
    assert response.status_code == 401
