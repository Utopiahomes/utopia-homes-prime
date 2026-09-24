"""Exception hierarchy mirroring RC2 §17's 13-row error table exactly.

Every (http_status, message, retryable) triple below was cross-checked directly against the
vendored schemas/error.response.schema.json's per-code const pairs and
fixtures/consumer/retry-policy.json's http/auto_retry_permitted columns — not re-derived from
memory. No subclass accepts caller-supplied text: RC2 pins the exact generic message string per
code, so a subclass constructor that took free text would risk leaking implementation detail.
"""

from __future__ import annotations

from typing import ClassVar

from utopia_homes_prime.guest_answer.patterns import ERROR_CODE_VALUES

assert set(ERROR_CODE_VALUES) == {
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
}


class GuestAnswerError(Exception):
    code: ClassVar[str]
    http_status: ClassVar[int]
    retryable: ClassVar[bool]
    default_message: ClassVar[str]

    def __init__(self, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(self.default_message)
        self.retry_after_seconds = retry_after_seconds


class InvalidRequestError(GuestAnswerError):
    code = "invalid_request"
    http_status = 400
    retryable = False
    default_message = "The request is invalid."


class UnsupportedVersionError(GuestAnswerError):
    code = "unsupported_version"
    http_status = 400
    retryable = False
    default_message = "This request version is not supported."


class AuthenticationFailedError(GuestAnswerError):
    code = "authentication_failed"
    http_status = 401
    retryable = False
    default_message = "Authentication failed."


class CapabilityForbiddenError(GuestAnswerError):
    code = "capability_forbidden"
    http_status = 403
    retryable = False
    default_message = "This capability is not permitted."


class IdempotencyConflictError(GuestAnswerError):
    code = "idempotency_conflict"
    http_status = 409
    retryable = False
    default_message = "This request conflicts with an earlier request."


class RequestInProgressError(GuestAnswerError):
    code = "request_in_progress"
    http_status = 409
    retryable = True
    default_message = "This request is still in progress."


class IdempotencyRecoveryUnavailableError(GuestAnswerError):
    code = "idempotency_recovery_unavailable"
    http_status = 409
    retryable = False
    default_message = "The earlier response is no longer available."


class ResponseInvalidatedError(GuestAnswerError):
    code = "response_invalidated"
    http_status = 409
    retryable = False
    default_message = "The earlier response is no longer valid."


class RequestTooLargeError(GuestAnswerError):
    code = "request_too_large"
    http_status = 413
    retryable = False
    default_message = "The request is too large."


class RateLimitedError(GuestAnswerError):
    code = "rate_limited"
    http_status = 429
    retryable = True
    default_message = "Too many requests. Please try again shortly."


class TemporarilyUnavailableError(GuestAnswerError):
    code = "temporarily_unavailable"
    http_status = 503
    retryable = True
    default_message = "Lucy is temporarily unavailable. Please try again shortly."


class AnswerValidationFailedError(GuestAnswerError):
    """RC2 §17's load-bearing non-retryable-503 case (acceptance criterion 33): HTTP 503 normally
    reads as retryable, but this specific code must never be auto-retried."""

    code = "answer_validation_failed"
    http_status = 503
    retryable = False
    default_message = "Lucy could not produce a supported answer for this request."


class DeadlineExceededError(GuestAnswerError):
    code = "deadline_exceeded"
    http_status = 504
    retryable = True
    default_message = "Lucy could not respond within the allowed time."


ERROR_CLASSES: tuple[type[GuestAnswerError], ...] = (
    InvalidRequestError,
    UnsupportedVersionError,
    AuthenticationFailedError,
    CapabilityForbiddenError,
    IdempotencyConflictError,
    RequestInProgressError,
    IdempotencyRecoveryUnavailableError,
    ResponseInvalidatedError,
    RequestTooLargeError,
    RateLimitedError,
    TemporarilyUnavailableError,
    AnswerValidationFailedError,
    DeadlineExceededError,
)
assert {cls.code for cls in ERROR_CLASSES} == set(ERROR_CODE_VALUES)
