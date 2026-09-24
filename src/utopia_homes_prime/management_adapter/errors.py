"""Exception hierarchy mirroring the §13 error table exactly.

Each subclass carries its fixed HTTP status, error code, retryable flag, and generic message
text verbatim from the contract. No subclass accepts caller-supplied text — that is what keeps
an authentication failure and an internal error from ever leaking which check actually failed.
"""

from __future__ import annotations

from typing import ClassVar

from utopia_homes_prime.management_adapter.models import ErrorCode


class ManagementAdapterError(Exception):
    code: ClassVar[ErrorCode]
    http_status: ClassVar[int]
    retryable: ClassVar[bool]
    default_message: ClassVar[str]

    def __init__(self, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(self.default_message)
        self.retry_after_seconds = retry_after_seconds


class InvalidRequestError(ManagementAdapterError):
    code = "invalid_request"
    http_status = 400
    retryable = False
    default_message = "The management request is invalid."


class AuthenticationFailedError(ManagementAdapterError):
    code = "authentication_failed"
    http_status = 401
    retryable = False
    default_message = "Authentication failed."


class AuthorizationDeniedError(ManagementAdapterError):
    code = "authorization_denied"
    http_status = 403
    retryable = False
    default_message = "The authenticated principal is not authorized for this resource."


class MethodNotAllowedError(ManagementAdapterError):
    code = "method_not_allowed"
    http_status = 405
    retryable = False
    default_message = "This resource only supports GET."


class ContractNotSupportedError(ManagementAdapterError):
    code = "contract_not_supported"
    http_status = 406
    retryable = False
    default_message = "The requested contract representation is not supported."


class RequestTooLargeError(ManagementAdapterError):
    code = "request_too_large"
    http_status = 413
    retryable = False
    default_message = "The request exceeds configured size limits."


class RateLimitedError(ManagementAdapterError):
    code = "rate_limited"
    http_status = 429
    retryable = True
    default_message = "The management reader has exceeded its request rate."


class InternalError(ManagementAdapterError):
    code = "internal_error"
    http_status = 500
    retryable = True
    default_message = "The provider could not assemble a valid response."


class ManagementUnavailableError(ManagementAdapterError):
    code = "management_unavailable"
    http_status = 503
    retryable = True
    default_message = "The management adapter cannot currently report state."
