from __future__ import annotations

import pytest
from pydantic import ValidationError

from utopia_homes_prime.management_adapter.models import (
    ErrorBodyV1,
    HealthResponseV1,
    IdentityResponseV1,
    format_observed_at,
)


def test_identity_model_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        IdentityResponseV1(
            contract_version="1.0",
            observed_at="2026-09-15T20:15:30Z",
            synth_id="stoin:synth:utopia-homes-prime",
            realm_id="stoin:realm:utopia-homes",
            synth_class="business-prime",
            display_name="Utopia Homes Prime",
            extra_field="not allowed",  # type: ignore[call-arg]
        )


def test_identity_model_accepts_the_contract_example() -> None:
    model = IdentityResponseV1(
        contract_version="1.0",
        observed_at="2026-09-15T20:15:30Z",
        synth_id="stoin:synth:utopia-homes-prime",
        realm_id="stoin:realm:utopia-homes",
        synth_class="business-prime",
        display_name="Utopia Homes Prime",
    )
    assert model.display_name == "Utopia Homes Prime"


@pytest.mark.parametrize(
    ("status", "degraded", "reason_codes"),
    [
        ("healthy", ["guest.answer"], []),  # healthy requires empty degraded_capabilities
        ("degraded", [], ["dependency_unavailable"]),  # degraded requires non-empty
        ("unknown", [], []),  # unknown requires non-empty reason_codes
        ("unavailable", [], ["dependency_unavailable"]),  # empty I needs no_enabled_capabilities
    ],
)
def test_health_model_rejects_invalid_status_combinations(
    status: str, degraded: list[str], reason_codes: list[str]
) -> None:
    with pytest.raises(ValidationError):
        HealthResponseV1(
            contract_version="1.0",
            observed_at="2026-09-15T20:15:30Z",
            status=status,  # type: ignore[arg-type]
            management_provider_release_id="homes-management:release:test.1",
            degraded_capabilities=degraded,
            reason_codes=reason_codes,  # type: ignore[arg-type]
        )


def test_health_model_accepts_transitional_unknown() -> None:
    model = HealthResponseV1(
        contract_version="1.0",
        observed_at="2026-09-15T20:15:30Z",
        status="unknown",
        management_provider_release_id="homes-management:release:test.1",
        degraded_capabilities=[],
        reason_codes=["health_coverage_limited"],
    )
    assert model.status == "unknown"


def test_health_model_rejects_unspecified_alongside_another_code() -> None:
    with pytest.raises(ValidationError):
        HealthResponseV1(
            contract_version="1.0",
            observed_at="2026-09-15T20:15:30Z",
            status="unknown",
            management_provider_release_id="homes-management:release:test.1",
            degraded_capabilities=[],
            reason_codes=["unspecified", "health_coverage_limited"],
        )


def test_health_model_rejects_unsorted_degraded_capabilities() -> None:
    with pytest.raises(ValidationError):
        HealthResponseV1(
            contract_version="1.0",
            observed_at="2026-09-15T20:15:30Z",
            status="degraded",
            management_provider_release_id="homes-management:release:test.1",
            degraded_capabilities=["guest.answer", "guest.aaa"],
            reason_codes=["dependency_unavailable"],
        )


def test_error_body_retryable_must_match_code() -> None:
    with pytest.raises(ValidationError):
        ErrorBodyV1(
            code="invalid_request",
            message="The management request is invalid.",
            correlation_id="1eb6c24c-adfb-4512-987c-13ae318741bb",
            retryable=True,  # invalid_request is never retryable
        )


def test_format_observed_at_is_whole_second_with_trailing_z() -> None:
    from datetime import UTC, datetime

    dt = datetime(2026, 9, 15, 20, 15, 30, 123456, tzinfo=UTC)
    assert format_observed_at(dt) == "2026-09-15T20:15:30Z"
