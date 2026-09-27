"""The scrubber removes codes, contact details, booking codes, and guest names (synthetic data
only; no real guest text lives in this repository)."""

from __future__ import annotations

import pytest

from utopia_homes_prime.business_core.scrub import leaks, scrub


@pytest.mark.parametrize(
    "text",
    [
        "The door code is 4821.",
        "Front door code: 4821# and the lockbox is 0937",
        "Keypad 482193 then pull the handle",
        "Use 4821 on the lock",
        "gate combo 1122",
        "Wifi password: SunnyDays2024",
        "The wifi is MyNetwork and the password is beachhouse99",
        "Owner's closet code 77120",
        "Here is the code for the front door. That is 12345678",
        "The lock is also [CODE]. Sometimes my husband changes it to 5566",
    ],
)
def test_codes_and_passwords_are_removed(text):
    cleaned = scrub(text)
    assert "[CODE]" in cleaned
    assert not any(ch.isdigit() for ch in cleaned.replace("[CODE]", "")) or "2024" not in cleaned
    assert leaks(cleaned) == []


def test_contact_details_and_booking_codes_are_removed():
    cleaned = scrub(
        "Text me at (609) 555-0142 or 609.555.0142, email jane.doe@example.com, HMABCD1234"
    )
    assert cleaned.count("[PHONE]") == 2 and "[EMAIL]" in cleaned and "[BOOKING]" in cleaned
    assert leaks(cleaned) == []


def test_guest_names_go_but_host_names_stay():
    assert scrub("Hi Jennifer, the pool is heated!") == "Hi [GUEST], the pool is heated!"
    assert scrub("Hello Meghan, quick question") == "Hello Meghan, quick question"
    assert scrub("Hello, Brendan, sorry!") == "Hello [GUEST], sorry!"
    assert scrub("Hi, Meghan! Quick one") == "Hi, Meghan! Quick one"
    assert scrub("hi how are you? Hey there!") == "hi how are you? Hey there!"
    assert scrub("See you soon!\nThanks,\nBrendan").endswith("[GUEST]")
    assert "Jordan" not in scrub("Is it ready? Thank you!\n\nJordan ! ")
    assert scrub("Hi, it's Jennifer! My name is Pat.") == "Hi, it's [GUEST]! My name is [GUEST]."
    assert scrub("It's Monday and this is Meghan") == "It's Monday and this is Meghan"
    assert scrub("See you then!\nMeghan").endswith("Meghan")


def test_years_in_dates_are_not_codes():
    text = "The door lock broke in August 2024 and again in 2025; the lock code is 4821."
    assert scrub(text) == (
        "The door lock broke in August 2024 and again in 2025; the lock code is [CODE]."
    )
    assert leaks(scrub(text)) == []


def test_ordinary_numbers_survive():
    text = "The house sleeps 22 with 7 bedrooms, 3.5 baths, and parking for 4 cars. Check-in 4 PM."
    assert scrub(text) == text
    assert (
        scrub("Pool is 84 degrees and the hot tub 102") == "Pool is 84 degrees and the hot tub 102"
    )
