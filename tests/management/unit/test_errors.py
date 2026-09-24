from __future__ import annotations

import pytest

from utopia_homes_prime.management_adapter import errors

# The §13 table, verbatim, as the source of truth this test asserts every exception against.
_EXPECTED = {
    errors.InvalidRequestError: ("invalid_request", 400, False),
    errors.AuthenticationFailedError: ("authentication_failed", 401, False),
    errors.AuthorizationDeniedError: ("authorization_denied", 403, False),
    errors.MethodNotAllowedError: ("method_not_allowed", 405, False),
    errors.ContractNotSupportedError: ("contract_not_supported", 406, False),
    errors.RequestTooLargeError: ("request_too_large", 413, False),
    errors.RateLimitedError: ("rate_limited", 429, True),
    errors.InternalError: ("internal_error", 500, True),
    errors.ManagementUnavailableError: ("management_unavailable", 503, True),
}


@pytest.mark.parametrize(("exc_type", "expected"), _EXPECTED.items())
def test_exception_matches_section_13_table(
    exc_type: type[errors.ManagementAdapterError], expected: tuple[str, int, bool]
) -> None:
    code, http_status, retryable = expected
    exc = exc_type()
    assert exc.code == code
    assert exc.http_status == http_status
    assert exc.retryable == retryable
    assert exc.default_message  # non-empty, fixed generic text


def test_retry_after_seconds_is_optional_and_carried() -> None:
    exc = errors.RateLimitedError(retry_after_seconds=42)
    assert exc.retry_after_seconds == 42
    assert errors.RateLimitedError().retry_after_seconds is None


def test_every_error_code_in_section_13_is_covered() -> None:
    from utopia_homes_prime.management_adapter.patterns import ERROR_CODE_VALUES

    covered = {exc.code for exc in _EXPECTED}
    assert covered == set(ERROR_CODE_VALUES)
