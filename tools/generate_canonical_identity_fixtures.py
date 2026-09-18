#!/usr/bin/env python
"""Generates the differential fixtures for tests/unit/test_sme_canonical_identity.py.

This is a one-time generation aid, not part of the runtime or the test suite's own import graph
-- nothing at test time imports Tiamat's source. It exists so the fixture file
(tests/fixtures/sme_canonical_identity_vectors.json) is REGENERATABLE and AUDITABLE rather than a
black-box value someone once typed in: run this script against a checkout of
cloud-hermes-lucy-management-v1 and diff the output.

Usage:
    python tools/generate_canonical_identity_fixtures.py <path-to-cloud-hermes-lucy-management-v1>

Mirrors the bundle's own build_bundle.py/verify_bundle.py split (generation and verification
share no code) and this project's "the generator must not become its own oracle" discipline:
this script calls Tiamat's REAL, unmodified `canonical_identity()` function -- it does not
reimplement or restate the formula. The test that consumes the output
(test_sme_canonical_identity.py) calls this repo's OWN `sme_wire.canonical_identity()`
independently and compares against these captured values.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BUNDLE_REQUEST_VECTORS = [
    "request.pos.json.json",
    "request.pos.text.json",
]

# Handwritten edge cases beyond the bundle's own request vectors, chosen to exercise corners RFC
# 8785 canonicalization and JSON round-tripping are known to be tricky about: non-ASCII content
# requiring UTF-8/astral handling, and a JSON Schema output with nested objects/arrays so key
# ordering inside a nested structure is also exercised, not just the top level.
EXTRA_DOCUMENTS = [
    {
        "contract": "stoin.inference.execute.request.v1",
        "execution_profile_id": "utopia-homes.public-answer.generate.v1",
        "messages": [
            {"role": "system", "content": "Use approved Utopia context only."},
            {"role": "user", "content": "Café, naïve, 日本語, emoji: \U0001f600"},
        ],
        "output": {"mode": "text"},
        "limits": {"max_output_tokens": 900, "max_cost_microusd": 2000},
    },
    {
        "contract": "stoin.inference.execute.request.v1",
        "execution_profile_id": "utopia-homes.public-answer.support-review.v1",
        "messages": [
            {"role": "system", "content": "Review the candidate answer for factual support."},
            {"role": "user", "content": "Candidate: Buttercup sleeps six guests."},
        ],
        "output": {
            "mode": "json_schema",
            "name": "support-review-verdict",
            "schema": {
                "type": "object",
                "properties": {
                    "supported": {"type": "boolean"},
                    "issues": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 200},
                        "maxItems": 8,
                    },
                    "nested": {
                        "type": "object",
                        "properties": {"z": {"type": "integer"}, "a": {"type": "integer"}},
                        "required": ["z", "a"],
                        "additionalProperties": False,
                    },
                },
                "required": ["supported", "issues", "nested"],
                "additionalProperties": False,
            },
        },
        "limits": {"max_output_tokens": 300, "max_cost_microusd": 800},
    },
]


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)

    tiamat_repo = Path(sys.argv[1]).resolve()
    tiamat_src = tiamat_repo / "src"
    if not (tiamat_src / "lucy" / "shared_execution" / "service.py").is_file():
        raise SystemExit(f"not a cloud-hermes-lucy-management-v1 checkout: {tiamat_repo}")

    sys.path.insert(0, str(tiamat_src))
    from lucy.shared_execution.service import canonical_identity  # type: ignore[import-not-found]
    from lucy.shared_execution.wire import ExecutionRequest  # type: ignore[import-not-found]

    bundle_dir = (
        Path(__file__).resolve().parent.parent
        / "contracts"
        / "stoin-shared-model-execution-v1-rc1-bundle"
    )
    documents: list[dict[str, object]] = []
    for name in BUNDLE_REQUEST_VECTORS:
        vector_path = bundle_dir / "vectors" / "positive" / name
        vector = json.loads(vector_path.read_text(encoding="utf-8"))
        documents.append(vector["document"])
    documents.extend(EXTRA_DOCUMENTS)

    fixtures = []
    for document in documents:
        # model_validate (not the constructor) mirrors how Tiamat itself parses an incoming
        # request body -- the same code path the real server runs, not a shortcut around it.
        request = ExecutionRequest.model_validate(document)
        expected = canonical_identity(request)
        fixtures.append({"document": document, "expected_canonical_identity": expected})

    out_path = (
        Path(__file__).resolve().parent.parent
        / "tests"
        / "fixtures"
        / "sme_canonical_identity_vectors.json"
    )
    out_path.write_text(
        json.dumps(fixtures, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {len(fixtures)} fixtures to {out_path}")


if __name__ == "__main__":
    main()
