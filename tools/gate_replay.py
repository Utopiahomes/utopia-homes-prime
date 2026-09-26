"""Run the guest-reply gate over real host replies from the private Airbnb import.

Real replies were written by Meghan and Ray, so most should pass or go to review; a `block` is
either a true catch (a code, a price) or a false alarm to fix. The report prints counts and a few
examples per finding, scrubbed text only. Everything stays on this machine.

    python tools/gate_replay.py --threads PRIVATE/threads.jsonl --api URL [--examples 3]

Each reply is checked against the home's record and all of its active knowledge, as if Lucy had
cited everything; that isolates the never-send and number rules from citation quality.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import urllib.request
from pathlib import Path

from utopia_homes_prime.business_core.knowledge_items import KnowledgeItem
from utopia_homes_prime.business_core.records import PropertyRecord
from utopia_homes_prime.guest_reply.gate import Draft, ReplyContext, check_reply

ROOT = Path(__file__).resolve().parents[1]


def _get(api: str, token: str, path: str) -> dict:
    request = urllib.request.Request(
        api.rstrip("/") + path, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--threads", type=Path, required=True)
    parser.add_argument("--api", required=True)
    parser.add_argument(
        "--token-file", type=Path, default=ROOT / ".secrets/utopia-business-lucy-token.txt"
    )
    parser.add_argument("--examples", type=int, default=3)
    args = parser.parse_args()
    token = args.token_file.read_text(encoding="utf-8").strip()

    homes = {
        p["slug"]: PropertyRecord.model_validate(
            _get(args.api, token, f"/internal/v1/properties/{p['slug']}")["property"]
        )
        for p in _get(args.api, token, "/internal/v1/properties")["properties"]
    }
    evidence = {
        slug: {
            i["id"]: KnowledgeItem.model_validate(i)
            for i in _get(
                args.api, token, f"/internal/v1/knowledge?property={slug}&status=active&limit=500"
            )["items"]
            if i["audience"] != "internal"
        }
        for slug in homes
    }

    decisions: collections.Counter[str] = collections.Counter()
    findings: collections.Counter[str] = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)
    for line in args.threads.open(encoding="utf-8"):
        thread = json.loads(line)
        home = homes.get(thread["home"])
        if home is None:
            continue
        guest = ""
        for message in thread["messages"]:
            if message["role"] == "guest":
                guest = message["text"]
                continue
            if message["role"] != "host":
                continue
            items = evidence[home.slug]
            ctx = ReplyContext(
                property=home,
                channel="airbnb",
                evidence=items,
                other_home_names=frozenset(h.name for h in homes.values() if h.slug != home.slug),
                guest_message=guest,
            )
            verdict = check_reply(Draft(message["text"], tuple(items)), ctx)
            decisions[verdict.decision] += 1
            for f in verdict.findings:
                findings[f"{f.severity}:{f.code}"] += 1
                if len(examples[f.code]) < args.examples:
                    examples[f.code].append(f"{f.detail} || {message['text'][:220]!r}")
    total = sum(decisions.values())
    print(f"{total} host replies: " + ", ".join(f"{k} {v}" for k, v in decisions.most_common()))
    for key, count in findings.most_common():
        print(f"\n{key}: {count}")
        for example in examples[key.split(":", 1)[1]]:
            print("   ", example)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
