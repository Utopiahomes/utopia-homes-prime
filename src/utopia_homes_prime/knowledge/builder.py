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


def _common(p: dict[str, Any], key: str) -> dict[str, Any]:
    """The fields every entry about this home shares: its facts, page source, and page link."""
    name, slug = p["name"], p["slug"]
    href = f"{SITE}/stays/{slug}"
    return {
        "service_line": "homes",
        "route": "property",
        "property_slug": slug,
        "property_facts": _facts(p),
        "source": {"id": f"{key}-page", "label": name, "href": href},
        "links": [{"id": f"{key}-page-link", "label": f"Explore {name}", "href": href}],
    }


def _stay_rules(p: dict[str, Any]) -> str | None:
    parts = []
    if p.get("check_in_time"):
        parts.append(f"check-in is at {p['check_in_time']}")
    if p.get("check_out_time"):
        parts.append(f"checkout is at {p['check_out_time']}")
    if p.get("min_age"):
        parts.append(f"groups must be {p['min_age']} or older unless families with children")
    if p.get("min_stay"):
        parts.append(f"minimum stay: {p['min_stay'].rstrip('.')}")
    if not parts:
        return None
    text = "; ".join(parts)
    return f"At {p['name']}, {text}."


def property_entries(p: dict[str, Any], key: str) -> list[dict[str, Any]]:
    name = p["name"]
    nick = _nick(p)
    common = _common(p, key)

    capacity = [f"welcomes up to {p['max_guests']} guests"]
    rooms = [
        f"{_number(p[k])} {label}"
        for k, label in (("bedrooms", "bedrooms"), ("beds", "beds"), ("bathrooms", "bathrooms"))
        if p.get(k) is not None
    ]
    capacity.append(f"has {_series(rooms)}")
    layout = "; ".join(
        f"{r['room']}: {_series(r['beds'])}" for r in p.get("beds_by_room") or [] if r.get("beds")
    )

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
            f"{name} {' and '.join(capacity)}." + (f" Bedrooms: {layout}." if layout else ""),
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
    rules = _stay_rules(p)
    if rules:
        specs.append(
            (
                "stay-rules",
                "policy",
                f"{name} check-in, checkout, and stay rules",
                rules,
                ["arrive", "arrival time", "leave", "departure", "age", "minimum nights"],
                ["check-in", "checkout", "rules"],
                True,
            )
        )
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
        text = f"The current Utopia Homes collection includes {_series(places)}, {min(states)}."
    else:
        places = [f"{p['name']} in {p['city']}, {p['state']}" for p in properties]
        text = f"The current Utopia Homes collection includes {_series(places)}."
    if len(text) <= 1500:
        return text
    # Too many homes to name in one entry: each home's own entries carry its name and town.
    towns = sorted({f"{p['city']}, {p['state']}" for p in properties})
    return (
        f"The current Utopia Homes collection includes {len(properties)} homes in "
        f"{_series(towns[:20])}{' and more' if len(towns) > 20 else ''}."
    )


_ITEM_KINDS = {"fact": "fact", "faq": "fact", "policy": "policy", "place": "description"}
GENERAL_SOURCE = {"id": "utopia-home", "label": "Utopia Homes", "href": f"{SITE}/"}


def item_entries(
    items: list[dict[str, Any]], properties: list[dict[str, Any]], keys: dict[str, str]
) -> list[dict[str, Any]]:
    """Approved public knowledge items as guest-knowledge entries. Items for a home that is not
    active are left out, and only public, active items may ever be passed in."""
    by_slug = {p["slug"]: p for p in properties}
    entries = []
    for item in items:
        if item["audience"] != "public" or item["status"] != "active":
            raise FeedError("only active public knowledge items can reach guest knowledge")
        kind = _ITEM_KINDS.get(item["kind"])
        if kind is None:
            continue  # playbooks and other internal kinds never become guest knowledge
        slug = item.get("property_slug")
        if slug is not None:
            p = by_slug.get(slug)
            if p is None:
                continue
            placement = _common(p, keys[slug])
        else:
            placement = {
                "service_line": "general",
                "route": "general",
                "source": GENERAL_SOURCE,
                "links": [],
            }
        entry = {
            "id": item["id"],
            "kind": kind,
            "title": item["title"],
            "approved_text": item["text"],
            "aliases": [],
            "topics": [item["topic"]],
            "direct_answer": True,
            **placement,
        }
        if item.get("effective_from"):
            entry["effective_from"] = item["effective_from"]
        if item.get("effective_until"):
            entry["effective_until"] = item["effective_until"]
        entries.append(entry)
    return entries


def build_corpus(
    feed: dict[str, Any],
    base: dict[str, Any],
    previous: dict[str, Any] | None,
    today: date,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The next corpus. An entry whose content is unchanged keeps its previous effective time, so
    an unchanged website produces byte-identical knowledge and no new release. `items` are the
    approved public knowledge items; an item's own effective dates are kept."""
    entries: list[dict[str, Any]] = []
    for entry in base["entries"]:
        entry = dict(entry)
        if entry["id"] == "collection-overview":
            entry["approved_text"] = collection_text(feed["properties"])
        entries.append(entry)
    keys = id_keys(feed["properties"])
    for p in feed["properties"]:
        entries.extend(property_entries(p, keys[p["slug"]]))
    dated = {i["id"] for i in items or [] if i.get("effective_from")}
    entries.extend(item_entries(items or [], feed["properties"], keys))

    before = {e["id"]: e for e in (previous or {}).get("entries", [])}
    stamp = f"{today.isoformat()}T00:00:00Z"
    result = []
    for entry in entries:
        if entry["id"] in dated:
            result.append(entry)
            continue
        entry.pop("effective_from", None)
        old = before.get(entry["id"])
        unchanged = old is not None and {k: v for k, v in old.items() if k != "effective_from"} == {
            k: v for k, v in entry.items()
        }
        entry["effective_from"] = old["effective_from"] if unchanged and old else stamp
        result.append(entry)
    result.sort(key=lambda e: e["id"])
    return {"schema": "lucy-public-knowledge-v1", "entries": result}
