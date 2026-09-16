"""Replays every vendored auth.*.json and jti-replay.*.json vector directly against auth.py's
authenticate(), plus the capability_forbidden gate api.py applies right after it — the same
two-step check the live HTTP handler performs, exercised here at the unit level with a mocked
`now` so the exact iat/nbf/exp boundary vectors (e.g. exp+30 accept / exp+31 reject) are
deterministic instead of racing the real clock.
"""

from __future__ import annotations

from datetime import UTC, datetime

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from vector_helpers import load_json, vectors_in

from guest_answer_provider.auth import (
    REQUIRED_SCOPE,
    AuthenticationFailure,
    KeyAllowlist,
    authenticate,
)
from guest_answer_provider.config import JwtAllowlistedKey
from guest_answer_provider.jti_replay import JtiReplayStore


def _generate_keypair() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


# One fixed real Ed25519 keypair per canonical kid name used across the vendored vectors, built
# once for the whole module so `known-key-1` etc. mean the same key across every vector.
_KID_KEYPAIRS: dict[str, tuple[str, str]] = {
    kid: _generate_keypair() for kid in ("known-key-1", "restricted-key-1", "preview-key-1")
}
_DEFAULT_ISSUER = "stoin:application:utopia-homes-web"
_DEFAULT_SUBJECT = "stoin:service:utopia-homes-web-guest-adapter"
_DEFAULT_AUDIENCE = "stoin:business:utopia-homes-prime"


def _build_allowlist(vector: dict) -> KeyAllowlist:
    entries = []
    for kid, meta in vector["key_metadata"].items():
        _, public_pem = _KID_KEYPAIRS.setdefault(kid, _generate_keypair())
        entries.append(
            JwtAllowlistedKey(
                kid=kid,
                public_key_pem=public_pem,
                environment=meta["environment"],
                issuer=_DEFAULT_ISSUER,
                subject=_DEFAULT_SUBJECT,
                audience=_DEFAULT_AUDIENCE,
                capabilities=tuple(meta["capabilities"]),
                status="active",
            )
        )
    return KeyAllowlist(entries)


def _sign(vector: dict) -> str:
    header = vector["header"]
    kid = header["kid"]
    alg = header["alg"]
    claims = vector["claims"]

    if alg == "EdDSA":
        private_pem, _ = _KID_KEYPAIRS.setdefault(kid, _generate_keypair())
        return pyjwt.encode(claims, private_pem, algorithm="EdDSA", headers={"kid": kid})
    # Algorithm-confusion vectors (e.g. HS256): the signature is irrelevant since auth.py rejects
    # on the header's declared alg before ever verifying it against an allowlisted key.
    return pyjwt.encode(claims, "irrelevant-secret", algorithm=alg, headers={"kid": kid})


async def _authenticate_and_authorize(vector: dict, *, token: str) -> tuple[bool, str | None]:
    """Returns (accepted, error_code) mirroring api.py's authenticate-then-capability-check
    sequence. error_code is None on acceptance."""
    allowlist = _build_allowlist(vector)
    jti_store = JtiReplayStore()
    now = datetime.fromtimestamp(vector["evaluated_at"], UTC)
    try:
        principal = await authenticate(
            f"Bearer {token}",
            allowlist=allowlist,
            provider_environment=vector["serving_environment"],
            jti_replay_store=jti_store,
            now=now,
        )
    except AuthenticationFailure:
        return False, "authentication_failed"

    if REQUIRED_SCOPE not in principal.capabilities:
        return False, "capability_forbidden"
    return True, None


@pytest.mark.parametrize("path", vectors_in("auth", "auth.*.json"), ids=lambda p: p.stem)
async def test_auth_vector(path):
    vector = load_json(path)
    token = _sign(vector)
    accepted, error_code = await _authenticate_and_authorize(vector, token=token)

    if vector["expect"] == "accept":
        assert accepted, f"{path.name}: expected accept, got error {error_code!r}"
        assert vector["expect_error_code"] is None
    else:
        assert vector["expect"] == "reject"
        assert not accepted, f"{path.name}: expected reject, was accepted"
        assert error_code == vector["expect_error_code"], (
            f"{path.name}: expected error code {vector['expect_error_code']!r}, got {error_code!r}"
        )


@pytest.mark.parametrize("path", vectors_in("auth", "jti-replay.*.json"), ids=lambda p: p.stem)
async def test_jti_replay_sequence(path):
    vector = load_json(path)
    allowlist = KeyAllowlist(
        [
            JwtAllowlistedKey(
                kid="known-key-1",
                public_key_pem=_KID_KEYPAIRS.setdefault("known-key-1", _generate_keypair())[1],
                environment="production",
                issuer=_DEFAULT_ISSUER,
                subject=_DEFAULT_SUBJECT,
                audience=_DEFAULT_AUDIENCE,
                capabilities=("guest.answer",),
                status="active",
            )
        ]
    )
    jti_store = JtiReplayStore()

    for step in vector["sequence"]:
        private_pem, _ = _KID_KEYPAIRS[step["kid"]]
        token = pyjwt.encode(
            step["claims"], private_pem, algorithm="EdDSA", headers={"kid": step["kid"]}
        )
        now = datetime.fromtimestamp(step["evaluated_at"], UTC)
        if step["expect"] == "accept":
            principal = await authenticate(
                f"Bearer {token}",
                allowlist=allowlist,
                provider_environment="production",
                jti_replay_store=jti_store,
                now=now,
            )
            assert principal.kid == step["kid"]
        else:
            with pytest.raises(AuthenticationFailure):
                await authenticate(
                    f"Bearer {token}",
                    allowlist=allowlist,
                    provider_environment="production",
                    jti_replay_store=jti_store,
                    now=now,
                )
