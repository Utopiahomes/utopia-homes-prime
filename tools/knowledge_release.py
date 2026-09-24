#!/usr/bin/env python
"""Homes knowledge releases (cutover gate C2): candidate -> check -> Ray approves -> record -> pin.

Knowledge stays as reviewable files in this repository. A release is a corpus file plus its
canonical digest, recorded in knowledge/releases.json with who approved it. A deployment pins
exactly one recorded release through its environment, so only approved bytes ever reach a guest.

    python tools/knowledge_release.py check knowledge/candidates/r2.json
        Validates the candidate the way Homes Prime admits it at startup, and shows what changed
        against the current approved release.
    python tools/knowledge_release.py record knowledge/r2/utopia-public-knowledge.r2.json \\
        --release-id homes-knowledge:r2 --approved-by Ray --note "..."
        Adds the release to the register. Run it only when Ray has approved the exact file.
    python tools/knowledge_release.py pin homes-knowledge:r2
        Prints the environment a deployment uses to serve that release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from utopia_homes_prime.knowledge.projection import (
    KnowledgeProjection,
    KnowledgeUnavailable,
    canonical_corpus_digest,
)

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "knowledge" / "releases.json"
DEFAULT_HOSTNAMES = frozenset({"www.utopiahomes.com"})
RELEASE_ID_RE = re.compile(r"^[\x21-\x7e]{1,128}$")


def load_register() -> list[dict[str, Any]]:
    return list(json.loads(REGISTER.read_text(encoding="utf-8"))["releases"])


def check(path: Path, hostnames: frozenset[str]) -> dict[str, Any]:
    """Admits the candidate exactly as Homes Prime would at startup; raises on any problem."""
    document = json.loads(path.read_text(encoding="utf-8"))
    digest = canonical_corpus_digest(document)
    projection = KnowledgeProjection.load(
        path,
        release_id="candidate",
        allowed_corpus_digests=frozenset({digest}),
        withdrawn_ids=frozenset(),
        approved_hostnames=hostnames,
    )
    effective = projection.effective(datetime.now(UTC))
    return {
        "file": str(path),
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "canonical_digest": digest,
        "entries": len(document["entries"]),
        "effective_now": len(effective.entries_by_id),
        "links": len(effective.links_by_id),
        "ids": sorted(entry["id"] for entry in document["entries"]),
        "texts": {entry["id"]: json.dumps(entry, sort_keys=True) for entry in document["entries"]},
    }


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[str]]:
    old_ids, new_ids = set(old["texts"]), set(new["texts"])
    return {
        "added": sorted(new_ids - old_ids),
        "removed": sorted(old_ids - new_ids),
        "changed": sorted(i for i in old_ids & new_ids if old["texts"][i] != new["texts"][i]),
    }


def command_check(args: argparse.Namespace) -> None:
    result = check(args.candidate, args.hostnames)
    print(
        f"OK: {result['entries']} entries ({result['effective_now']} effective now), "
        f"{result['links']} links"
    )
    print(f"canonical digest: {result['canonical_digest']}")
    print(f"file sha256:      {result['file_sha256']}")
    releases = load_register()
    if releases:
        current = releases[-1]
        previous = check(ROOT / current["file"], args.hostnames)
        print(f"changes against {current['release_id']}: {json.dumps(diff(previous, result))}")


def command_record(args: argparse.Namespace) -> None:
    if not RELEASE_ID_RE.fullmatch(args.release_id):
        raise SystemExit("release ID must be 1-128 printable ASCII characters")
    result = check(args.candidate, args.hostnames)
    path = args.candidate.resolve()
    if ROOT not in path.parents:
        raise SystemExit("a release file must live in this repository")
    register = json.loads(REGISTER.read_text(encoding="utf-8"))
    for release in register["releases"]:
        if release["release_id"] == args.release_id:
            raise SystemExit(f"{args.release_id} is already recorded")
        if release["canonical_digest"] == result["canonical_digest"]:
            raise SystemExit(f"these exact contents are already {release['release_id']}")
    register["releases"].append(
        {
            "release_id": args.release_id,
            "file": path.relative_to(ROOT).as_posix(),
            "canonical_digest": result["canonical_digest"],
            "file_sha256": result["file_sha256"],
            "approved_by": args.approved_by,
            "approved_on": args.approved_on or date.today().isoformat(),
            "note": args.note,
        }
    )
    REGISTER.write_text(json.dumps(register, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"recorded {args.release_id} ({result['canonical_digest']})")


def command_pin(args: argparse.Namespace) -> None:
    release = next((r for r in load_register() if r["release_id"] == args.release_id), None)
    if release is None:
        raise SystemExit(f"{args.release_id} is not a recorded release")
    check(ROOT / release["file"], args.hostnames)  # never pin a file that no longer admits
    print(f"GUEST_ANSWER_PROVIDER_KNOWLEDGE_RELEASE_ID={release['release_id']}")
    print(f"GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_PATH=/app/{release['file']}")
    print(
        f"GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_ALLOWED_DIGESTS={release['canonical_digest']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--hostnames",
        type=lambda v: frozenset(h.strip() for h in v.split(",") if h.strip()),
        default=DEFAULT_HOSTNAMES,
        help="approved link hostnames (comma-separated)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check")
    p.add_argument("candidate", type=Path)
    p.set_defaults(run=command_check)
    p = sub.add_parser("record")
    p.add_argument("candidate", type=Path)
    p.add_argument("--release-id", required=True)
    p.add_argument("--approved-by", required=True)
    p.add_argument("--approved-on", help="YYYY-MM-DD; defaults to today")
    p.add_argument("--note", default="")
    p.set_defaults(run=command_record)
    p = sub.add_parser("pin")
    p.add_argument("release_id")
    p.set_defaults(run=command_pin)
    args = parser.parse_args()
    try:
        args.run(args)
    except (KnowledgeUnavailable, ValueError) as exc:
        raise SystemExit(f"REJECTED: {exc}") from None


if __name__ == "__main__":
    sys.exit(main())
