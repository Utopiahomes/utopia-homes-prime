"""The website feed becomes admissible knowledge, and only real website changes propose a release.
No network: the feed is a saved copy of utopiahomes.com/lucy-knowledge/properties.json."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from utopia_homes_prime.config import ConfigError, resolve_knowledge_release

ROOT = Path(__file__).resolve().parents[2]
FEED = json.loads(
    (ROOT / "tests/fixtures/website-properties-feed.json").read_text(encoding="utf-8")
)
BASE = json.loads((ROOT / "knowledge/base/homes-base-entries.json").read_text(encoding="utf-8"))
HOSTS = frozenset({"www.utopiahomes.com"})


def _tool():
    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location(
        "knowledge_sync", ROOT / "tools/knowledge_sync.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _tool()


def _admit(corpus, tmp_path):
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(corpus), encoding="utf-8")
    return sync.knowledge_release.check(path, HOSTS)


def test_the_feed_becomes_knowledge_homes_prime_admits(tmp_path):
    corpus = sync.build_corpus(FEED, BASE, None, date(2026, 9, 25))
    result = _admit(corpus, tmp_path)
    assert result["entries"] == len(BASE["entries"]) + 7 * len(FEED["properties"])
    by_id = {e["id"]: e for e in corpus["entries"]}
    assert by_id["shamrock-capacity"]["approved_text"] == (
        "The Shamrock welcomes up to 32 guests and has 10 bedrooms, 19 beds, and 5 bathrooms."
    )
    assert by_id["shamrock-pets"]["property_facts"]["has_pool"] is False
    assert by_id["buttercup-parking"]["property_facts"]["parking_spaces"] == 4
    assert (
        "Central Ave Socialization in North Wildwood"
        in by_id["collection-overview"]["approved_text"]
    )
    assert "airbnb" not in json.dumps(corpus).lower()


def test_an_unchanged_website_proposes_nothing():
    first = sync.build_corpus(FEED, BASE, None, date(2026, 9, 25))
    again = sync.build_corpus(FEED, BASE, first, date(2026, 10, 30))
    assert again == first


def test_a_change_touches_only_that_home():
    first = sync.build_corpus(FEED, BASE, None, date(2026, 9, 25))
    feed = copy.deepcopy(FEED)
    shamrock = next(p for p in feed["properties"] if p["slug"] == "the-shamrock")
    shamrock["parking"] = "Parking for 5 cars."
    later = sync.build_corpus(feed, BASE, first, date(2026, 10, 30))
    changed = {e["id"] for e in later["entries"] if e["effective_from"] == "2026-10-30T00:00:00Z"}
    assert "shamrock-parking" in changed
    assert all(i.startswith("shamrock-") for i in changed)
    parking = next(e for e in later["entries"] if e["id"] == "shamrock-parking")
    assert parking["approved_text"] == "The Shamrock has parking for 5 cars."
    assert parking["property_facts"]["parking_spaces"] == 5
    assert "was: The Shamrock has parking for 6 cars." in sync.summarize(first, later, "r9")


def test_a_home_without_a_parking_number_is_rejected():
    feed = copy.deepcopy(FEED)
    feed["properties"][0]["parking"] = "Street parking nearby."
    with pytest.raises(sync.FeedError, match="parking"):
        sync.build_corpus(feed, BASE, None, date(2026, 9, 25))


def test_homes_whose_short_names_collide_fall_back_to_their_slugs():
    homes = [
        {"slug": "the-shamrock", "name": "The Shamrock"},
        {"slug": "shamrock-too", "name": "Shamrock Too"},
        {"slug": "buttercup-beauty", "name": "Buttercup Beauty"},
    ]
    assert sync.id_keys(homes) == {
        "the-shamrock": "the-shamrock",
        "shamrock-too": "shamrock-too",
        "buttercup-beauty": "buttercup",
    }


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ("Heated private pool", "heated private pool"),
        ("In-unit dryer", "in-unit dryer"),
        ("Wi-Fi", "Wi-Fi"),
        ("HDTV", "HDTV"),
        ("TV", "TV"),
    ],
)
def test_amenities_read_naturally_mid_sentence(item, expected):
    assert sync._in_sentence(item) == expected


def test_latest_approved_serves_the_last_registered_release():
    register = ROOT / "knowledge/releases.json"
    latest = json.loads(register.read_text(encoding="utf-8"))["releases"][-1]
    env = resolve_knowledge_release(
        {
            "GUEST_ANSWER_PROVIDER_KNOWLEDGE_RELEASE_ID": "latest-approved",
            "GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_REGISTER": str(register),
        }
    )
    assert env["GUEST_ANSWER_PROVIDER_KNOWLEDGE_RELEASE_ID"] == latest["release_id"]
    assert (
        env["GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_ALLOWED_DIGESTS"]
        == latest["canonical_digest"]
    )
    assert Path(env["GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_PATH"]) == ROOT / latest["file"]


def test_latest_approved_refuses_a_conflicting_explicit_pin():
    with pytest.raises(ConfigError, match="must be unset"):
        resolve_knowledge_release(
            {
                "GUEST_ANSWER_PROVIDER_KNOWLEDGE_RELEASE_ID": "latest-approved",
                "GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_REGISTER": "knowledge/releases.json",
                "GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_PATH": "/app/knowledge/r1/x.json",
            }
        )
