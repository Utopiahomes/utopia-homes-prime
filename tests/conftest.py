from __future__ import annotations

import pytest
from vector_helpers import BUNDLE


@pytest.fixture(scope="session")
def bundle_path():
    return BUNDLE
