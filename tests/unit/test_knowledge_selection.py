"""Per-answer knowledge selection keeps every evidence packet within the 64-ID limit as the
portfolio and each home's knowledge grow, without losing the home the guest is asking about."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from utopia_homes_prime.guest_answer.homes_prime import select_for_request
from utopia_homes_prime.knowledge.builder import build_corpus
from utopia_homes_prime.knowledge.projection import (
    MAX_ADMITTED_IDS,
    KnowledgeProjection,
    canonical_corpus_digest,
)

ROOT = Path(__file__).resolve().parents[2]
SEED = json.loads(
    (ROOT / "knowledge/business-seed/utopia-properties.seed.json").read_text(encoding="utf-8")
)
BASE = json.loads((ROOT / "knowledge/base/homes-base-entries.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
NAMES = [
    "Seagull", "Driftwood", "Harborview", "Saltmarsh", "Boardwalk", "Lighthouse", "Sandpiper",
    "Heron", "Osprey", "Dunegrass", "Tidewater", "Marlin",
]  # fmt: skip


def portfolio(homes: int) -> dict:
    """The real three homes plus synthetic ones, each with the same 7 standard entries."""
    feed = {"schema": SEED["schema"], "properties": copy.deepcopy(SEED["properties"])}
    template = SEED["properties"][0]
    for i in range(homes - 3):
        name = f"{NAMES[i % len(NAMES)]} Cottage {i}"
        slug = f"{NAMES[i % len(NAMES)].lower()}-cottage-{i}"
        feed["properties"].append({**copy.deepcopy(template), "slug": slug, "name": name})
    return feed


def knowledge(feed: dict, items: list[dict] | None = None):
    corpus = build_corpus(feed, BASE, None, date(2026, 9, 25), items)
    digest = canonical_corpus_digest(corpus)
    projection = KnowledgeProjection.from_document(
        corpus,
        release_id="test",
        allowed_corpus_digests=frozenset({digest}),
        withdrawn_ids=frozenset(),
        approved_hostnames=frozenset({"www.utopiahomes.com"}),
    )
    return projection.effective(NOW, enforce_cap=False)


def request(message: str, *, subject: str | None = None, history: list[str] | None = None):
    body: dict = {"message": {"content": message}}
    if subject:
        body["page_context"] = {"path": f"/stays/{subject}", "subject_type": "property",
                                "subject_id": subject}  # fmt: skip
    if history:
        body["history"] = [{"role": "user", "content": h} for h in history]
    return body


def buttercup_items(count: int) -> list[dict]:
    return [
        {
            "id": f"ki-{i:012d}",
            "property_slug": "buttercup-beauty",
            "audience": "public",
            "kind": "faq",
            "topic": "pool" if i % 5 == 0 else f"topic{i}",
            "title": f"Buttercup detail {i}",
            "text": f"Buttercup Beauty detail number {i}."
            + (" The heated pool opens the first weekend of May." if i % 5 == 0 else ""),
            "status": "active",
        }
        for i in range(count)
    ]


@pytest.mark.parametrize("homes", [8, 20, 100])
def test_every_anchored_and_unanchored_packet_fits_as_the_portfolio_grows(homes):
    k = knowledge(portfolio(homes))
    assert len(k.entries_by_id) > MAX_ADMITTED_IDS or homes == 8
    for req in (
        request("Does it have a hot tub?", subject="the-shamrock"),
        request("Which of your homes sleeps 20 and allows dogs?"),
    ):
        packet = select_for_request(k, req, req["message"]["content"])
        assert 0 < len(packet.entries_by_id) <= MAX_ADMITTED_IDS
        assert len(packet.links_by_id) <= MAX_ADMITTED_IDS
        # The wrong-property check still knows every home, selected or not.
        assert len(packet.property_labels) == homes


def test_the_page_home_is_fully_present_when_selected():
    k = knowledge(portfolio(100))
    packet = select_for_request(k, request("How much parking?", subject="the-shamrock"),
                                "How much parking?")  # fmt: skip
    shamrock = {i for i, e in packet.entries_by_id.items() if e.property_slug == "the-shamrock"}
    assert {"shamrock-parking", "shamrock-capacity", "shamrock-pets"} <= shamrock
    assert {e.property_slug for e in packet.entries_by_id.values()} <= {None, "the-shamrock"}


def test_a_home_named_in_the_conversation_is_anchored_even_off_its_page():
    k = knowledge(portfolio(20))
    packet = select_for_request(
        k, request("And how many cars can we park?", history=["Tell me about Buttercup"]),
        "And how many cars can we park?",
    )  # fmt: skip
    assert "buttercup-parking" in packet.entries_by_id


def test_110_buttercup_items_are_cut_to_the_ones_matching_the_question():
    k = knowledge(portfolio(3), buttercup_items(110))
    question = "When does the heated pool open?"
    packet = select_for_request(k, request(question, subject="buttercup-beauty"), question)
    assert len(packet.entries_by_id) == MAX_ADMITTED_IDS
    pool_items = {f"ki-{i:012d}" for i in range(0, 110, 5)}
    assert pool_items <= set(packet.entries_by_id)


def test_selection_is_deterministic_for_replays():
    k = knowledge(portfolio(40), buttercup_items(80))
    req = request("Is the pool heated?", subject="buttercup-beauty")
    first = select_for_request(k, req, "Is the pool heated?")
    again = select_for_request(k, req, "Is the pool heated?")
    assert list(first.entries_by_id) == list(again.entries_by_id)


def test_only_active_public_items_can_reach_guest_knowledge():
    from utopia_homes_prime.knowledge.builder import FeedError

    internal = buttercup_items(1)[0] | {"audience": "internal"}
    with pytest.raises(FeedError):
        knowledge(portfolio(3), [internal])
    proposed = buttercup_items(1)[0] | {"status": "proposed"}
    with pytest.raises(FeedError):
        knowledge(portfolio(3), [proposed])
