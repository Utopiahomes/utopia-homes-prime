"""§11 canonical request identity: differential parity with Tiamat's real
`lucy.shared_execution.service.canonical_identity`.

The fixtures in tests/fixtures/sme_canonical_identity_vectors.json are NOT re-derived here with
the same formula this module restates in its own docstring — they were captured by actually
running Tiamat's unmodified function against a checkout of cloud-hermes-lucy-management-v1 (see
tools/generate_canonical_identity_fixtures.py). This file only exercises this repo's own
`sme_wire.canonical_identity()` and compares against those captured values, so a bug that made
both sides wrong the same way would still be caught.

Regenerate the fixtures with:
    python tools/generate_canonical_identity_fixtures.py <path-to-cloud-hermes-lucy-management-v1>
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from guest_answer_provider.sme_wire import (
    ExecutionMessage,
    JsonSchemaOutput,
    PreparedRequest,
    b64url_sha256,
    canonical_identity,
    prepare_request,
    request_binding,
)

KEY = "0b4f8a2e-6c1d-4e3a-9f5b-7d2c1e0a9b8c"

FIXTURES_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "sme_canonical_identity_vectors.json"
)


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _load_fixture_file() -> dict:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


def _load_fixtures() -> list[dict]:
    return _load_fixture_file()["vectors"]


def _fixture_id(fixture: dict) -> str:
    document = fixture["document"]
    return f'{document["execution_profile_id"]}:{document["output"]["mode"]}'


def _prepared_via_prepare_request(document: dict) -> PreparedRequest:
    """Builds the PreparedRequest through the real caller path (prepare_request), for documents
    whose output mode this client actually produces (json_schema). Asserting prepared.document
    round-trips to the fixture's document proves prepare_request() itself builds this exact
    shape, not just that canonical_identity() would hash it correctly if it did."""
    prepared = prepare_request(
        execution_profile_id=document["execution_profile_id"],
        idempotency_key=KEY,
        messages=tuple(ExecutionMessage(m["role"], m["content"]) for m in document["messages"]),
        output=JsonSchemaOutput(document["output"]["name"], document["output"]["schema"]),
        max_output_tokens=document["limits"]["max_output_tokens"],
        max_cost_microusd=document["limits"]["max_cost_microusd"],
    )
    assert prepared.document == document
    return prepared


def _prepared_direct(document: dict) -> PreparedRequest:
    """For documents whose shape this client doesn't build itself (e.g. output.mode == "text" --
    Homes Prime only ever sends json_schema output, per docs/homes-prime-stage2.md), constructs a
    PreparedRequest directly so canonical_identity() -- which only reads `.document` -- is still
    exercised against the exact document Tiamat's function was run against. The other fields are
    not under test here; they're filled with values consistent with the document."""
    body = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = b64url_sha256(body)
    return PreparedRequest(
        execution_profile_id=document["execution_profile_id"],
        idempotency_key=KEY,
        body=body,
        body_sha256_b64url=digest,
        request_binding=request_binding(idempotency_key=KEY, body_sha256_b64url=digest),
        output=JsonSchemaOutput(
            "unused",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        ),
        max_output_tokens=document["limits"]["max_output_tokens"],
        max_cost_microusd=document["limits"]["max_cost_microusd"],
        document=document,
    )


@pytest.mark.parametrize("fixture", _load_fixtures(), ids=_fixture_id)
def test_canonical_identity_matches_tiamat(fixture: dict) -> None:
    document = fixture["document"]
    if document["output"]["mode"] == "json_schema":
        prepared = _prepared_via_prepare_request(document)
    else:
        prepared = _prepared_direct(document)
    assert canonical_identity(prepared) == fixture["expected_canonical_identity"]


def test_fixtures_cover_json_schema_and_text_modes() -> None:
    modes = {fixture["document"]["output"]["mode"] for fixture in _load_fixtures()}
    assert modes == {"json_schema", "text"}


def test_fixtures_are_pinned_to_a_named_tiamat_revision() -> None:
    """The fixtures aren't just a checked-in value with no provenance: the generation script
    records exactly which cloud-hermes-lucy-management-v1 commit produced them (see
    tools/generate_canonical_identity_fixtures.py), so parity can be re-verified against a named
    revision rather than an untraceable one. `tiamat_tree_dirty` is recorded rather than enforced
    here -- Homes' test suite can observe that Tiamat's working tree had uncommitted changes at
    generation time, but can't fix that from this side; it's surfaced for the regeneration record,
    not treated as a failure of this repo's own tests."""
    meta = _load_fixture_file()
    assert _SHA_RE.fullmatch(meta["tiamat_revision"]), "tiamat_revision must be a full 40-hex SHA"
    assert isinstance(meta["tiamat_tree_dirty"], bool)
    assert meta["generated_at"]


def test_canonical_identity_differs_from_wire_body_digest() -> None:
    """§11 vs §7.3: canonical_identity is a distinct mechanism from body_sha256_b64url, not an
    alias for it -- confirms the two hashes aren't accidentally computed over the same bytes."""
    prepared = _prepared_via_prepare_request(_load_fixtures()[0]["document"])
    assert canonical_identity(prepared) != prepared.body_sha256_b64url
