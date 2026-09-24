from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from utopia_homes_prime.management_adapter.config import CapabilityConfig, Config
from utopia_homes_prime.management_adapter.health import assemble_health


def test_zero_enabled_capabilities_is_unavailable(make_config: Callable[..., Config]) -> None:
    config = make_config(
        capabilities=(
            CapabilityConfig(
                capability_id="guest.answer", contract_version="1.0", state="disabled"
            ),
        )
    )
    result = assemble_health(config=config, observed_at=datetime.now(UTC))
    assert result.status == "unavailable"
    assert result.degraded_capabilities == []
    assert result.reason_codes == ["no_enabled_capabilities"]


def test_one_enabled_capability_is_transitional_unknown(make_config: Callable[..., Config]) -> None:
    config = make_config(
        capabilities=(
            CapabilityConfig(capability_id="guest.answer", contract_version="1.0", state="enabled"),
        )
    )
    result = assemble_health(config=config, observed_at=datetime.now(UTC))
    assert result.status == "unknown"
    assert result.degraded_capabilities == []
    assert result.reason_codes == ["health_coverage_limited"]


def test_release_id_matches_config_release_id(make_config: Callable[..., Config]) -> None:
    config = make_config(release_id="homes-management:release:distinct.7")
    result = assemble_health(config=config, observed_at=datetime.now(UTC))
    assert result.management_provider_release_id == "homes-management:release:distinct.7"


def test_never_reports_healthy(make_config: Callable[..., Config]) -> None:
    config = make_config(
        capabilities=(
            CapabilityConfig(capability_id="guest.answer", contract_version="1.0", state="enabled"),
            CapabilityConfig(capability_id="guest.book", contract_version="1.0", state="enabled"),
        )
    )
    result = assemble_health(config=config, observed_at=datetime.now(UTC))
    assert result.status != "healthy"
