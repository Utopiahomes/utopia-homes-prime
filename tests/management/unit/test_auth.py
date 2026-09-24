from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from fixtures.management.keys import Ed25519KeyPair
from fixtures.management.tokens import make_hs256_confusion_token, make_token

from utopia_homes_prime.management_adapter.auth import (
    AuthenticationFailure,
    KeyAllowlist,
    authenticate,
)
from utopia_homes_prime.management_adapter.config import JwtAllowlistedKey


@pytest.fixture
def allowlist(keypair: Ed25519KeyPair) -> KeyAllowlist:
    return KeyAllowlist(
        [JwtAllowlistedKey(kid=keypair.kid, public_key_pem=keypair.public_key_pem, status="active")]
    )


def _bearer(token: str) -> str:
    return f"Bearer {token}"


def test_valid_token_authenticates(keypair: Ed25519KeyPair, allowlist: KeyAllowlist) -> None:
    token = make_token(keypair.private_key, keypair.kid)
    kid = authenticate(_bearer(token), allowlist=allowlist)
    assert kid == keypair.kid


def test_missing_authorization_header_fails(allowlist: KeyAllowlist) -> None:
    with pytest.raises(AuthenticationFailure):
        authenticate(None, allowlist=allowlist)


def test_malformed_authorization_header_fails(allowlist: KeyAllowlist) -> None:
    with pytest.raises(AuthenticationFailure):
        authenticate("NotBearer abc", allowlist=allowlist)


def test_malformed_jwt_fails(allowlist: KeyAllowlist) -> None:
    with pytest.raises(AuthenticationFailure):
        authenticate(_bearer("not-a-jwt"), allowlist=allowlist)


def test_unknown_kid_fails(keypair: Ed25519KeyPair, allowlist: KeyAllowlist) -> None:
    token = make_token(keypair.private_key, "some-other-kid")
    with pytest.raises(AuthenticationFailure):
        authenticate(_bearer(token), allowlist=allowlist)


def test_revoked_key_fails(keypair: Ed25519KeyPair) -> None:
    allowlist = KeyAllowlist(
        [
            JwtAllowlistedKey(
                kid=keypair.kid, public_key_pem=keypair.public_key_pem, status="revoked"
            )
        ]
    )
    token = make_token(keypair.private_key, keypair.kid)
    with pytest.raises(AuthenticationFailure):
        authenticate(_bearer(token), allowlist=allowlist)


def test_wrong_algorithm_hs256_confusion_fails(
    keypair: Ed25519KeyPair, allowlist: KeyAllowlist
) -> None:
    # Classic algorithm-confusion attempt: sign with HS256 using the (public!) key PEM as secret.
    token = make_hs256_confusion_token(keypair.kid, keypair.public_key_pem.encode("ascii"))
    with pytest.raises(AuthenticationFailure):
        authenticate(_bearer(token), allowlist=allowlist)


@pytest.mark.parametrize(
    "kwargs",
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
def test_each_claim_failure_mode_is_rejected(
    keypair: Ed25519KeyPair, allowlist: KeyAllowlist, kwargs: dict[str, object]
) -> None:
    token = make_token(keypair.private_key, keypair.kid, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationFailure):
        authenticate(_bearer(token), allowlist=allowlist)


def test_expired_token_fails(keypair: Ed25519KeyPair, allowlist: KeyAllowlist) -> None:
    now = int(time.time())
    token = make_token(keypair.private_key, keypair.kid, iat=now - 600, exp=now - 300)
    with pytest.raises(AuthenticationFailure):
        authenticate(_bearer(token), allowlist=allowlist)


def test_premature_token_fails(keypair: Ed25519KeyPair, allowlist: KeyAllowlist) -> None:
    now = int(time.time())
    token = make_token(
        keypair.private_key, keypair.kid, iat=now + 3600, nbf=now + 3600, exp=now + 3900
    )
    with pytest.raises(AuthenticationFailure):
        authenticate(_bearer(token), allowlist=allowlist)


def test_iat_too_far_in_future_fails(keypair: Ed25519KeyPair, allowlist: KeyAllowlist) -> None:
    now = int(time.time())
    # nbf kept sane so only the iat-futurity check (our manual one) trips.
    token = make_token(keypair.private_key, keypair.kid, iat=now + 60, nbf=now, exp=now + 360)
    with pytest.raises(AuthenticationFailure):
        authenticate(_bearer(token), allowlist=allowlist)


def test_lifetime_exceeding_300_seconds_fails(
    keypair: Ed25519KeyPair, allowlist: KeyAllowlist
) -> None:
    now = int(time.time())
    token = make_token(keypair.private_key, keypair.kid, iat=now, exp=now + 301)
    with pytest.raises(AuthenticationFailure):
        authenticate(_bearer(token), allowlist=allowlist)


def test_accepted_exactly_at_exp_plus_30(keypair: Ed25519KeyPair, allowlist: KeyAllowlist) -> None:
    iat = 1_000_000
    exp = iat + 300
    token = make_token(keypair.private_key, keypair.kid, iat=iat, nbf=iat, exp=exp)
    moment = datetime.fromtimestamp(exp + 30, tz=UTC)
    kid = authenticate(_bearer(token), allowlist=allowlist, now=moment)
    assert kid == keypair.kid


def test_rejected_one_second_past_exp_plus_30(
    keypair: Ed25519KeyPair, allowlist: KeyAllowlist
) -> None:
    iat = 1_000_000
    exp = iat + 300
    token = make_token(keypair.private_key, keypair.kid, iat=iat, nbf=iat, exp=exp)
    moment = datetime.fromtimestamp(exp + 31, tz=UTC)
    with pytest.raises(AuthenticationFailure):
        authenticate(_bearer(token), allowlist=allowlist, now=moment)


def test_duplicate_kid_in_allowlist_rejected(keypair: Ed25519KeyPair) -> None:
    with pytest.raises(ValueError, match="duplicate kid"):
        KeyAllowlist(
            [
                JwtAllowlistedKey(kid=keypair.kid, public_key_pem=keypair.public_key_pem),
                JwtAllowlistedKey(kid=keypair.kid, public_key_pem=keypair.public_key_pem),
            ]
        )
