"""The guest-reply gate blocks what must never reach a guest, sends good replies to review or
send, and checks citations itself (synthetic replies only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from utopia_homes_prime.business_core.knowledge_items import MemoryKnowledgeItemStore
from utopia_homes_prime.business_core.records import PropertyRecord
from utopia_homes_prime.business_core.store import MemoryPropertyStore
from utopia_homes_prime.guest_reply.gate import Draft, check_reply, gather_context

ROOT = Path(__file__).resolve().parents[2]
SEED = json.loads(
    (ROOT / "knowledge/business-seed/utopia-properties.seed.json").read_text(encoding="utf-8")
)
SUPPORT = "(609) 555-0100"


def stores():
    properties = MemoryPropertyStore()
    properties.seed_if_empty([PropertyRecord.model_validate(p) for p in SEED["properties"]])
    properties.update(
        "buttercup-beauty",
        {
            "beds": 12,
            "pet_policy": "Dogs of any size are welcome, up to 4 dogs, with no pet fee. No cats.",
            "check_in_time": "4:00 PM",
            "check_out_time": "10:00 AM",
        },
        changed_by="test",
        reason="test",
    )
    items = MemoryKnowledgeItemStore()
    ids = {}
    for key, data in {
        "pool": {"property_slug": "buttercup-beauty", "audience": "booked_guest", "topic": "pool",
                 "title": "Pool season", "text": "The heated pool is open May 20 to October 1; "
                 "the heater takes about 24 hours to warm up.", "status": "active"},
        "internal": {"property_slug": "buttercup-beauty", "audience": "internal",
                     "kind": "playbook", "topic": "pool", "title": "Pool vendor",
                     "text": "Call the pool vendor; they charge extra on weekends.",
                     "status": "active"},
        "proposed": {"property_slug": "buttercup-beauty", "audience": "booked_guest",
                     "topic": "grill", "title": "Grill", "text": "Propane is provided.",
                     "status": "proposed"},
        "shamrock": {"property_slug": "the-shamrock", "audience": "booked_guest",
                     "topic": "parking", "title": "Garage", "text": "3 cars fit in the garage.",
                     "status": "active"},
    }.items():  # fmt: skip
        ids[key] = items.create({"kind": "fact"} | data, created_by="test").id
    return properties, items, ids


PROPERTIES, ITEMS, IDS = stores()


def verdict(text, cited=(), *, channel="airbnb", guest="", home="buttercup-beauty"):
    draft = Draft(text, tuple(IDS.get(c, c) for c in cited))
    ctx = gather_context(
        draft, property_slug=home, channel=channel, properties=PROPERTIES, items=ITEMS,
        guest_message=guest, support_phone=SUPPORT,
    )  # fmt: skip
    return check_reply(draft, ctx)


def test_a_supported_reply_can_be_sent():
    v = verdict(
        "Hi! The heated pool is open May 20 to October 1, and the heater needs about 24 hours.",
        ["pool"],
    )
    assert v.decision == "send", v.findings


def test_greetings_and_acknowledgements_need_no_evidence():
    for text in ("Thank you so much!", "Hi there! Let me check on that for you.",
                 "You're welcome, enjoy your stay!"):  # fmt: skip
        assert verdict(text).decision == "send", text


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("The door code is 4821.", "secret"),
        ("Front door: [CODE]", "secret"),
        ("Your reservation HMABCD1234 is set.", "secret"),
        ("Early check-in is $50.", "money"),
        ("That will be 75 dollars for the extra night.", "money"),
        ("Venmo me and we can skip the fees.", "off_platform"),
        ("Text me at 609-555-0142 when you arrive.", "off_platform"),
        ("Email me at host@example.com.", "off_platform"),
        ("Next time book direct on our website!", "off_platform"),
        ("See www.utopiahomes.com/stays for photos.", "off_platform"),
        ("Yes, you can bring 6 dogs.", "over_limit"),
        ("The house sleeps 30 guests.", "over_limit"),
        ("There is parking for 7 cars.", "over_limit"),
    ],
)
def test_never_send_on_airbnb(text, code):
    v = verdict(text)
    assert v.decision == "block" and code in v.codes, v.findings


def test_direct_bookings_may_share_approved_links_and_the_support_line():
    v = verdict(
        "Photos are at https://www.utopiahomes.com/stays/buttercup-beauty or call (609) 555-0100.",
        channel="direct",
    )
    assert "link" not in v.codes and "contact" not in v.codes
    assert verdict("Details at https://example.net", channel="direct").decision == "review"
    assert "link" in verdict("Events: https://wildwoodsnj.com/events", ["pool"]).codes


def test_counts_are_read_next_to_their_noun():
    assert "over_limit" not in verdict("The pool stays near 80 degrees; your dog is welcome.").codes
    assert "over_limit" in verdict("Sure, 5 small dogs are fine.").codes


def test_citations_are_checked_not_trusted():
    assert verdict("Pool info.", ["internal"]).decision == "block"
    assert "unapproved_evidence" in verdict("Propane is provided.", ["proposed"]).codes
    assert "wrong_home_evidence" in verdict("3 cars fit in the garage.", ["shamrock"]).codes
    assert "unknown_evidence" in verdict("Pool info.", ["ki-000000000000"]).codes


def test_numbers_need_evidence_but_the_guests_own_numbers_are_fine():
    assert "unsupported_number" in verdict("The pool heater takes 48 hours.", ["pool"]).codes
    v = verdict("Great, see you and your group of 14 on May 20!", ["pool"],
                guest="We are a group of 14 arriving May 20")  # fmt: skip
    assert "unsupported_number" not in v.codes
    assert verdict("Check-in is at 4 PM and checkout at 10.", ["pool"]).decision == "send"


def test_promises_and_claimed_actions_go_to_a_person():
    for text in ("I've contacted the pool company.", "Someone will come by tomorrow.",
                 "Early check-in is approved!", "We guarantee the hot tub will be ready.",
                 "We can offer a partial refund."):  # fmt: skip
        v = verdict(text, ["pool"])
        assert v.decision == "review", (text, v.findings)


def test_another_homes_name_needs_review_and_uncited_claims_too():
    assert "wrong_home" in verdict("The Shamrock has a garage too.", ["pool"]).codes
    assert "uncited" in verdict("The pool is heated to 84 degrees.").codes


def test_the_home_record_can_be_cited():
    v = verdict("Check-in is at 4 PM and checkout at 10 AM.", ["record"])
    assert v.decision == "send", v.findings


def test_saying_something_is_not_guaranteed_is_not_a_promise():
    for text in (
        "Early check-in isn't guaranteed.",
        "Early check-in isn’t guaranteed.",
        "We can't guarantee an early check-in.",
        "There is no guarantee of that.",
    ):
        assert "action_claim" not in verdict(text, ["pool"]).codes, text
    assert "action_claim" in verdict("We guarantee the hot tub will be ready.", ["pool"]).codes
