"""Loads the vendored conformance bundle's schemas and vectors, and checks invariants I-01..I-07
against live response pairs. Used by both in-process integration tests and the real-network
contract tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "contracts" / "stoin-management-v1-bundle"
SCHEMAS_DIR = BUNDLE_DIR / "schemas"
VECTORS_DIR = BUNDLE_DIR / "vectors"

_SCHEMA_FILENAMES = {
    "identity.response": "identity.response.schema.json",
    "health.response": "health.response.schema.json",
    "version.response": "version.response.schema.json",
    "capabilities.response": "capabilities.response.schema.json",
    "error.response": "error.response.schema.json",
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_registry() -> Registry:
    resources = []
    for path in SCHEMAS_DIR.glob("*.json"):
        contents = _load_json(path)
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


_REGISTRY = _build_registry()


def get_validator(schema_name: str) -> Draft202012Validator:
    schema = _load_json(SCHEMAS_DIR / _SCHEMA_FILENAMES[schema_name])
    return Draft202012Validator(schema, registry=_REGISTRY)  # type: ignore[call-arg]


def is_valid(document: dict[str, Any], schema_name: str) -> bool:
    return get_validator(schema_name).is_valid(document)


def validate(document: dict[str, Any], schema_name: str) -> None:
    get_validator(schema_name).validate(document)


def iter_vector_files(*, positive: bool) -> list[dict[str, Any]]:
    """Every vendored vector's full envelope (vector_id/schema/expect/document/...)."""
    directory = VECTORS_DIR / ("positive" if positive else "negative")
    return [_load_json(p) for p in sorted(directory.glob("*.json"))]


def iter_vectors(*, positive: bool) -> list[tuple[str, dict[str, Any]]]:
    """Every vendored vector's (vector_id, response document) — the `document` field is the
    actual response body; the envelope around it (vector_id/schema/expect/...) is metadata."""
    return [(v["vector_id"], v["document"]) for v in iter_vector_files(positive=positive)]


def schema_name_for_vector(vector_name: str) -> str:
    prefix = vector_name.split(".")[0]
    mapping = {
        "identity": "identity.response",
        "health": "health.response",
        "version": "version.response",
        "capabilities": "capabilities.response",
        "error": "error.response",
    }
    return mapping[prefix]


# --- Cross-resource invariants (I-01..I-07), checked against a live response pair. ---


def check_i01_sorted(document: dict[str, Any]) -> None:
    for key in ("degraded_capabilities", "reason_codes", "supported_management_contracts"):
        if key in document and list(document[key]) != sorted(document[key]):
            raise AssertionError(f"I-01 violated: {key} is not sorted ascending")
    if "capabilities" in document:
        ids = [c["capability_id"] for c in document["capabilities"]]
        if ids != sorted(ids):
            raise AssertionError("I-01 violated: capabilities not sorted by capability_id")


def check_i02_unique_capability_ids(capabilities_doc: dict[str, Any]) -> None:
    ids = [c["capability_id"] for c in capabilities_doc["capabilities"]]
    if len(set(ids)) != len(ids):
        raise AssertionError("I-02 violated: duplicate capability_id")


def check_i03_release_id_match(health_doc: dict[str, Any], version_doc: dict[str, Any]) -> None:
    health_release = health_doc["management_provider_release_id"]
    version_release = version_doc["management_provider"]["release_id"]
    if health_release != version_release:
        raise AssertionError(
            f"I-03 violated: /health release id {health_release!r} != "
            f"/version release id {version_release!r}"
        )


def check_i04_i06_i07(health_doc: dict[str, Any], capabilities_doc: dict[str, Any]) -> None:
    enabled = {
        c["capability_id"] for c in capabilities_doc["capabilities"] if c["state"] == "enabled"
    }
    impaired = set(health_doc["degraded_capabilities"])
    status = health_doc["status"]
    reason_codes = set(health_doc["reason_codes"])

    if not impaired.issubset(enabled):
        raise AssertionError("I-06 violated: degraded_capabilities contains a non-enabled id")

    no_enabled_present = "no_enabled_capabilities" in reason_codes
    if no_enabled_present != (not enabled):
        raise AssertionError("I-07 violated: no_enabled_capabilities must match an empty E exactly")

    if not enabled:
        if status != "unavailable":
            raise AssertionError("I-04 violated: empty E must be status=unavailable")
        return

    if status == "healthy" and impaired:
        raise AssertionError("I-04 violated: status=healthy requires I empty")
    if status == "degraded" and not (impaired and impaired < enabled):
        raise AssertionError(
            "I-04 violated: status=degraded requires I a non-empty proper subset of E"
        )
    if status == "unavailable" and impaired != enabled:
        raise AssertionError("I-04 violated: status=unavailable with non-empty E requires I = E")
    if status == "unknown" and not (impaired == set() or impaired < enabled):
        raise AssertionError(
            "I-04 violated: status=unknown requires I empty or a proper subset of E"
        )


def check_i05_transitional(health_doc: dict[str, Any], capabilities_doc: dict[str, Any]) -> None:
    """This adapter is always transitional (§17): while any capability is enabled it must report
    unknown + health_coverage_limited, and it must never claim healthy."""
    enabled = any(c["state"] == "enabled" for c in capabilities_doc["capabilities"])
    if health_doc["status"] == "healthy":
        raise AssertionError("I-05 violated: transitional adapter must never report healthy")
    if enabled:
        if health_doc["status"] != "unknown":
            raise AssertionError(
                "I-05 violated: transitional adapter with E non-empty must be unknown"
            )
        if "health_coverage_limited" not in health_doc["reason_codes"]:
            raise AssertionError("I-05 violated: missing health_coverage_limited")
        if health_doc["degraded_capabilities"]:
            raise AssertionError("I-05 violated: transitional unknown must have empty impaired set")
