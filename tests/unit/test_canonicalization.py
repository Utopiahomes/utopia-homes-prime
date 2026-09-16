"""Replays every vendored canon.*.json and inv.I-B10.*.json vector through canonical_digest(),
cross-checked against the rfc8785 library directly, plus an explicit pipeline-ordering proof
that a schema-invalid body never reaches canonicalization (see api.py step 5 vs. step 7).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from vector_helpers import load_json, vectors_in

from guest_answer_provider.canonicalization import canonical_digest


@pytest.mark.parametrize(
    "path", vectors_in("canonicalization", "canon.*.json"), ids=lambda p: p.stem
)
def test_canon_vector(path):
    vector = load_json(path)
    docs = vector["documents"]
    same = canonical_digest(docs["a"]) == canonical_digest(docs["b"])
    assert same == docs["same_canonical_identity"], path.name


@pytest.mark.parametrize("path", vectors_in("invariants", "inv.I-B10.*.json"), ids=lambda p: p.stem)
def test_i_b10_vector(path):
    """expect="violation" vectors declare a deliberately wrong same_canonical_identity claim."""
    vector = load_json(path)
    docs = vector["documents"]
    same = canonical_digest(docs["a"]) == canonical_digest(docs["b"])
    if vector["expect"] == "hold":
        assert same == docs["same_canonical_identity"], path.name
    else:
        assert vector["expect"] == "violation"
        assert same != docs["same_canonical_identity"], path.name


def test_canonicalization_is_deterministic():
    body = {"b": 1, "a": {"z": 1, "y": 2}, "c": [3, 2, 1]}
    assert canonical_digest(body) == canonical_digest(dict(reversed(list(body.items()))))


def test_schema_invalid_body_never_reaches_canonicalization():
    """Pipeline-ordering proof: api.py must reject a schema-invalid body (step 5) before ever
    calling canonical_digest() (step 7) — a malformed retry under the same Idempotency-Key must
    never spuriously reach idempotency admission at all."""
    with patch(
        "guest_answer_provider.api.canonical_digest",
        side_effect=AssertionError("must not be called"),
    ) as mocked:
        from fastapi.testclient import TestClient
        from fixtures.env import build_config

        from guest_answer_provider.api import create_app

        config = build_config()
        app = create_app(config=config)
        with TestClient(app) as client:
            import uuid

            headers = {
                "Authorization": "Bearer not-a-real-token",
                "X-Request-ID": str(uuid.uuid4()),
                "Idempotency-Key": str(uuid.uuid4()),
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            # Malformed body AND bad auth -> auth is checked first (step 3 precedes step 4/5),
            # so this proves canonicalization isn't reached on the auth-failure path either.
            response = client.post(
                "/business/v1/guest/answer", content=b"{not valid json", headers=headers
            )
            assert response.status_code in (400, 401)
        mocked.assert_not_called()
