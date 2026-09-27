"""Host cards: one card on screen, urgent first and cutting in, quiet hours for the rest,
reminders, and answers that always land on the card the host is looking at."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from utopia_homes_prime.business_core.guest import GuestDesk, GuestError, MemoryGuestStore
from utopia_homes_prime.business_core.host_cards import CardDispatcher, eastern_now, quiet
from utopia_homes_prime.business_core.knowledge_items import MemoryKnowledgeItemStore
from utopia_homes_prime.business_core.records import PropertyRecord
from utopia_homes_prime.business_core.store import MemoryPropertyStore
from utopia_homes_prime.business_core.work import MemoryWorkStore, WorkDesk

ROOT = Path(__file__).resolve().parents[2]
# 2 PM Eastern on a Tuesday in October (EDT, UTC-4).
AFTERNOON = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def setup(now: datetime = AFTERNOON):
    seed = json.loads(
        (ROOT / "knowledge/business-seed/utopia-properties.seed.json").read_text(encoding="utf-8")
    )
    properties = MemoryPropertyStore()
    properties.seed_if_empty([PropertyRecord.model_validate(p) for p in seed["properties"]])
    clock = Clock(now)
    desk = GuestDesk(MemoryGuestStore(), properties, MemoryKnowledgeItemStore(),
                     WorkDesk(MemoryWorkStore()), clock=clock)  # fmt: skip
    sent: list[str] = []
    desk.cards = CardDispatcher(desk, sent.append, clock)
    return desk, sent, clock


def waiting_turn(desk, message, *, urgency="normal", arriving_in_days=30, hand_over=False):
    start = (AFTERNOON + timedelta(days=arriving_in_days)).date()
    r = desk.create_reservation({"property_slug": "buttercup-beauty",
                                 "check_in": start.isoformat(),
                                 "check_out": (start + timedelta(days=3)).isoformat(),
                                 "guests": 8})  # fmt: skip
    turn = desk.receive(r.id, message)
    if hand_over:
        holding = "Hi! Let me check with the team and get back to you shortly."
        desk.escalate(turn.id, "exception", "needs a host", holding, urgency)
    else:
        desk.reply(turn.id, "Hi! Thanks so much, enjoy your stay!", [], urgency)
    return desk.store.turn(turn.id)


def test_eastern_time_and_quiet_hours():
    assert eastern_now(AFTERNOON).hour == 14
    assert eastern_now(datetime(2026, 12, 1, 18, tzinfo=UTC)).hour == 13  # EST in winter
    assert quiet(datetime(2026, 10, 7, 12, 30, tzinfo=UTC))  # 8:30 AM
    assert not quiet(datetime(2026, 10, 7, 13, 0, tzinfo=UTC))  # 9:00 AM
    assert quiet(datetime(2026, 10, 7, 2, 0, tzinfo=UTC))  # 10 PM


def test_one_card_at_a_time_most_urgent_first():
    desk, sent, clock = setup()
    normal = waiting_turn(desk, "Where are the beach chairs?")
    desk.cards.tick()
    assert len(sent) == 1 and normal.id in sent[0]
    later = waiting_turn(desk, "Is there a coffee maker?")
    desk.cards.tick()
    assert len(sent) == 1  # a normal card waits for the one on screen
    desk.answer_card("send", by="Ray")
    desk.cards.tick()
    assert len(sent) == 2 and later.id in sent[1]


def test_urgent_cuts_in_and_the_interrupted_card_comes_back():
    desk, sent, clock = setup()
    normal = waiting_turn(desk, "Where are the beach chairs?")
    desk.cards.tick()
    leak = waiting_turn(desk, "The dishwasher is leaking everywhere!", arriving_in_days=0,
                        hand_over=True)  # fmt: skip
    assert leak.priority == "urgent"
    desk.cards.tick()
    assert "URGENT" in sent[-1] and leak.id in sent[-1]
    desk.answer_card("revise", by="Ray", text="Hi! So sorry, someone is on the way.")
    assert desk.store.turn(leak.id).attempts[-1].author == "host"  # the plain answer hit the leak
    desk.answer_card("send", by="Ray")
    desk.cards.tick()
    assert normal.id in sent[-1]


def test_an_urgent_card_waits_while_the_host_is_mid_answer():
    desk, sent, clock = setup()
    waiting_turn(desk, "Can we check in early?", hand_over=True)
    desk.cards.tick()
    desk.answer_card("revise", by="Ray", text="Hi! Yes, you can check in at 1 PM.")
    count = len(sent)
    waiting_turn(desk, "We are locked out!", arriving_in_days=0, hand_over=True)
    desk.cards.tick()
    assert len(sent) == count  # held: Ray answered the card on screen a moment ago
    clock.now += timedelta(minutes=3)
    desk.cards.tick()
    assert "URGENT" in sent[-1]


def test_quiet_hours_hold_normal_cards_until_9_am_but_not_urgent_ones():
    night = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)  # midnight Eastern
    desk, sent, clock = setup(night)
    waiting_turn(desk, "Do you have a crib?")
    desk.cards.tick()
    assert sent == []
    waiting_turn(desk, "No heat and it's freezing", arriving_in_days=0, hand_over=True)
    desk.cards.tick()
    assert len(sent) == 1 and "URGENT" in sent[0]
    desk.answer_card("send", by="Ray")
    desk.cards.tick()
    assert len(sent) == 1  # the crib question waits
    clock.now = datetime(2026, 10, 7, 13, 0, tzinfo=UTC)  # 9 AM
    desk.cards.tick()
    assert len(sent) == 2 and "crib" in sent[1]


def test_reminders_repeat_by_priority():
    desk, sent, clock = setup()
    waiting_turn(desk, "Water is leaking from the ceiling", arriving_in_days=0, hand_over=True)
    desk.cards.tick()
    clock.now += timedelta(minutes=14)
    desk.cards.tick()
    assert len(sent) == 1
    clock.now += timedelta(minutes=2)
    desk.cards.tick()
    assert len(sent) == 2  # urgent: every 15 minutes


def test_send_needs_the_latest_wording_and_blanks_filled():
    desk, sent, clock = setup()
    turn = waiting_turn(desk, "Can we check in at noon?", hand_over=True)
    desk.cards.tick()
    desk.answer_card("revise", by="Ray", text="Hi! Yes, you can check in at [time].")
    with pytest.raises(GuestError, match="blanks"):
        desk.answer_card("send", by="Ray")
    desk.answer_card("revise", by="Ray", text="Hi! Yes, you can check in at noon.")
    with pytest.raises(GuestError, match="latest wording"):
        desk.answer_card("send", by="Ray", turn_id=turn.id, version=2)
    result = desk.answer_card("send", by="Ray")
    assert result["reply"] == "Hi! Yes, you can check in at noon."
    assert desk.store.turn(turn.id).decision.edited is True


def test_undo_reopens_the_last_answer_and_resends_its_card():
    desk, sent, clock = setup()
    turn = waiting_turn(desk, "Where are the towels?")
    desk.cards.tick()
    desk.answer_card("send", by="Ray")
    count = len(sent)
    assert desk.answer_card("undo", by="Ray")["turn_id"] == turn.id
    assert desk.store.turn(turn.id).state == "queued" and len(sent) == count + 1


def test_no_card_on_screen_means_nothing_to_answer():
    desk, sent, clock = setup()
    with pytest.raises(GuestError, match="no guest card"):
        desk.answer_card("send", by="Ray")
