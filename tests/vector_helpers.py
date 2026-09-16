"""Shared vector-loading helpers for the vendored bundle. Kept out of conftest.py (and not named
`conftest`) so `from vector_helpers import ...` resolves unambiguously regardless of which test
subdirectory's own conftest.py pytest has also loaded — `tests/` and `tests/integration/` would
otherwise both be candidate homes for a module literally named `conftest` on sys.path.
"""

from __future__ import annotations

import json
from pathlib import Path

BUNDLE = (
    Path(__file__).resolve().parent.parent / "contracts" / "stoin-business-guest-answer-v1-bundle"
)
VECTORS = BUNDLE / "vectors"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def vectors_in(*parts: str) -> list[Path]:
    directory = VECTORS.joinpath(*parts[:-1])
    pattern = parts[-1]
    return sorted(directory.glob(pattern))
