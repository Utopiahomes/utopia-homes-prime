from __future__ import annotations

import json
from pathlib import Path

from utopia_homes_prime.business_core.fact_corrections import apply_corrections
from utopia_homes_prime.business_core.records import PropertyRecord
from utopia_homes_prime.business_core.store import MemoryPropertyStore

ROOT = Path(__file__).resolve().parents[2]


def seeded() -> MemoryPropertyStore:
    feed = json.loads(
        (ROOT / "knowledge/business-seed/utopia-properties.seed.json").read_text(encoding="utf-8")
    )
    store = MemoryPropertyStore()
    store.seed_if_empty([PropertyRecord.model_validate(item) for item in feed["properties"]])
    store.update("buttercup-beauty", {"beds": 13}, changed_by="test", reason="old value")
    return store


def test_corrects_the_confirmed_facts_with_an_audit_trail():
    store = seeded()
    results = apply_corrections(store)
    assert all(line.startswith("corrected") for line in results)
    assert store.get("buttercup-beauty").beds == 12
    shamrock = store.get("the-shamrock")
    assert shamrock.parking.startswith("Parking for 8 cars")
    assert "Garage and driveway parking for eight cars" in shamrock.unique_features
    latest = store.history("buttercup-beauty")[0]
    assert (latest.old, latest.new, latest.changed_by) == (13, 12, "Ray")


def test_running_twice_changes_nothing():
    store = seeded()
    apply_corrections(store)
    assert all(line.startswith("already correct") for line in apply_corrections(store))


def test_a_later_edit_is_left_alone():
    store = seeded()
    store.update("buttercup-beauty", {"beds": 14}, changed_by="Ray", reason="new bed added")
    assert "left alone, it was edited since: buttercup-beauty beds" in apply_corrections(store)
    assert store.get("buttercup-beauty").beds == 14
