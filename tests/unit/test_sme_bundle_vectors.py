"""Replays the vendored Shared Model Execution v1 RC1 bundle's own vectors through sme_wire.py's
actual caller-side functions -- pinned to bundle digest
sha256:5185680e2cb9ac9aff6006c9abc6a582b67933db077d5c7bd5dcc596f574cb85 (verified separately by
contracts/stoin-shared-model-execution-v1-rc1-bundle/tools/compute_digest.py --check). Unlike
test_sme_wire.py's hand-built fixtures, every document here is loaded byte-for-byte from the
frozen bundle, so a change to the bundle's own vectors would surface here as a test change, not
just as a digest mismatch.

Two things make a literal, unmodified replay impossible for some vectors, both handled explicitly
below rather than silently worked around:

1. **Output mode.** Homes Prime Stage 2 only ever requests/accepts `json_schema`-mode output (see
   the "structured output" decision in docs/homes-prime-stage2.md) and parse_success() rejects
   any other mode outright (sme_wire.py:727). The bundle's own generic response/invariant vectors
   default to `text` mode. For vectors that must be ACCEPTED (the tolerant-consumer proof and the
   usage/cost invariant positives), `_as_json_schema_output()` substitutes a compatible `output`
   block while keeping every other vendored field verbatim. For vectors that must be REJECTED,
   each negative-vector section below has two tests: a "_literal" one that replays the vector
   unmodified (proves *some* WireViolation fires, which for text-mode vectors may be the
   categorical mode check rather than the vector's own named rule), and a
   "_reject_for_their_named_rule" one that adapts the output to json_schema mode where needed and
   asserts the *exact* WireViolation message the vector's own `rule` field names -- real proof of
   the specific check, not just that rejection happened for some reason. Which rule actually fires
   for the unmodified literal replay is stated per vector below, confirmed by running it, not
   assumed from check order.

2. **Error receipts.** `error.pos.*.json` vectors test only the §16 exact message/retryable tuple
   (their own `rule` field says so) and omit the `execution`/`cost` receipts that
   POST_DISPATCH_TERMINAL_CODES and a few other codes additionally require.
   `_augment_error_document` adds those receipts the same way test_sme_wire.py's own hand-built
   fixtures already do via `execution=`/`cost=` kwargs, sourced from the vendored vector instead
   of a fresh document.

Deliberately NOT replayed here, as a documented scope boundary rather than a gap:

- `vectors/auth/*.json` -- JWT claim acceptance criteria for a token RECEIVER (Tiamat). Homes is
  the token issuer only (sme_client.py's ExecutionIdentity always builds compliant claims from
  trusted internal state); there is no untrusted input for a caller-side validator to reject.
- `vectors/state/*.json` -- Tiamat's own server-side dispatch/lease/idempotency state machine,
  already covered by Tiamat's own unit and integration tests per the implementation checkpoint.
  Homes does not implement or observe that state machine directly.
- `error.neg.message.{route_not_found,method_not_allowed}.json` and the matching `.retry.` pair --
  RC1 §6.2 (and sme_wire.py's own comment at the top of parse_error) treats 404/405 as ordinary
  transport responses whose body is not contract-shaped; parse_error() correctly returns before
  ever decoding the body for these two codes, so a corrupted message/retryable field in the body
  is unreachable and cannot be detected -- these vectors are skipped with a reason, not silently
  dropped.
"""

from __future__ import annotations

import json
import re

import pytest
from sme_vector_helpers import VECTORS, load_json, vectors_in

from guest_answer_provider import sme_wire
from guest_answer_provider.sme_wire import (
    ExecutionMessage,
    JsonSchemaOutput,
    WireViolation,
    attempt_headers,
    parse_error,
    parse_success,
    prepare_request,
)

KEY = "0b4f8a2e-6c1d-4e3a-9f5b-7d2c1e0a9b8c"

_GENERATE_REQUEST_DOC = load_json(VECTORS / "positive" / "request.pos.json.json")["document"]
GENERATE_SCHEMA = _GENERATE_REQUEST_DOC["output"]["schema"]
BASE_MESSAGES = tuple(
    ExecutionMessage(m["role"], m["content"]) for m in _GENERATE_REQUEST_DOC["messages"]
)
BASE_OUTPUT = JsonSchemaOutput(_GENERATE_REQUEST_DOC["output"]["name"], GENERATE_SCHEMA)
ANSWER_CONTENT = {"answer": "Buttercup is a Utopia home."}


def _generate_prepared(*, max_output_tokens: int = 900, max_cost_microusd: int = 2000):
    return prepare_request(
        execution_profile_id=_GENERATE_REQUEST_DOC["execution_profile_id"],
        idempotency_key=KEY,
        messages=BASE_MESSAGES,
        output=BASE_OUTPUT,
        max_output_tokens=max_output_tokens,
        max_cost_microusd=max_cost_microusd,
    )


def _generate_headers(*, request_id: str) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "cache-control": "no-store",
        "x-request-id": request_id,
        "x-stoin-execution-release": "tiamat-local.1",
        "x-stoin-execution-policy-release": "profiles-local.1",
    }


def _parse_generate_success(document: dict):
    return parse_success(
        status=200,
        headers=_generate_headers(request_id=document["request_id"]),
        body=json.dumps(document).encode(),
        request_id=document["request_id"],
        prepared=_generate_prepared(),
    )


def _as_json_schema_output(document: dict, *, content: dict) -> dict:
    adapted = dict(document)
    adapted["output"] = {"mode": "json_schema", "content": content}
    return adapted


# --- response vectors (direct replay, already json_schema mode) -----------------------------------


def test_response_pos_json_vector_accepted():
    vector = load_json(VECTORS / "positive" / "response.pos.json.json")
    result = _parse_generate_success(vector["document"])
    assert result.execution_id == vector["document"]["execution_id"]
    assert result.content == ANSWER_CONTENT


@pytest.mark.parametrize(
    "path",
    [
        VECTORS / "negative" / "response.bad-finish.json",
        VECTORS / "negative" / "response.bad-uuid.json",
        VECTORS / "negative" / "response.mode-shape.json",
    ],
    ids=lambda p: p.stem,
)
def test_response_negative_vectors_fail_closed_literal(path):
    """Literal RC1 vector replay, byte-for-byte, no adaptation: proves the response is rejected.
    For bad-finish/bad-uuid this already happens to hit the exact named rule, because sme_wire.py
    checks finish_reason (line 723) and execution_id (line 712) before the output-mode check (line
    727) -- but this test doesn't assert that, only that *some* WireViolation was raised, so it
    would keep passing even if a future check-ordering change let the mode mismatch mask the named
    rule instead. See test_response_negative_vectors_reject_for_their_named_rule below for the
    assertion that pins the exact message and would catch that regression."""
    vector = load_json(path)
    with pytest.raises(WireViolation):
        _parse_generate_success(vector["document"])


@pytest.mark.parametrize(
    ("path", "expected_message"),
    [
        (VECTORS / "negative" / "response.bad-finish.json", "finish_reason must be exactly 'stop'"),
        (VECTORS / "negative" / "response.bad-uuid.json", "execution_id must be a UUID v4"),
        (
            VECTORS / "negative" / "response.mode-shape.json",
            "json_schema output content must be a parsed JSON object",
        ),
    ],
    ids=lambda x: x if isinstance(x, str) else x.stem,
)
def test_response_negative_vectors_reject_for_their_named_rule(path, expected_message):
    """Homes-adapted fixture, not a literal bundle vector: response.mode-shape is already
    json_schema mode and needs no change. bad-finish/bad-uuid are adapted to json_schema mode
    (content becomes a dict, preserving the vector's own deliberate mutation) so the categorical
    mode check from the literal-replay test above cannot mask which rule actually fires -- this
    asserts the exact WireViolation message the vector's own `rule` field names, not just that
    *some* WireViolation was raised."""
    vector = load_json(path)
    document = vector["document"]
    if document["output"]["mode"] != "json_schema":
        document = _as_json_schema_output(document, content=ANSWER_CONTENT)
    with pytest.raises(WireViolation, match=re.escape(expected_message)):
        _parse_generate_success(document)


# --- tolerant-consumer proof: permitted additive fields are accepted ------------------------------


def test_response_unknown_vector_is_accepted_as_additive():
    """§10 provider-neutral strict output: response.unknown.json is schema-INVALID for the
    strict provider schema (its own `valid: false` / additionalProperties:false) because of the
    extra top-level `model` member -- but a tolerant consumer must accept it. Adapted to
    json_schema mode per this file's module docstring; the `model` member is preserved."""
    vector = load_json(VECTORS / "negative" / "response.unknown.json")
    assert "model" in vector["document"]
    document = _as_json_schema_output(vector["document"], content=ANSWER_CONTENT)
    assert document["model"] == vector["document"]["model"]
    result = _parse_generate_success(document)
    assert result.execution_id == vector["document"]["execution_id"]


# --- usage/cost invariants (I-02, I-03), adapted to json_schema mode ------------------------------


@pytest.mark.parametrize(
    "path",
    [
        VECTORS / "invariants" / "inv.usage.pos.json",
        VECTORS / "invariants" / "inv.usage.pos.combined.json",
        VECTORS / "invariants" / "inv.cost.pos.pending.json",
        VECTORS / "invariants" / "inv.cost.pos.settled.json",
    ],
    ids=lambda p: p.stem,
)
def test_usage_cost_invariant_positive_vectors_accepted(path):
    vector = load_json(path)
    assert vector["holds"] is True
    document = _as_json_schema_output(vector["document"], content=ANSWER_CONTENT)
    _parse_generate_success(document)


@pytest.mark.parametrize(
    "path",
    [
        VECTORS / "invariants" / "inv.usage.neg.sum.json",
        VECTORS / "invariants" / "inv.usage.neg.partial-null.json",
        VECTORS / "invariants" / "inv.cost.neg.over.json",
    ],
    ids=lambda p: p.stem,
)
def test_usage_cost_invariant_negative_vectors_fail_closed_literal(path):
    """Literal replay: these vectors are text-mode in the bundle, and sme_wire.py's output-mode
    check (line 727) runs before the usage/cost checks (lines 741+), so this genuinely fails via
    the categorical mode mismatch -- confirmed empirically, not assumed -- rather than the named
    usage/cost arithmetic rule. See the adapted test below for proof of the named rule itself."""
    vector = load_json(path)
    assert vector["holds"] is False
    with pytest.raises(WireViolation):
        _parse_generate_success(vector["document"])


@pytest.mark.parametrize(
    ("path", "expected_message"),
    [
        (
            VECTORS / "invariants" / "inv.usage.neg.sum.json",
            "usage breakdown does not sum to generated_tokens",
        ),
        (
            VECTORS / "invariants" / "inv.usage.neg.partial-null.json",
            "usage breakdown fields must both be integers or both be null",
        ),
        (
            VECTORS / "invariants" / "inv.cost.neg.over.json",
            "settled cost must be non-null and no greater than the reservation",
        ),
    ],
    ids=lambda x: x if isinstance(x, str) else x.stem,
)
def test_usage_cost_invariant_negative_vectors_reject_for_their_named_rule(path, expected_message):
    """Homes-adapted fixture: substitutes a json_schema output block so the categorical mode
    check can't mask the usage/cost arithmetic rule under test, then asserts the exact message
    naming that rule -- I-02/I-03's own arithmetic, not just "some WireViolation fired"."""
    vector = load_json(path)
    assert vector["holds"] is False
    document = _as_json_schema_output(vector["document"], content=ANSWER_CONTENT)
    with pytest.raises(WireViolation, match=re.escape(expected_message)):
        _parse_generate_success(document)


# --- messages invariant (I-01), mode-independent --------------------------------------------------


@pytest.mark.parametrize(
    "path", vectors_in("invariants", "inv.messages.*.json"), ids=lambda p: p.stem
)
def test_messages_invariant_vectors(path):
    vector = load_json(path)
    messages = tuple(
        ExecutionMessage(m["role"], m["content"]) for m in vector["document"]["messages"]
    )
    if vector["holds"]:
        sme_wire._validate_messages(messages)
    else:
        with pytest.raises(WireViolation):
            sme_wire._validate_messages(messages)


# --- request construction bounds, replayed against prepare_request() ------------------------------


def test_request_cost_limit_vector_rejected():
    vector = load_json(VECTORS / "negative" / "request.cost-limit.json")
    limits = vector["document"]["limits"]
    with pytest.raises(WireViolation):
        prepare_request(
            execution_profile_id=vector["document"]["execution_profile_id"],
            idempotency_key=KEY,
            messages=BASE_MESSAGES,
            output=BASE_OUTPUT,
            max_output_tokens=limits["max_output_tokens"],
            max_cost_microusd=limits["max_cost_microusd"],
        )


def test_request_output_limit_vector_rejected():
    vector = load_json(VECTORS / "negative" / "request.output-limit.json")
    limits = vector["document"]["limits"]
    with pytest.raises(WireViolation):
        prepare_request(
            execution_profile_id=vector["document"]["execution_profile_id"],
            idempotency_key=KEY,
            messages=BASE_MESSAGES,
            output=BASE_OUTPUT,
            max_output_tokens=limits["max_output_tokens"],
            max_cost_microusd=limits["max_cost_microusd"],
        )


def test_request_messages_short_vector_rejected():
    vector = load_json(VECTORS / "negative" / "request.messages-short.json")
    messages = tuple(
        ExecutionMessage(m["role"], m["content"]) for m in vector["document"]["messages"]
    )
    limits = vector["document"]["limits"]
    with pytest.raises(WireViolation):
        prepare_request(
            execution_profile_id=vector["document"]["execution_profile_id"],
            idempotency_key=KEY,
            messages=messages,
            output=BASE_OUTPUT,
            max_output_tokens=limits["max_output_tokens"],
            max_cost_microusd=limits["max_cost_microusd"],
        )


def test_prepare_request_only_ever_emits_the_closed_member_set():
    """request.bad-contract / request.unknown / request.output-extra (§9 exact contract, rejects
    unknown members, no provider parameters) describe documents a strict PROVIDER must reject.
    Homes never receives inbound requests to validate against those rules -- it only ever BUILDS
    requests via prepare_request(), which structurally cannot produce a wrong contract, an unknown
    top-level member, or extra provider parameters: it always emits exactly this fixed key set."""
    prepared = _generate_prepared()
    assert set(prepared.document) == set(_GENERATE_REQUEST_DOC)
    assert prepared.document["contract"] == _GENERATE_REQUEST_DOC["contract"]


# --- headers vectors ------------------------------------------------------------------------------


def _lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


@pytest.mark.parametrize(
    ("path", "expect_ok"),
    [
        (VECTORS / "headers" / "headers.pos.success.json", True),
        (VECTORS / "headers" / "headers.neg.cookie.json", False),
    ],
    ids=lambda x: x if isinstance(x, bool) else x.stem,
)
def test_authenticated_response_headers_vectors(path, expect_ok):
    vector = load_json(path)
    assert vector["valid"] is expect_ok
    document = _as_json_schema_output(
        load_json(VECTORS / "positive" / "response.pos.json.json")["document"],
        content=ANSWER_CONTENT,
    )
    headers = _lower_headers(vector["headers"])

    def call():
        return parse_success(
            status=200,
            headers=headers,
            body=json.dumps(document).encode(),
            request_id=document["request_id"],
            prepared=_generate_prepared(),
        )

    if expect_ok:
        call()
    else:
        with pytest.raises(WireViolation):
            call()


@pytest.mark.parametrize(
    ("path", "expect_ok"),
    [
        (VECTORS / "headers" / "headers.pos.request.json", True),
        (VECTORS / "headers" / "headers.neg.timeout.json", False),
    ],
    ids=lambda x: x if isinstance(x, bool) else x.stem,
)
def test_request_headers_timeout_vectors(path, expect_ok):
    vector = load_json(path)
    assert vector["valid"] is expect_ok
    headers = vector["headers"]

    def call():
        return attempt_headers(
            _generate_prepared(),
            token="t",
            request_id=headers["X-Request-ID"],
            timeout_ms=int(headers["X-Execution-Timeout-Ms"]),
        )

    if expect_ok:
        call()
    else:
        with pytest.raises(WireViolation):
            call()


# --- error vectors, replayed against parse_error() ------------------------------------------------


_POST_DISPATCH_RECEIPT = {
    "reserved_microusd": 1000,
    "settled_microusd": 400,
    "settlement_status": "settled",
}
_ABORTED_RECEIPT = {
    "reserved_microusd": 1000,
    "settled_microusd": 0,
    "settlement_status": "settled",
}

_RECEIPT_STATE_FOR: dict[str, tuple[str, dict | None]] = {
    code: ("failed", _POST_DISPATCH_RECEIPT) for code in sme_wire.POST_DISPATCH_TERMINAL_CODES
}
_RECEIPT_STATE_FOR["execution_aborted"] = ("failed", _ABORTED_RECEIPT)
_RECEIPT_STATE_FOR["execution_outcome_unknown"] = ("outcome_unknown", None)
_RECEIPT_STATE_FOR["deadline_exceeded"] = ("failed", _POST_DISPATCH_RECEIPT)


def _augment_error_document(document: dict) -> dict:
    code = document["error"]["code"]
    if code not in _RECEIPT_STATE_FOR:
        return document
    state, cost = _RECEIPT_STATE_FOR[code]
    document = dict(document)
    document["execution"] = {"execution_id": "59eeddf3-35a1-4d22-a51e-08acf5d5f34d", "state": state}
    if cost is not None:
        document["cost"] = cost
    return document


def _error_headers(code: str, *, request_id: str, correlation_id: str) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "cache-control": "no-store",
        "x-request-id": request_id,
        "x-correlation-id": correlation_id,
    }
    if code not in sme_wire.RELEASE_HEADERLESS_CODES:
        headers["x-stoin-execution-release"] = "tiamat-local.1"
        headers["x-stoin-execution-policy-release"] = "profiles-local.1"
    if code in sme_wire.RETRY_AFTER_CODES:
        headers["retry-after"] = "3"
    return headers


@pytest.mark.parametrize("path", vectors_in("positive", "error.pos.*.json"), ids=lambda p: p.stem)
def test_error_positive_vector(path):
    vector = load_json(path)
    document = vector["document"]
    code = document["error"]["code"]
    spec = sme_wire.ERROR_TABLE[code]
    if code in sme_wire.TRANSPORT_CODES:
        result = parse_error(
            status=spec.http_status, headers={}, body=b"", request_id=document["request_id"]
        )
    else:
        document = _augment_error_document(document)
        headers = _error_headers(
            code, request_id=document["request_id"], correlation_id=document["correlation_id"]
        )
        result = parse_error(
            status=spec.http_status,
            headers=headers,
            body=json.dumps(document).encode(),
            request_id=document["request_id"],
        )
    assert result.code == code
    assert result.retryable == spec.retryable


@pytest.mark.parametrize(
    "path", vectors_in("negative", "error.neg.message.*.json"), ids=lambda p: p.stem
)
def test_error_negative_message_vector(path):
    vector = load_json(path)
    document = vector["document"]
    code = document["error"]["code"]
    if code in sme_wire.TRANSPORT_CODES:
        pytest.skip("§6.2: 404/405 bodies are not contract-shaped; parse_error ignores them")
    spec = sme_wire.ERROR_TABLE[code]
    document = _augment_error_document(document)
    headers = _error_headers(
        code, request_id=document["request_id"], correlation_id=document["correlation_id"]
    )
    with pytest.raises(WireViolation):
        parse_error(
            status=spec.http_status,
            headers=headers,
            body=json.dumps(document).encode(),
            request_id=document["request_id"],
        )


@pytest.mark.parametrize(
    "path", vectors_in("negative", "error.neg.retry.*.json"), ids=lambda p: p.stem
)
def test_error_negative_retry_vector(path):
    vector = load_json(path)
    document = vector["document"]
    code = document["error"]["code"]
    if code in sme_wire.TRANSPORT_CODES:
        pytest.skip("§6.2: 404/405 bodies are not contract-shaped; parse_error ignores them")
    spec = sme_wire.ERROR_TABLE[code]
    document = _augment_error_document(document)
    headers = _error_headers(
        code, request_id=document["request_id"], correlation_id=document["correlation_id"]
    )
    with pytest.raises(WireViolation):
        parse_error(
            status=spec.http_status,
            headers=headers,
            body=json.dumps(document).encode(),
            request_id=document["request_id"],
        )
