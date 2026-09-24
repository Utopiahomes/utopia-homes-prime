from __future__ import annotations

import json

import pytest

from utopia_homes_prime.management_adapter.config import Config, ConfigError

_BASE_ENV = {
    "MANAGEMENT_ADAPTER_DEPLOYMENT_ID": "stoin:deployment:utopia-homes-management:test",
    "MANAGEMENT_ADAPTER_RUNTIME_ID": "stoin:runtime:utopia-homes-management:test-0001",
    "MANAGEMENT_ADAPTER_RELEASE_ID": "homes-management:release:test.1",
    "MANAGEMENT_ADAPTER_SOFTWARE_VERSION": "0.1.0",
    "MANAGEMENT_ADAPTER_ARTIFACT_DIGEST": "sha256:" + "0" * 64,
    "MANAGEMENT_ADAPTER_DEPLOYED_AT": "2026-09-15T19:45:00Z",
    "MANAGEMENT_ADAPTER_CAPABILITIES_JSON": json.dumps(
        [{"capability_id": "guest.answer", "contract_version": "1.0", "state": "enabled"}]
    ),
    "MANAGEMENT_ADAPTER_JWT_PUBLIC_KEYS_JSON": json.dumps(
        [
            {
                "kid": "test-key-1",
                "public_key_pem": "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n",
                "status": "active",
            }
        ]
    ),
}


def test_from_environment_happy_path() -> None:
    config = Config.from_environment(_BASE_ENV)
    assert config.deployment_id == "stoin:deployment:utopia-homes-management:test"
    assert config.release_id == "homes-management:release:test.1"
    assert config.capabilities[0].capability_id == "guest.answer"
    assert config.jwt_keys[0].kid == "test-key-1"
    assert config.port == 8080
    assert config.rate_limit_per_minute == 120


@pytest.mark.parametrize("missing", sorted(_BASE_ENV))
def test_from_environment_missing_required_var_fails_closed(missing: str) -> None:
    env = {k: v for k, v in _BASE_ENV.items() if k != missing}
    with pytest.raises(ConfigError):
        Config.from_environment(env)


def test_artifact_digest_must_match_pattern() -> None:
    env = dict(_BASE_ENV, MANAGEMENT_ADAPTER_ARTIFACT_DIGEST="not-a-digest")
    with pytest.raises(ConfigError):
        Config.from_environment(env)


def test_software_version_must_be_semver() -> None:
    env = dict(_BASE_ENV, MANAGEMENT_ADAPTER_SOFTWARE_VERSION="v0.1")
    with pytest.raises(ConfigError):
        Config.from_environment(env)


def test_duplicate_capability_id_rejected() -> None:
    env = dict(
        _BASE_ENV,
        MANAGEMENT_ADAPTER_CAPABILITIES_JSON=json.dumps(
            [
                {"capability_id": "guest.answer", "contract_version": "1.0", "state": "enabled"},
                {"capability_id": "guest.answer", "contract_version": "1.0", "state": "disabled"},
            ]
        ),
    )
    with pytest.raises(ConfigError, match="I-02"):
        Config.from_environment(env)


def test_duplicate_kid_rejected() -> None:
    env = dict(
        _BASE_ENV,
        MANAGEMENT_ADAPTER_JWT_PUBLIC_KEYS_JSON=json.dumps(
            [
                {
                    "kid": "k1",
                    "public_key_pem": "-----BEGIN PUBLIC KEY-----\nX\n-----END PUBLIC KEY-----",
                },
                {
                    "kid": "k1",
                    "public_key_pem": "-----BEGIN PUBLIC KEY-----\nY\n-----END PUBLIC KEY-----",
                },
            ]
        ),
    )
    with pytest.raises(ConfigError, match="duplicate kid"):
        Config.from_environment(env)


def test_rate_limit_below_contract_floor_rejected() -> None:
    env = dict(_BASE_ENV, MANAGEMENT_ADAPTER_RATE_LIMIT_PER_MINUTE="5")
    with pytest.raises(ConfigError):
        Config.from_environment(env)


def test_managed_synth_observed_release_id_optional() -> None:
    config = Config.from_environment(_BASE_ENV)
    assert config.managed_synth_observed_release_id is None

    env = dict(
        _BASE_ENV, MANAGEMENT_ADAPTER_MANAGED_SYNTH_OBSERVED_RELEASE_ID="homes-prime:release:1"
    )
    config = Config.from_environment(env)
    assert config.managed_synth_observed_release_id == "homes-prime:release:1"
