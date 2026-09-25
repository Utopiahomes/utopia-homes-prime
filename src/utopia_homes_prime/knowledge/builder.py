"""Builds Lucy's guest knowledge (`lucy-public-knowledge-v1`) from property records.

Each active home becomes 7 entries (overview, capacity, amenities, highlights, parking, pets,
accessibility), plus the hand-maintained non-property entries in knowledge/base/. Used live by
Homes Prime (from the business core's records) and by tools/knowledge_sync.py (release proposals).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

SITE = "https://www.utopiahomes.com"


class FeedError(ValueError):
    """The property records cannot be turned into knowledge."""


def _number(value: float | int) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _series(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _in_sentence(item: str) -> str:
    """'Heated private pool' -> 'heated private pool', but 'Wi-Fi' and 'HDTV' stay as written."""
    first, _, rest = item.partition(" ")
    if re.fullmatch(r"[A-Z][a-z'-]*", first):
        return first[0].lower() + first[1:] + (" " + rest if rest else "")
    return item


def _facts(p: dict[str, Any]) -> dict[str, Any]:
    """The structured facts Homes checks answers against, derived the way the website always has."""
    for field in ("max_guests", "bedrooms", "bathrooms"):
        if not isinstance(p.get(field), (int, float)):
            raise FeedError(f"{p['name']} has no {field} on the website")
    parking = re.search(r"\d+", p["parking"])
    if parking is None:
        raise FeedError(f"{p['name']}'s parking text has no number of cars: {p['parking']!r}")
    amenities = [a for group in p["amenities"] for a in group["amenities"]]
    return {
        "max_guests": int(p["max_guests"]),
        "parking_spaces": int(parking.group()),
        "has_pool": any(re.search(r"\bpool\b", a, re.I) for a in amenities),
        "has_hot_tub": any(re.search(r"hot tub", a, re.I) for a in amenities),
        "bedrooms": int(p["bedrooms"]),
        "bathrooms": p["bathrooms"],
        "pets_allowed": bool(re.search(r"welcome", p["pet_policy"], re.I)),
    }


def _nick(p: dict[str, Any]) -> str:
    return re.sub(r"^The\s+", "", p["name"]).split()[0]


def id_keys(properties: list[dict[str, Any]]) -> dict[str, str]:
    """Each home's identifier prefix: its short name ('buttercup', 'shamrock'), which keeps entry
    and source IDs stable for the acceptance set, or its slug when short names would collide."""
    nicks = {p["slug"]: _nick(p).lower() for p in properties}
    counts = {n: list(nicks.values()).count(n) for n in nicks.values()}
    return {
        slug: n if counts[n] == 1 and re.fullmatch(r"[a-z0-9]+", n) else slug
        for slug, n in nicks.items()
    }


def property_entries(p: dict[str, Any], key: str) -> list[dict[str, Any]]:
    name, slug = p["name"], p["slug"]
    nick = _nick(p)
    href = f"{SITE}/stays/{slug}"
    common = {
        "service_line": "homes",
        "route": "property",
        "property_slug": slug,
        "property_facts": _facts(p),
        "source": {"id": f"{key}-page", "label": name, "href": href},
        "links": [{"id": f"{key}-page-link", "label": f"Explore {name}", "href": href}],
    }

    capacity = [f"welcomes up to {p['max_guests']} guests"]
    rooms = [
        f"{_number(p[k])} {label}"
        for k, label in (("bedrooms", "bedrooms"), ("beds", "beds"), ("bathrooms", "bathrooms"))
        if p.get(k) is not None
    ]
    capacity.append(f"has {_series(rooms)}")

    amenity_text = " ".join(
        f"{group['name']}: {_series([_in_sentence(a) for a in group['amenities']])}."
        for group in p["amenities"]
        if group["amenities"]
    )
    parking = p["parking"].strip()
    parking_text = (
        f"{name} has {parking[0].lower()}{parking[1:]}"
        if parking.lower().startswith("parking")
        else f"{name}: {parking}"
    )
    pets = p["pet_policy"].strip().rstrip(".")
    pets_text = f"{pets} at {name}." if pets.lower().endswith("welcome") else f"{name}: {pets}."

    specs: list[tuple[str, str, str, str, list[str], list[str], bool]] = [
        (
            "overview",
            "description",
            f"About {name}",
            f"{p['full_description'].strip()} {name} is in {p['city']}, {p['state']}.",
            [nick, f"{nick} house", f"{p['city']} house"],
            ["overview", "location"],
            False,
        ),
        (
            "capacity",
            "fact",
            f"{name} capacity",
            f"{name} {' and '.join(capacity)}.",
            [f"{nick} size", f"{nick} sleeps", "large group", "people"],
            ["capacity", "bedrooms", "bathrooms"],
            True,
        ),
        (
            "amenities",
            "fact",
            f"{name} amenities",
            f"{name} amenities. {amenity_text}",
            ["swimming", "outside", "yard", "spa", "beach", "wifi", "kitchen", "laundry"],
            ["amenities", "pool", "outdoors"],
            True,
        ),
        (
            "highlights",
            "fact",
            f"{name} highlights",
            f"Highlights of {name}: {'; '.join(f.rstrip('.') for f in p['unique_features'])}.",
            [f"what makes {nick} special", "features", "square feet"],
            ["features", "overview"],
            True,
        ),
        (
            "parking",
            "fact",
            f"{name} parking",
            f"{parking_text.rstrip('.')}.",
            ["driveway", "vehicles", "automobiles", "garage"],
            ["parking"],
            True,
        ),
        (
            "pets",
            "policy",
            f"{name} pet policy",
            pets_text,
            ["pet friendly", "bring my dog", "pups"],
            ["pets"],
            True,
        ),
        (
            "accessibility",
            "policy",
            f"{name} accessibility",
            f"{name}: {p['accessibility'].strip()}",
            ["wheelchair", "stairs", "mobility", "accessible"],
            ["accessibility"],
            True,
        ),
    ]
    entries = []
    for suffix, kind, title, text, aliases, topics, direct in specs:
        if suffix == "highlights" and not p["unique_features"]:
            continue
        entries.append(
            {
                "id": f"{key}-{suffix}",
                "kind": kind,
                "title": title,
                "approved_text": text,
                "aliases": aliases,
                "topics": topics,
                "direct_answer": direct,
                **common,
            }
        )
    return entries


def collection_text(properties: list[dict[str, Any]]) -> str:
    states = {p["state"] for p in properties}
    if len(states) == 1:
        places = [f"{p['name']} in {p['city']}" for p in properties]
        return f"The current Utopia Homes collection includes {_series(places)}, {states.pop()}."
    places = [f"{p['name']} in {p['city']}, {p['state']}" for p in properties]
    return f"The current Utopia Homes collection includes {_series(places)}."


def build_corpus(
    feed: dict[str, Any], base: dict[str, Any], previous: dict[str, Any] | None, today: date
) -> dict[str, Any]:
    """The next corpus. An entry whose content is unchanged keeps its previous effective time, so
    an unchanged website produces byte-identical knowledge and no new release."""
    entries: list[dict[str, Any]] = []
    for entry in base["entries"]:
        entry = dict(entry)
        if entry["id"] == "collection-overview":
            entry["approved_text"] = collection_text(feed["properties"])
        entries.append(entry)
    keys = id_keys(feed["properties"])
    for p in feed["properties"]:
        entries.extend(property_entries(p, keys[p["slug"]]))

    before = {e["id"]: e for e in (previous or {}).get("entries", [])}
    stamp = f"{today.isoformat()}T00:00:00Z"
    result = []
    for entry in entries:
        entry.pop("effective_from", None)
        old = before.get(entry["id"])
        unchanged = old is not None and {k: v for k, v in old.items() if k != "effective_from"} == {
            k: v for k, v in entry.items()
        }
        entry["effective_from"] = old["effective_from"] if unchanged and old else stamp
        result.append(entry)
    result.sort(key=lambda e: e["id"])
    return {"schema": "lucy-public-knowledge-v1", "entries": result}
