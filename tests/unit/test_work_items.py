"""Work items: requests to do something become work handed to an executor (a person today),
with a trail of notes; proposed work waits for an operator to open it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fixtures.env import build_env
from fixtures.keys import generate_test_keypair

from utopia_homes_prime.business_api.api import create_app
from utopia_homes_prime.business_core.knowledge_items import MemoryKnowledgeItemStore
from utopia_homes_prime.business_core.store import MemoryPropertyStore
from utopia_homes_prime.business_core.work import (
    InvalidWork,
    MemoryWorkStore,
    PostgresWorkStore,
    WorkDesk,
    WorkItem,
)
from utopia_homes_prime.config import Config

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 40
POOL = {
    "type": "maintenance",
    "title": "Buttercup pool heater not heating",
    "purpose": "Guests report the pool is cold. Get the pool company out and confirm it's fixed.",
    "property_slug": "buttercup-beauty",
    "input_refs": ["reservation:demo-123"],
}


class FakeExecutor:
    name = "anton"

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.cancelled: list[str] = []

    def submit(self, item: WorkItem) -> str | None:
        self.submitted.append(item.id)
        return f"anton:job-{len(self.submitted)}"

    def cancel(self, item: WorkItem) -> None:
        self.cancelled.append(item.id)


def test_opened_work_is_handed_to_its_executor_and_proposed_work_waits():
    anton = FakeExecutor()
    desk = WorkDesk(MemoryWorkStore(), [anton])
    proposed = desk.submit(POOL | {"status": "proposed", "assigned_executor": "anton"},
                           requested_by="guest Lucy")  # fmt: skip
    assert anton.submitted == []
    opened = desk.update(proposed.id, {"status": "open"}, changed_by="Ray")
    assert anton.submitted == [proposed.id] and opened.executor_ref == "anton:job-1"
    desk.update(opened.id, {"status": "cancelled"}, changed_by="Ray", note="fixed itself")
    assert anton.cancelled == [opened.id]
    assert desk.store.get(opened.id).notes[-1].text == "fixed itself"


def test_unknown_executors_and_fields_are_refused():
    desk = WorkDesk(MemoryWorkStore())
    with pytest.raises(InvalidWork, match="no executor"):
        desk.submit(POOL | {"assigned_executor": "nobody"}, requested_by="Ray")
    item = desk.submit(POOL, requested_by="Ray")
    with pytest.raises(InvalidWork, match="cannot be changed"):
        desk.update(item.id, {"requested_by": "someone else"}, changed_by="Ray")


@pytest.fixture
def client():
    env = build_env(keypair=generate_test_keypair())
    env |= {
        "UTOPIA_BUSINESS_DATABASE_URL": "postgresql://unused",
        "UTOPIA_BUSINESS_LUCY_TOKEN": TOKEN,
        "UTOPIA_BUSINESS_SEED_PATH": str(
            ROOT / "knowledge/business-seed/utopia-properties.seed.json"
        ),
    }
    app = create_app(
        config=Config.from_environment(env),
        business_store=MemoryPropertyStore(),
        knowledge_store=MemoryKnowledgeItemStore(),
    )
    with TestClient(app) as c:
        yield c


def test_work_over_the_internal_api(client):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/internal/v1/work").status_code == 401
    created = client.post(
        "/internal/v1/work", json={"item": POOL, "requested_by": "Ray via Utopia Lucy"},
        headers=headers,
    )  # fmt: skip
    assert created.status_code == 201
    work = created.json()["work"]
    assert work["status"] == "open" and work["assigned_executor"] == "manual"
    updated = client.patch(
        f"/internal/v1/work/{work['id']}",
        json={"changes": {"status": "waiting"}, "note": "Pool company coming Tuesday",
              "changed_by": "Ray via Utopia Lucy"},
        headers=headers,
    )  # fmt: skip
    assert updated.json()["work"]["notes"][0]["text"] == "Pool company coming Tuesday"
    open_now = client.get("/internal/v1/work?status=open", headers=headers).json()["work"]
    assert [w["id"] for w in open_now] == [work["id"]]
    client.patch(f"/internal/v1/work/{work['id']}", headers=headers,
                 json={"changes": {"status": "done"}, "changed_by": "Ray"})  # fmt: skip
    assert client.get("/internal/v1/work?status=open", headers=headers).json()["work"] == []
    bad = client.post("/internal/v1/work", json={"item": {"title": "x"}, "requested_by": "Ray"},
                      headers=headers)  # fmt: skip
    assert bad.status_code == 422


@pytest.mark.skipif(
    not os.environ.get("UTOPIA_TEST_DATABASE_URL"), reason="needs UTOPIA_TEST_DATABASE_URL"
)
def test_postgres_work_store_round_trip():
    store = PostgresWorkStore(os.environ["UTOPIA_TEST_DATABASE_URL"])
    with store._connect() as conn:
        conn.execute("TRUNCATE work_item_changes, work_items")
    item = store.create(POOL, requested_by="Ray")
    store.update(item.id, {"status": "in_progress"}, changed_by="Ray", note="called them")
    again = store.get(item.id)
    assert again.status == "in_progress" and again.notes[0].by == "Ray"
    assert [w.id for w in store.search(statuses=("in_progress",))] == [item.id]
