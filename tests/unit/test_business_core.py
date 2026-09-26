"""The business core: Lucy reads and corrects property records through a token-guarded API, every
change is attributed, and the public feed carries only page facts. In-memory store; no network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fixtures.env import build_env
from fixtures.keys import generate_test_keypair

from utopia_homes_prime.business_api.api import create_app
from utopia_homes_prime.business_core.knowledge_items import MemoryKnowledgeItemStore
from utopia_homes_prime.business_core.records import PropertyRecord
from utopia_homes_prime.business_core.store import (
    InvalidChange,
    MemoryPropertyStore,
    PostgresPropertyStore,
    apply_changes,
)
from utopia_homes_prime.config import Config, ConfigError

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "knowledge/business-seed/utopia-properties.seed.json"
TOKEN = "t" * 40


@pytest.fixture
def client():
    env = build_env(keypair=generate_test_keypair())
    env |= {
        "UTOPIA_BUSINESS_DATABASE_URL": "postgresql://unused",
        "UTOPIA_BUSINESS_LUCY_TOKEN": TOKEN,
        "UTOPIA_BUSINESS_SEED_PATH": str(SEED),
    }
    app = create_app(
        config=Config.from_environment(env),
        business_store=MemoryPropertyStore(),
        knowledge_store=MemoryKnowledgeItemStore(),
    )
    with TestClient(app) as c:
        yield c


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_internal_routes_require_lucys_token(client):
    assert client.get("/internal/v1/properties").status_code == 401
    assert client.get("/internal/v1/properties", headers=auth("x" * 40)).status_code == 401
    listing = client.get("/internal/v1/properties", headers=auth()).json()["properties"]
    assert {p["slug"] for p in listing} == {
        "buttercup-beauty",
        "central-ave-socialization",
        "the-shamrock",
    }


def test_a_correction_is_saved_attributed_and_visible_everywhere(client):
    response = client.patch(
        "/internal/v1/properties/the-shamrock",
        headers=auth(),
        json={
            "changes": {"parking": "Parking for 5 cars."},
            "changed_by": "Ray (Telegram)",
            "reason": "Ray said the garage now fits one fewer car.",
        },
    )
    assert response.status_code == 200
    assert response.json()["changes"][0] | {"changed_at": None} == {
        "field": "parking",
        "old": "Parking for 6 cars.",
        "new": "Parking for 5 cars.",
        "changed_by": "Ray (Telegram)",
        "reason": "Ray said the garage now fits one fewer car.",
        "changed_at": None,
    }
    record = client.get("/internal/v1/properties/the-shamrock", headers=auth()).json()["property"]
    assert record["parking"] == "Parking for 5 cars."
    history = client.get("/internal/v1/properties/the-shamrock/history", headers=auth()).json()
    assert history["changes"][0]["changed_by"] == "Ray (Telegram)"
    feed = client.get("/business/v1/public/properties").json()
    shamrock = next(p for p in feed["properties"] if p["slug"] == "the-shamrock")
    assert shamrock["parking"] == "Parking for 5 cars."


def test_invalid_or_unknown_changes_are_refused(client):
    for changes in ({"slug": "renamed"}, {"max_guests": 0}, {"wifi_password": "x"}):
        response = client.patch(
            "/internal/v1/properties/the-shamrock",
            headers=auth(),
            json={"changes": changes, "changed_by": "Ray", "reason": "test"},
        )
        assert response.status_code == 422, changes
    missing = client.patch(
        "/internal/v1/properties/nowhere",
        headers=auth(),
        json={"changes": {"parking": "x"}, "changed_by": "Ray", "reason": "test"},
    )
    assert missing.status_code == 404


def test_a_hidden_home_leaves_the_public_feed(client):
    client.patch(
        "/internal/v1/properties/central-ave-socialization",
        headers=auth(),
        json={"changes": {"status": "hidden"}, "changed_by": "Ray", "reason": "off market"},
    )
    feed = client.get("/business/v1/public/properties").json()
    assert "central-ave-socialization" not in {p["slug"] for p in feed["properties"]}
    assert all("status" not in p for p in feed["properties"])


def test_an_unchanged_value_records_nothing():
    record = PropertyRecord(
        slug="a",
        name="A",
        city="C",
        state="S",
        short_description="",
        full_description="A home.",
        max_guests=4,
        bedrooms=2,
        bathrooms=1,
        pet_policy="Dogs are welcome.",
        parking="Parking for 2 cars.",
        accessibility="Ask us.",
    )
    _, diff = apply_changes(record, {"parking": "Parking for 2 cars."})
    assert diff == {}
    with pytest.raises(InvalidChange):
        apply_changes(record, {"bathrooms": -1})


def test_the_business_token_must_be_long():
    env = build_env(keypair=generate_test_keypair())
    env |= {"UTOPIA_BUSINESS_DATABASE_URL": "postgresql://x", "UTOPIA_BUSINESS_LUCY_TOKEN": "short"}
    with pytest.raises(ConfigError, match="at least 32"):
        Config.from_environment(env)


@pytest.mark.skipif(
    not os.environ.get("UTOPIA_TEST_DATABASE_URL"), reason="needs UTOPIA_TEST_DATABASE_URL"
)
def test_postgres_store_round_trip():
    store = PostgresPropertyStore(os.environ["UTOPIA_TEST_DATABASE_URL"])
    with store._connect() as conn:  # a clean slate for this test database
        conn.execute("TRUNCATE property_changes, properties")
    import json

    seed = json.loads(SEED.read_text(encoding="utf-8"))["properties"]
    assert store.seed_if_empty([PropertyRecord.model_validate(p) for p in seed]) == 3
    assert store.seed_if_empty([]) == 0
    before = store.version()
    record, changes = store.update(
        "buttercup-beauty", {"max_guests": 20}, changed_by="Ray", reason="test"
    )
    assert record.max_guests == 20 and changes[0].old == 22
    assert store.get("buttercup-beauty").max_guests == 20
    assert store.history("buttercup-beauty")[0].new == 20
    assert store.version() != before
