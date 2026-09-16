"""RFC 8785 JCS canonicalization + SHA-256, for idempotency's canonical-request-identity check.

Uses `rfc8785`, the same pinned library (see requirements.lock) the Tier A bundle itself
validated against the official RFC 8785 reference vectors and cross-checked with an independent
from-scratch implementation — never hand-rolled, per RC2 §11.2's explicit warning.
"""

from __future__ import annotations

import hashlib
from typing import Any

import rfc8785


def canonical_digest(request_body: dict[str, Any]) -> bytes:
    """Returns the SHA-256 of the RFC 8785 canonical bytes of `request_body`.

    Caller must pass the already-schema-validated dict with only `message.content` trimmed and
    every omitted-vs-null distinction preserved exactly as parsed off the wire (RC2 §11.2) — this
    function does no validation or normalization of its own.
    """
    return hashlib.sha256(rfc8785.dumps(request_body)).digest()


def canonical_digest_hex(request_body: dict[str, Any]) -> str:
    return canonical_digest(request_body).hex()
