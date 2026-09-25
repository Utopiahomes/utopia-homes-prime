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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge_release  # noqa: E402

from utopia_homes_prime.knowledge.builder import (  # noqa: E402,F401  (re-exported for tests)
    FeedError,
    _in_sentence,
    build_corpus,
    id_keys,
    property_entries,
)

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "knowledge" / "base" / "homes-base-entries.json"
FEED_SCHEMA = "utopia-homes-public-properties-v1"
DEFAULT_SOURCE = "https://www.utopiahomes.com/lucy-knowledge/properties.json"
RELEASE_RE = re.compile(r"^homes-knowledge:r(\d+)$")


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
