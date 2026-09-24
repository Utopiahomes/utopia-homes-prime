"""Response models mirroring contracts/stoin-management-v1-bundle/schemas/*.json field-for-field.

Pydantic's own validation (regexes/enums copied verbatim from common.defs.json, `extra="forbid"`,
`frozen=True`) is a second line of defense: if production code ever tries to construct a
non-conforming response, model construction itself raises rather than serializing bad JSON.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from utopia_homes_prime.management_adapter import patterns

_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)

ContractVersion = Literal["1.0"]
HealthStatus = Literal["healthy", "degraded", "unavailable", "unknown"]
ReasonCode = Literal[
    "capacity_limited",
    "configuration_error",
    "dependency_unavailable",
    "health_coverage_limited",
    "maintenance",
    "no_enabled_capabilities",
    "release_in_progress",
    "unspecified",
]
CapabilityState = Literal["enabled", "disabled", "deprecated"]
ErrorCode = Literal[
    "invalid_request",
    "authentication_failed",
    "authorization_denied",
    "method_not_allowed",
    "contract_not_supported",
    "request_too_large",
    "rate_limited",
    "internal_error",
    "management_unavailable",
]


def format_observed_at(dt: datetime) -> str:
    """§8: RFC 3339 UTC, whole-second precision, trailing Z. Used by every success response."""
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class IdentityResponseV1(BaseModel):
    model_config = _MODEL_CONFIG

    contract_version: ContractVersion
    observed_at: str = Field(pattern=patterns.OBSERVED_AT_RE)
    synth_id: Literal["stoin:synth:utopia-homes-prime"]
    realm_id: Literal["stoin:realm:utopia-homes"]
    synth_class: Literal["business-prime"]
    display_name: str = Field(min_length=1, max_length=100)


class HealthResponseV1(BaseModel):
    model_config = _MODEL_CONFIG

    contract_version: ContractVersion
    observed_at: str = Field(pattern=patterns.OBSERVED_AT_RE)
    status: HealthStatus
    management_provider_release_id: str = Field(pattern=patterns.PRINTABLE_ASCII_1_128_RE)
    degraded_capabilities: list[str] = Field(default_factory=list)
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    retry_after_seconds: int | None = Field(default=None, ge=1, le=3600)

    @model_validator(mode="after")
    def _check_invariants(self) -> HealthResponseV1:
        for capability_id in self.degraded_capabilities:
            if not re.fullmatch(patterns.CAPABILITY_ID_RE, capability_id):
                raise ValueError(f"invalid degraded_capabilities entry: {capability_id!r}")
        if len(set(self.degraded_capabilities)) != len(self.degraded_capabilities):
            raise ValueError("degraded_capabilities must not contain duplicates (I-02)")
        if self.degraded_capabilities != sorted(self.degraded_capabilities):
            raise ValueError("degraded_capabilities must be sorted ascending (I-01)")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes must not contain duplicates")
        if list(self.reason_codes) != sorted(self.reason_codes):
            raise ValueError("reason_codes must be sorted ascending (I-01)")

        if self.status == "healthy" and self.degraded_capabilities:
            raise ValueError("status=healthy requires an empty degraded_capabilities")
        if self.status == "degraded" and not self.degraded_capabilities:
            raise ValueError("status=degraded requires a non-empty degraded_capabilities")
        if self.status == "unknown" and not self.reason_codes:
            raise ValueError("status=unknown requires non-empty reason_codes")
        if "unspecified" in self.reason_codes and len(self.reason_codes) > 1:
            raise ValueError("'unspecified' must not appear alongside any other reason code")
        no_enabled = "no_enabled_capabilities" in self.reason_codes
        if self.status == "unavailable" and not self.degraded_capabilities and not no_enabled:
            raise ValueError(
                "status=unavailable with an empty degraded_capabilities requires "
                "no_enabled_capabilities (I-04/I-07)"
            )
        if no_enabled and self.status != "unavailable":
            raise ValueError("no_enabled_capabilities requires status=unavailable (I-07)")
        if no_enabled and self.degraded_capabilities:
            raise ValueError("no_enabled_capabilities requires empty degraded_capabilities (I-07)")
        return self


class ManagementProviderV1(BaseModel):
    model_config = _MODEL_CONFIG

    deployment_id: str = Field(pattern=patterns.PRINTABLE_ASCII_1_160_RE)
    runtime_id: str = Field(pattern=patterns.PRINTABLE_ASCII_1_160_RE)
    release_id: str = Field(pattern=patterns.PRINTABLE_ASCII_1_128_RE)
    software_version: str = Field(pattern=patterns.SEMANTIC_VERSION_RE)
    artifact_digest: str = Field(pattern=patterns.ARTIFACT_DIGEST_RE)
    deployed_at: str = Field(pattern=patterns.RFC3339_UTC_RE)


class ManagedSynthV1(BaseModel):
    model_config = _MODEL_CONFIG

    synth_id: Literal["stoin:synth:utopia-homes-prime"]
    observed_release_id: str | None = Field(default=None, pattern=patterns.PRINTABLE_ASCII_1_128_RE)


class VersionResponseV1(BaseModel):
    model_config = _MODEL_CONFIG

    contract_version: ContractVersion
    observed_at: str = Field(pattern=patterns.OBSERVED_AT_RE)
    management_provider: ManagementProviderV1
    managed_synth: ManagedSynthV1
    supported_management_contracts: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_invariants(self) -> VersionResponseV1:
        if "1.0" not in self.supported_management_contracts:
            raise ValueError("supported_management_contracts must include '1.0'")
        if list(self.supported_management_contracts) != sorted(self.supported_management_contracts):
            raise ValueError("supported_management_contracts must be sorted ascending (I-01)")
        return self


class CapabilityEntryV1(BaseModel):
    model_config = _MODEL_CONFIG

    capability_id: str = Field(pattern=patterns.CAPABILITY_ID_RE)
    contract_version: str = Field(pattern=patterns.CAPABILITY_CONTRACT_VERSION_RE)
    state: CapabilityState
    business_contract_id: str | None = None


class CapabilitiesResponseV1(BaseModel):
    model_config = _MODEL_CONFIG

    contract_version: ContractVersion
    observed_at: str = Field(pattern=patterns.OBSERVED_AT_RE)
    capabilities: list[CapabilityEntryV1]

    @model_validator(mode="after")
    def _check_invariants(self) -> CapabilitiesResponseV1:
        ids = [c.capability_id for c in self.capabilities]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate capability_id (I-02)")
        if ids != sorted(ids):
            raise ValueError("capabilities must be sorted by capability_id (I-01)")
        return self


class ErrorBodyV1(BaseModel):
    model_config = _MODEL_CONFIG

    code: ErrorCode
    message: str = Field(min_length=1)
    correlation_id: str = Field(pattern=patterns.UUID_V4_RE)
    request_id: str | None = Field(default=None, pattern=patterns.UUID_V4_RE)
    retryable: bool

    @model_validator(mode="after")
    def _check_retryable(self) -> ErrorBodyV1:
        expected = self.code in ("rate_limited", "internal_error", "management_unavailable")
        if self.retryable != expected:
            raise ValueError(f"retryable must be {expected} for code {self.code!r}")
        return self


class ErrorResponseV1(BaseModel):
    model_config = _MODEL_CONFIG

    contract_version: ContractVersion
    error: ErrorBodyV1
