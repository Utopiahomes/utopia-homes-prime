"""Knowledge items: a person-stated item is active and confirmed, an extracted one is proposed;
only active public items reach guest knowledge; field confirmations are recorded, and seeded
website facts start unconfirmed so evidence can challenge them."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fixtures.env import build_env
from fixtures.keys import generate_test_keypair

from utopia_homes_prime.business_api.api import create_app
from utopia_homes_prime.business_core.knowledge_items import (
    InvalidItem,
    MemoryKnowledgeItemStore,
    PostgresKnowledgeItemStore,
)
from utopia_homes_prime.business_core.records import PropertyRecord
from utopia_homes_prime.business_core.store import MemoryPropertyStore
from utopia_homes_prime.config import Config
from utopia_homes_prime.knowledge.live import LiveKnowledgeProjection

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "knowledge/business-seed/utopia-properties.seed.json"
BASE = json.loads((ROOT / "knowledge/base/homes-base-entries.json").read_text(encoding="utf-8"))
TOKEN = "k" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BEDS = {
    "property_slug": "buttercup-beauty",
    "audience": "public",
    "kind": "faq",
    "topic": "linens",
    "title": "Beds are not made on arrival",
    "text": "Sheets and bath towels are provided; beds are not made. Sheets are in the top drawer.",
}


def seeded() -> MemoryPropertyStore:
    store = MemoryPropertyStore()
    seed = json.loads(SEED.read_text(encoding="utf-8"))["properties"]
    store.seed_if_empty([PropertyRecord.model_validate(p) for p in seed])
    return store


@pytest.fixture
def client():
    env = build_env(keypair=generate_test_keypair()) | {
        "UTOPIA_BUSINESS_DATABASE_URL": "postgresql://unused",
        "UTOPIA_BUSINESS_LUCY_TOKEN": TOKEN,
    }
    app = create_app(
        config=Config.from_environment(env),
        business_store=seeded(),
        knowledge_store=MemoryKnowledgeItemStore(),
    )
    with TestClient(app) as c:
        yield c


def test_a_person_stated_item_is_active_and_confirmed_an_extracted_one_is_proposed(client):
    stated = client.post(
        "/internal/v1/knowledge",
        headers=AUTH,
        json={"item": BEDS | {"status": "active"}, "created_by": "Ray", "confirm": True},
    ).json()["item"]
    assert stated["status"] == "active" and stated["last_confirmed"]
    extracted = client.post(
        "/internal/v1/knowledge",
        headers=AUTH,
        json={
            "item": BEDS | {"confidence": 0.8, "evidence_ids": ["thread:123"], "relation": "new"},
            "created_by": "import: airbnb export 2026-09",
        },
    ).json()["item"]
    assert extracted["status"] == "proposed" and extracted["last_confirmed"] is None
    found = client.get(
        "/internal/v1/knowledge?property=buttercup-beauty&status=proposed", headers=AUTH
    ).json()
    assert [i["id"] for i in found["items"]] == [extracted["id"]]
    approved = client.patch(
        f"/internal/v1/knowledge/{extracted['id']}",
        headers=AUTH,
        json={"changes": {"status": "active"}, "changed_by": "Ray", "confirm": True},
    ).json()["item"]
    assert approved["status"] == "active" and approved["last_confirmed"]
    assert client.get("/internal/v1/knowledge", headers={"Authorization": "x"}).status_code == 401


def test_invalid_items_are_refused(client):
    for bad in ({**BEDS, "audience": "everyone"}, {**BEDS, "text": ""}, {**BEDS, "code": "1234"}):
        response = client.post(
            "/internal/v1/knowledge", headers=AUTH, json={"item": bad, "created_by": "Ray"}
        )
        assert response.status_code == 422, bad


def test_seeded_facts_start_unconfirmed_and_confirmation_is_recorded(client):
    record = client.get("/internal/v1/properties/the-shamrock", headers=AUTH).json()
    assert record["confirmed"] == {} and "parking" in record["unconfirmed_fields"]
    client.post(
        "/internal/v1/properties/the-shamrock/confirm",
        headers=AUTH,
        json={"fields": ["parking", "max_guests"], "confirmed_by": "Ray"},
    )
    client.patch(
        "/internal/v1/properties/the-shamrock",
        headers=AUTH,
        json={"changes": {"check_in_time": "4:00 PM"}, "changed_by": "Ray", "reason": "t"},
    )
    record = client.get("/internal/v1/properties/the-shamrock", headers=AUTH).json()
    assert {"parking", "max_guests", "check_in_time"} <= set(record["confirmed"])
    assert record["confirmed"]["parking"]["by"] == "Ray"


def test_only_active_public_items_reach_guest_knowledge():
    store, items = seeded(), MemoryKnowledgeItemStore()
    active = items.create(BEDS | {"status": "active"}, created_by="Ray", confirm=True)
    items.create(BEDS | {"status": "proposed", "title": "Proposed"}, created_by="import")
    items.create(
        BEDS | {"status": "active", "audience": "internal", "title": "Breaker panel"},
        created_by="Ray",
    )
    items.create(
        BEDS | {"status": "active", "audience": "booked_guest", "title": "Trash day"},
        created_by="Ray",
    )
    live = LiveKnowledgeProjection(
        store,
        BASE,
        approved_hostnames=frozenset({"www.utopiahomes.com"}),
        items=items,
        today=lambda: date(2026, 9, 26),
    )
    ids = live.effective(datetime(2026, 9, 27, tzinfo=UTC), enforce_cap=False).entries_by_id
    item_ids = {i for i in ids if i.startswith("ki-")}
    assert item_ids == {active.id}
    assert ids[active.id].property_slug == "buttercup-beauty"


def test_stay_rules_and_bed_layout_become_guest_knowledge():
    store = seeded()
    store.update(
        "buttercup-beauty",
        {
            "check_in_time": "4:00 PM",
            "check_out_time": "10:00 AM",
            "min_age": 21,
            "beds_by_room": [{"room": "First-floor king room", "beds": ["1 king"]}],
        },
        changed_by="Ray",
        reason="interview",
    )
    live = LiveKnowledgeProjection(
        store, BASE, approved_hostnames=frozenset({"www.utopiahomes.com"})
    )
    entries = live.effective(datetime(2026, 9, 27, tzinfo=UTC), enforce_cap=False).entries_by_id
    assert "check-in is at 4:00 PM" in entries["buttercup-stay-rules"].approved_text
    assert "the person booking must be at least 21" in entries["buttercup-stay-rules"].approved_text
    assert "First-floor king room: 1 king" in entries["buttercup-capacity"].approved_text


def test_an_item_with_an_unknown_field_is_invalid_in_memory_too():
    with pytest.raises(InvalidItem):
        MemoryKnowledgeItemStore().create(BEDS | {"door_code": "1234"}, created_by="Ray")


@pytest.mark.skipif(
    not os.environ.get("UTOPIA_TEST_DATABASE_URL"), reason="needs UTOPIA_TEST_DATABASE_URL"
)
def test_postgres_item_store_round_trip():
    store = PostgresKnowledgeItemStore(os.environ["UTOPIA_TEST_DATABASE_URL"])
    with store._connect() as conn:
        conn.execute("TRUNCATE knowledge_item_changes, knowledge_items")
    item = store.create(BEDS, created_by="import")
    before = store.version()
    store.update(item.id, {"status": "active"}, changed_by="Ray", confirm=True)
    assert store.get(item.id).status == "active" and store.get(item.id).last_confirmed
    assert [i.id for i in store.search(audiences=("public",), statuses=("active",))] == [item.id]
    assert store.search(query="top drawer")[0].id == item.id
    assert store.version() != before
