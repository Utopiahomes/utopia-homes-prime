"""Vector-loading helpers for the vendored Shared Model Execution v1 RC1 bundle. A sibling of
vector_helpers.py (which points at the guest.answer@1.0 business-contract bundle) rather than a
shared/parameterized module -- vector_helpers.py's own docstring already explains why a module
literally named `conftest` would collide across tests/ and tests/integration/; the same reasoning
argues against overloading one BUNDLE-pointing helper for two unrelated bundles.
"""

from __future__ import annotations

import json
from pathlib import Path

BUNDLE = (
    Path(__file__).resolve().parent.parent
    / "contracts"
    / "stoin-shared-model-execution-v1-rc1-bundle"
)
VECTORS = BUNDLE / "vectors"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def vectors_in(*parts: str) -> list[Path]:
    directory = VECTORS.joinpath(*parts[:-1])
    pattern = parts[-1]
    return sorted(directory.glob(pattern))
