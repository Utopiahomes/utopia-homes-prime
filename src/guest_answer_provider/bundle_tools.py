"""Imports the frozen bundle's own invariant-checking logic directly, rather than re-deriving it.

`check_invariants_impl.py` (I-B01..I-B10) is part of the vendored, digest-verified bundle at
contracts/stoin-business-guest-answer-v1-bundle/tools/. Reusing it here — instead of hand-porting
the same branch logic into this package — means the provider's idempotency decision table (I-B09)
and request cross-field checks (I-B01..I-B03) can never silently drift from the bundle the digest
actually pins. If the bundle is ever re-pinned to a new digest, this import picks up the new logic
automatically instead of requiring a second manual port to stay in sync.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BUNDLE_TOOLS = (
    Path(__file__).resolve().parent.parent.parent
    / "contracts"
    / "stoin-business-guest-answer-v1-bundle"
    / "tools"
)
if not _BUNDLE_TOOLS.is_dir():
    raise RuntimeError(f"vendored bundle tools not found at {_BUNDLE_TOOLS}")
if str(_BUNDLE_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_TOOLS))

import check_invariants_impl  # noqa: E402

__all__ = ["check_invariants_impl"]
