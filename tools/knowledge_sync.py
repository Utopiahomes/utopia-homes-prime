#!/usr/bin/env python
"""Keeps Lucy's knowledge in step with the website: website property feed -> candidate release.

The website publishes the facts shown on its property pages at /lucy-knowledge/properties.json.
This tool turns them, plus the hand-maintained entries in knowledge/base/, into a
`lucy-public-knowledge-v1` corpus. When the result differs from the latest approved release, it
proposes the next release (file + register entry) for Ray to approve by merging the pull request
the knowledge-sync workflow opens. Nothing reaches a guest until that merge is deployed.

    python tools/knowledge_sync.py --source https://www.utopiahomes.com/lucy-knowledge/properties.json
        Shows whether the website has changed since the latest release. Writes nothing.
    python tools/knowledge_sync.py --source <url-or-file> --propose --summary summary.md
        Also writes the next release and its register entry, and a readable summary of changes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge_release  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "knowledge" / "base" / "homes-base-entries.json"
FEED_SCHEMA = "utopia-homes-public-properties-v1"
SITE = "https://www.utopiahomes.com"
DEFAULT_SOURCE = f"{SITE}/lucy-knowledge/properties.json"
RELEASE_RE = re.compile(r"^homes-knowledge:r(\d+)$")


class FeedError(ValueError):
    """The website feed cannot be turned into knowledge. Nothing is proposed."""


def load_feed(source: str) -> dict[str, Any]:
    if source.startswith("https://"):
        request = urllib.request.Request(source, headers={"User-Agent": "utopia-homes-prime-sync"})
        with urllib.request.urlopen(request, timeout=30) as response:
            feed = json.load(response)
    else:
        feed = json.loads(Path(source).read_text(encoding="utf-8"))
    if not isinstance(feed, dict) or feed.get("schema") != FEED_SCHEMA:
        raise FeedError(f"the website feed is not {FEED_SCHEMA}")
    if not feed.get("properties"):
        raise FeedError("the website feed lists no active properties")
    return feed


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


def summarize(previous: dict[str, Any] | None, corpus: dict[str, Any], release_id: str) -> str:
    old = {e["id"]: e for e in (previous or {}).get("entries", [])}
    new = {e["id"]: e for e in corpus["entries"]}
    lines = [
        f"## Lucy knowledge update: {release_id}",
        "",
        "The website's property pages changed, so Lucy's knowledge should change with them. "
        "**Merging this pull request approves exactly these entries** for Lucy to use once "
        "Homes Prime is deployed. Close it to reject them.",
        "",
    ]
    facts_shown: set[str] = set()
    for label, ids in (
        ("Changed", sorted(i for i in old.keys() & new.keys() if old[i] != new[i])),
        ("Added", sorted(new.keys() - old.keys())),
        ("Removed", sorted(old.keys() - new.keys())),
    ):
        if not ids:
            continue
        lines.append(f"### {label} ({len(ids)})")
        for i in ids:
            if label == "Removed":
                lines.append(f"- **{old[i]['title']}** — ~~{old[i]['approved_text']}~~")
                continue
            lines.append(f"- **{new[i]['title']}**: {new[i]['approved_text']}")
            if label == "Changed" and old[i]["approved_text"] != new[i]["approved_text"]:
                lines.append(f"  - was: {old[i]['approved_text']}")
            slug = new[i].get("property_slug")
            if (
                slug
                and slug not in facts_shown
                and (label == "Added" or old[i].get("property_facts") != new[i]["property_facts"])
            ):
                facts_shown.add(slug)
                facts = json.dumps(new[i]["property_facts"])
                lines.append(
                    f"  - {new[i]['source']['label']} facts Lucy checks against: `{facts}`"
                )
        lines.append("")
    return "\n".join(lines)


def next_release_id(releases: list[dict[str, Any]]) -> tuple[str, int]:
    numbers = [int(m.group(1)) for r in releases if (m := RELEASE_RE.match(r["release_id"]))]
    n = max(numbers, default=0) + 1
    return f"homes-knowledge:r{n}", n


def github_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="feed URL or local file")
    parser.add_argument("--propose", action="store_true", help="write the next release")
    parser.add_argument("--summary", type=Path, help="write a Markdown summary of the changes")
    parser.add_argument("--approved-by", default="Ray")
    parser.add_argument(
        "--note",
        default="Proposed by tools/knowledge_sync.py from the website property feed; "
        "approved by merging its pull request.",
    )
    args = parser.parse_args()

    releases = knowledge_release.load_register()
    latest = releases[-1]
    previous = json.loads((ROOT / latest["file"]).read_text(encoding="utf-8"))
    base = json.loads(BASE.read_text(encoding="utf-8"))
    try:
        corpus = build_corpus(load_feed(args.source), base, previous, datetime.now(UTC).date())
    except FeedError as exc:
        raise SystemExit(f"REJECTED: {exc}") from None

    if knowledge_release.canonical_corpus_digest(corpus) == latest["canonical_digest"]:
        print(f"No changes: the website matches {latest['release_id']}.")
        github_output(changed="false")
        return

    release_id, n = next_release_id(releases)
    summary = summarize(previous, corpus, release_id)
    print(summary)
    if args.summary:
        args.summary.write_text(summary + "\n", encoding="utf-8", newline="\n")
    if not args.propose:
        github_output(changed="true", release_id=release_id)
        return

    path = ROOT / "knowledge" / f"r{n}" / f"utopia-public-knowledge.r{n}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # LF on every platform: the register records this file's exact bytes.
    text = json.dumps(corpus, ensure_ascii=False, indent=1) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        knowledge_release.command_record(
            argparse.Namespace(
                candidate=path,
                release_id=release_id,
                approved_by=args.approved_by,
                approved_on=None,
                note=args.note,
                hostnames=knowledge_release.DEFAULT_HOSTNAMES,
            )
        )
    except (knowledge_release.KnowledgeUnavailable, ValueError) as exc:
        path.unlink()
        raise SystemExit(f"REJECTED: {exc}") from None
    github_output(changed="true", release_id=release_id)


if __name__ == "__main__":
    main()
