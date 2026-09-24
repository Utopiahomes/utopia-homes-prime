"""Structured access logging restricted to exactly the §15 allowlist.

§15 permits logging only: request id, provider correlation id, endpoint name, authenticated
service-principal id, HTTP response class, latency, contract version, synth id, opaque release
id, and safe health status/reason codes. Never a header dict, a JWT, or an exception object.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

_logger = logging.getLogger("utopia_homes_prime.management_adapter.access")


def access_log(
    *,
    endpoint: str,
    http_class: str,
    latency_ms: float,
    contract_version: str | None = None,
    synth_id: str | None = None,
    release_id: str | None = None,
    status: str | None = None,
    reason_codes: Sequence[str] | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    principal_id: str | None = None,
) -> None:
    fields = {
        "endpoint": endpoint,
        "http_class": http_class,
        "latency_ms": round(latency_ms, 2),
        "contract_version": contract_version,
        "synth_id": synth_id,
        "release_id": release_id,
        "status": status,
        "reason_codes": list(reason_codes) if reason_codes is not None else None,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "principal_id": principal_id,
    }
    _logger.info(json.dumps({k: v for k, v in fields.items() if v is not None}))
