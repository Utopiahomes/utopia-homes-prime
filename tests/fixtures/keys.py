"""Ephemeral, test-only Ed25519 keypair generation and JWT signing. Never used for anything but
local test fixtures — no production credentials are ever generated or stored by this module.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_ISSUER = "stoin:application:utopia-homes-web"
DEFAULT_SUBJECT = "stoin:service:utopia-homes-web-guest-adapter"
DEFAULT_AUDIENCE = "stoin:business:utopia-homes-prime"
DEFAULT_KID = "test-key-1"
DEFAULT_SCOPE = "guest.answer"


@dataclass(frozen=True, slots=True)
class TestKeypair:
    kid: str
    private_pem: str
    public_pem: str
    issuer: str
    subject: str
    audience: str


def generate_test_keypair(
    *,
    kid: str = DEFAULT_KID,
    issuer: str = DEFAULT_ISSUER,
    subject: str = DEFAULT_SUBJECT,
    audience: str = DEFAULT_AUDIENCE,
) -> TestKeypair:
    private_key = Ed25519PrivateKey.generate()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return TestKeypair(
        kid=kid,
        private_pem=private_pem,
        public_pem=public_pem,
        issuer=issuer,
        subject=subject,
        audience=audience,
    )


def sign_token(
    keypair: TestKeypair,
    *,
    scope: str = DEFAULT_SCOPE,
    lifetime_seconds: int = 280,
    iat_offset_seconds: int = 0,
    exp_offset_seconds: int | None = None,
    jti: str | None = None,
    algorithm: str = "EdDSA",
    issuer: str | None = None,
    subject: str | None = None,
    audience: str | None = None,
) -> str:
    now = int(time.time()) + iat_offset_seconds
    exp = (
        now + lifetime_seconds
        if exp_offset_seconds is None
        else int(time.time()) + exp_offset_seconds
    )
    claims = {
        "iss": issuer if issuer is not None else keypair.issuer,
        "sub": subject if subject is not None else keypair.subject,
        "aud": audience if audience is not None else keypair.audience,
        "scope": scope,
        "iat": now,
        "nbf": now,
        "exp": exp,
        "jti": jti or str(uuid.uuid4()),
    }
    return jwt.encode(
        claims, keypair.private_pem, algorithm=algorithm, headers={"kid": keypair.kid}
    )
