"""Replays every vendored inv.I-B09.*.json vector through the store's branch-detection logic,
so the exact same vectors that prove the bundle's own check_i_b09() also prove this store's
_decide() restatement matches it.
"""

from __future__ import annotations

import asyncio

import pytest
from vector_helpers import load_json, vectors_in

from guest_answer_provider.idempotency import IdempotencyScopeKey, IdempotencyStore, _decide


@pytest.mark.parametrize("path", vectors_in("invariants", "inv.I-B09.*.json"), ids=lambda p: p.stem)
def test_i_b09_vector(path):
    """expect="hold" vectors declare a correct outcome (computed must match); expect="violation"
    vectors declare a deliberately WRONG outcome (computed must NOT match) — mirrors the bundle's
    own expect-aware check, not a plain equality assertion."""
    vector = load_json(path)
    state = vector["documents"]["state"]
    declared = vector["documents"]["expected_outcome"]

    computed = _decide(
        durable_status=None if state["durable_status"] == "none" else state["durable_status"],
        canonical_request_matches=state["canonical_request_matches"],
        volatile_content_present=state["volatile_content_present"],
        still_eligible=state["still_eligible"],
    )
    if vector["expect"] == "hold":
        assert computed == declared, f"{path.name}: expected {declared!r}, got {computed!r}"
    else:
        assert vector["expect"] == "violation"
        assert computed != declared, (
            f"{path.name}: vector's wrong claim {declared!r} unexpectedly matched"
        )


def test_new_execution_then_replay():
    store = IdempotencyStore(ttl_seconds=900, in_progress_ceiling_seconds=15)
    key = IdempotencyScopeKey(
        principal_subject="sub-1", environment="preview", idempotency_key="k1"
    )

    async def scenario():
        decision = await store.decide_and_admit(
            key, canonical_digest=b"abc", current_snapshot_digest="d1"
        )
        assert decision == "new_execution"

        # Second admission attempt before complete() -> in progress.
        decision2 = await store.decide_and_admit(
            key, canonical_digest=b"abc", current_snapshot_digest="d1"
        )
        assert decision2 == "request_in_progress"

        await store.complete(
            key, response={"kind": "success", "body": {"x": 1}}, snapshot_digest="d1", failed=False
        )

        decision3 = await store.decide_and_admit(
            key, canonical_digest=b"abc", current_snapshot_digest="d1"
        )
        assert decision3 == "replay"
        replayed = await store.get_replay_response(key)
        assert replayed == {"kind": "success", "body": {"x": 1}}

    asyncio.run(scenario())


def test_conflict_on_different_canonical_digest():
    store = IdempotencyStore(ttl_seconds=900, in_progress_ceiling_seconds=15)
    key = IdempotencyScopeKey(
        principal_subject="sub-1", environment="preview", idempotency_key="k1"
    )

    async def scenario():
        await store.decide_and_admit(key, canonical_digest=b"abc", current_snapshot_digest="d1")
        await store.complete(
            key, response={"kind": "success", "body": {}}, snapshot_digest="d1", failed=False
        )
        decision = await store.decide_and_admit(
            key, canonical_digest=b"different", current_snapshot_digest="d1"
        )
        assert decision == "idempotency_conflict"

    asyncio.run(scenario())


def test_response_invalidated_on_snapshot_rotation():
    store = IdempotencyStore(ttl_seconds=900, in_progress_ceiling_seconds=15)
    key = IdempotencyScopeKey(
        principal_subject="sub-1", environment="preview", idempotency_key="k1"
    )

    async def scenario():
        await store.decide_and_admit(key, canonical_digest=b"abc", current_snapshot_digest="d1")
        await store.complete(
            key, response={"kind": "success", "body": {}}, snapshot_digest="d1", failed=False
        )
        decision = await store.decide_and_admit(
            key, canonical_digest=b"abc", current_snapshot_digest="d2-rotated"
        )
        assert decision == "response_invalidated"

    asyncio.run(scenario())


def test_unresolved_in_progress_becomes_recovery_unavailable_after_ceiling():
    store = IdempotencyStore(ttl_seconds=900, in_progress_ceiling_seconds=0.01)
    key = IdempotencyScopeKey(
        principal_subject="sub-1", environment="preview", idempotency_key="k1"
    )

    async def scenario():
        await store.decide_and_admit(key, canonical_digest=b"abc", current_snapshot_digest="d1")
        await asyncio.sleep(0.05)
        # complete() never called (simulated crash) -> ceiling elapses -> unresolved, no content.
        decision = await store.decide_and_admit(
            key, canonical_digest=b"abc", current_snapshot_digest="d1"
        )
        assert decision == "idempotency_recovery_unavailable"

    asyncio.run(scenario())
