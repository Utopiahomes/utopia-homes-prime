"""Transitional-mode health assembly (RC3 §10, §17, criterion 10, invariants I-04/I-05/I-07).

This adapter has no authoritative signal into the real `guest.answer` knowledge path, so it must
never report `status=healthy`. With E (enabled capabilities) empty, the only legal status is
`unavailable` + `no_enabled_capabilities`. With E non-empty, the only legal status here is
`unknown` + `health_coverage_limited` — never `healthy`, and (with a single enabled capability)
never `degraded` or `unavailable` either, since naming the sole capability as impaired would force
I = E and therefore require `unavailable` (§10's own note on why the allowance is inert here).
"""

from __future__ import annotations

from datetime import datetime

from utopia_homes_prime.management_adapter.config import Config
from utopia_homes_prime.management_adapter.models import HealthResponseV1, format_observed_at


def assemble_health(*, config: Config, observed_at: datetime) -> HealthResponseV1:
    enabled_ids = sorted(c.capability_id for c in config.capabilities if c.state == "enabled")

    if not enabled_ids:
        return HealthResponseV1(
            contract_version="1.0",
            observed_at=format_observed_at(observed_at),
            status="unavailable",
            management_provider_release_id=config.release_id,
            degraded_capabilities=[],
            reason_codes=["no_enabled_capabilities"],
        )

    return HealthResponseV1(
        contract_version="1.0",
        observed_at=format_observed_at(observed_at),
        status="unknown",
        management_provider_release_id=config.release_id,
        degraded_capabilities=[],
        reason_codes=["health_coverage_limited"],
    )
