#!/usr/bin/env python
"""Airbnb export → proposed Utopia knowledge, for Ray to review (offline, private).

The raw export never enters this repository or production. This tool reads it locally, keeps
only the active homes, scrubs every message (codes, contact details, booking codes, guest names),
and asks a model to prefill knowledge items with confidence and evidence. The result is a review
file Ray edits; `apply` then writes the approved rows through the business core's API.

    python tools/airbnb_import.py prepare --export <Airbnb export folder> --out <private dir>
        Normalizes, attributes, classifies, and scrubs the active homes' conversations, house
        manuals, and listing text. Fails closed if anything that looks like a code or contact
        detail survives scrubbing.
    python tools/airbnb_import.py extract --out <private dir> [--homes buttercup-beauty,...]
        Prefills knowledge items per home (a model reads the scrubbed text in batches, then
        consolidates), compares them with the current property record, and writes
        review-<home>.csv plus a readable review-<home>.md.
    python tools/airbnb_import.py apply --out <private dir> --reviewed <csv> --api <base url>
        Writes the rows Ray marked approve/edit as active knowledge (and the rest he kept as
        proposed), attributed to the import and to Ray's review.

Model calls go to OpenRouter with zero data retention; only scrubbed text is ever sent.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from utopia_homes_prime.business_core.scrub import leaks, scrub  # noqa: E402

# The active homes (dormant listings stay archived; Ray, 2026-09-26).
HOMES = {
    "639163446287777223": ("buttercup-beauty", "Buttercup Beauty", ["buttercup"]),
    "1629070581710289311": ("the-shamrock", "The Shamrock", ["shamrock"]),
    "1137760792304016998": ("central-ave-socialization", "Central Ave Socialization",
                            ["central ave", "central avenue"]),
}  # fmt: skip
HOST_ACCOUNT = "16594890"
RECORD_FIELDS = [
    "max_guests", "bedrooms", "beds", "bathrooms", "amenities", "unique_features",
    "pet_policy", "parking", "accessibility", "check_in_time", "check_out_time", "min_age",
    "min_stay", "beds_by_room",
]  # fmt: skip
DEFAULT_MODEL = "openai/gpt-6-luna"
BATCH_CHARS = 90_000


# ---------------------------------------------------------------------------------- prepare


def _text(item: dict[str, Any]) -> str | None:
    content = item.get("messageContent") or {}
    text = content.get("textContent")
    if isinstance(text, dict):
        text = text.get("body")
    if text is None:
        text = (content.get("textAndReferenceContent") or {}).get("text")
    if text is None:
        translated = content.get("translatedTextContent") or {}
        text = translated.get("originalText") or translated.get("text")
    return text if isinstance(text, str) and text.strip() else None


def _norm(text: str) -> str:
    text = re.sub(r"\b(hi|hello|hey|dear)\s+\S+", "hi X", text.strip().lower())
    return re.sub(r"\s+", " ", text)[:160]


def prepare(export: Path, out: Path) -> None:
    data = export / "json"
    threads = json.loads((data / "messages.json").read_text(encoding="utf-8"))[0]["messageThreads"]
    reservations = json.loads((data / "reservations.json").read_text(encoding="utf-8"))[0][
        "reservations"
    ]
    listings = json.loads((data / "listings.json").read_text(encoding="utf-8"))[0]["listings"]
    quick = json.loads((data / "host_quick_replies.json").read_text(encoding="utf-8"))[0][
        "templates"
    ]

    by_guest: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in reservations:
        m = re.search(r"(\d+)\s*$", r.get("guestProfileUrl") or "")
        listing = (r.get("hostingUrl") or "").rsplit("/", 1)[-1]
        if m and listing in HOMES and r.get("status") == "accepted":
            by_guest[m.group(1)].append(r)

    host_counts = collections.Counter(
        _norm(t)
        for th in threads
        for mc in th["messagesAndContents"]
        if str(mc["message"].get("accountId")) == HOST_ACCOUNT and (t := _text(mc))
    )
    quick_starts = {
        _norm(json.dumps(q.get("texts") or q.get("templateMessage") or ""))[:60] for q in quick
    }

    records = []
    stats = collections.Counter()
    for th in threads:
        tags = {t.get("tagName") for t in th.get("messageThreadUserTags") or []}
        if "GUEST_INBOX" in tags:  # Utopia was the guest; roles are reversed
            stats["skipped_utopia_as_guest"] += 1
            continue
        raw = json.dumps(th, ensure_ascii=False)
        texts = [t for mc in th["messagesAndContents"] if (t := _text(mc))]
        joined = " ".join(texts).lower()
        home, resolution = None, "unresolved"
        ids = [lid for lid in HOMES if re.search(rf"(?<!\d){lid}(?!\d)", raw)]
        if len(ids) == 1:
            home, resolution = ids[0], "verified"
        else:
            accounts = {
                str(mc["message"].get("accountId"))
                for mc in th["messagesAndContents"]
                if mc["message"].get("accountId")
            } - {HOST_ACCOUNT}
            candidates = [r for a in accounts for r in by_guest.get(a, [])]
            if candidates:
                first = min(mc["message"]["createdAt"] for mc in th["messagesAndContents"])[:10]
                best = min(
                    candidates,
                    key=lambda r: abs(
                        (
                            datetime.fromisoformat(r["startDate"]) - datetime.fromisoformat(first)
                        ).days
                    ),
                )
                home, resolution = best["hostingUrl"].rsplit("/", 1)[-1], "probable"
            else:
                named = [
                    lid for lid, (_, _, words) in HOMES.items() if any(w in joined for w in words)
                ]
                if len(named) == 1:
                    home, resolution = named[0], "probable"
        if home is None:
            stats["skipped_not_an_active_home"] += 1
            continue
        messages = []
        for mc in sorted(th["messagesAndContents"], key=lambda x: x["message"]["createdAt"]):
            text = _text(mc)
            if not text:
                continue
            message = mc["message"]
            if message.get("accountType") == "service":
                role = "airbnb"
            elif str(message.get("accountId")) == HOST_ACCOUNT:
                norm = _norm(text)
                templated = host_counts[norm] >= 3 or norm[:60] in quick_starts
                role = "host_template" if templated else "host"
            else:
                role = "guest"
            clean = scrub(text)
            found = leaks(clean)
            if found:
                raise SystemExit(f"scrub left {found} in thread {th['id']}; refusing to continue")
            messages.append({"at": message["createdAt"][:16], "role": role, "text": clean})
            stats[f"messages_{role}"] += 1
        if messages:
            slug = HOMES[home][0]
            records.append(
                {
                    "thread": str(th["id"]),
                    "home": slug,
                    "resolution": resolution,
                    "first": messages[0]["at"][:10],
                    "last": messages[-1]["at"][:10],
                    "messages": messages,
                }  # fmt: skip
            )
            stats[f"threads_{slug}_{resolution}"] += 1

    sources = []
    for listing in listings:
        lid = str(listing["id"])
        if lid not in HOMES:
            continue
        slug = HOMES[lid][0]
        manual = listing.get("houseManual")
        if isinstance(manual, str) and manual.strip():
            sources.append({"home": slug, "kind": "house_manual", "text": scrub(manual)})
        for d in listing.get("listingDescriptions") or []:
            parts = [f"{k}: {d[k]}" for k in ("summary", "space", "description", "houseRules",
                                               "interaction") if d.get(k)]  # fmt: skip
            if parts:
                sources.append(
                    {"home": slug, "kind": "listing_text", "text": scrub("\n".join(parts))}
                )
    for s in sources:
        if leaks(s["text"]):
            raise SystemExit(f"scrub left {leaks(s['text'])} in {s['home']} {s['kind']}")

    out.mkdir(parents=True, exist_ok=True)
    with (out / "threads.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=1), "utf-8")
    (out / "prepare-stats.json").write_text(
        json.dumps(dict(sorted(stats.items())), indent=1), "utf-8"
    )
    print(json.dumps(dict(sorted(stats.items())), indent=1))


# ---------------------------------------------------------------------------------- extract

ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "topic", "kind", "audience", "title", "text", "confidence", "statements",
        "first_seen", "last_seen", "evidence_threads", "record_field", "record_relation",
        "conflict_note",
    ],
    "properties": {
        "topic": {"type": "string"},
        "kind": {"type": "string", "enum": ["fact", "policy", "faq", "place", "playbook"]},
        "audience": {"type": "string", "enum": ["public", "booked_guest", "internal"]},
        "title": {"type": "string"},
        "text": {"type": "string"},
        "confidence": {"type": "number"},
        "statements": {"type": "integer"},
        "first_seen": {"type": "string"},
        "last_seen": {"type": "string"},
        "evidence_threads": {"type": "array", "items": {"type": "string"}},
        "record_field": {"type": "string", "enum": ["", *RECORD_FIELDS]},
        "record_relation": {"type": "string", "enum": ["none", "confirms", "conflicts", "adds"]},
        "conflict_note": {"type": "string"},
    },
}  # fmt: skip
BATCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items", "threads"],
    "properties": {
        "items": {"type": "array", "items": ITEM_SCHEMA},
        "threads": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["thread", "intents", "stage"],
                "properties": {
                    "thread": {"type": "string"},
                    "intents": {"type": "array", "items": {"type": "string"}},
                    "stage": {
                        "type": "string",
                        "enum": [
                            "inquiry",
                            "booked_pre_arrival",
                            "arrival",
                            "during_stay",
                            "checkout",
                            "post_stay",
                            "cancellation",
                            "other",
                        ],
                    },  # fmt: skip
                },
            },
        },
    },
}
FINAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": ITEM_SCHEMA}},
}

GUIDE = """You are extracting business knowledge for Utopia Homes, a vacation-rental business in the
Wildwoods, NJ. You read scrubbed guest conversations (roles: host = Utopia, written by the owners;
host_template = Utopia's saved templates; guest; airbnb = platform automation), plus the home's
Airbnb house manual and listing text. Your output prefills a knowledge base the owner will review.

Rules:
- Extract knowledge about THIS home (or Utopia in general): how things work, what guests need to
  know, stay rules and policies, layout and beds, amenities and their quirks, local tips, and
  recurring problems and how they were resolved.
- Messages are dated evidence, not truth. Facts change (equipment added, rules changed). State
  the most recent consistent version in `text`, set first_seen/last_seen (YYYY-MM), count the
  statements that support it, and describe any disagreement or older version in conflict_note.
- Host statements outweigh guest statements; the house manual and listing text are strong
  sources but can be outdated; recent (2024–2026) outweighs old.
- confidence: 0.9+ consistent, repeated, recent or in the manual; 0.7–0.89 stated clearly but
  once or with minor drift; 0.5–0.69 conflicting or old; below 0.5 do not include.
- Policies (age minimums, group size, events, pets, cancellations, early check-in/late checkout):
  state the rule the host enforces, and list any exceptions you saw in conflict_note. Never
  turn a one-off exception into the rule.
- audience: public = fine for anyone browsing the website; booked_guest = only for guests with a
  booking (arrival steps, trash day, checkout tasks, how to operate equipment); internal = only
  the owners (vendors, breaker/shutoff locations, recurring defects, goodwill refunds, business
  decisions). kind=playbook (always internal) for recurring problems: situation, what the host
  checked or did, who was contacted, how it was resolved.
- NEVER include codes, passwords, phone numbers, emails, guest names, or anything marked
  [CODE]/[PHONE]/[EMAIL]/[GUEST]/[BOOKING]. Never include prices or availability.
- record_field: if the item corresponds to a field of the current property record (given below),
  name it and say whether it confirms, conflicts with, or adds to the record's value.
- Write `text` as a clear standalone statement a guest-facing assistant could use, in plain
  language (for internal items, as a note to the owners). Titles short.
"""


def _key() -> str:
    for line in (ROOT / "API Keys.txt").read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("OPENROUTER_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no OPENROUTER_API_KEY in API Keys.txt")


def _model_call(model: str, system: str, user: str, schema: dict[str, Any], name: str) -> Any:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        },
        "provider": {"zdr": True, "data_collection": "deny", "require_parameters": True},
        "max_tokens": 16_000,
        "reasoning": {"effort": "low"},
    }
    for attempt in range(3):
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = json.load(response)
            content = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage") or {}
            print(f"    {name}: {usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} "
                  f"out, ${usage.get('cost', 0):.4f}")  # fmt: skip
            return json.loads(content)
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
            print(f"    {name}: attempt {attempt + 1} failed ({type(exc).__name__})")
            time.sleep(5 * (attempt + 1))
    raise SystemExit(f"model call {name} failed three times")


def _record(slug: str) -> dict[str, Any]:
    seed = json.loads(
        (ROOT / "knowledge/business-seed/utopia-properties.seed.json").read_text(encoding="utf-8")
    )
    return next(p for p in seed["properties"] if p["slug"] == slug)


def _render_thread(t: dict[str, Any]) -> str:
    lines = [
        f"=== thread {t['thread']} ({t['first']} to {t['last']}, attribution {t['resolution']})"
    ]
    lines += [
        f"[{m['at'][:10]}] {m['role']}: {m['text']}" for m in t["messages"] if m["role"] != "airbnb"
    ]
    return "\n".join(lines)


def extract(out: Path, homes: list[str], model: str) -> None:
    threads = [json.loads(line) for line in (out / "threads.jsonl").open(encoding="utf-8")]
    sources = json.loads((out / "sources.json").read_text(encoding="utf-8"))
    for slug in homes:
        record = _record(slug)
        record_text = json.dumps({k: record.get(k) for k in RECORD_FIELDS}, ensure_ascii=False)
        mine = sorted((t for t in threads if t["home"] == slug), key=lambda t: t["first"])
        print(f"{slug}: {len(mine)} threads")
        batches: list[str] = []
        current = ""
        for s in sources:
            if s["home"] == slug:
                current += f"=== {s['kind']} (current Airbnb listing)\n{s['text']}\n\n"
        for t in mine:
            block = _render_thread(t) + "\n\n"
            if len(current) + len(block) > BATCH_CHARS and current:
                batches.append(current)
                current = ""
            current += block
        if current:
            batches.append(current)
        candidates: list[dict[str, Any]] = []
        intents: list[dict[str, Any]] = []
        for i, batch in enumerate(batches, 1):
            user = (
                f"Home: {record['name']} ({slug}), {record['city']}.\n"
                f"Current property record: {record_text}\n\n"
                f"Evidence batch {i}/{len(batches)}:\n\n{batch}"
            )
            result = _model_call(model, GUIDE, user, BATCH_SCHEMA, f"{slug}-batch-{i}")
            candidates += result["items"]
            intents += result["threads"]
        final = _model_call(
            model,
            GUIDE
            + "\nNow CONSOLIDATE: merge duplicate candidates from different batches into one item "
            "per distinct piece of knowledge, keep the most recent consistent statement, sum "
            "statements, widen first/last_seen, union evidence threads, recompute confidence, and "
            "keep every real conflict in conflict_note. Drop anything that breaks the rules.",
            f"Home: {record['name']} ({slug}).\nCurrent property record: {record_text}\n\n"
            f"Candidates:\n{json.dumps(candidates, ensure_ascii=False)}",
            FINAL_SCHEMA,
            f"{slug}-consolidate",
        )
        items = [i for i in final["items"] if i["confidence"] >= 0.5]
        for item in items:
            # The model's own relation label is unreliable: if its note describes the record
            # disagreeing with the evidence, Ray sees it as a conflict (the seeded website facts
            # are unconfirmed and must each be challenged once).
            note = item["conflict_note"].lower()
            if (
                item["record_field"]
                and "record" in note
                and re.search(
                    r"vari|differ|conflict|disagree|inconsisten|not (?:match|map)"
                    r"|rather than|however",
                    note,
                )
            ):
                item["record_relation"] = "conflicts"
        for item in items:
            for field in ("title", "text", "conflict_note"):
                if leaks(item[field]) or re.search(
                    r"\[(CODE|PHONE|EMAIL|GUEST|BOOKING)\]", item[field]
                ):
                    item[field] = scrub(item[field]).replace("[GUEST]", "the guest")
        _write_review(out, slug, record, items)
        (out / f"intents-{slug}.json").write_text(json.dumps(intents, indent=1), "utf-8")
        (out / f"candidates-{slug}.json").write_text(json.dumps(items, indent=1), "utf-8")


def _write_review(
    out: Path, slug: str, record: dict[str, Any], items: list[dict[str, Any]]
) -> None:
    order = {"conflicts": 0, "adds": 1, "confirms": 2, "none": 3}
    items.sort(
        key=lambda i: (order[i["record_relation"]], i["audience"], -i["confidence"], i["topic"])
    )
    columns = [
        "decision", "ref", "home", "audience", "kind", "topic", "title", "text", "confidence",
        "statements", "first_seen", "last_seen", "record_field", "record_value", "record_relation",
        "conflict_note", "evidence_threads",
    ]  # fmt: skip
    with (out / f"review-{slug}.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for n, item in enumerate(items, 1):
            field = item["record_field"]
            writer.writerow(
                {
                    **{k: item[k] for k in columns if k in item},
                    "decision": "",
                    "ref": f"{slug}-{n:03d}",
                    "home": slug,
                    "record_value": json.dumps(record.get(field), ensure_ascii=False)
                    if field
                    else "",
                    "evidence_threads": " ".join(item["evidence_threads"][:12]),
                }
            )
    lines = [f"# {record['name']}: {len(items)} proposed knowledge items", ""]
    lines.append("Mark each row in the CSV: approve, edit (change the text first), or reject.")
    lines.append("Conflicts with the current record are listed first.")
    for n, item in enumerate(items, 1):
        lines.append(
            f"\n**{slug}-{n:03d} · {item['title']}** ({item['audience']}, {item['kind']}, "
            f"confidence {item['confidence']:.2f}, {item['statements']} statements, "
            f"{item['first_seen']}–{item['last_seen']})"
        )
        lines.append(f"> {item['text']}")
        field = item["record_field"]
        # Long list fields (amenities, features) are only worth showing when they conflict.
        if field and (item["record_relation"] != "adds" or not isinstance(record.get(field), list)):
            value = json.dumps(record.get(field), ensure_ascii=False)
            lines.append(f"- Record `{field}` = {value} → **{item['record_relation']}**")
        if item["conflict_note"]:
            lines.append(f"- Note: {item['conflict_note']}")
    (out / f"review-{slug}.md").write_text("\n".join(lines) + "\n", "utf-8")
    print(f"  wrote review-{slug}.csv / .md with {len(items)} items")


# ---------------------------------------------------------------------------------- apply


def apply(out: Path, reviewed: Path, api: str, token_file: Path) -> None:
    token = token_file.read_text(encoding="utf-8").strip()
    rows = list(csv.DictReader(reviewed.open(encoding="utf-8-sig")))
    written = collections.Counter()
    for row in rows:
        decision = (row.get("decision") or "").strip().lower()
        if decision in ("", "reject", "skip"):
            written["skipped"] += 1
            continue
        approved = decision in ("approve", "edit", "yes", "ok")
        item = {
            "property_slug": row["home"],
            "audience": row["audience"],
            "kind": row["kind"],
            "topic": row["topic"][:60],
            "title": row["title"][:160],
            "text": row["text"][:2000],
            "status": "active" if approved else "proposed",
            "confidence": float(row["confidence"] or 0) or None,
            "evidence_ids": [f"airbnb-thread:{t}" for t in row["evidence_threads"].split()][:50],
            "source_note": f"airbnb export 2026-09 ({row['statements']} statements, "
            f"{row['first_seen']}–{row['last_seen']}); review ref {row['ref']}",
            "relation": {"confirms": "confirms", "conflicts": "conflicts"}.get(
                row["record_relation"], "new"
            ),
        }
        body = json.dumps(
            {
                "item": item,
                "created_by": "import: airbnb export, reviewed by Ray",
                "confirm": approved,
            }
        ).encode()
        request = urllib.request.Request(
            api.rstrip("/") + "/internal/v1/knowledge",
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
        written["active" if approved else "proposed"] += 1
    print(dict(written))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--export", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("extract")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--homes", default="buttercup-beauty")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p = sub.add_parser("apply")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--reviewed", type=Path, required=True)
    p.add_argument("--api", required=True)
    p.add_argument(
        "--token-file", type=Path, default=ROOT / ".secrets/utopia-business-lucy-token.txt"
    )
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.export, args.out)
    elif args.command == "extract":
        extract(args.out, [h.strip() for h in args.homes.split(",") if h.strip()], args.model)
    else:
        apply(args.out, args.reviewed, args.api, args.token_file)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
