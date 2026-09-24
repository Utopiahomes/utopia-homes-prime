"""Caller-side wire layer for the private Shared Model Execution contract `inference.execute@1.0`.

Pinned to contracts/stoin-shared-model-execution-v1-rc1/ (RC1 contract §§6-10, 16-17). This module
is pure: it builds exact request bytes and headers and parses provider responses strictly, with no
network or clock access, so every rule here is unit-testable against the pinned text.

Homes Prime is the calling synth. Nothing here is exposed on the guest.answer wire: the website
never sees these requests, responses, credentials, or identifiers (RC2 §5, RC1 §19 step 6).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Final, Literal

import rfc8785

# --- §6 transport bounds -------------------------------------------------------------------------

EXECUTION_PATH: Final = "/execution/v1/inference"
REQUEST_BODY_MAX_BYTES: Final = 262_144
RESPONSE_BODY_MAX_BYTES: Final = 131_072
TIMEOUT_HEADER_MIN_MS: Final = 1_000
TIMEOUT_HEADER_MAX_MS: Final = 18_000
RETRY_AFTER_MIN_SECONDS: Final = 1
RETRY_AFTER_MAX_SECONDS: Final = 30

# --- §3 identifiers ------------------------------------------------------------------------------

UUID_V4_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
STABLE_ID_RE: Final = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z][a-z0-9]*)+$")
RELEASE_ID_RE: Final = re.compile(r"^[\x21-\x7e]{1,128}$")
CONTENT_SHA256_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43}$")

# --- §7 authentication ---------------------------------------------------------------------------

JWT_AUDIENCE: Final = "stoin:shared-model-execution"
JWT_SCOPE: Final = "inference.execute"
JWT_MAX_LIFETIME_SECONDS: Final = 300

# --- §9 request bounds ---------------------------------------------------------------------------

REQUEST_CONTRACT: Final = "stoin.inference.execute.request.v1"
RESPONSE_CONTRACT: Final = "stoin.inference.execute.response.v1"
ERROR_CONTRACT: Final = "stoin.inference.execute.error.v1"

MESSAGES_MIN: Final = 2
MESSAGES_MAX: Final = 32
MESSAGE_CONTENT_MAX_SCALARS: Final = 65_536
MESSAGES_TOTAL_MAX_BYTES: Final = 196_608
SCHEMA_MAX_CANONICAL_BYTES: Final = 32_768
SCHEMA_MAX_DEPTH: Final = 8
SCHEMA_MAX_TOTAL_PROPERTIES: Final = 128
SCHEMA_MAX_ENUM_MEMBERS: Final = 64
MAX_OUTPUT_TOKENS_RANGE: Final = (1, 4_096)
MAX_COST_MICROUSD_RANGE: Final = (1, 1_000_000)
OUTPUT_CONTENT_MAX_BYTES: Final = 65_536
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

ExecutionState = Literal["admitted", "dispatched", "completed", "failed", "outcome_unknown"]
SettlementStatus = Literal[
    "settled", "pending_reconciliation", "reservation_forfeited", "settlement_overrun"
]
EXECUTION_STATES: Final = frozenset(
    {"admitted", "dispatched", "completed", "failed", "outcome_unknown"}
)
SETTLEMENT_STATUSES: Final = frozenset(
    {"settled", "pending_reconciliation", "reservation_forfeited", "settlement_overrun"}
)


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    http_status: int
    message: str
    retryable: bool


ERROR_TABLE: Final[dict[str, ErrorSpec]] = {
    "invalid_request": ErrorSpec(400, "The execution request is invalid.", False),
    "authentication_failed": ErrorSpec(401, "Service authentication failed.", False),
    "capability_forbidden": ErrorSpec(403, "This execution capability is not permitted.", False),
    "route_not_found": ErrorSpec(404, "The requested execution route was not found.", False),
    "method_not_allowed": ErrorSpec(405, "The execution method is not allowed.", False),
    "response_media_not_acceptable": ErrorSpec(
        406, "The requested response media type is not supported.", False
    ),
    "execution_aborted": ErrorSpec(409, "The execution ended before model dispatch.", False),
    "idempotency_conflict": ErrorSpec(
        409, "The idempotency key conflicts with an earlier request.", False
    ),
    "request_in_progress": ErrorSpec(409, "The execution request is already in progress.", True),
    "idempotency_recovery_unavailable": ErrorSpec(
        409, "The earlier execution result is no longer available.", False
    ),
    "execution_outcome_unknown": ErrorSpec(
        409, "The execution outcome could not be determined.", False
    ),
    "execution_invalidated": ErrorSpec(
        409, "The earlier execution result is no longer eligible.", False
    ),
    "request_too_large": ErrorSpec(413, "The execution request is too large.", False),
    "unsupported_media_type": ErrorSpec(
        415, "The execution request media type is not supported.", False
    ),
    "output_contract_unsupported": ErrorSpec(
        422, "The requested output contract is not supported.", False
    ),
    "cost_ceiling_insufficient": ErrorSpec(
        422, "The execution cost ceiling is insufficient.", False
    ),
    "rate_limited": ErrorSpec(429, "Execution capacity is temporarily limited.", True),
    "provider_response_invalid": ErrorSpec(502, "The model returned an unusable result.", False),
    "provider_response_too_large": ErrorSpec(502, "The model response is too large.", False),
    "output_limit_reached": ErrorSpec(502, "The model reached its output limit.", False),
    "content_filtered": ErrorSpec(502, "The model response was filtered.", False),
    "provider_execution_failed": ErrorSpec(502, "The model execution failed.", False),
    "cost_settlement_violation": ErrorSpec(
        502, "The provider charge exceeded its reservation.", False
    ),
    "privacy_route_unavailable": ErrorSpec(
        503, "No approved private execution route is available.", False
    ),
    "authentication_state_unavailable": ErrorSpec(
        503, "Service authentication state is temporarily unavailable.", True
    ),
    "state_store_unavailable": ErrorSpec(503, "Execution state is temporarily unavailable.", True),
    "spending_authority_exhausted": ErrorSpec(
        503, "Execution spending authority is unavailable.", False
    ),
    "temporarily_unavailable": ErrorSpec(503, "Model execution is temporarily unavailable.", True),
    "deadline_exceeded": ErrorSpec(504, "Model execution exceeded its deadline.", False),
}

RETRY_AFTER_CODES: Final = frozenset(
    {
        "request_in_progress",
        "rate_limited",
        "authentication_state_unavailable",
        "state_store_unavailable",
        "temporarily_unavailable",
    }
)
RELEASE_HEADERLESS_CODES: Final = frozenset(
    {"authentication_failed", "authentication_state_unavailable"}
)
"""§6.2: authentication failures and authentication_state_unavailable carry no release headers."""
TRANSPORT_CODES: Final = frozenset({"route_not_found", "method_not_allowed"})
"""§6.2: 404/405 are ordinary private-service transport responses, not contract-shaped bodies."""
RECEIPTLESS_CODES: Final = frozenset({"idempotency_conflict", "state_store_unavailable"})
"""§16: these always omit both `execution` and `cost`."""
POST_DISPATCH_TERMINAL_CODES: Final = frozenset(
    {
        "provider_response_invalid",
        "provider_response_too_large",
        "output_limit_reached",
        "content_filtered",
        "provider_execution_failed",
        "cost_settlement_violation",
    }
)
"""§16: every 502 commits `failed` before returning, with execution and cost receipts."""


class WireViolation(ValueError):
    """A request Homes Prime is about to send, or a response it received, breaks RC1."""


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
        raise WireViolation(f"{where}: non-finite numeric literal")
    if isinstance(value, int) and abs(value) > SAFE_INTEGER_MAX:
        raise WireViolation(f"{where}: integer literal outside the safe-integer range")


def _schema_types(schema: dict[str, Any], *, where: str) -> tuple[str, bool]:
    """Returns (non_null_type_or_null, nullable)."""
    declared = schema.get("type")
    if isinstance(declared, str):
        if declared not in SCHEMA_TYPES:
            raise WireViolation(f"{where}: unsupported type {declared!r}")
        return declared, False
    if (
        isinstance(declared, list)
        and len(declared) == 2
        and all(isinstance(item, str) for item in declared)
        and declared.count("null") == 1
    ):
        non_null = next(item for item in declared if item != "null")
        if non_null not in SCHEMA_TYPES:
            raise WireViolation(f"{where}: unsupported type {non_null!r}")
        return non_null, True
    raise WireViolation(f"{where}: `type` must be a supported type or [type, 'null']")


def check_restricted_schema(schema: object) -> None:
    """Raises WireViolation unless `schema` is inside RC1 §9.3.2's structured-output subset."""
    if not isinstance(schema, dict):
        raise WireViolation("schema must be an object")
    root_type, root_nullable = _schema_types(schema, where="$")
    if root_type != "object" or root_nullable:
        raise WireViolation("schema root type must be object")

    total_properties = 0

    def walk(node: object, depth: int, where: str) -> None:
        nonlocal total_properties
        if not isinstance(node, dict):
            raise WireViolation(f"{where}: schema node must be an object")
        if depth > SCHEMA_MAX_DEPTH:
            raise WireViolation(f"{where}: nesting depth exceeds {SCHEMA_MAX_DEPTH}")
        unknown = set(node) - SCHEMA_KEYWORDS
        if unknown:
            raise WireViolation(f"{where}: unsupported keywords {sorted(unknown)}")
        node_type, nullable = _schema_types(node, where=where)

        for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
            if keyword in node and (not _is_json_integer(node[keyword]) or node[keyword] < 0):
                raise WireViolation(f"{where}: {keyword} must be a nonnegative integer")
        for keyword in ("minimum", "maximum"):
            if keyword in node:
                if not _is_json_number(node[keyword]):
                    raise WireViolation(f"{where}: {keyword} must be a number")
                _check_numeric_literal(node[keyword], where=f"{where}.{keyword}")

        if "enum" in node:
            members = node["enum"]
            if not isinstance(members, list) or not members:
                raise WireViolation(f"{where}: enum must be a non-empty array")
            if len(members) > SCHEMA_MAX_ENUM_MEMBERS:
                raise WireViolation(f"{where}: enum exceeds {SCHEMA_MAX_ENUM_MEMBERS} members")
            for member in members:
                _check_numeric_literal(member, where=f"{where}.enum")
            null_count = sum(1 for member in members if member is None)
            if nullable and null_count != 1:
                raise WireViolation(f"{where}: nullable enum must include null exactly once")
            if not nullable and null_count:
                raise WireViolation(f"{where}: non-nullable enum cannot include null")
        if "const" in node:
            _check_numeric_literal(node["const"], where=f"{where}.const")
            if nullable and node["const"] is not None:
                raise WireViolation(f"{where}: nullable const must itself be null")

        if node_type == "object":
            properties = node.get("properties")
            required = node.get("required")
            if not isinstance(properties, dict):
                raise WireViolation(f"{where}: object must declare properties")
            if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
                raise WireViolation(f"{where}: object must declare required")
            if len(required) != len(set(required)) or set(required) != set(properties):
                raise WireViolation(f"{where}: required must list every property exactly once")
            if node.get("additionalProperties") is not False:
                raise WireViolation(f"{where}: additionalProperties must be false")
            total_properties += len(properties)
            if total_properties > SCHEMA_MAX_TOTAL_PROPERTIES:
                raise WireViolation(f"schema exceeds {SCHEMA_MAX_TOTAL_PROPERTIES} properties")
            for name, child in properties.items():
                walk(child, depth + 1, f"{where}.{name}")
        elif any(k in node for k in ("properties", "required", "additionalProperties")):
            raise WireViolation(f"{where}: object keywords on a non-object schema")

        if "items" in node:
            if node_type != "array":
                raise WireViolation(f"{where}: items on a non-array schema")
            walk(node["items"], depth + 1, f"{where}[]")

    walk(schema, 1, "$")
    if len(rfc8785.dumps(schema)) > SCHEMA_MAX_CANONICAL_BYTES:
        raise WireViolation(f"schema exceeds {SCHEMA_MAX_CANONICAL_BYTES} canonical bytes")


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
            raise WireViolation(f"{where}: value differs from const")
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
        raise WireViolation(f"{where}: expected {node_type}")

    if "enum" in schema and not any(_json_equal(value, member) for member in schema["enum"]):
        raise WireViolation(f"{where}: value is not an allowed enum member")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise WireViolation(f"{where}: value differs from const")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise WireViolation(f"{where}: string shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise WireViolation(f"{where}: string longer than maxLength")
    if node_type in ("integer", "number"):
        assert isinstance(value, int | float)
        if "minimum" in schema and value < schema["minimum"]:
            raise WireViolation(f"{where}: number below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise WireViolation(f"{where}: number above maximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise WireViolation(f"{where}: array shorter than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise WireViolation(f"{where}: array longer than maxItems")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_instance(schema["items"], item, f"{where}[{index}]")
    if isinstance(value, dict):
        properties: dict[str, Any] = schema["properties"]
        if set(value) != set(properties):
            raise WireViolation(f"{where}: object members differ from the schema's properties")
        for name, child in properties.items():
            validate_instance(child, value[name], f"{where}.{name}")


# --- request construction (§§6.1, 7.3, 9) --------------------------------------------------------


MessageRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ExecutionMessage:
    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class JsonSchemaOutput:
    name: str
    schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """Exact bytes and digests for one logical execution. Every attempt, including the one
    permitted retry, reuses these same bytes and the same Idempotency-Key (§§7.3, 12)."""

    execution_profile_id: str
    idempotency_key: str
    body: bytes
    body_sha256_b64url: str
    request_binding: str
    output: JsonSchemaOutput
    max_output_tokens: int
    max_cost_microusd: int
    document: dict[str, Any]
    """The exact JSON-able request object `body` was serialized from — kept separately from the
    compact wire bytes so canonical_identity() below can RFC 8785-canonicalize the same logical
    document without reconstructing it (and risking drift from what was actually sent)."""


def b64url_sha256(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def request_binding(*, idempotency_key: str, body_sha256_b64url: str) -> str:
    """§7.3 `req` claim."""
    material = f"POST\n{EXECUTION_PATH}\n{idempotency_key}\n{body_sha256_b64url}".encode("ascii")
    return b64url_sha256(material)


def canonical_identity(prepared: PreparedRequest) -> str:
    """§11 canonical request identity: RFC 8785 canonicalization of the *validated* request
    object, SHA-256-hashed. This is deliberately separate from `body_sha256_b64url` (the hash of
    the exact compact-JSON wire bytes, used for `req`/replay-byte-reuse per §7.3) — §11 explicitly
    calls out that idempotency conflict detection uses the canonical identity, not the wire bytes,
    so two requests that differ only in member order or insignificant whitespace still collide as
    "the same request" for idempotency purposes even though their wire bytes differ.

    Tiamat's own server-side implementation (`lucy.shared_execution.service.canonical_identity`)
    is `hashlib.sha256(rfc8785.dumps(request.model_dump(mode="json", by_alias=True))).hexdigest()`
    over its validated `ExecutionRequest` Pydantic model. `prepared.document` is already the exact
    JSON-able object `prepared.body` was serialized from by `prepare_request()` — the same shape
    Tiamat's model_dump would produce for the same logical request — so canonicalizing it here
    (rather than re-deriving a new dict) is what makes this byte-for-byte comparable to Tiamat's
    own digest. See tests/unit/test_sme_canonical_identity.py for the differential proof against
    fixtures generated by actually running Tiamat's function, not just this same formula restated.
    """
    return hashlib.sha256(rfc8785.dumps(prepared.document)).hexdigest()


def _validate_messages(messages: tuple[ExecutionMessage, ...]) -> None:
    if not MESSAGES_MIN <= len(messages) <= MESSAGES_MAX:
        raise WireViolation(f"messages must contain {MESSAGES_MIN}-{MESSAGES_MAX} entries")
    if messages[0].role != "system":
        raise WireViolation("the first message must be the single system message")
    total_bytes = 0
    for index, message in enumerate(messages):
        expected: MessageRole = (
            "system" if index == 0 else "user" if index % 2 == 1 else "assistant"
        )
        if message.role != expected:
            raise WireViolation(f"message {index} must have role {expected!r}")
        if not 1 <= len(message.content) <= MESSAGE_CONTENT_MAX_SCALARS:
            raise WireViolation(f"message {index} content length is out of bounds")
        if "\x00" in message.content:
            raise WireViolation(f"message {index} contains NUL")
        try:
            total_bytes += len(message.content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise WireViolation(f"message {index} contains an unpaired surrogate") from exc
    if messages[-1].role != "user":
        raise WireViolation("the final message must have role 'user'")
    if total_bytes > MESSAGES_TOTAL_MAX_BYTES:
        raise WireViolation("total message content exceeds its byte bound")


def prepare_request(
    *,
    execution_profile_id: str,
    idempotency_key: str,
    messages: tuple[ExecutionMessage, ...],
    output: JsonSchemaOutput,
    max_output_tokens: int,
    max_cost_microusd: int,
) -> PreparedRequest:
    if not STABLE_ID_RE.fullmatch(execution_profile_id) or len(execution_profile_id) > 128:
        raise WireViolation("execution_profile_id must be a stable ID")
    if not UUID_V4_RE.fullmatch(idempotency_key):
        raise WireViolation("Idempotency-Key must be a canonical UUID v4")
    if not STABLE_ID_RE.fullmatch(output.name) or len(output.name) > 128:
        raise WireViolation("output name must be a stable ID")
    if not MAX_OUTPUT_TOKENS_RANGE[0] <= max_output_tokens <= MAX_OUTPUT_TOKENS_RANGE[1]:
        raise WireViolation("max_output_tokens is out of bounds")
    if not MAX_COST_MICROUSD_RANGE[0] <= max_cost_microusd <= MAX_COST_MICROUSD_RANGE[1]:
        raise WireViolation("max_cost_microusd is out of bounds")
    _validate_messages(messages)
    check_restricted_schema(output.schema)

    document = {
        "contract": REQUEST_CONTRACT,
        "execution_profile_id": execution_profile_id,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "output": {"mode": "json_schema", "name": output.name, "schema": output.schema},
        "limits": {"max_output_tokens": max_output_tokens, "max_cost_microusd": max_cost_microusd},
    }
    body = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    if len(body) > REQUEST_BODY_MAX_BYTES:
        raise WireViolation("request body exceeds its byte bound")
    digest = b64url_sha256(body)
    return PreparedRequest(
        execution_profile_id=execution_profile_id,
        idempotency_key=idempotency_key,
        body=body,
        body_sha256_b64url=digest,
        request_binding=request_binding(idempotency_key=idempotency_key, body_sha256_b64url=digest),
        output=output,
        max_output_tokens=max_output_tokens,
        max_cost_microusd=max_cost_microusd,
        document=document,
    )


def attempt_headers(
    prepared: PreparedRequest, *, token: str, request_id: str, timeout_ms: int
) -> dict[str, str]:
    if not UUID_V4_RE.fullmatch(request_id):
        raise WireViolation("X-Request-ID must be a canonical UUID v4")
    if not TIMEOUT_HEADER_MIN_MS <= timeout_ms <= TIMEOUT_HEADER_MAX_MS:
        raise WireViolation("X-Execution-Timeout-Ms is out of bounds")
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": request_id,
        "Idempotency-Key": prepared.idempotency_key,
        "X-Execution-Timeout-Ms": str(timeout_ms),
        "X-Content-SHA256": prepared.body_sha256_b64url,
        "Content-Type": "application/json",
        "Accept": "application/json",
        # §6: compression is disabled so byte bounds are unambiguous.
        "Accept-Encoding": "identity",
    }


# --- response parsing (§§6.2, 10, 13, 16) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostReceipt:
    reserved_microusd: int
    settled_microusd: int | None
    settlement_status: SettlementStatus


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    execution_id: str
    state: ExecutionState


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    generated_tokens: int
    output_tokens: int | None
    reasoning_tokens: int | None


@dataclass(frozen=True, slots=True)
class ExecutionSuccess:
    execution_id: str
    replayed: bool
    profile_release_id: str
    content: dict[str, Any]
    usage: Usage
    cost: CostReceipt
    execution_release: str
    policy_release: str


@dataclass(frozen=True, slots=True)
class ExecutionError:
    code: str
    retryable: bool
    retry_after_seconds: int | None
    execution: ExecutionReceipt | None
    cost: CostReceipt | None


def _header(headers: dict[str, str], name: str) -> str | None:
    return headers.get(name.lower())


def _is_nonneg_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WireViolation(f"{where} must be an object")
    return value


def parse_cost(value: object) -> CostReceipt:
    cost = _require_object(value, "cost")
    reserved = cost.get("reserved_microusd")
    settled = cost.get("settled_microusd")
    status = cost.get("settlement_status")
    if not _is_nonneg_int(reserved):
        raise WireViolation("cost.reserved_microusd must be a nonnegative integer")
    if settled is not None and not _is_nonneg_int(settled):
        raise WireViolation("cost.settled_microusd must be a nonnegative integer or null")
    if status not in SETTLEMENT_STATUSES:
        raise WireViolation("cost.settlement_status is unknown")
    assert isinstance(reserved, int)
    if status == "settled" and (settled is None or settled > reserved):
        raise WireViolation("settled cost must be non-null and no greater than the reservation")
    if status == "pending_reconciliation" and settled is not None:
        raise WireViolation("pending cost must have a null settled amount")
    if status == "reservation_forfeited" and settled != reserved:
        raise WireViolation("forfeited cost must settle at the full reservation")
    if status == "settlement_overrun" and (settled is None or settled <= reserved):
        raise WireViolation("overrun cost must exceed the reservation")
    return CostReceipt(reserved, settled, status)


def _parse_execution_receipt(value: object) -> ExecutionReceipt:
    execution = _require_object(value, "execution")
    execution_id = execution.get("execution_id")
    state = execution.get("state")
    if not isinstance(execution_id, str) or not UUID_V4_RE.fullmatch(execution_id):
        raise WireViolation("execution.execution_id must be a UUID v4")
    if state not in EXECUTION_STATES:
        raise WireViolation("execution.state is unknown")
    return ExecutionReceipt(execution_id, state)


def _decode_json(body: bytes) -> object:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise WireViolation("duplicate JSON member name")
            result[key] = item
        return result

    def reject_constant(token: str) -> object:
        raise WireViolation(f"non-finite JSON number {token}")

    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WireViolation("response body is not strict UTF-8 JSON") from exc


def _check_common_headers(headers: dict[str, str], *, request_id: str) -> None:
    content_type = _header(headers, "content-type") or ""
    if content_type.split(";")[0].strip().lower() != "application/json":
        raise WireViolation("response Content-Type must be application/json")
    if (_header(headers, "cache-control") or "").strip().lower() != "no-store":
        raise WireViolation("response must carry Cache-Control: no-store")
    if _header(headers, "set-cookie") is not None:
        raise WireViolation("response must not set cookies")
    encoding = (_header(headers, "content-encoding") or "identity").strip().lower()
    if encoding != "identity":
        raise WireViolation("response compression is prohibited")
    if _header(headers, "x-request-id") != request_id:
        raise WireViolation("response did not echo X-Request-ID")


def _release_headers(headers: dict[str, str]) -> tuple[str, str]:
    execution_release = _header(headers, "x-stoin-execution-release")
    policy_release = _header(headers, "x-stoin-execution-policy-release")
    if (
        execution_release is None
        or policy_release is None
        or not RELEASE_ID_RE.fullmatch(execution_release)
        or not RELEASE_ID_RE.fullmatch(policy_release)
    ):
        raise WireViolation("authenticated response is missing valid release headers")
    return execution_release, policy_release


def parse_success(
    *,
    status: int,
    headers: dict[str, str],
    body: bytes,
    request_id: str,
    prepared: PreparedRequest,
) -> ExecutionSuccess:
    """`headers` keys must already be lowercased."""
    if status != 200:
        raise WireViolation("success parsing requires HTTP 200")
    if len(body) > RESPONSE_BODY_MAX_BYTES:
        raise WireViolation("response body exceeds its byte bound")
    _check_common_headers(headers, request_id=request_id)
    execution_release, policy_release = _release_headers(headers)
    document = _require_object(_decode_json(body), "response")

    # Tolerant consumer (§17): unknown members are ignored; known members stay strict.
    if document.get("contract") != RESPONSE_CONTRACT:
        raise WireViolation("response contract discriminator is wrong")
    if document.get("request_id") != request_id:
        raise WireViolation("response request_id does not match this attempt")
    execution_id = document.get("execution_id")
    if not isinstance(execution_id, str) or not UUID_V4_RE.fullmatch(execution_id):
        raise WireViolation("execution_id must be a UUID v4")
    replayed = document.get("replayed")
    if not isinstance(replayed, bool):
        raise WireViolation("replayed must be a boolean")
    if document.get("execution_profile_id") != prepared.execution_profile_id:
        raise WireViolation("response names a different execution profile")
    profile_release_id = document.get("profile_release_id")
    if not isinstance(profile_release_id, str) or not RELEASE_ID_RE.fullmatch(profile_release_id):
        raise WireViolation("profile_release_id is malformed")
    if document.get("finish_reason") != "stop":
        raise WireViolation("finish_reason must be exactly 'stop'")

    output = _require_object(document.get("output"), "output")
    if output.get("mode") != "json_schema":
        raise WireViolation("output mode differs from the requested mode")
    content = output.get("content")
    if not isinstance(content, dict):
        raise WireViolation("json_schema output content must be a parsed JSON object")
    try:
        canonical_size = len(rfc8785.dumps(content))
    except Exception as exc:  # rfc8785 raises its own error types for non-canonicalizable data
        raise WireViolation("output content cannot be canonicalized") from exc
    if canonical_size > OUTPUT_CONTENT_MAX_BYTES:
        raise WireViolation("output content exceeds its canonical byte bound")
    # Homes never trusts provider-side enforcement alone: re-validate against Homes' own schema.
    validate_instance(prepared.output.schema, content, "$output")

    usage_doc = _require_object(document.get("usage"), "usage")
    input_tokens = usage_doc.get("input_tokens")
    generated_tokens = usage_doc.get("generated_tokens")
    output_tokens = usage_doc.get("output_tokens")
    reasoning_tokens = usage_doc.get("reasoning_tokens")
    if not _is_nonneg_int(input_tokens) or not _is_nonneg_int(generated_tokens):
        raise WireViolation("usage token counts must be nonnegative integers")
    assert isinstance(input_tokens, int) and isinstance(generated_tokens, int)
    if (output_tokens is None) != (reasoning_tokens is None):
        raise WireViolation("usage breakdown fields must both be integers or both be null")
    if output_tokens is not None:
        if not _is_nonneg_int(output_tokens) or not _is_nonneg_int(reasoning_tokens):
            raise WireViolation("usage breakdown fields must be nonnegative integers")
        assert isinstance(output_tokens, int) and isinstance(reasoning_tokens, int)
        if output_tokens + reasoning_tokens != generated_tokens:
            raise WireViolation("usage breakdown does not sum to generated_tokens")
    if generated_tokens > prepared.max_output_tokens:
        raise WireViolation("generated_tokens exceeds the requested ceiling")

    cost = parse_cost(document.get("cost"))
    if cost.settlement_status not in ("settled", "pending_reconciliation"):
        raise WireViolation("a success receipt must be settled or pending reconciliation")
    if cost.reserved_microusd > prepared.max_cost_microusd:
        raise WireViolation("reservation exceeds the declared per-call cost ceiling")

    return ExecutionSuccess(
        execution_id=execution_id,
        replayed=replayed,
        profile_release_id=profile_release_id,
        content=content,
        usage=Usage(input_tokens, generated_tokens, output_tokens, reasoning_tokens),
        cost=cost,
        execution_release=execution_release,
        policy_release=policy_release,
    )


def parse_error(
    *, status: int, headers: dict[str, str], body: bytes, request_id: str
) -> ExecutionError:
    """Strictly parses a non-200 response. Raises WireViolation for anything off-contract."""
    if status in (404, 405):
        # §6.2: ordinary private-service transport responses; body shape is not contractual.
        transport_code = "route_not_found" if status == 404 else "method_not_allowed"
        return ExecutionError(transport_code, False, None, None, None)
    if len(body) > RESPONSE_BODY_MAX_BYTES:
        raise WireViolation("response body exceeds its byte bound")
    _check_common_headers(headers, request_id=request_id)
    document = _require_object(_decode_json(body), "error response")
    if document.get("contract") != ERROR_CONTRACT:
        raise WireViolation("error contract discriminator is wrong")
    correlation_id = document.get("correlation_id")
    if not isinstance(correlation_id, str) or not UUID_V4_RE.fullmatch(correlation_id):
        raise WireViolation("correlation_id must be a UUID v4")
    header_correlation = _header(headers, "x-correlation-id")
    if header_correlation is None or not UUID_V4_RE.fullmatch(header_correlation):
        raise WireViolation("error response is missing X-Correlation-ID")
    if document.get("request_id") != request_id:
        raise WireViolation("error request_id does not match this attempt")

    error = _require_object(document.get("error"), "error")
    raw_code = error.get("code")
    if not isinstance(raw_code, str) or raw_code not in ERROR_TABLE:
        raise WireViolation("error code is unknown")
    code: str = raw_code
    spec = ERROR_TABLE[code]
    if status != spec.http_status:
        raise WireViolation("HTTP status does not match the error code")
    if error.get("message") != spec.message or error.get("retryable") is not spec.retryable:
        raise WireViolation("error message or retryable flag differs from the pinned table")

    if code in RELEASE_HEADERLESS_CODES:
        if _header(headers, "x-stoin-execution-release") or _header(
            headers, "x-stoin-execution-policy-release"
        ):
            raise WireViolation("authentication responses must not disclose release headers")
    else:
        _release_headers(headers)

    retry_after_raw = _header(headers, "retry-after")
    retry_after: int | None = None
    if code in RETRY_AFTER_CODES:
        if retry_after_raw is not None:
            if not re.fullmatch(r"[0-9]{1,2}", retry_after_raw):
                raise WireViolation("Retry-After must be an integer")
            retry_after = int(retry_after_raw)
            if not RETRY_AFTER_MIN_SECONDS <= retry_after <= RETRY_AFTER_MAX_SECONDS:
                raise WireViolation("Retry-After is out of bounds")
    elif retry_after_raw is not None:
        raise WireViolation(f"Retry-After is not permitted for {code}")

    execution = _parse_execution_receipt(document["execution"]) if "execution" in document else None
    cost = parse_cost(document["cost"]) if "cost" in document else None
    if code in RECEIPTLESS_CODES and (execution is not None or cost is not None):
        raise WireViolation(f"{code} must omit execution and cost receipts")
    if code in POST_DISPATCH_TERMINAL_CODES and (
        execution is None or cost is None or execution.state != "failed"
    ):
        raise WireViolation(f"{code} requires a failed execution receipt and a cost receipt")
    if code == "execution_aborted" and (
        execution is None
        or execution.state != "failed"
        or cost is None
        or cost.settlement_status != "settled"
        or cost.settled_microusd != 0
    ):
        raise WireViolation("execution_aborted requires a failed receipt settled at zero")
    if code == "execution_outcome_unknown" and (
        execution is None or execution.state != "outcome_unknown"
    ):
        raise WireViolation("execution_outcome_unknown requires an outcome_unknown receipt")
    if code == "deadline_exceeded" and (
        execution is None or execution.state not in ("failed", "outcome_unknown") or cost is None
    ):
        raise WireViolation("deadline_exceeded requires a failed or outcome_unknown receipt")

    return ExecutionError(code, spec.retryable, retry_after, execution, cost)
