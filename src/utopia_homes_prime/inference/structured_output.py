"""Backend-neutral structured output for Homes inference: messages, the restricted JSON
Schema subset every Homes output schema stays inside, and strict validation of a returned
instance. Homes re-validates every model result against its own schema, whichever backend
produced it. The subset is the one Shared Model Execution RC1 §9.3.2 defines, so a schema
valid here is valid for the optional Tiamat backend too."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final, Literal

import rfc8785

SCHEMA_MAX_CANONICAL_BYTES: Final = 32_768
SCHEMA_MAX_DEPTH: Final = 8
SCHEMA_MAX_TOTAL_PROPERTIES: Final = 128
SCHEMA_MAX_ENUM_MEMBERS: Final = 64
SAFE_INTEGER_MAX: Final = 2**53 - 1

SCHEMA_KEYWORDS: Final = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
    }
)
SCHEMA_TYPES: Final = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


class OutputViolation(ValueError):
    """A schema or instance breaks the restricted structured-output subset."""


# --- restricted JSON Schema subset (§9.3.2) ------------------------------------------------------


def _is_json_integer(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def _is_json_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _check_numeric_literal(value: object, *, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise OutputViolation(f"{where}: non-finite numeric literal")
    if isinstance(value, int) and abs(value) > SAFE_INTEGER_MAX:
        raise OutputViolation(f"{where}: integer literal outside the safe-integer range")


def _schema_types(schema: dict[str, Any], *, where: str) -> tuple[str, bool]:
    """Returns (non_null_type_or_null, nullable)."""
    declared = schema.get("type")
    if isinstance(declared, str):
        if declared not in SCHEMA_TYPES:
            raise OutputViolation(f"{where}: unsupported type {declared!r}")
        return declared, False
    if (
        isinstance(declared, list)
        and len(declared) == 2
        and all(isinstance(item, str) for item in declared)
        and declared.count("null") == 1
    ):
        non_null = next(item for item in declared if item != "null")
        if non_null not in SCHEMA_TYPES:
            raise OutputViolation(f"{where}: unsupported type {non_null!r}")
        return non_null, True
    raise OutputViolation(f"{where}: `type` must be a supported type or [type, 'null']")


def check_restricted_schema(schema: object) -> None:
    """Raises OutputViolation unless `schema` is inside RC1 §9.3.2's structured-output subset."""
    if not isinstance(schema, dict):
        raise OutputViolation("schema must be an object")
    root_type, root_nullable = _schema_types(schema, where="$")
    if root_type != "object" or root_nullable:
        raise OutputViolation("schema root type must be object")

    total_properties = 0

    def walk(node: object, depth: int, where: str) -> None:
        nonlocal total_properties
        if not isinstance(node, dict):
            raise OutputViolation(f"{where}: schema node must be an object")
        if depth > SCHEMA_MAX_DEPTH:
            raise OutputViolation(f"{where}: nesting depth exceeds {SCHEMA_MAX_DEPTH}")
        unknown = set(node) - SCHEMA_KEYWORDS
        if unknown:
            raise OutputViolation(f"{where}: unsupported keywords {sorted(unknown)}")
        node_type, nullable = _schema_types(node, where=where)

        for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
            if keyword in node and (not _is_json_integer(node[keyword]) or node[keyword] < 0):
                raise OutputViolation(f"{where}: {keyword} must be a nonnegative integer")
        for keyword in ("minimum", "maximum"):
            if keyword in node:
                if not _is_json_number(node[keyword]):
                    raise OutputViolation(f"{where}: {keyword} must be a number")
                _check_numeric_literal(node[keyword], where=f"{where}.{keyword}")

        if "enum" in node:
            members = node["enum"]
            if not isinstance(members, list) or not members:
                raise OutputViolation(f"{where}: enum must be a non-empty array")
            if len(members) > SCHEMA_MAX_ENUM_MEMBERS:
                raise OutputViolation(f"{where}: enum exceeds {SCHEMA_MAX_ENUM_MEMBERS} members")
            for member in members:
                _check_numeric_literal(member, where=f"{where}.enum")
            null_count = sum(1 for member in members if member is None)
            if nullable and null_count != 1:
                raise OutputViolation(f"{where}: nullable enum must include null exactly once")
            if not nullable and null_count:
                raise OutputViolation(f"{where}: non-nullable enum cannot include null")
        if "const" in node:
            _check_numeric_literal(node["const"], where=f"{where}.const")
            if nullable and node["const"] is not None:
                raise OutputViolation(f"{where}: nullable const must itself be null")

        if node_type == "object":
            properties = node.get("properties")
            required = node.get("required")
            if not isinstance(properties, dict):
                raise OutputViolation(f"{where}: object must declare properties")
            if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
                raise OutputViolation(f"{where}: object must declare required")
            if len(required) != len(set(required)) or set(required) != set(properties):
                raise OutputViolation(f"{where}: required must list every property exactly once")
            if node.get("additionalProperties") is not False:
                raise OutputViolation(f"{where}: additionalProperties must be false")
            total_properties += len(properties)
            if total_properties > SCHEMA_MAX_TOTAL_PROPERTIES:
                raise OutputViolation(f"schema exceeds {SCHEMA_MAX_TOTAL_PROPERTIES} properties")
            for name, child in properties.items():
                walk(child, depth + 1, f"{where}.{name}")
        elif any(k in node for k in ("properties", "required", "additionalProperties")):
            raise OutputViolation(f"{where}: object keywords on a non-object schema")

        if "items" in node:
            if node_type != "array":
                raise OutputViolation(f"{where}: items on a non-array schema")
            walk(node["items"], depth + 1, f"{where}[]")

    walk(schema, 1, "$")
    if len(rfc8785.dumps(schema)) > SCHEMA_MAX_CANONICAL_BYTES:
        raise OutputViolation(f"schema exceeds {SCHEMA_MAX_CANONICAL_BYTES} canonical bytes")


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if _is_json_number(left) and _is_json_number(right):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(_json_equal, left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_json_equal(left[k], right[k]) for k in left)
    return type(left) is type(right) and left == right


def validate_instance(schema: dict[str, Any], value: object, where: str = "$") -> None:
    """Validates `value` against a schema already accepted by check_restricted_schema()."""
    node_type, nullable = _schema_types(schema, where=where)
    if value is None and (nullable or node_type == "null"):
        if "const" in schema and schema["const"] is not None:
            raise OutputViolation(f"{where}: value differs from const")
        return

    type_ok = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": _is_json_integer(value),
        "number": _is_json_number(value),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[node_type]
    if not type_ok:
        raise OutputViolation(f"{where}: expected {node_type}")

    if "enum" in schema and not any(_json_equal(value, member) for member in schema["enum"]):
        raise OutputViolation(f"{where}: value is not an allowed enum member")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise OutputViolation(f"{where}: value differs from const")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise OutputViolation(f"{where}: string shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise OutputViolation(f"{where}: string longer than maxLength")
    if node_type in ("integer", "number"):
        assert isinstance(value, int | float)
        if "minimum" in schema and value < schema["minimum"]:
            raise OutputViolation(f"{where}: number below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise OutputViolation(f"{where}: number above maximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise OutputViolation(f"{where}: array shorter than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise OutputViolation(f"{where}: array longer than maxItems")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_instance(schema["items"], item, f"{where}[{index}]")
    if isinstance(value, dict):
        properties: dict[str, Any] = schema["properties"]
        if set(value) != set(properties):
            raise OutputViolation(f"{where}: object members differ from the schema's properties")
        for name, child in properties.items():
            validate_instance(child, value[name], f"{where}.{name}")


MessageRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ExecutionMessage:
    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class JsonSchemaOutput:
    name: str
    schema: dict[str, Any]
