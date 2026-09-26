"""Scrubbing guest-message text before it is stored, searched, or sent to a model.

Removes what must never spread: door, lock, gate and Wi-Fi codes and passwords, phone numbers,
email addresses, booking confirmation codes, and the guest's name in greetings and sign-offs.
Used by the Airbnb import now and by the PMS message ingest later. Deliberately conservative:
a stray number near "code" is removed even when it is harmless.
"""

from __future__ import annotations

import re

_CODE_WORDS = (
    r"code|codes|lock|lockbox|lock box|keypad|key pad|door|gate|pin|garage|closet|"
    r"wi-?fi|wifi|password|passcode|pass code|pw|network|combo|combination|alarm"
)
# A 3–8 digit (or #-prefixed) token within ~70 characters after a code word, even across a
# sentence ("...code for the front door. That is 12345678"), or just before one.
_CODE_AFTER = re.compile(rf"(?i)\b({_CODE_WORDS})\b([^\n]{{0,70}}?)(#?\d{{3,8}}#?)(?!\d)")
_CODE_BEFORE = re.compile(
    rf"(?i)(?<![\d$.])(#?\d{{3,8}}#?)(\s+(?:[a-z']+\s+){{0,3}}|\s*)({_CODE_WORDS})\b"
)
# "password: sunshine42" / "network name: X" style secrets.
_SECRET_AFTER_LABEL = re.compile(
    r"(?i)\b(password|passcode|pw|wi-?fi password|network key)\s*(?:is|:|=|-)\s*(\S{4,40})"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_CONFIRMATION = re.compile(r"\bHM[A-Z0-9]{8}\b")
# Greeting words match any case; the name must be Capitalized (so "hi how are you" is untouched).
_GREETING = re.compile(
    r"\b((?i:hi|hello|hey|dear|good morning|good afternoon|good evening))"
    r"\s+([A-Z][a-z'’-]{1,20})\b"
)
_SIGNOFF = re.compile(
    r"(?m)^((?i:thanks|thank you|best|cheers|regards|sincerely)[!,.]*\s*)\n?"
    r"([A-Z][a-z'’-]{1,20})\s*$"
)
_HOST_NAMES = frozenset({"Meghan", "Meg", "Ray", "Raymond", "Utopia", "Lucy", "There", "All",
                         "Everyone", "Again", "Guys", "Team"})  # fmt: skip


# Years in dates ("August 2024", "in 2023", "summer 2025") are not codes. They are hidden from the
# code rules while scrubbing (digits spelled as letters) and restored afterwards.
_DATED_YEAR = re.compile(
    r"(?i)\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
    r"(?:\s+\d{1,2}(?:st|nd|rd|th)?,?)?\s+|(?:in|since|by|of|from|until|during|summer|spring|"
    r"fall|winter|early|late|mid)\s+)(20[12]\d)\b"
)
_DIGITS_AS_LETTERS = str.maketrans("0123456789", "abcdefghij")
_LETTERS_AS_DIGITS = str.maketrans("abcdefghij", "0123456789")


def _hide_years(text: str) -> str:
    return _DATED_YEAR.sub(
        lambda m: f"{m.group(1)}⟦{m.group(2).translate(_DIGITS_AS_LETTERS)}⟧", text
    )


def _show_years(text: str) -> str:
    return re.sub(r"⟦([a-j]{4})⟧", lambda m: m.group(1).translate(_LETTERS_AS_DIGITS), text)


def scrub(text: str, *, keep_names: frozenset[str] = _HOST_NAMES) -> str:
    """The text with secrets and personal details replaced by [CODE], [PHONE], [EMAIL],
    [BOOKING], and [GUEST]. Host names (Meghan, Ray) are kept."""
    return _show_years(_scrub(_hide_years(text), keep_names))


def _scrub(text: str, keep_names: frozenset[str]) -> str:
    text = _EMAIL.sub("[EMAIL]", text)
    text = _CONFIRMATION.sub("[BOOKING]", text)
    text = _SECRET_AFTER_LABEL.sub(lambda m: f"{m.group(1)}: [CODE]", text)
    text = _PHONE.sub("[PHONE]", text)
    # A message can carry several codes after one word ("codes are 1234 and 5678").
    for _ in range(3):
        text = _CODE_AFTER.sub(lambda m: f"{m.group(1)}{m.group(2)}[CODE]", text)
    text = _CODE_BEFORE.sub(lambda m: f"[CODE]{m.group(2)}{m.group(3)}", text)
    text = _GREETING.sub(
        lambda m: m.group(0) if m.group(2) in keep_names else f"{m.group(1)} [GUEST]", text
    )
    text = _SIGNOFF.sub(
        lambda m: m.group(0) if m.group(2) in keep_names else f"{m.group(1)}[GUEST]", text
    )
    return text


def leaks(text: str) -> list[str]:
    """What a scrubbed text still contains that looks like a secret or contact detail. The
    import fails closed if this returns anything."""
    text = _hide_years(text)
    found = []
    if _EMAIL.search(text):
        found.append("email")
    if _PHONE.search(text):
        found.append("phone")
    if _CODE_AFTER.search(text) or _CODE_BEFORE.search(text):
        found.append("code")
    if _CONFIRMATION.search(text):
        found.append("booking")
    return found
