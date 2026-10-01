"""Learning: when a host answers what Lucy could not, or finishes work, Lucy learns lasting
knowledge automatically (and, in the optional review mode, asks first with a learning card)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from utopia_homes_prime.business_core.guest import GuestDesk, MemoryGuestStore
from utopia_homes_prime.business_core.host_cards import CardDispatcher
from utopia_homes_prime.business_core.knowledge_items import MemoryKnowledgeItemStore
from utopia_homes_prime.business_core.learning import LearningDesk, MemoryLearningStore
from utopia_homes_prime.business_core.records import PropertyRecord
from utopia_homes_prime.business_core.store import MemoryPropertyStore
from utopia_homes_prime.business_core.work import MemoryWorkStore, WorkDesk

ROOT = Path(__file__).resolve().parents[2]
AFTERNOON = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)  # 2 PM Eastern
POOL = {
    "audience": "booked_guest",
    "kind": "fact",
    "topic": "pool",
    "title": "Pool season",
    "text": "The heated pool stays open through mid-October.",
    "why": "host said so",
}


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def setup(proposals, now=AFTERNOON, review_by_card=True):
    seed = json.loads(
        (ROOT / "knowledge/business-seed/utopia-properties.seed.json").read_text(encoding="utf-8")
    )
    properties = MemoryPropertyStore()
    properties.seed_if_empty([PropertyRecord.model_validate(p) for p in seed["properties"]])
    items = MemoryKnowledgeItemStore()
    clock = Clock(now)
    work = WorkDesk(MemoryWorkStore())
    desk = GuestDesk(MemoryGuestStore(), properties, items, work, clock=clock)
    seen: list[str] = []

    def propose(context: str):
        seen.append(context)
        return proposals

    learning = LearningDesk(
        MemoryLearningStore(), items, properties, propose, clock, review_by_card=review_by_card
    )
    learning.learn_in_background = learning.learn  # type: ignore[method-assign]  # synchronous
    desk.learning = learning
    sent: list[str] = []
    desk.cards = CardDispatcher(desk, sent.append, clock)
    learning.on_new_card = desk.cards.poke
    return desk, items, sent, clock, seen, work


def guest_card(desk, message="Is the pool open in October?", hand_over=True):
    start = (AFTERNOON + timedelta(days=20)).date()
    r = desk.create_reservation({"property_slug": "buttercup-beauty", "check_in": start.isoformat(),
                                 "check_out": (start + timedelta(days=3)).isoformat(),
                                 "guests": 8})  # fmt: skip
    turn = desk.receive(r.id, message)
    if hand_over:
        desk.escalate(turn.id, "unknown", "not in knowledge", "Hi! Let me check with the team.")
    else:
        desk.reply(turn.id, "Hi! Thanks so much!", [])
    desk.cards.tick()
    return turn


def test_answering_a_hand_over_proposes_knowledge_and_save_makes_it_active():
    desk, items, sent, clock, seen, _ = setup([POOL])
    guest_card(desk)
    desk.answer_card("revise", by="Ray", text="Hi! Yes, the pool is open through mid-October.")
    desk.answer_card("send", by="Ray")
    assert "The host sent: Hi! Yes, the pool is open through mid-October." in seen[0]
    proposed = items.search(property_slug="buttercup-beauty", statuses=("proposed",))
    assert [i.title for i in proposed] == ["Pool season"]
    desk.cards.tick()
    assert sent[-1].startswith("📚 Learn · Buttercup Beauty")
    assert 'Reply "save"' in sent[-1] and "booked guests" in sent[-1]
    result = desk.answer_card("send", by="Ray")  # "save"
    assert result["status"] == "saved"
    active = items.get(proposed[0].id)
    assert active.status == "active" and active.last_confirmed is not None


def test_a_lesson_can_be_reworded_or_skipped():
    desk, items, sent, clock, seen, _ = setup([POOL])
    guest_card(desk)
    desk.answer_card("revise", by="Ray", text="Hi! It's open until October 15.")
    desk.answer_card("send", by="Ray")
    desk.cards.tick()
    desk.answer_card("revise", by="Ray", text="The heated pool is open through October 15.")
    assert "v2" in sent[-1] and "October 15" in sent[-1]
    desk.answer_card("send", by="Ray")
    [item] = items.search(property_slug="buttercup-beauty", statuses=("active",))
    assert item.text == "The heated pool is open through October 15."

    desk2, items2, *_ = setup([POOL])
    guest_card(desk2)
    desk2.answer_card("send", by="Ray")  # approves Lucy's holding reply on a hand-over
    desk2.cards.tick()
    assert desk2.answer_card("reject", by="Ray")["status"] == "skipped"
    assert items2.search(property_slug="buttercup-beauty", statuses=("withdrawn",))


def test_nothing_is_learned_when_lucys_own_reply_was_sent_unchanged():
    desk, items, sent, clock, seen, _ = setup([POOL])
    guest_card(desk, "Thanks!", hand_over=False)
    desk.answer_card("send", by="Ray")
    assert seen == [] and items.search(statuses=("proposed",)) == []


def test_lessons_wait_for_guests_and_quiet_hours():
    desk, items, sent, clock, seen, _ = setup([POOL])
    guest_card(desk)
    second = guest_card(desk, "Can we bring a crib?")
    desk.answer_card("revise", by="Ray", text="Hi! Yes, it's open through mid-October.")
    desk.answer_card("send", by="Ray")
    desk.cards.tick()
    assert second.id in sent[-1]  # the waiting guest comes before the lesson
    desk.answer_card("reject", by="Ray")
    clock.now = datetime(2026, 10, 7, 3, 0, tzinfo=UTC)  # 11 PM Eastern
    count = len(sent)
    desk.cards.tick()
    assert len(sent) == count  # no lessons at night
    clock.now = datetime(2026, 10, 7, 13, 0, tzinfo=UTC)  # 9 AM
    desk.cards.tick()
    assert sent[-1].startswith("📚 Learn")


def test_unsafe_or_bracketed_proposals_are_dropped_and_sources_learn_once():
    bad = [POOL | {"text": "The lockbox code is 4821."}, POOL | {"text": "Open until [date]."}]
    desk, items, *_ = setup(bad)
    learning = desk.learning
    assert learning.learn("guest-turn:gt-000000000001", "buttercup-beauty", "x") == []
    good, *_ = setup([POOL])
    assert len(good.learning.learn("work:wi-1", "buttercup-beauty", "x")) == 1
    assert good.learning.learn("work:wi-1", "buttercup-beauty", "x") == []


def test_finished_work_teaches_too():
    breaker = POOL | {"audience": "internal", "title": "Pool pump breaker",
                      "text": "The pool pump breaker is in the garage panel."}  # fmt: skip
    desk, items, sent, clock, seen, work = setup([breaker])
    work.on_done = lambda item: desk.learning.learn(f"work:{item.id}", item.property_slug,
                                                     f"Work finished: {item.title}")  # fmt: skip
    job = work.submit({"type": "maintenance", "title": "Pool pump off", "purpose": "No filter",
                       "property_slug": "buttercup-beauty"}, requested_by="Ray")  # fmt: skip
    work.update(job.id, {"status": "done", "result": "Tripped breaker in the garage panel"},
                changed_by="Ray")  # fmt: skip
    [lesson] = desk.learning.store.recent()
    assert lesson.source_ref == f"work:{job.id}" and lesson.audience == "internal"
    desk.cards.tick()
    assert "From work you just finished" in sent[-1] and "hosts only" in sent[-1]


def test_lucy_learns_automatically_without_asking():
    desk, items, sent, clock, seen, _ = setup([POOL | {"audience": "public"}], review_by_card=False)
    guest_card(desk)
    desk.answer_card("revise", by="Ray", text="Hi! Yes, the pool is open through mid-October.")
    desk.answer_card("send", by="Ray")
    [learned] = items.search(property_slug="buttercup-beauty", query="learned")
    assert learned.status == "active" and learned.last_confirmed is None
    assert learned.audience == "public"  # the website learns too
    count = len(sent)
    desk.cards.tick()
    assert len(sent) == count  # no card to answer
    assert desk.learning.store.recent() == []


def test_a_correction_updates_what_lucy_knew_instead_of_adding_a_second_fact():
    fix = POOL | {"text": "The heated pool is open through October 15.", "replaces": "Pool season"}
    desk, items, sent, clock, seen, _ = setup([fix], review_by_card=False)
    old = items.create({"property_slug": "buttercup-beauty", "audience": "booked_guest",
                        "kind": "fact", "topic": "pool", "title": "Pool season",
                        "text": "The pool is open through September.", "status": "active"},
                       created_by="test")  # fmt: skip
    desk.learning.learn("guest-turn:gt-000000000009", "buttercup-beauty", "x")
    assert items.get(old.id).text == "The heated pool is open through October 15."
    assert len(items.search(property_slug="buttercup-beauty", statuses=("active",))) == 1
    assert desk.learning.learn("guest-turn:gt-000000000009", "buttercup-beauty", "x") == []
