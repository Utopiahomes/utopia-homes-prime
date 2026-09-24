from __future__ import annotations

from collections.abc import Callable

import pytest
from fixtures.management.keys import Ed25519KeyPair, generate_test_keypair

from utopia_homes_prime.management_adapter.config import CapabilityConfig, Config, JwtAllowlistedKey

TEST_ARTIFACT_DIGEST = "sha256:" + "0" * 64


@pytest.fixture
def keypair() -> Ed25519KeyPair:
    return generate_test_keypair()


@pytest.fixture
def make_config(keypair: Ed25519KeyPair) -> Callable[..., Config]:
    def _make(**overrides: object) -> Config:
        defaults: dict[str, object] = {
            "environment": "test",
            "port": 0,
            "display_name": "Utopia Homes Prime",
            "deployment_id": "stoin:deployment:utopia-homes-management:test",
            "runtime_id": "stoin:runtime:utopia-homes-management:test-0001",
            "release_id": "homes-management:release:test.1",
            "software_version": "0.1.0",
            "artifact_digest": TEST_ARTIFACT_DIGEST,
            "deployed_at": "2026-09-15T19:45:00Z",
            "managed_synth_observed_release_id": None,
            "capabilities": (
                CapabilityConfig(
                    capability_id="guest.answer", contract_version="1.0", state="enabled"
                ),
            ),
            "jwt_keys": (
                JwtAllowlistedKey(
                    kid=keypair.kid, public_key_pem=keypair.public_key_pem, status="active"
                ),
            ),
            "rate_limit_per_minute": 120,
            "log_level": "INFO",
        }
        defaults.update(overrides)
        return Config(**defaults)  # type: ignore[arg-type]

    return _make
