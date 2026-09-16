"""Pydantic mirrors of schemas/{response,error.response}.schema.json, field-for-field.

Request bodies are validated directly against the vendored JSON Schema (schema_validation.py) so
the wire-parsed dict's omitted-vs-null shape is preserved exactly for canonicalization — see
docs/implementation-notes.md. Pydantic is used only for constructing outgoing success/error
bodies: a second line of defense so production code can never serialize a non-conforming response.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from guest_answer_provider import patterns
from guest_answer_provider.errors import ERROR_CLASSES

_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)

ContractVersion = Literal["1.0"]
Outcome = Literal["answered", "partial", "clarification_needed", "out_of_scope", "refused"]
ActionKind = Literal["open_internal_link", "open_external_booking_link", "contact_utopia"]
ErrorCode = Literal[
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
]

_ERROR_CODE_TABLE = {cls.code: (cls.default_message, cls.retryable) for cls in ERROR_CLASSES}

_NAMESPACED_ID_COMPILED = re.compile(patterns.NAMESPACED_ID_RE)


def _validate_namespaced_id(value: str) -> str:
    # pydantic-core's regex engine (Rust) does not support lookaround, which
    # patterns.NAMESPACED_ID_RE uses for its 32/95 sub-bounds — validated with Python's `re`
    # instead of Field(pattern=...).
    if not _NAMESPACED_ID_COMPILED.fullmatch(value):
        raise ValueError(f"{value!r} is not a valid namespaced id")
    return value


class SourceV1(BaseModel):
    model_config = _MODEL_CONFIG

    source_id: str
    title: str = Field(min_length=1, max_length=patterns.SOURCE_TITLE_MAX)
    url: str = Field(pattern=patterns.HTTPS_URL_RE, max_length=2048)

    _validate_source_id = field_validator("source_id")(_validate_namespaced_id)


class ActionV1(BaseModel):
    model_config = _MODEL_CONFIG

    action_id: str
    kind: ActionKind
    label: str = Field(min_length=1, max_length=patterns.ACTION_LABEL_MAX)

    _validate_action_id = field_validator("action_id")(_validate_namespaced_id)
    url: str = Field(pattern=patterns.HTTPS_URL_RE, max_length=2048)


class GuestAnswerResponseV1(BaseModel):
    model_config = _MODEL_CONFIG

    contract_version: ContractVersion
    response_id: str = Field(pattern=patterns.UUID_V4_RE)
    session_id: str = Field(pattern=patterns.UUID_V4_RE)
    assistant_turn_id: str = Field(pattern=patterns.UUID_V4_RE)
    outcome: Outcome
    answer: str = Field(min_length=patterns.ANSWER_TEXT_MIN, max_length=patterns.ANSWER_TEXT_MAX)
    sources: list[SourceV1] = Field(max_length=patterns.SOURCES_MAX_ITEMS)
    actions: list[ActionV1] = Field(max_length=patterns.ACTIONS_MAX_ITEMS)
    limitations: list[str] = Field(max_length=patterns.LIMITATIONS_MAX_ITEMS)

    @model_validator(mode="after")
    def _check_invariants(self) -> GuestAnswerResponseV1:
        # I-B05: no duplicate source_id or url within sources.
        source_ids = [s.source_id for s in self.sources]
        source_urls = [s.url for s in self.sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("duplicate source_id within sources (I-B05)")
        if len(set(source_urls)) != len(source_urls):
            raise ValueError("duplicate url within sources (I-B05)")

        # I-B06: no duplicate action_id or url within actions.
        action_ids = [a.action_id for a in self.actions]
        action_urls = [a.url for a in self.actions]
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("duplicate action_id within actions (I-B06)")
        if len(set(action_urls)) != len(action_urls):
            raise ValueError("duplicate url within actions (I-B06)")

        for limitation in self.limitations:
            if not (1 <= len(limitation) <= patterns.LIMITATION_TEXT_MAX):
                raise ValueError(f"limitation text out of bounds: {limitation!r}")

        return self


class ErrorBodyV1(BaseModel):
    model_config = _MODEL_CONFIG

    code: ErrorCode
    message: str = Field(min_length=1)
    correlation_id: str = Field(pattern=patterns.UUID_V4_RE)
    retryable: bool

    @model_validator(mode="after")
    def _check_exact_pinned_values(self) -> ErrorBodyV1:
        expected_message, expected_retryable = _ERROR_CODE_TABLE[self.code]
        if self.message != expected_message:
            raise ValueError(f"message for {self.code!r} must be the exact pinned string")
        if self.retryable != expected_retryable:
            raise ValueError(f"retryable for {self.code!r} must be {expected_retryable}")
        return self


class ErrorResponseV1(BaseModel):
    model_config = _MODEL_CONFIG

    contract_version: ContractVersion
    error: ErrorBodyV1
