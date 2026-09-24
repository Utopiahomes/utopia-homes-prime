"""Signed-JWT builder for tests. Deliberately flexible so tests can construct invalid tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from utopia_homes_prime.management_adapter.config import (
    REQUIRED_AUDIENCE,
    REQUIRED_ISSUER,
    REQUIRED_SCOPE,
    REQUIRED_SUBJECT,
)


def make_token(
    private_key: Ed25519PrivateKey,
    kid: str,
    *,
    iss: str | None = REQUIRED_ISSUER,
    sub: str | None = REQUIRED_SUBJECT,
    aud: str | None = REQUIRED_AUDIENCE,
    scope: str | None = REQUIRED_SCOPE,
    iat: int | None = None,
    nbf: int | None = None,
    exp: int | None = None,
    jti: str | None = None,
    algorithm: str = "EdDSA",
    signing_key: Any = None,
    headers: dict[str, Any] | None = None,
    omit_claims: tuple[str, ...] = (),
) -> str:
    """Build a JWT. Defaults produce a fully valid `stoin-service-jwt-v1` token; every claim
    can be overridden with a bad value, or dropped entirely via `omit_claims`, so the auth test
    matrix can construct exactly the invalid token each failure case needs."""
    now = int(time.time())
    resolved_iat = now if iat is None else iat
    resolved_nbf = resolved_iat if nbf is None else nbf
    resolved_exp = resolved_iat + 300 if exp is None else exp
    resolved_jti = str(uuid4()) if jti is None else jti

    all_claims: dict[str, Any] = {
        "iss": iss,
        "sub": sub,
        "aud": aud,
        "scope": scope,
        "iat": resolved_iat,
        "nbf": resolved_nbf,
        "exp": resolved_exp,
        "jti": resolved_jti,
    }
    claims = {k: v for k, v in all_claims.items() if k not in omit_claims}

    key = signing_key if signing_key is not None else private_key
    token_headers = {"kid": kid}
    if headers:
        token_headers.update(headers)

    return jwt.encode(claims, key, algorithm=algorithm, headers=token_headers)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_hs256_confusion_token(kid: str, hmac_secret: bytes, **claim_overrides: Any) -> str:
    """Hand-crafts an HS256-signed token using the given bytes (e.g. a public key's PEM) as the
    HMAC secret. PyJWT's own `encode()` refuses to build this (it detects PEM/DER/SSH key
    material and blocks it as a known algorithm-confusion vector), which is a real defense but
    means the attack has to be assembled by hand to verify our *verification* side rejects it
    too, in case a differently-encoded key ever slipped past that check."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": REQUIRED_ISSUER,
        "sub": REQUIRED_SUBJECT,
        "aud": REQUIRED_AUDIENCE,
        "scope": REQUIRED_SCOPE,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "jti": str(uuid4()),
    }
    claims.update(claim_overrides)
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}

    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    signature = hmac.new(hmac_secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"
