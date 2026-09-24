"""Format rules copied verbatim from contracts/stoin-management-v1-bundle/schemas/common.defs.json.

Single source of truth shared by startup config validation (config.py) and the response
models (models.py), so the two layers cannot silently drift apart.
"""

from __future__ import annotations

OBSERVED_AT_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
"""§8 observed_at: whole-second precision, trailing Z. No fractional seconds."""

RFC3339_UTC_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$"
"""§11.2 deployed_at: UTC required; optional fractional seconds permitted (interpretation I-1)."""

PRINTABLE_ASCII_1_128_RE = r"^[\x20-\x7E]{1,128}$"
PRINTABLE_ASCII_1_160_RE = r"^[\x20-\x7E]{1,160}$"

UUID_V4_RE = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
"""Version nibble 4, variant nibble 8/9/a/b. Uppercase hex accepted (interpretation I-2)."""

CAPABILITY_ID_RE = r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$"
CAPABILITY_CONTRACT_VERSION_RE = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
MANAGEMENT_CONTRACT_VERSION_RE = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
SEMANTIC_VERSION_RE = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
ARTIFACT_DIGEST_RE = r"^sha256:[0-9a-f]{64}$"

HEALTH_STATUS_VALUES = ("healthy", "degraded", "unavailable", "unknown")
REASON_CODE_VALUES = (
    "capacity_limited",
    "configuration_error",
    "dependency_unavailable",
    "health_coverage_limited",
    "maintenance",
    "no_enabled_capabilities",
    "release_in_progress",
    "unspecified",
)
CAPABILITY_STATE_VALUES = ("enabled", "disabled", "deprecated")
ERROR_CODE_VALUES = (
    "invalid_request",
    "authentication_failed",
    "authorization_denied",
    "method_not_allowed",
    "contract_not_supported",
    "request_too_large",
    "rate_limited",
    "internal_error",
    "management_unavailable",
)
