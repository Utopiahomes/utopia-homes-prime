"""Replays every vendored request/response/error schema vector through this provider's own
schema_validation.py wiring — proves the registry/$ref resolution built here (mirroring
verify_bundle.py) validates identically to the bundle's own verifier.
"""

from __future__ import annotations

import pytest
from vector_helpers import load_json, vectors_in

from utopia_homes_prime.guest_answer import schema_validation


def _document(vector: dict) -> dict:
    return vector["document"]


@pytest.mark.parametrize("path", vectors_in("positive", "request.pos.*.json"), ids=lambda p: p.stem)
def test_request_positive_vector(path):
    vector = load_json(path)
    schema_validation.validate_request(_document(vector))


@pytest.mark.parametrize("path", vectors_in("negative", "request.neg.*.json"), ids=lambda p: p.stem)
def test_request_negative_vector(path):
    vector = load_json(path)
    with pytest.raises(schema_validation.SchemaValidationError):
        schema_validation.validate_request(_document(vector))


@pytest.mark.parametrize(
    "path", vectors_in("positive", "response.pos.*.json"), ids=lambda p: p.stem
)
def test_response_positive_vector(path):
    vector = load_json(path)
    schema_validation.validate_response(_document(vector))


@pytest.mark.parametrize(
    "path", vectors_in("negative", "response.neg.*.json"), ids=lambda p: p.stem
)
def test_response_negative_vector(path):
    vector = load_json(path)
    with pytest.raises(schema_validation.SchemaValidationError):
        schema_validation.validate_response(_document(vector))


@pytest.mark.parametrize("path", vectors_in("positive", "error.pos.*.json"), ids=lambda p: p.stem)
def test_error_positive_vector(path):
    vector = load_json(path)
    schema_validation.validate_error_response(_document(vector))


@pytest.mark.parametrize("path", vectors_in("negative", "error.neg.*.json"), ids=lambda p: p.stem)
def test_error_negative_vector(path):
    vector = load_json(path)
    with pytest.raises(schema_validation.SchemaValidationError):
        schema_validation.validate_error_response(_document(vector))
