"""JSON Schema validation against the vendored bundle's own schema files.

Mirrors contracts/stoin-business-guest-answer-v1-bundle/tools/verify_bundle.py's
build_registry()/get_validator() exactly, so wire validation is driven by the frozen schema
files themselves rather than a hand-maintained re-encoding of RC2's field rules — the same
reasoning as bundle_tools.py for the invariant checks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

_BUNDLE = (
    Path(__file__).resolve().parent.parent.parent
    / "contracts"
    / "stoin-business-guest-answer-v1-bundle"
)
_SCHEMAS = _BUNDLE / "schemas"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_registry() -> Registry:
    resources = []
    for path in _SCHEMAS.glob("*.json"):
        contents = _load(path)
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


_REGISTRY = _build_registry()


def _get_validator(schema_filename: str) -> Draft202012Validator:
    schema = _load(_SCHEMAS / schema_filename)
    return Draft202012Validator(schema, registry=_REGISTRY)


REQUEST_VALIDATOR = _get_validator("request.schema.json")
RESPONSE_VALIDATOR = _get_validator("response.schema.json")
ERROR_RESPONSE_VALIDATOR = _get_validator("error.response.schema.json")


class SchemaValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors) or "schema validation failed")
        self.errors = errors


def validate_request(body: object) -> None:
    errors = sorted(
        f"{'/'.join(str(p) for p in e.path)}: {e.message}"
        for e in REQUEST_VALIDATOR.iter_errors(body)
    )
    if errors:
        raise SchemaValidationError(errors)


def validate_response(body: object) -> None:
    errors = sorted(
        f"{'/'.join(str(p) for p in e.path)}: {e.message}"
        for e in RESPONSE_VALIDATOR.iter_errors(body)
    )
    if errors:
        raise SchemaValidationError(errors)


def validate_error_response(body: object) -> None:
    errors = sorted(
        f"{'/'.join(str(p) for p in e.path)}: {e.message}"
        for e in ERROR_RESPONSE_VALIDATOR.iter_errors(body)
    )
    if errors:
        raise SchemaValidationError(errors)
