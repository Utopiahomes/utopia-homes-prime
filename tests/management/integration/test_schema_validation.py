"""Provider-side obligation from the bundle README: "assert that live responses from the
deployed adapter validate against the schemas" and cross-check the vendored vectors themselves
with an independent (non-bundle) JSON-Schema implementation.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from fixtures.management.bundle import is_valid, iter_vectors, schema_name_for_vector, validate


@pytest.mark.parametrize("name_and_doc", iter_vectors(positive=True), ids=lambda v: v[0])
def test_every_positive_vector_validates(name_and_doc: tuple[str, dict[str, object]]) -> None:
    name, doc = name_and_doc
    validate(doc, schema_name_for_vector(name))


@pytest.mark.parametrize("name_and_doc", iter_vectors(positive=False), ids=lambda v: v[0])
def test_every_negative_vector_is_rejected(name_and_doc: tuple[str, dict[str, object]]) -> None:
    name, doc = name_and_doc
    assert not is_valid(doc, schema_name_for_vector(name))


@pytest.mark.parametrize(
    "endpoint",
    [
        "/management/v1/identity",
        "/management/v1/health",
        "/management/v1/version",
        "/management/v1/capabilities",
    ],
)
def test_live_endpoint_response_validates_against_bundle_schema(
    client: TestClient, auth_headers: Callable[..., dict[str, str]], endpoint: str
) -> None:
    response = client.get(endpoint, headers=auth_headers())
    assert response.status_code == 200
    schema_name = endpoint.rsplit("/", 1)[-1] + ".response"
    validate(response.json(), schema_name)


def test_live_error_response_validates_against_bundle_schema(
    client: TestClient, auth_headers: Callable[..., dict[str, str]]
) -> None:
    headers = auth_headers()
    del headers["Authorization"]
    response = client.get("/management/v1/identity", headers=headers)
    assert response.status_code == 401
    validate(response.json(), "error.response")
