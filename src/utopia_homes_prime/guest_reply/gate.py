"""The guest-reply gate: Lucy writes a reply to a booked guest, and this code checks it.

Every reply Lucy drafts for a guest passes through `check_reply` before anyone can send it. The
gate never trusts Lucy's account of her own evidence: the caller fetches the cited items itself
(`gather_context`), and the gate treats each citation as a claim to verify.

A verdict is one of:

- `block`: the reply must never be sent as written (a door code, a price, an off-Airbnb contact or
  a link to our own booking site on Airbnb, a number over the home's limit, internal knowledge).
- `review`: a person must look first (an unsupported number, another home's name, a promise that
  something was done, a link to a site we have not approved, no evidence at all).
- `send`: nothing found. Whether a `send` goes out on its own is decided by the approval queue per
  category; at first every reply goes to a person anyway.

The checks are deliberately simple regexes and set lookups. They will miss things a person would
catch and flag things a person would pass; the replay report and human edits tell us which.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from utopia_homes_prime.business_core.knowledge_items import (
    ItemNotFound,
    KnowledgeItem,
    KnowledgeItemStore,
)
from utopia_homes_prime.business_core.records import PropertyRecord
from utopia_homes_prime.business_core.scrub import leaks
from utopia_homes_prime.business_core.store import PropertyStore

Channel = Literal["airbnb", "direct", "vrbo", "other"]
Decision = Literal["send", "review", "block"]


@dataclass(frozen=True)
class Draft:
    text: str
    cited_ids: tuple[str, ...] = ()
    """Knowledge item ids, or `record` for the home's own record (always in the context)."""


RECORD = "record"


@dataclass(frozen=True)
class ReplyContext:
    """What the gate checks a draft against. Built by `gather_context`, never by the model."""

    property: PropertyRecord
    """The reservation's home, bound from the reservation, never taken from the draft."""
    channel: Channel
    evidence: Mapping[str, KnowledgeItem]
    """The cited items as the store has them. A cited id missing here does not exist."""
    other_home_names: frozenset[str]
    guest_message: str = ""
    """The guest's latest message: a reply may repeat the guest's own numbers."""
    reservation_facts: Mapping[str, str] = field(default_factory=dict)
    approved_hostnames: frozenset[str] = frozenset({"www.utopiahomes.com"})
    support_phone: str | None = None


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Literal["block", "review"]
    detail: str


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    findings: tuple[Finding, ...]

    @property
    def codes(self) -> set[str]:
        return {f.code for f in self.findings}


# Money: Lucy never quotes prices, fees, deposits, or refunds; the platform does.
_MONEY_AMOUNT = re.compile(r"(?i)(\$\s?\d|\d+(?:\.\d\d)?\s?(?:dollars|usd|bucks)\b)")
_MONEY_PROMISE = re.compile(
    r"(?i)\b(refund(?:ed)?|reimburs\w*|discount\w*|compensat\w*|credit (?:you|your)|"
    r"waive[ds]?|partial refund|(?:we|i)(?:'ll| will) (?:pay|cover))\b"
)
# Off-platform steering, banned on Airbnb (it breaks Airbnb's rules and removes its protection).
_OFF_PLATFORM = re.compile(
    r"(?i)\b(venmo|zelle|paypal|cash ?app|wire transfer|whats ?app|telegram|signal app|"
    r"text me|call me|e-?mail me|my (?:cell|number|email)|book(?:ing)? direct(?:ly)?|"
    r"outside (?:of )?(?:airbnb|the app)|off (?:of )?airbnb|our (?:own )?website)\b"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>()\"']+")
_BARE_DOMAIN = re.compile(r"(?i)\b[a-z0-9-]+\.(?:com|net|org|io|co|us)\b(?:/[^\s]*)?")
# Saying something was done, or promising an outcome, needs a work item or a person behind it.
_ACTION_CLAIM = re.compile(
    r"(?i)\b(?:i|we)(?:'ve| have)? (?:already )?(?:contacted|called|messaged|scheduled|"
    r"booked|arranged|sent|dispatched|notified|ordered|fixed|replaced|reset)\b|"
    r"\b(?:someone|a tech(?:nician)?|the (?:pool|hvac|cleaning|repair) (?:company|team|guy))"
    r" (?:will|is going to|is on (?:the|their) way)\b|"
    r"\b(?:i|we)(?:'ll| will) (?:send|have) (?:someone|a|the)\b|"
    # "we guarantee" is a promise; "isn't guaranteed" / "no guarantee" / "can't guarantee" is not.
    r"(?<!not )(?<!n't )(?<!n’t )(?<!no )(?<!never )\bguarantee\w*\b|"
    r"\b(?:early check-?in|late check-?out|early arrival|late departure)"
    r" (?:is|are|has been|will be) (?:fine|ok(?:ay)?|approved|confirmed|no problem)\b"
)
# Replies that need no evidence: greetings, thanks, "let me check".
_NO_EVIDENCE_NEEDED = re.compile(
    r"(?i)^\W*(?:(?:hi|hello|hey|good (?:morning|afternoon|evening))\b[^.!?]*[.!?]?\s*)?"
    r"(?:thanks?(?: you)?(?: so much)?|you'?re (?:very )?welcome|my pleasure|no problem|"
    r"(?:let me|i'?ll|i will) (?:check|look into|find out|confirm)[^.!?]*|"
    r"(?:checking|looking into it) now|glad (?:to hear|you)[^.!?]*|enjoy your stay|"
    r"have a (?:great|wonderful|safe) (?:trip|stay|time)|safe travels)"
    r"[\s.!?,:)\w'-]*$"
)

_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
}  # fmt: skip
_NUMBER_WORD = re.compile(r"(?i)\b(" + "|".join(_NUMBER_WORDS) + r")\b")
_NUMBER = re.compile(r"(?<![\w.])(\d{1,5}(?::\d\d)?(?:\.\d+)?)(?![\d])")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_NUMBER = re.compile(
    rf"(?i)\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b20[2-3]\d\b"
)
# "22 guests", "4 dogs": a count the home's record caps.
_CAPPED = re.compile(
    r"(?i)\b(\d{1,3})\s+(?:(?:more|extra|additional|total|small|smaller|large|larger|big|adult)"
    r"\s+)?(guests?|people|persons|dogs?|cars?|vehicles?|beds|bedrooms)\b"
)


def _words_to_digits(text: str) -> str:
    return _NUMBER_WORD.sub(lambda m: _NUMBER_WORDS[m.group(1).lower()], text)


def _numbers(text: str) -> set[str]:
    text = _DATE_NUMBER.sub(" ", _words_to_digits(text))
    found = set()
    for n in _NUMBER.findall(text):
        found.add(n[:-3] if n.endswith(":00") else n)  # "4:00" and "4" are the same time
    return found


def _record_text(p: PropertyRecord) -> str:
    return p.model_dump_json()


def _caps(p: PropertyRecord) -> dict[str, int]:
    caps: dict[str, int] = {}
    if p.max_guests:
        caps["guest"] = p.max_guests
    if p.beds is not None:
        caps["bed"] = p.beds
    if p.bedrooms is not None:
        caps["bedroom"] = p.bedrooms
    dogs = re.search(r"(?i)up to (\d+|\w+) dogs", _words_to_digits(p.pet_policy))
    if dogs and dogs.group(1).isdigit():
        caps["dog"] = int(dogs.group(1))
    cars = re.search(r"(?i)(\d+) (?:cars|spots|spaces)", p.parking)
    if cars:
        caps["car"] = int(cars.group(1))
    return caps


def _cap_key(noun: str) -> str:
    noun = noun.lower()
    if noun in ("people", "persons") or noun.startswith("guest"):
        return "guest"
    if noun.startswith("vehicle"):
        return "car"
    return noun.rstrip("s")


def check_reply(draft: Draft, ctx: ReplyContext) -> Verdict:
    text = draft.text
    findings: list[Finding] = []

    def block(code: str, detail: str) -> None:
        findings.append(Finding(code, "block", detail))

    def review(code: str, detail: str) -> None:
        findings.append(Finding(code, "review", detail))

    # 1. Secrets. Codes are never knowledge; they reach guests from the lock system, not Lucy.
    found = leaks(text)
    if "code" in found or "[CODE]" in text:
        block("secret", "looks like a door, lock, or Wi-Fi code")
    if "booking" in found:
        block("secret", "contains a booking confirmation code")

    # 2. Money.
    if _MONEY_AMOUNT.search(text):
        block("money", "quotes a money amount")
    elif m := _MONEY_PROMISE.search(text):
        review("money_promise", f"mentions {m.group(0)!r}")

    # 3. Contact details and off-platform steering.
    support = re.sub(r"\D", "", ctx.support_phone or "")[-10:]
    phones = [p for p in _PHONE.findall(text) if re.sub(r"\D", "", p)[-10:] != support]
    if ctx.channel == "airbnb":
        if m := _OFF_PLATFORM.search(text):
            block("off_platform", f"steers the guest off Airbnb ({m.group(0)!r})")
        if _EMAIL.search(text) or phones:
            block("off_platform", "shares an email address or phone number on Airbnb")
    elif _EMAIL.search(text) or phones:
        review("contact", "shares an email address or phone number")

    # 4. Links: never our own booking site on Airbnb; anything unapproved goes to a person.
    urls = _URL.findall(text)
    bare = _BARE_DOMAIN.findall(_EMAIL.sub(" ", _URL.sub(" ", text)))
    links = urls + bare
    ours = {h.removeprefix("www.") for h in ctx.approved_hostnames}
    for link in links:
        host = (urlsplit(link if "://" in link else "https://" + link).hostname or "").lower()
        if host.removeprefix("www.") in ours:
            if ctx.channel == "airbnb":
                block("off_platform", f"links an Airbnb guest to our own site ({host})")
        elif not host.endswith("airbnb.com"):
            review("link", f"links to a site we have not approved ({host})")

    # 5. Evidence: every citation must exist, be approved, fit this guest, and be about this home.
    for cited in draft.cited_ids:
        if cited == RECORD:
            continue
        item = ctx.evidence.get(cited)
        if item is None:
            review("unknown_evidence", f"cites {cited}, which does not exist")
        elif item.audience == "internal":
            block("internal_evidence", f"cites internal knowledge {cited}")
        elif item.status != "active":
            review("unapproved_evidence", f"cites {cited}, which is {item.status}")
        elif item.property_slug not in (None, ctx.property.slug):
            review("wrong_home_evidence", f"cites {cited}, which is about {item.property_slug}")

    # 6. Another home's name in a reply about this one.
    lowered = text.lower()
    for name in sorted(ctx.other_home_names):
        if name.lower() != ctx.property.name.lower() and name.lower() in lowered:
            review("wrong_home", f"mentions {name}")

    # 7. Counts over the home's limits, then numbers nothing supports.
    caps = _caps(ctx.property)
    for count, noun in _CAPPED.findall(_words_to_digits(text)):
        key = _cap_key(noun)
        if key in caps and int(count) > caps[key]:
            block("over_limit", f"says {count} {noun}; {ctx.property.name} allows {caps[key]}")
    supported = _numbers(
        " ".join(
            [
                _record_text(ctx.property),
                ctx.guest_message,
                *ctx.reservation_facts.values(),
                *(ctx.evidence[i].text for i in draft.cited_ids if i in ctx.evidence),
            ]
        )
    )
    # Contact details and links were judged above; their digits are not claims.
    claims = _PHONE.sub(" ", _EMAIL.sub(" ", _URL.sub(" ", text)))
    unsupported = sorted(_numbers(claims) - supported - {"1"})
    if unsupported:
        review("unsupported_number", f"no evidence for {', '.join(unsupported)}")

    # 8. Claims that something was done, or promises of an outcome.
    if m := _ACTION_CLAIM.search(text):
        review("action_claim", f"claims or promises an action ({m.group(0)!r})")

    # 9. A substantive reply needs evidence.
    if not draft.cited_ids and not _NO_EVIDENCE_NEEDED.match(text.strip()):
        review("uncited", "states things without citing any knowledge")

    decision: Decision = (
        "block"
        if any(f.severity == "block" for f in findings)
        else "review"
        if findings
        else "send"
    )
    return Verdict(decision, tuple(findings))


def gather_context(
    draft: Draft,
    *,
    property_slug: str,
    channel: Channel,
    properties: PropertyStore,
    items: KnowledgeItemStore,
    guest_message: str = "",
    reservation_facts: Mapping[str, str] | None = None,
    approved_hostnames: Iterable[str] = ("www.utopiahomes.com",),
    support_phone: str | None = None,
) -> ReplyContext:
    """The gate's own view of the world: the home from the reservation, the cited items from the
    store, and every other home's name. Nothing here comes from the draft except the ids to look
    up."""
    evidence: dict[str, KnowledgeItem] = {}
    for cited in draft.cited_ids:
        if cited == RECORD:
            continue
        with contextlib.suppress(ItemNotFound):  # a made-up id stays missing; the gate flags it
            evidence[cited] = items.get(cited)
    return ReplyContext(
        property=properties.get(property_slug),
        channel=channel,
        evidence=evidence,
        other_home_names=frozenset(p.name for p in properties.all() if p.slug != property_slug),
        guest_message=guest_message,
        reservation_facts=dict(reservation_facts or {}),
        approved_hostnames=frozenset(approved_hostnames),
        support_phone=support_phone,
    )
