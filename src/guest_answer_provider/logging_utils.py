"""Structured access logging restricted to exactly the RC2 §16 / fixtures/logs/privacy-safe-
telemetry.json allowlist. Never message content, answer text, source/label text, raw session_id,
a JWT or Authorization header, or an internal exception message/stack trace/hostname.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

_logger = logging.getLogger("guest_answer_provider.access")

_SESSION_DIGEST_KEY = os.environ.get(
    "GUEST_ANSWER_PROVIDER_SESSION_DIGEST_KEY", os.urandom(32).hex()
).encode("utf-8")
"""RC2 §16: session_id logging must be a one-way, environment-scoped-key digest, never the raw
value. Falls back to a process-lifetime-only random key when unset — acceptable for a preview
instance (digests just won't be stable across restarts); a real deployment must set this."""


def digest_session_id(session_id: str) -> str:
    mac = hmac.new(_SESSION_DIGEST_KEY, session_id.encode("utf-8"), hashlib.sha256)
    return f"sha256:{mac.hexdigest()}"


def digest_idempotency_key(idempotency_key: str) -> str:
    return f"sha256:{hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()}"


def access_log(
    *,
    endpoint: str,
    http_class: str,
    latency_ms: float,
    request_id: str | None = None,
    response_id: str | None = None,
    correlation_id: str | None = None,
    idempotency_key_digest: str | None = None,
    session_id_keyed_digest: str | None = None,
    outcome: str | None = None,
    error_category: str | None = None,
    principal_kid: str | None = None,
) -> None:
    fields = {
        "endpoint": endpoint,
        "http_class": http_class,
        "latency_ms": round(latency_ms, 2),
        "request_id": request_id,
        "response_id": response_id,
        "correlation_id": correlation_id,
        "idempotency_key_digest": idempotency_key_digest,
        "session_id_keyed_digest": session_id_keyed_digest,
        "outcome": outcome,
        "error_category": error_category,
        "principal_kid": principal_kid,
    }
    _logger.info(json.dumps({k: v for k, v in fields.items() if v is not None}))
