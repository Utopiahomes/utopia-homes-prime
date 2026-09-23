"""stoin-business-jwt-v1 verification (RC2 §7).

Extends the same shape as the Management Contract adapter's auth.py with what RC2 adds: per-key
(not per-contract-global) iss/sub/aud binding, production/preview environment separation
(auth.024/025 — both fold into the same generic AuthenticationFailure, since a wrong-environment
key is an identity-binding problem, not a capability grant problem), and jti replay rejection.
Every failure mode still collapses to one internal `AuthenticationFailure` — the real reason lives
only in the exception message/`__cause__` for internal logging, never in the response body.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from guest_answer_provider import patterns
from guest_answer_provider.config import Environment, JwtAllowlistedKey
from guest_answer_provider.jti_replay import JtiReplayStore

_JWT_ALGORITHM = "EdDSA"
REQUIRED_SCOPE = "guest.answer"
"""RC2 §7: the only capability this contract exposes. A scope mismatch is an authentication
failure (401) — distinct from CapabilityForbiddenError (403), which fires when the scope claim
is correct but the *key's own configured grant* (JwtAllowlistedKey.capabilities) excludes it."""


class AuthenticationFailure(Exception):
    """Internal-only. Never let this message or its cause reach an HTTP response body."""


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    kid: str
    environment: Environment
    subject: str
    capabilities: tuple[str, ...]


class KeyAllowlist:
    """kid -> active JwtAllowlistedKey. Built once at startup from deployment config."""

    def __init__(self, keys: Sequence[JwtAllowlistedKey]) -> None:
        by_kid: dict[str, JwtAllowlistedKey] = {}
        for key in keys:
            if key.kid in by_kid:
                raise ValueError(f"duplicate kid {key.kid!r} in JWT key allowlist")
            by_kid[key.kid] = key
        self._by_kid = by_kid

    def active_entry(self, kid: str) -> JwtAllowlistedKey | None:
        entry = self._by_kid.get(kid)
        if entry is None or entry.status != "active":
            return None
        return entry

    @staticmethod
    def public_key(entry: JwtAllowlistedKey) -> Ed25519PublicKey | None:
        try:
            loaded = serialization.load_pem_public_key(entry.public_key_pem.encode("utf-8"))
        except ValueError:
            return None
        if not isinstance(loaded, Ed25519PublicKey):
            return None
        return loaded


async def authenticate(
    authorization_header: str | None,
    *,
    allowlist: KeyAllowlist,
    provider_environment: Environment,
    jti_replay_store: JtiReplayStore,
    now: datetime | None = None,
    required_scope: str = REQUIRED_SCOPE,
) -> AuthenticatedPrincipal:
    """Raises AuthenticationFailure on any problem; on success returns the verified principal."""
    moment = now if now is not None else datetime.now(UTC)
    try:
        return await _authenticate(
            authorization_header,
            allowlist=allowlist,
            provider_environment=provider_environment,
            jti_replay_store=jti_replay_store,
            moment=moment,
            required_scope=required_scope,
        )
    except AuthenticationFailure:
        raise
    except Exception as exc:  # fail closed: any unexpected error is an auth failure, not a pass
        raise AuthenticationFailure("unexpected error during authentication") from exc


async def _authenticate(
    authorization_header: str | None,
    *,
    allowlist: KeyAllowlist,
    provider_environment: Environment,
    jti_replay_store: JtiReplayStore,
    moment: datetime,
    required_scope: str,
) -> AuthenticatedPrincipal:
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

    entry = allowlist.active_entry(kid)
    if entry is None:
        raise AuthenticationFailure("unknown or inactive kid")

    # auth.024/025: a key issued for one environment must never authenticate against the other.
    if entry.environment != provider_environment:
        raise AuthenticationFailure("key environment does not match provider environment")

    public_key = allowlist.public_key(entry)
    if public_key is None:
        raise AuthenticationFailure("could not load public key for kid")

    try:
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=[_JWT_ALGORITHM],
            issuer=entry.issuer,
            audience=entry.audience,
            options={
                "require": ["iss", "sub", "aud", "iat", "nbf", "exp", "jti"],
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
    except jwt.InvalidTokenError as exc:
        raise AuthenticationFailure("JWT claim verification failed") from exc

    iat = claims.get("iat")
    nbf = claims.get("nbf")
    exp = claims.get("exp")
    for name, value in (("iat", iat), ("nbf", nbf), ("exp", exp)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise AuthenticationFailure(f"{name} must be an integer")
    assert isinstance(iat, int) and isinstance(nbf, int) and isinstance(exp, int)

    now_epoch = int(moment.timestamp())
    if nbf != iat:
        # RC2 §7.2: nbf must equal iat exactly — unlike the Management Contract's looser
        # "not yet valid" skew check, RC2 gives nbf no independent meaning of its own.
        raise AuthenticationFailure("nbf must equal iat")
    if iat > now_epoch + patterns.JWT_CLOCK_SKEW_SECONDS:
        raise AuthenticationFailure("iat too far in the future")
    if nbf > now_epoch + patterns.JWT_CLOCK_SKEW_SECONDS:
        raise AuthenticationFailure("token is not yet valid (nbf)")
    if now_epoch > exp + patterns.JWT_CLOCK_SKEW_SECONDS:
        raise AuthenticationFailure("token has expired")
    if exp <= iat:
        raise AuthenticationFailure("exp must be after iat")
    if exp - iat > patterns.JWT_MAX_LIFETIME_SECONDS:
        raise AuthenticationFailure("token lifetime exceeds 300 seconds")

    if claims.get("sub") != entry.subject:
        raise AuthenticationFailure("wrong subject")
    # Exactly one capability per token: a meeting.assist token never also carries guest.answer.
    if claims.get("scope") != required_scope:
        raise AuthenticationFailure("wrong scope")

    jti = claims.get("jti")
    if not isinstance(jti, str) or not re.fullmatch(patterns.UUID_V4_RE, jti):
        raise AuthenticationFailure("jti must be a UUID v4")

    is_new = await jti_replay_store.check_and_record(jti)
    if not is_new:
        raise AuthenticationFailure("jti replay detected")

    return AuthenticatedPrincipal(
        kid=kid,
        environment=entry.environment,
        subject=entry.subject,
        capabilities=entry.capabilities,
    )
