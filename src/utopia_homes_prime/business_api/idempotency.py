"""In-memory idempotency store implementing RC2 §11's I-B09 five-branch decision table.

Single-process, matching the bundle's documented single-coordinator v1.0 replica profile. Branch
detection below is a direct restatement of the vendored bundle's own check_i_b09() (see
bundle_tools.py) so the exact same vectors that verify the bundle also verify this store's
behavior — see tests/unit/test_idempotency.py.

TTL and the stuck-in_progress ceiling are architectural in RC2 (not pinned by the contract); the
values used here are this provider's own conservative choice — see docs/implementation-notes.md.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

DurableStatus = Literal["in_progress", "completed", "unresolved"]
Decision = Literal[
    "new_execution",
    "idempotency_conflict",
    "request_in_progress",
    "idempotency_recovery_unavailable",
    "response_invalidated",
    "replay",
]


@dataclass(frozen=True, slots=True)
class IdempotencyScopeKey:
    """RC2 §11: idempotency-key scoping is (identity, environment, operation, contract_version,
    idempotency_key) — the JWT-authenticated principal, not just the raw header value alone."""

    principal_subject: str
    environment: str
    idempotency_key: str
    operation: str = "guest.answer"
    contract_version: str = "1.0"


@dataclass(slots=True)
class _Record:
    canonical_digest: bytes
    durable_status: DurableStatus
    volatile_response: dict[str, object] | None
    response_snapshot_digest: str | None
    admitted_at: float
    ttl_deadline: float
    in_progress_deadline: float | None


def _decide(
    *,
    durable_status: DurableStatus | None,
    canonical_request_matches: bool,
    volatile_content_present: bool,
    still_eligible: bool,
) -> Decision:
    """Restates bundle_tools.check_invariants_impl.check_i_b09's branch logic directly (not
    imported, since this function's inputs are already-derived booleans rather than the vector's
    raw `documents["state"]` shape) — see tests/unit/test_idempotency.py for the vector replay
    that proves this restatement matches the bundle's own check exactly."""
    if durable_status is None:
        return "new_execution"
    if not canonical_request_matches:
        return "idempotency_conflict"
    if durable_status == "in_progress":
        return "request_in_progress"
    if not volatile_content_present:
        return "idempotency_recovery_unavailable"
    if not still_eligible:
        return "response_invalidated"
    return "replay"


class IdempotencyStore:
    def __init__(
        self,
        *,
        ttl_seconds: float,
        in_progress_ceiling_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._records: dict[IdempotencyScopeKey, _Record] = {}
        self._ttl_seconds = ttl_seconds
        self._in_progress_ceiling_seconds = in_progress_ceiling_seconds
        self._now = now
        self._lock = asyncio.Lock()

    def _effective_status(self, record: _Record, now: float) -> DurableStatus:
        if (
            record.durable_status == "in_progress"
            and record.in_progress_deadline is not None
            and now > record.in_progress_deadline
        ):
            return "unresolved"
        return record.durable_status

    async def decide_and_admit(
        self,
        key: IdempotencyScopeKey,
        *,
        canonical_digest: bytes,
        current_snapshot_digest: str,
    ) -> Decision:
        """Looks up any existing record for `key`, computes the I-B09 branch, and — only on
        new_execution — admits a fresh `in_progress` record before returning. The caller must call
        `complete()` after the new_execution branch, whether the upstream call succeeded or not.
        """
        async with self._lock:
            now = self._now()
            # Every expired record goes, not only this key's: a cached response (for the meeting
            # operations, a Dragon answer or draft) must not outlive its replay window in memory.
            for expired in [k for k, r in self._records.items() if r.ttl_deadline <= now]:
                del self._records[expired]
            record = self._records.get(key)

            if record is None:
                self._records[key] = _Record(
                    canonical_digest=canonical_digest,
                    durable_status="in_progress",
                    volatile_response=None,
                    response_snapshot_digest=None,
                    admitted_at=now,
                    ttl_deadline=now + self._ttl_seconds,
                    in_progress_deadline=now + self._in_progress_ceiling_seconds,
                )
                return "new_execution"

            durable_status = self._effective_status(record, now)
            decision = _decide(
                durable_status=durable_status,
                canonical_request_matches=record.canonical_digest == canonical_digest,
                volatile_content_present=record.volatile_response is not None,
                still_eligible=record.response_snapshot_digest == current_snapshot_digest,
            )
            return decision

    def __len__(self) -> int:
        return len(self._records)

    async def get_replay_response(self, key: IdempotencyScopeKey) -> dict[str, object] | None:
        async with self._lock:
            record = self._records.get(key)
            return None if record is None else record.volatile_response

    async def complete(
        self,
        key: IdempotencyScopeKey,
        *,
        response: dict[str, object] | None,
        snapshot_digest: str | None,
        failed: bool,
    ) -> None:
        """response/snapshot_digest are the values to cache for future replay. `failed=True`
        (upstream call raised, or the process is otherwise unable to produce a durable result)
        marks the record `unresolved` rather than `completed`, so a later lookup correctly
        reaches `idempotency_recovery_unavailable` instead of replaying a response that doesn't
        exist. A *successful* answer_validation_failed / temporarily_unavailable error response is
        still `failed=False` here — the provider DID durably decide the outcome, it just decided
        "error"; only an unhandled exception counts as `failed=True`.
        """
        async with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            record.durable_status = "unresolved" if failed else "completed"
            record.volatile_response = response
            record.response_snapshot_digest = snapshot_digest
            record.in_progress_deadline = None
