"""Booked-guest turns end to end: a guest message becomes a turn, guest Lucy sees only her home's
guest-safe knowledge, the gate checks every draft, and a person's decision is recorded."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fixtures.env import build_env
from fixtures.keys import generate_test_keypair

from utopia_homes_prime.business_api.api import create_app
from utopia_homes_prime.business_core.knowledge_items import MemoryKnowledgeItemStore
from utopia_homes_prime.business_core.store import MemoryPropertyStore
from utopia_homes_prime.config import Config, ConfigError

ROOT = Path(__file__).resolve().parents[2]
LUCY = {"Authorization": "Bearer " + "l" * 40}
GUEST = {"Authorization": "Bearer " + "g" * 40}
INTRO = "Hi, it's Jennifer! Call me at 609-555-0142"


@pytest.fixture
def api():
    env = build_env(keypair=generate_test_keypair())
    env |= {
        "UTOPIA_BUSINESS_DATABASE_URL": "postgresql://unused",
        "UTOPIA_BUSINESS_LUCY_TOKEN": "l" * 40,
        "UTOPIA_BUSINESS_GUEST_TOKEN": "g" * 40,
        "UTOPIA_BUSINESS_SEED_PATH": str(
            ROOT / "knowledge/business-seed/utopia-properties.seed.json"
        ),
    }
    items = MemoryKnowledgeItemStore()
    for slug, audience, title, text in [
        ("buttercup-beauty", "booked_guest", "Trash day", "Trash goes out Sunday night."),
        ("buttercup-beauty", "internal", "Pool vendor", "Pool vendor charges extra weekends."),
        ("the-shamrock", "booked_guest", "Garage", "The garage door opener is by the stairs."),
    ]:
        items.create({"property_slug": slug, "audience": audience, "kind": "fact",
                      "topic": "t", "title": title, "text": text, "status": "active"},
                     created_by="test")  # fmt: skip
    app = create_app(
        config=Config.from_environment(env),
        business_store=MemoryPropertyStore(),
        knowledge_store=items,
    )
    with TestClient(app) as client:
        yield client


def new_turn(api, message="When does the trash go out?"):
    r = api.post("/internal/v1/reservations", headers=LUCY, json={
        "property_slug": "buttercup-beauty", "check_in": "2026-10-02", "check_out": "2026-10-05",
        "guests": 14, "label": "TEST family weekend"})  # fmt: skip
    assert r.status_code == 201, r.text
    t = api.post("/internal/v1/guest/turns", headers=LUCY, json={
        "reservation_id": r.json()["reservation"]["id"], "message": message,
        "history": [{"role": "guest", "text": INTRO}]})  # fmt: skip
    assert t.status_code == 201, t.text
    return t.json()["turn"]["id"]


def test_tokens_open_only_their_own_side(api):
    turn = new_turn(api)
    assert api.get(f"/guest/v1/turns/{turn}/context", headers=LUCY).status_code == 401
    assert api.get("/internal/v1/guest/turns", headers=GUEST).status_code == 401
    assert api.get(f"/guest/v1/turns/{turn}/context", headers=GUEST).status_code == 200


def test_guest_lucy_sees_only_this_homes_guest_safe_knowledge_and_no_pii(api):
    turn = new_turn(api)
    ctx = api.get(f"/guest/v1/turns/{turn}/context", headers=GUEST).json()
    titles = {k["title"] for k in ctx["knowledge"]}
    assert "Trash day" in titles and "Pool vendor" not in titles and "Garage" not in titles
    assert ctx["reservation"]["home"] == "Buttercup Beauty" and ctx["reservation"]["guests"] == 14
    assert "Jennifer" not in str(ctx) and "555-0142" not in str(ctx)


def test_a_blocked_draft_comes_back_then_a_good_one_is_queued_and_decided(api):
    turn = new_turn(api)
    ctx = api.get(f"/guest/v1/turns/{turn}/context", headers=GUEST).json()
    trash = next(k["id"] for k in ctx["knowledge"] if k["title"] == "Trash day")
    bad = api.post(
        f"/guest/v1/turns/{turn}/reply",
        headers=GUEST,
        json={"text": "Trash is Sunday. Text me at 609-555-0100!", "cited_ids": [trash]},
    )
    assert bad.json()["status"] == "blocked_try_again"  # fmt: skip
    good = api.post(
        f"/guest/v1/turns/{turn}/reply",
        headers=GUEST,
        json={"text": "Hi! Trash goes out Sunday night. Enjoy your stay!", "cited_ids": [trash]},
    )
    assert good.json()["status"] == "queued_for_approval" and good.json()["gate"] == "send"
    again = api.post(f"/guest/v1/turns/{turn}/reply", headers=GUEST,
                     json={"text": "One more thing", "cited_ids": []})  # fmt: skip
    assert again.status_code == 422

    queue = api.get("/internal/v1/guest/turns", headers=LUCY).json()["turns"]
    assert queue[0]["id"] == turn and queue[0]["draft"].startswith("Hi! Trash")
    decided = api.post(f"/internal/v1/guest/turns/{turn}/decision", headers=LUCY, json={
        "action": "edit", "decided_by": "Meghan",
        "final_text": "Hi! Trash and recycling go out Sunday night. Enjoy!",
        "edit_categories": ["missing_info"], "reason": "recycling too"})  # fmt: skip
    assert decided.json()["turn"]["state"] == "approved"
    full = api.get(f"/internal/v1/guest/turns/{turn}", headers=LUCY).json()["turn"]
    assert len(full["attempts"]) == 2 and full["attempts"][0]["decision"] == "block"
    assert full["decision"]["final_text"].startswith("Hi! Trash and recycling")


def test_repeated_blocks_escalate_and_guest_lucy_can_only_propose_work(api):
    turn = new_turn(api, "The pool heater isn't working")
    for _ in range(3):
        r = api.post(f"/guest/v1/turns/{turn}/reply", headers=GUEST,
                     json={"text": "We'll refund $100 for the pool.", "cited_ids": []})  # fmt: skip
    assert r.json()["status"] == "escalated"
    work = api.post(
        f"/guest/v1/turns/{turn}/propose_work",
        headers=GUEST,
        json={
            "type": "maintenance",
            "title": "Buttercup pool heater",
            "purpose": "Guest says cold",
        },
    )
    work_id = work.json()["proposed_work"]
    item = api.get(f"/internal/v1/work/{work_id}", headers=LUCY).json()["work"]
    assert item["status"] == "proposed" and item["property_slug"] == "buttercup-beauty"
    assert item["input_refs"][1] == f"guest-turn:{turn}"


def test_the_guest_token_must_differ_from_lucys():
    env = build_env(keypair=generate_test_keypair()) | {
        "UTOPIA_BUSINESS_DATABASE_URL": "postgresql://x",
        "UTOPIA_BUSINESS_LUCY_TOKEN": "l" * 40,
        "UTOPIA_BUSINESS_GUEST_TOKEN": "l" * 40,
    }
    with pytest.raises(ConfigError, match="GUEST_TOKEN"):
        Config.from_environment(env)
