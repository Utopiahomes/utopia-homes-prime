"""stoin-service-jwt-v1 verification (RC3 §6).

Every failure mode collapses to one internal `AuthenticationFailure`. The real reason lives only
as `__cause__`/the exception message for internal logging (never returned to the caller) — §6.2
requires that authentication failures "return the same generic body and must not disclose which
check failed."
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from utopia_homes_prime.management_adapter import patterns
from utopia_homes_prime.management_adapter.config import (
    JWT_CLOCK_SKEW_SECONDS,
    JWT_MAX_LIFETIME_SECONDS,
    REQUIRED_AUDIENCE,
    REQUIRED_ISSUER,
    REQUIRED_SCOPE,
    REQUIRED_SUBJECT,
    JwtAllowlistedKey,
)

_JWT_ALGORITHM = "EdDSA"


class AuthenticationFailure(Exception):
    """Internal-only. Never let this message or its cause reach an HTTP response body."""


class KeyAllowlist:
    """kid -> active Ed25519 public key. Built once at startup from deployment config."""

    def __init__(self, keys: Sequence[JwtAllowlistedKey]) -> None:
        by_kid: dict[str, JwtAllowlistedKey] = {}
        for key in keys:
            if key.kid in by_kid:
                raise ValueError(f"duplicate kid {key.kid!r} in JWT key allowlist")
            by_kid[key.kid] = key
        self._by_kid = by_kid

    def active_public_key(self, kid: str) -> Ed25519PublicKey | None:
        entry = self._by_kid.get(kid)
        if entry is None or entry.status != "active":
            return None
        try:
            loaded = serialization.load_pem_public_key(entry.public_key_pem.encode("utf-8"))
        except ValueError:
            return None
        if not isinstance(loaded, Ed25519PublicKey):
            return None
        return loaded


def authenticate(
    authorization_header: str | None,
    *,
    allowlist: KeyAllowlist,
    now: datetime | None = None,
) -> str:
    """Raises AuthenticationFailure on any problem; on success returns the verified `kid`.

    The returned `kid` is safe to log (§15 allows "authenticated service-principal ID") and to
    key a rate limiter by. Order matches §6.2's list, but the caller must treat every failure
    branch identically — order only affects which internal diagnostic gets logged.
    """
    moment = now if now is not None else datetime.now(UTC)
    try:
        return _authenticate(authorization_header, allowlist=allowlist, moment=moment)
    except AuthenticationFailure:
        raise
    except Exception as exc:  # fail closed: any unexpected error is an auth failure, not a pass
        raise AuthenticationFailure("unexpected error during authentication") from exc


def _authenticate(
    authorization_header: str | None, *, allowlist: KeyAllowlist, moment: datetime
) -> str:
    if authorization_header is None:
        raise AuthenticationFailure("missing Authorization header")

    scheme, _, token = authorization_header.partition(" ")
    if scheme != "Bearer" or not token:
        raise AuthenticationFailure("malformed Authorization header")

    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise AuthenticationFailure("malformed JWT header") from exc

    if unverified_header.get("alg") != _JWT_ALGORITHM:
        raise AuthenticationFailure("unsupported or missing alg")

    kid = unverified_header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise AuthenticationFailure("missing kid")

    public_key = allowlist.active_public_key(kid)
    if public_key is None:
        raise AuthenticationFailure("unknown or inactive kid")

    try:
        # exp/nbf are verified manually below against `moment`, not wall-clock time, so that
        # the exact skew boundary (accept at exp+30, reject at exp+31) is deterministic and
        # testable rather than racing the real clock.
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=[_JWT_ALGORITHM],
            issuer=REQUIRED_ISSUER,
            audience=REQUIRED_AUDIENCE,
            options={
                "require": ["iss", "sub", "aud", "iat", "nbf", "exp", "jti"],
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
    except jwt.InvalidTokenError as exc:
        raise AuthenticationFailure("JWT claim verification failed") from exc

    # Claims PyJWT does not validate the value/type of once verify_exp/verify_nbf are disabled.
    iat = claims.get("iat")
    nbf = claims.get("nbf")
    exp = claims.get("exp")
    for name, value in (("iat", iat), ("nbf", nbf), ("exp", exp)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise AuthenticationFailure(f"{name} must be an integer")
    assert isinstance(iat, int) and isinstance(nbf, int) and isinstance(exp, int)

    now_epoch = int(moment.timestamp())
    if iat > now_epoch + JWT_CLOCK_SKEW_SECONDS:
        raise AuthenticationFailure("iat too far in the future")
    if nbf > now_epoch + JWT_CLOCK_SKEW_SECONDS:
        raise AuthenticationFailure("token is not yet valid (nbf)")
    if now_epoch > exp + JWT_CLOCK_SKEW_SECONDS:
        raise AuthenticationFailure("token has expired")
    if exp <= iat:
        raise AuthenticationFailure("exp must be after iat")
    if exp - iat > JWT_MAX_LIFETIME_SECONDS:
        raise AuthenticationFailure("token lifetime exceeds 300 seconds")

    if claims.get("sub") != REQUIRED_SUBJECT:
        raise AuthenticationFailure("wrong subject")
    if claims.get("scope") != REQUIRED_SCOPE:
        raise AuthenticationFailure("wrong scope")

    jti = claims.get("jti")
    if not isinstance(jti, str) or not re.fullmatch(patterns.UUID_V4_RE, jti):
        raise AuthenticationFailure("jti must be a UUID v4")

    return kid
