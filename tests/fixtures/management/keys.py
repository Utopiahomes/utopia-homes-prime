"""Ed25519 keypair generation for tests only. Never used for production credentials."""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@dataclass(frozen=True, slots=True)
class Ed25519KeyPair:
    kid: str
    private_key: Ed25519PrivateKey
    public_key_pem: str


def generate_test_keypair(kid: str = "test-key-1") -> Ed25519KeyPair:
    private_key = Ed25519PrivateKey.generate()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return Ed25519KeyPair(kid=kid, private_key=private_key, public_key_pem=public_pem)
