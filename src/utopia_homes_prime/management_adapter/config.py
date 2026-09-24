"""Deployment-owned static configuration for the transitional adapter (RC3 §17).

§17 permits this adapter to "use deployment-owned static identity/version/capability
configuration" instead of reading live state from a Homes Prime runtime that does not yet
exist independently. Every value the contract fixes exactly (synth_id, realm_id, synth_class)
is a module-level constant, never an environment variable — there is nothing to configure.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from utopia_homes_prime.management_adapter import patterns

SYNTH_ID: Final = "stoin:synth:utopia-homes-prime"
REALM_ID: Final = "stoin:realm:utopia-homes"
SYNTH_CLASS: Final = "business-prime"

REQUIRED_ISSUER: Final = "stoin:control"
REQUIRED_SUBJECT: Final = "stoin:service:control-management-reader"
REQUIRED_AUDIENCE: Final = "stoin:management:utopia-homes-prime"
REQUIRED_SCOPE: Final = "management.read"

JWT_CLOCK_SKEW_SECONDS: Final = 30
"""§6.3: at most 30 seconds of symmetric clock skew on iat/nbf/exp."""

JWT_MAX_LIFETIME_SECONDS: Final = 300
"""§6.3: exp must be no more than 300 seconds after iat."""

MINIMUM_RATE_LIMIT_PER_MINUTE: Final = 12
"""§13: the provider SHOULD permit at least 12 requests per minute from one principal."""

_ENV_PREFIX = "MANAGEMENT_ADAPTER_"

CapabilityState = Literal["enabled", "disabled", "deprecated"]
KeyStatus = Literal["active", "retired", "revoked"]


class ConfigError(ValueError):
    """Raised when startup configuration is missing or malformed. Callers must fail closed."""


@dataclass(frozen=True, slots=True)
class CapabilityConfig:
    capability_id: str
    contract_version: str
    state: CapabilityState
    business_contract_id: str | None = None


@dataclass(frozen=True, slots=True)
class JwtAllowlistedKey:
    kid: str
    public_key_pem: str
    status: KeyStatus = "active"


@dataclass(frozen=True, slots=True)
class Config:
    environment: str
    port: int
    display_name: str
    deployment_id: str
    runtime_id: str
    release_id: str
    software_version: str
    artifact_digest: str
    deployed_at: str
    managed_synth_observed_release_id: str | None
    capabilities: tuple[CapabilityConfig, ...]
    jwt_keys: tuple[JwtAllowlistedKey, ...]
    rate_limit_per_minute: int
    log_level: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> Config:
        import os

        env = environment if environment is not None else os.environ

        def require(name: str) -> str:
            value = env.get(f"{_ENV_PREFIX}{name}")
            if value is None or value == "":
                raise ConfigError(f"missing required environment variable {_ENV_PREFIX}{name}")
            return value

        def optional(name: str, default: str | None = None) -> str | None:
            value = env.get(f"{_ENV_PREFIX}{name}")
            return value if value not in (None, "") else default

        def require_pattern(name: str, value: str, pattern: str) -> str:
            if not re.fullmatch(pattern, value):
                raise ConfigError(f"{_ENV_PREFIX}{name} does not match the required format")
            return value

        try:
            port = int(env.get("PORT", "8080"))
        except ValueError as exc:
            raise ConfigError("PORT must be an integer") from exc

        display_name = require_pattern(
            "DISPLAY_NAME",
            optional("DISPLAY_NAME", "Utopia Homes Prime") or "",
            r"^.{1,100}$",
        )
        deployment_id = require_pattern(
            "DEPLOYMENT_ID", require("DEPLOYMENT_ID"), patterns.PRINTABLE_ASCII_1_160_RE
        )
        runtime_id = require_pattern(
            "RUNTIME_ID", require("RUNTIME_ID"), patterns.PRINTABLE_ASCII_1_160_RE
        )
        release_id = require_pattern(
            "RELEASE_ID", require("RELEASE_ID"), patterns.PRINTABLE_ASCII_1_128_RE
        )
        software_version = require_pattern(
            "SOFTWARE_VERSION", require("SOFTWARE_VERSION"), patterns.SEMANTIC_VERSION_RE
        )
        artifact_digest = require_pattern(
            "ARTIFACT_DIGEST", require("ARTIFACT_DIGEST"), patterns.ARTIFACT_DIGEST_RE
        )
        deployed_at = require_pattern(
            "DEPLOYED_AT", require("DEPLOYED_AT"), patterns.RFC3339_UTC_RE
        )
        managed_synth_observed_release_id = optional("MANAGED_SYNTH_OBSERVED_RELEASE_ID")
        if managed_synth_observed_release_id is not None:
            require_pattern(
                "MANAGED_SYNTH_OBSERVED_RELEASE_ID",
                managed_synth_observed_release_id,
                patterns.PRINTABLE_ASCII_1_128_RE,
            )

        capabilities = cls._parse_capabilities(require("CAPABILITIES_JSON"))
        jwt_keys = cls._parse_jwt_keys(require("JWT_PUBLIC_KEYS_JSON"))

        rate_limit_raw = optional("RATE_LIMIT_PER_MINUTE", "120")
        try:
            rate_limit_per_minute = int(rate_limit_raw) if rate_limit_raw is not None else 120
        except ValueError as exc:
            raise ConfigError(f"{_ENV_PREFIX}RATE_LIMIT_PER_MINUTE must be an integer") from exc
        if rate_limit_per_minute < MINIMUM_RATE_LIMIT_PER_MINUTE:
            raise ConfigError(
                f"{_ENV_PREFIX}RATE_LIMIT_PER_MINUTE must be at least "
                f"{MINIMUM_RATE_LIMIT_PER_MINUTE} per §13"
            )

        log_level = (optional("LOG_LEVEL", "INFO") or "INFO").upper()

        return cls(
            environment=optional("ENVIRONMENT", "production") or "production",
            port=port,
            display_name=display_name,
            deployment_id=deployment_id,
            runtime_id=runtime_id,
            release_id=release_id,
            software_version=software_version,
            artifact_digest=artifact_digest,
            deployed_at=deployed_at,
            managed_synth_observed_release_id=managed_synth_observed_release_id,
            capabilities=capabilities,
            jwt_keys=jwt_keys,
            rate_limit_per_minute=rate_limit_per_minute,
            log_level=log_level,
        )

    @staticmethod
    def _parse_capabilities(raw: str) -> tuple[CapabilityConfig, ...]:
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{_ENV_PREFIX}CAPABILITIES_JSON is not valid JSON") from exc
        if not isinstance(entries, list) or not entries:
            raise ConfigError(f"{_ENV_PREFIX}CAPABILITIES_JSON must be a non-empty JSON array")

        seen_ids: set[str] = set()
        capabilities: list[CapabilityConfig] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConfigError(f"{_ENV_PREFIX}CAPABILITIES_JSON entries must be objects")
            capability_id = entry.get("capability_id")
            contract_version = entry.get("contract_version")
            state = entry.get("state")
            business_contract_id = entry.get("business_contract_id")

            if not isinstance(capability_id, str) or not re.fullmatch(
                patterns.CAPABILITY_ID_RE, capability_id
            ):
                raise ConfigError(f"invalid capability_id in {_ENV_PREFIX}CAPABILITIES_JSON")
            if capability_id in seen_ids:
                raise ConfigError(f"duplicate capability_id {capability_id!r} (violates I-02)")
            seen_ids.add(capability_id)

            if not isinstance(contract_version, str) or not re.fullmatch(
                patterns.CAPABILITY_CONTRACT_VERSION_RE, contract_version
            ):
                raise ConfigError(f"invalid contract_version for capability {capability_id!r}")

            if state not in patterns.CAPABILITY_STATE_VALUES:
                raise ConfigError(f"invalid state for capability {capability_id!r}")

            if business_contract_id is not None and not isinstance(business_contract_id, str):
                raise ConfigError(
                    f"business_contract_id for capability {capability_id!r} must be a string"
                )

            capabilities.append(
                CapabilityConfig(
                    capability_id=capability_id,
                    contract_version=contract_version,
                    state=state,
                    business_contract_id=business_contract_id,
                )
            )
        capabilities.sort(key=lambda c: c.capability_id)
        return tuple(capabilities)

    @staticmethod
    def _parse_jwt_keys(raw: str) -> tuple[JwtAllowlistedKey, ...]:
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{_ENV_PREFIX}JWT_PUBLIC_KEYS_JSON is not valid JSON") from exc
        if not isinstance(entries, list) or not entries:
            raise ConfigError(f"{_ENV_PREFIX}JWT_PUBLIC_KEYS_JSON must be a non-empty JSON array")

        seen_kids: set[str] = set()
        keys: list[JwtAllowlistedKey] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConfigError(f"{_ENV_PREFIX}JWT_PUBLIC_KEYS_JSON entries must be objects")
            kid = entry.get("kid")
            public_key_pem = entry.get("public_key_pem")
            status = entry.get("status", "active")

            if not isinstance(kid, str) or not kid:
                raise ConfigError("invalid kid in JWT_PUBLIC_KEYS_JSON")
            if kid in seen_kids:
                raise ConfigError(f"duplicate kid {kid!r} in JWT_PUBLIC_KEYS_JSON")
            seen_kids.add(kid)

            if not isinstance(public_key_pem, str) or "BEGIN PUBLIC KEY" not in public_key_pem:
                raise ConfigError(f"invalid public_key_pem for kid {kid!r}")

            if status not in ("active", "retired", "revoked"):
                raise ConfigError(f"invalid status for kid {kid!r}")

            keys.append(JwtAllowlistedKey(kid=kid, public_key_pem=public_key_pem, status=status))
        return tuple(keys)
