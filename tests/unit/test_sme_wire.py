"""RC1 caller-side wire rules: request construction, the restricted schema subset, and strict
response/error parsing. Expected values are re-derived here with hashlib/base64 rather than by
calling the functions under test."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import uuid

import pytest
from fixtures.homes_knowledge import synthetic_corpus

from utopia_homes_prime.guest_answer import homes_prime
from utopia_homes_prime.inference import sme_wire
from utopia_homes_prime.inference.sme_wire import (
    ExecutionMessage,
    JsonSchemaOutput,
    WireViolation,
    check_restricted_schema,
    parse_error,
    parse_success,
    prepare_request,
    validate_instance,
)
from utopia_homes_prime.knowledge.projection import KnowledgeEntry

KEY = "0b4f8a2e-6c1d-4e3a-9f5b-7d2c1e0a9b8c"
VERDICT = homes_prime.verdict_schema()


def _prepared(**overrides):
    kwargs = {
        "execution_profile_id": "utopia-homes.public-answer.support-review.v1",
        "idempotency_key": KEY,
        "messages": (
            ExecutionMessage("system", "policy"),
            ExecutionMessage("user", "Is there a pool?"),
        ),
        "output": JsonSchemaOutput("homes-support-verdict", VERDICT),
        "max_output_tokens": 300,
        "max_cost_microusd": 20_000,
    }
    kwargs.update(overrides)
    return prepare_request(**kwargs)


def _b64url_sha256(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


# --- request construction ------------------------------------------------------------------------


def test_request_body_digest_and_binding_are_exact():
    prepared = _prepared()
    document = json.loads(prepared.body)
    assert document == {
        "contract": "stoin.inference.execute.request.v1",
        "execution_profile_id": "utopia-homes.public-answer.support-review.v1",
        "messages": [
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "Is there a pool?"},
        ],
        "output": {"mode": "json_schema", "name": "homes-support-verdict", "schema": VERDICT},
        "limits": {"max_output_tokens": 300, "max_cost_microusd": 20_000},
    }
    assert prepared.body_sha256_b64url == _b64url_sha256(prepared.body)
    assert len(prepared.body_sha256_b64url) == 43
    expected_req = _b64url_sha256(
        f"POST\n/execution/v1/inference\n{KEY}\n{_b64url_sha256(prepared.body)}".encode()
    )
    assert prepared.request_binding == expected_req


def test_non_ascii_content_is_serialized_as_utf8_not_escapes():
    prepared = _prepared(
        messages=(ExecutionMessage("system", "policy"), ExecutionMessage("user", "Café’s pool?"))
    )
    assert "Café’s".encode() in prepared.body


def test_attempt_headers_are_exact():
    prepared = _prepared()
    request_id = str(uuid.uuid4())
    headers = sme_wire.attempt_headers(prepared, token="t", request_id=request_id, timeout_ms=4000)
    assert headers == {
        "Authorization": "Bearer t",
        "X-Request-ID": request_id,
        "Idempotency-Key": KEY,
        "X-Execution-Timeout-Ms": "4000",
        "X-Content-SHA256": prepared.body_sha256_b64url,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    for bad in (999, 18_001):
        with pytest.raises(WireViolation):
            sme_wire.attempt_headers(prepared, token="t", request_id=request_id, timeout_ms=bad)


@pytest.mark.parametrize(
    "messages",
    [
        (ExecutionMessage("user", "hi"),),
        (ExecutionMessage("user", "hi"), ExecutionMessage("assistant", "x")),
        (ExecutionMessage("system", "p"), ExecutionMessage("assistant", "x")),
        (
            ExecutionMessage("system", "p"),
            ExecutionMessage("user", "a"),
            ExecutionMessage("assistant", "b"),
        ),
        (ExecutionMessage("system", "p"), ExecutionMessage("system", "q")),
        (ExecutionMessage("system", "p"), ExecutionMessage("user", "")),
        (ExecutionMessage("system", "p"), ExecutionMessage("user", "nul\x00")),
        (ExecutionMessage("system", "p"), ExecutionMessage("user", "bad \ud800")),
        (ExecutionMessage("system", "p"), ExecutionMessage("user", "x" * 65_537)),
        (ExecutionMessage("system", "p" * 60_000), ExecutionMessage("user", "é" * 70_000)),
    ],
)
def test_message_rules_fail_before_send(messages):
    with pytest.raises(WireViolation):
        _prepared(messages=messages)


def test_request_bounds_fail_before_send():
    for overrides in (
        {"execution_profile_id": "Not-A-Stable-Id"},
        {"idempotency_key": KEY.upper()},
        {"max_output_tokens": 0},
        {"max_output_tokens": 4_097},
        {"max_cost_microusd": 0},
        {"max_cost_microusd": 1_000_001},
        {"output": JsonSchemaOutput("bad name", VERDICT)},
    ):
        with pytest.raises(WireViolation):
            _prepared(**overrides)


def test_thirty_two_messages_accepted_and_thirty_four_rejected():
    """32 is the RC1 maximum. 34 is the next well-ordered count (system + odd turns ending in
    user), so its rejection comes from the count bound rather than role ordering."""

    def conversation(count: int):
        turns = [ExecutionMessage("system", "p")]
        for index in range(count - 1):
            turns.append(ExecutionMessage("user" if index % 2 == 0 else "assistant", "t"))
        return tuple(turns)

    _prepared(messages=conversation(32))
    with pytest.raises(WireViolation, match="entries"):
        _prepared(messages=conversation(34))


# --- restricted schema subset --------------------------------------------------------------------


def _knowledge():
    from datetime import UTC, datetime

    from utopia_homes_prime.knowledge.projection import KnowledgeProjection

    entries = tuple(KnowledgeEntry.model_validate(e) for e in synthetic_corpus()["entries"])
    projection = KnowledgeProjection(
        release_id="k",
        corpus_digest="d",
        entries=entries,
        withdrawn_ids=frozenset(),
        approved_hostnames=frozenset({"www.utopiahomes.com"}),
    )
    return projection.effective(datetime.now(UTC))


def test_homes_output_schemas_are_inside_the_rc1_subset():
    check_restricted_schema(homes_prime.draft_schema(_knowledge()))
    check_restricted_schema(VERDICT)


def _object(**properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _nest(depth: int):
    node = {"type": "string"}
    for _ in range(depth - 1):
        node = _object(child=node)
    return node


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "items": {"type": "string"}},
        {
            "type": ["object", "null"],
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
        {"type": "object", "properties": {}, "required": []},
        _object(a={"$ref": "#/defs/x"}),
        _object(a={"type": "string", "pattern": "^x$"}),
        _object(a={"type": "string", "format": "uri"}),
        _object(a={"anyOf": [{"type": "string"}]}),
        _object(a={"type": ["string", "integer"]}),
        _object(a={"type": ["null", "null"]}),
        _object(a={"type": ["string", "null"], "enum": ["x"]}),
        _object(a={"type": ["string", "null"], "enum": ["x", None, None]}),
        _object(a={"type": "string", "enum": ["x", None]}),
        _object(a={"type": ["string", "null"], "const": "x"}),
        _object(a={"type": "integer", "maximum": 2**53}),
        _object(a={"type": "number", "minimum": float("inf")}),
        _object(a={"type": "string", "enum": [str(i) for i in range(65)]}),
        _object(a={"type": "string", "items": {"type": "string"}}),
        _nest(9),
        _object(**{f"p{i}": {"type": "string"} for i in range(129)}),
        _object(
            a={
                "type": "string",
                "enum": ["x" * 400 for _ in range(1)] + [str(i) * 500 for i in range(63)],
            }
        ),
    ],
)
def test_schema_subset_violations_are_rejected(schema):
    with pytest.raises(WireViolation):
        check_restricted_schema(schema)


def test_schema_subset_boundaries_are_accepted():
    check_restricted_schema(_nest(8))
    check_restricted_schema(_object(**{f"p{i}": {"type": "string"} for i in range(128)}))
    check_restricted_schema(_object(a={"type": "string", "enum": [str(i) for i in range(64)]}))
    check_restricted_schema(_object(a={"type": ["string", "null"], "enum": ["x", None]}))
    check_restricted_schema(_object(a={"type": ["string", "null"], "const": None}))
    check_restricted_schema(
        _object(a={"type": "integer", "minimum": -(2**53 - 1), "maximum": 2**53 - 1})
    )


def test_validate_instance_is_strict():
    validate_instance(
        VERDICT, {"supported": True, "unsupported_segment_indexes": [], "reason_codes": []}
    )
    for bad in (
        {"supported": 1, "unsupported_segment_indexes": [], "reason_codes": []},
        {"supported": True, "unsupported_segment_indexes": [True], "reason_codes": []},
        {"supported": True, "unsupported_segment_indexes": [8], "reason_codes": []},
        {"supported": True, "unsupported_segment_indexes": [], "reason_codes": ["made_up"]},
        {"supported": True, "unsupported_segment_indexes": []},
        {"supported": True, "unsupported_segment_indexes": [], "reason_codes": [], "extra": 1},
        {"supported": True, "unsupported_segment_indexes": list(range(9)), "reason_codes": []},
    ):
        with pytest.raises(WireViolation):
            validate_instance(VERDICT, bad)


# --- success parsing -----------------------------------------------------------------------------

REQUEST_ID = "5f0c7c1e-8a3b-4d2e-9c1f-2b3a4c5d6e7f"


def _success_document(**overrides):
    document = {
        "contract": "stoin.inference.execute.response.v1",
        "request_id": REQUEST_ID,
        "execution_id": "59eeddf3-35a1-4d22-a51e-08acf5d5f34d",
        "replayed": False,
        "execution_profile_id": "utopia-homes.public-answer.support-review.v1",
        "profile_release_id": "profiles-2026-09-17.1",
        "output": {
            "mode": "json_schema",
            "content": {"supported": True, "unsupported_segment_indexes": [], "reason_codes": []},
        },
        "finish_reason": "stop",
        "usage": {
            "input_tokens": 10,
            "generated_tokens": 5,
            "output_tokens": 4,
            "reasoning_tokens": 1,
        },
        "cost": {
            "reserved_microusd": 2000,
            "settled_microusd": 417,
            "settlement_status": "settled",
        },
    }
    document.update(overrides)
    return document


def _headers(**overrides):
    headers = {
        "content-type": "application/json",
        "cache-control": "no-store",
        "x-request-id": REQUEST_ID,
        "x-stoin-execution-release": "execution-1",
        "x-stoin-execution-policy-release": "policy-1",
    }
    headers.update(overrides)
    return {k: v for k, v in headers.items() if v is not None}


def _parse_success(document=None, headers=None, body=None):
    return parse_success(
        status=200,
        headers=headers or _headers(),
        body=body if body is not None else json.dumps(document or _success_document()).encode(),
        request_id=REQUEST_ID,
        prepared=_prepared(),
    )


def test_success_parses_and_tolerates_unknown_members():
    result = _parse_success(_success_document(future_member={"x": 1}))
    assert result.execution_id == "59eeddf3-35a1-4d22-a51e-08acf5d5f34d"
    assert result.cost.settled_microusd == 417
    paired_null = _success_document(
        usage={
            "input_tokens": 1,
            "generated_tokens": 5,
            "output_tokens": None,
            "reasoning_tokens": None,
        },
        cost={
            "reserved_microusd": 2000,
            "settled_microusd": None,
            "settlement_status": "pending_reconciliation",
        },
    )
    assert _parse_success(paired_null).usage.output_tokens is None


def _mutated(path, value):
    document = copy.deepcopy(_success_document())
    target = document
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return document


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("contract",), "stoin.inference.execute.response.v2"),
        (("request_id",), str(uuid.uuid4())),
        (("execution_id",), "not-a-uuid"),
        (("replayed",), "false"),
        (("execution_profile_id",), "utopia-homes.public-answer.generate.v1"),
        (("profile_release_id",), "has space"),
        (("finish_reason",), "length"),
        (("output", "mode"), "text"),
        (("output", "content"), '{"supported": true}'),
        (("output", "content", "supported"), "yes"),
        (("output", "content", "extra"), 1),
        (("usage", "input_tokens"), -1),
        (("usage", "generated_tokens"), 301),
        (("usage", "output_tokens"), None),
        (("usage", "reasoning_tokens"), 2),
        (("usage", "generated_tokens"), True),
        (("cost", "settlement_status"), "reservation_forfeited"),
        (("cost", "settlement_status"), "mystery"),
        (("cost", "settled_microusd"), 2001),
        (("cost", "settled_microusd"), None),
        (("cost", "reserved_microusd"), 20_001),
        (("usage",), KeyError),
    ],
)
def test_success_violations_fail_closed(path, value):
    with pytest.raises(WireViolation):
        _parse_success(_mutated(path, value))


@pytest.mark.parametrize(
    "headers",
    [
        _headers(**{"x-request-id": str(uuid.uuid4())}),
        _headers(**{"cache-control": None}),
        _headers(**{"content-type": "text/json"}),
        _headers(**{"x-stoin-execution-release": None}),
        _headers(**{"x-stoin-execution-policy-release": "bad value"}),
        _headers(**{"set-cookie": "a=b"}),
        _headers(**{"content-encoding": "gzip"}),
    ],
)
def test_success_header_violations_fail_closed(headers):
    with pytest.raises(WireViolation):
        _parse_success(headers=headers)


def test_success_body_encoding_violations_fail_closed():
    for body in (
        b'{"contract": "a", "contract": "b"}',
        b"\xff\xfe",
        b"x" * (sme_wire.RESPONSE_BODY_MAX_BYTES + 1),
        json.dumps(_success_document()).replace("417", "NaN").encode(),
    ):
        with pytest.raises(WireViolation):
            _parse_success(body=body)


# --- error parsing -------------------------------------------------------------------------------


def _error_body(code, *, execution=None, cost=None, request_id=REQUEST_ID):
    spec = sme_wire.ERROR_TABLE[code]
    document = {
        "contract": "stoin.inference.execute.error.v1",
        "correlation_id": str(uuid.uuid4()),
        "request_id": request_id,
        "error": {"code": code, "message": spec.message, "retryable": spec.retryable},
    }
    if execution is not None:
        document["execution"] = {"execution_id": str(uuid.uuid4()), "state": execution}
    if cost is not None:
        document["cost"] = cost
    return document


SETTLED = {"reserved_microusd": 1000, "settled_microusd": 400, "settlement_status": "settled"}
ZERO = {"reserved_microusd": 1000, "settled_microusd": 0, "settlement_status": "settled"}
PENDING = {
    "reserved_microusd": 1000,
    "settled_microusd": None,
    "settlement_status": "pending_reconciliation",
}


def _parse_error(code, *, status=None, headers=None, **body_kwargs):
    document = _error_body(code, **body_kwargs)
    merged = _headers(**{"x-correlation-id": document["correlation_id"]})
    if sme_wire.ERROR_TABLE[code].retryable:
        merged["retry-after"] = "3"
    if code in sme_wire.RELEASE_HEADERLESS_CODES:
        merged.pop("x-stoin-execution-release")
        merged.pop("x-stoin-execution-policy-release")
    merged.update(headers or {})
    merged = {k: v for k, v in merged.items() if v is not None}
    return parse_error(
        status=status or sme_wire.ERROR_TABLE[code].http_status,
        headers=merged,
        body=json.dumps(document).encode(),
        request_id=REQUEST_ID,
    )


@pytest.mark.parametrize(
    ("code", "kwargs"),
    [
        ("invalid_request", {}),
        ("authentication_failed", {}),
        ("authentication_state_unavailable", {}),
        ("idempotency_conflict", {}),
        ("state_store_unavailable", {}),
        ("request_in_progress", {}),
        ("rate_limited", {}),
        ("spending_authority_exhausted", {}),
        ("execution_aborted", {"execution": "failed", "cost": ZERO}),
        ("execution_outcome_unknown", {"execution": "outcome_unknown", "cost": PENDING}),
        ("execution_invalidated", {"execution": "completed", "cost": SETTLED}),
        ("content_filtered", {"execution": "failed", "cost": SETTLED}),
        ("provider_execution_failed", {"execution": "failed", "cost": PENDING}),
        (
            "cost_settlement_violation",
            {
                "execution": "failed",
                "cost": {
                    "reserved_microusd": 1000,
                    "settled_microusd": 1200,
                    "settlement_status": "settlement_overrun",
                },
            },
        ),
        ("deadline_exceeded", {"execution": "outcome_unknown", "cost": PENDING}),
        ("deadline_exceeded", {"execution": "failed", "cost": SETTLED}),
    ],
)
def test_error_receipts_accepted(code, kwargs):
    parsed = _parse_error(code, **kwargs)
    assert parsed.code == code
    assert parsed.retryable is sme_wire.ERROR_TABLE[code].retryable
    if parsed.retryable:
        assert parsed.retry_after_seconds == 3


@pytest.mark.parametrize(
    ("code", "kwargs"),
    [
        ("idempotency_conflict", {"execution": "failed", "cost": SETTLED}),
        ("state_store_unavailable", {"cost": PENDING}),
        ("content_filtered", {}),
        ("content_filtered", {"execution": "completed", "cost": SETTLED}),
        ("execution_aborted", {"execution": "failed", "cost": SETTLED}),
        ("execution_outcome_unknown", {"execution": "failed", "cost": PENDING}),
        ("deadline_exceeded", {"execution": "dispatched", "cost": PENDING}),
        ("rate_limited", {"status": 503}),
        ("temporarily_unavailable", {"headers": {"retry-after": "31"}}),
        ("temporarily_unavailable", {"headers": {"retry-after": "soon"}}),
        ("invalid_request", {"headers": {"retry-after": "1"}}),
        ("authentication_failed", {"headers": {"x-stoin-execution-release": "leak"}}),
        ("capability_forbidden", {"headers": {"x-stoin-execution-policy-release": None}}),
        ("invalid_request", {"headers": {"x-correlation-id": None}}),
        ("invalid_request", {"request_id": str(uuid.uuid4())}),
        (
            "execution_invalidated",
            {
                "execution": "completed",
                "cost": {
                    "reserved_microusd": 5,
                    "settled_microusd": 4,
                    "settlement_status": "reservation_forfeited",
                },
            },
        ),
    ],
)
def test_error_violations_fail_closed(code, kwargs):
    with pytest.raises(WireViolation):
        _parse_error(code, **kwargs)


def test_error_message_and_retryable_must_be_exact():
    document = _error_body("temporarily_unavailable")
    headers = _headers(**{"x-correlation-id": document["correlation_id"], "retry-after": "1"})
    for field, value in (
        ("message", "Model execution is temporarily unavailable"),
        ("retryable", False),
    ):
        tampered = copy.deepcopy(document)
        tampered["error"][field] = value
        with pytest.raises(WireViolation):
            parse_error(
                status=503,
                headers=headers,
                body=json.dumps(tampered).encode(),
                request_id=REQUEST_ID,
            )
    unknown = copy.deepcopy(document)
    unknown["error"]["code"] = "new_future_code"
    with pytest.raises(WireViolation):
        parse_error(
            status=503, headers=headers, body=json.dumps(unknown).encode(), request_id=REQUEST_ID
        )


def test_transport_404_and_405_are_terminal_without_contract_bodies():
    for status, code in ((404, "route_not_found"), (405, "method_not_allowed")):
        parsed = parse_error(status=status, headers={}, body=b"<html>", request_id=REQUEST_ID)
        assert (parsed.code, parsed.retryable) == (code, False)
