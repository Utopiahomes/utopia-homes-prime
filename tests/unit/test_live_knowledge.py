"""Guest knowledge follows the property records: a correction reaches the next answer's evidence,
a failed refresh keeps the last good knowledge, and live mode needs the business core."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fixtures.homes_prime import build_homes_prime_env

from utopia_homes_prime.business_api.api import create_app
from utopia_homes_prime.business_core.knowledge_items import MemoryKnowledgeItemStore
from utopia_homes_prime.business_core.records import PropertyRecord
from utopia_homes_prime.business_core.store import InvalidChange, MemoryPropertyStore
from utopia_homes_prime.config import Config, ConfigError
from utopia_homes_prime.knowledge.live import LiveKnowledgeProjection
from utopia_homes_prime.knowledge.projection import KnowledgeUnavailable

ROOT = Path(__file__).resolve().parents[2]
SEED = json.loads(
    (ROOT / "knowledge/business-seed/utopia-properties.seed.json").read_text(encoding="utf-8")
)
BASE = json.loads((ROOT / "knowledge/base/homes-base-entries.json").read_text(encoding="utf-8"))
HOSTS = frozenset({"www.utopiahomes.com"})
NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


def seeded() -> MemoryPropertyStore:
    store = MemoryPropertyStore()
    store.seed_if_empty([PropertyRecord.model_validate(p) for p in SEED["properties"]])
    return store


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def live(store, clock):
    return LiveKnowledgeProjection(
        store, BASE, approved_hostnames=HOSTS, monotonic=clock, today=lambda: date(2026, 9, 26)
    )


def parking(knowledge) -> str:
    return knowledge.effective(NOW).entries_by_id["shamrock-parking"].approved_text


def test_a_correction_reaches_guest_knowledge_after_the_refresh_window():
    store, clock = seeded(), Clock()
    knowledge = live(store, clock)
    assert parking(knowledge) == "The Shamrock has parking for 6 cars."
    first_release = knowledge.release_id
    store.update("the-shamrock", {"parking": "Parking for 5 cars."}, changed_by="Ray", reason="t")
    assert parking(knowledge) == "The Shamrock has parking for 6 cars."  # within 15 s
    clock.t = 16
    assert parking(knowledge) == "The Shamrock has parking for 5 cars."
    assert knowledge.release_id != first_release


def test_a_hidden_home_leaves_guest_knowledge():
    store, clock = seeded(), Clock()
    knowledge = live(store, clock)
    store.update("central-ave-socialization", {"status": "hidden"}, changed_by="Ray", reason="t")
    clock.t = 16
    ids = knowledge.effective(NOW).entries_by_id
    assert not any(i.startswith("central-") for i in ids)
    assert "Central Ave" not in ids["collection-overview"].approved_text


def test_an_unreadable_store_keeps_the_last_good_knowledge():
    store, clock = seeded(), Clock()
    knowledge = live(store, clock)
    parking(knowledge)

    def broken() -> str:
        raise ConnectionError("database down")

    store.version = broken  # type: ignore[method-assign]
    clock.t = 16
    assert parking(knowledge) == "The Shamrock has parking for 6 cars."
    with pytest.raises(KnowledgeUnavailable):
        live(store, Clock()).effective(NOW)


def test_an_edit_the_knowledge_builder_cannot_use_is_refused():
    store = seeded()
    with pytest.raises(InvalidChange, match="parking"):
        store.update(
            "the-shamrock", {"parking": "Street parking only."}, changed_by="R", reason="t"
        )


def test_live_mode_starts_with_the_business_core_and_needs_it(tmp_path):
    env = build_homes_prime_env(tmp_path).env
    env["GUEST_ANSWER_PROVIDER_KNOWLEDGE_RELEASE_ID"] = "live"
    env["GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_BASE_PATH"] = str(
        ROOT / "knowledge/base/homes-base-entries.json"
    )
    env.pop("GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_ALLOWED_DIGESTS", None)
    with pytest.raises(ConfigError, match="live needs"):
        Config.from_environment(env)
    env |= {
        "UTOPIA_BUSINESS_DATABASE_URL": "postgresql://unused",
        "UTOPIA_BUSINESS_LUCY_TOKEN": "t" * 40,
        "UTOPIA_BUSINESS_SEED_PATH": str(
            ROOT / "knowledge/business-seed/utopia-properties.seed.json"
        ),
    }
    config = Config.from_environment(env)
    assert config.homes_prime is not None and config.homes_prime.knowledge_live
    create_app(
        config=config,
        business_store=MemoryPropertyStore(),
        knowledge_store=MemoryKnowledgeItemStore(),
    )
