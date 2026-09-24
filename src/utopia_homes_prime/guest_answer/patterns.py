"""Format rules copied verbatim from
contracts/stoin-business-guest-answer-v1-bundle/schemas/common.defs.json.

Single source of truth shared by config validation, the request/response models, and the
legacy-upstream bridge, so these layers cannot silently drift apart from the frozen bundle.
"""

from __future__ import annotations

UUID_V4_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
"""RC2 §3: canonical lowercase RFC 4122 form only — unlike the Management Contract, uppercase
is NOT accepted here."""

SUBJECT_ID_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
NORMALIZED_PATH_RE = r"^/(?!/)(?!.*%3[Ff])(?!.*%23)[^?#:@]*$"
NAMESPACED_ID_RE = (
    r"^(?=[a-z0-9-]{1,32}:)[a-z][a-z0-9]*(?:-[a-z0-9]+)*:"
    r"(?=[a-z0-9-]{1,95}$)[a-z0-9]+(?:-[a-z0-9]+)*$"
)
HTTPS_URL_RE = r"^https://[^@/\s]+(/[^#\s]*)?$"
RELEASE_ID_RE = r"^[\x21-\x7e]{1,128}$"

SUBJECT_TYPE_VALUES = ("property", "destination", "service", "design", "none")
HISTORY_ROLE_VALUES = ("user", "assistant")
ACTION_KIND_VALUES = ("open_internal_link", "open_external_booking_link", "contact_utopia")
OUTCOME_VALUES = ("answered", "partial", "clarification_needed", "out_of_scope", "refused")
ERROR_CODE_VALUES = (
    "invalid_request",
    "unsupported_version",
    "authentication_failed",
    "capability_forbidden",
    "idempotency_conflict",
    "request_in_progress",
    "idempotency_recovery_unavailable",
    "response_invalidated",
    "request_too_large",
    "rate_limited",
    "temporarily_unavailable",
    "answer_validation_failed",
    "deadline_exceeded",
)

MESSAGE_CONTENT_MIN = 1
MESSAGE_CONTENT_MAX = 2000
HISTORY_MAX_ITEMS = 12
HISTORY_TOTAL_CHARS_MAX = 10_000
ANSWER_TEXT_MIN = 1
ANSWER_TEXT_MAX = 4000
SOURCE_TITLE_MAX = 120
ACTION_LABEL_MAX = 80
LIMITATION_TEXT_MAX = 240
SOURCES_MAX_ITEMS = 8
ACTIONS_MAX_ITEMS = 4
LIMITATIONS_MAX_ITEMS = 4

REQUEST_BODY_MAX_BYTES = 64 * 1024
RESPONSE_BODY_MAX_BYTES = 64 * 1024

JWT_CLOCK_SKEW_SECONDS = 30
JWT_MAX_LIFETIME_SECONDS = 300
