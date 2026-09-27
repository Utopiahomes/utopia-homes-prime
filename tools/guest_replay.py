"""Replay real guest questions through guest Lucy and compare her drafts with the hosts' replies.

For each guest message in the private Airbnb import that a host answered, this creates a TEST
reservation and turn on a Homes Prime (use a local one, never production), lets guest Lucy draft
or hand over, then asks a model to compare her result with what the host actually sent. The
report (CSV and a short Markdown summary) goes to the private import folder; nothing leaves this
machine except the scrubbed text sent to the model.

    python tools/guest_replay.py --threads PRIVATE/threads.jsonl --api http://127.0.0.1:18080 \
        --token-file LOCAL_LUCY_TOKEN --home buttercup-beauty --since 2025-09-27 --out PRIVATE

Caveat: guest Lucy's knowledge was extracted from these same conversations, so this measures
voice, judgment, and hand-over behaviour more than knowledge coverage.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from airbnb_import import DEFAULT_MODEL, _model_call  # noqa: E402

JUDGE_SYSTEM = """You compare a draft reply to a vacation-rental guest with the reply the host
actually sent. The draft was written by an assistant that may only use approved knowledge and
must hand money, exceptions, damage, complaints, and unknowns to the host (a "hand-over", often
with a short holding reply). Judge substance, not wording.

outcome:
- same: the draft says what the host said, or all that matters of it.
- draft_better: the draft is correct and more helpful than the host's reply.
- missing_info: correct but leaves out something the guest needed that the host gave.
- wrong: the draft states something that contradicts the host or is likely false.
- right_hand_over: the draft handed over, and the host's reply needed a host decision or
  information the assistant could not have (money, an exception, a repair, a personal call).
- needless_hand_over: the draft handed over, but the host's reply was routine information.
- not_comparable: the host's reply does not answer the guest (logistics between hosts, etc.).
- host_had_outside_info: the host's reply relies on something outside the conversation (an
  update from cleaners or a vendor, something the host already did or ordered, a decision made
  elsewhere) and the draft reasonably says it will check or hands over. This is not "wrong".
The assistant's approved knowledge can be newer than the host's reply (for example a corrected
bed count or pet limit), so a fact that differs from the host is "wrong" only if it looks
invented or contradicts the conversation itself.
Also rate the draft's tone against the host's warm, brief style: good, too_long, too_stiff."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["same", "draft_better", "missing_info", "wrong",
                                               "right_hand_over", "needless_hand_over",
                                               "host_had_outside_info", "not_comparable"]},
        "tone": {"type": "string", "enum": ["good", "too_long", "too_stiff"]},
        "note": {"type": "string"},
    },
    "required": ["outcome", "tone", "note"],
    "additionalProperties": False,
}  # fmt: skip


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base, self.token = base.rstrip("/"), token

    def __call__(self, method: str, path: str, body: Any = None) -> Any:
        request = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )  # fmt: skip
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)


def cases(threads: Path, home: str, since: str) -> list[dict[str, Any]]:
    found = []
    for line in threads.open(encoding="utf-8"):
        thread = json.loads(line)
        if thread["home"] != home:
            continue
        messages = [m for m in thread["messages"] if m["role"] in ("guest", "host")]
        for i, (a, b) in enumerate(zip(messages, messages[1:], strict=False)):
            if a["role"] == "guest" and b["role"] == "host" and a["at"] >= since:
                found.append({
                    "thread": thread["thread"], "at": a["at"], "guest": a["text"],
                    "host": b["text"],
                    "history": [{"role": m["role"], "text": m["text"]} for m in messages[:i]][-20:],
                })  # fmt: skip
    return found


def run_case(api: Api, case: dict[str, Any], home: str) -> dict[str, Any]:
    # Real stay dates are not in the import; a stay starting a week after the message keeps the
    # dates plausible without inventing a season the guest never mentioned.
    start = date.fromisoformat(case["at"][:10]) + timedelta(days=7)
    reservation = api("POST", "/internal/v1/reservations", {
        "property_slug": home, "check_in": start.isoformat(),
        "check_out": (start + timedelta(days=3)).isoformat(),
        "guests": 10, "label": f"TEST replay {case['thread']}"})["reservation"]  # fmt: skip
    turn = api("POST", "/internal/v1/guest/turns", {
        "reservation_id": reservation["id"], "message": case["guest"],
        "history": case["history"]})["turn"]  # fmt: skip
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(5)
        t = api("GET", f"/internal/v1/guest/turns/{turn['id']}")["turn"]
        if t["state"] != "awaiting_draft":
            last = t["attempts"][-1] if t["attempts"] else None
            return case | {
                "turn": t["id"], "state": t["state"],
                "draft": last["text"] if last else "",
                "gate": last["decision"] if last else "",
                "gate_notes": "; ".join(f["detail"] for f in last["findings"]) if last else "",
                "hand_over": (t["escalation"] or {}).get("category", ""),
                "hand_over_reason": (t["escalation"] or {}).get("reason", ""),
                "attempts": len(t["attempts"]),
            }  # fmt: skip
    empty = dict.fromkeys(("draft", "gate", "gate_notes", "hand_over", "hand_over_reason"), "")
    return case | empty | {"turn": turn["id"], "state": "timeout", "attempts": 0}


def judge(result: dict[str, Any], model: str) -> dict[str, Any]:
    if result["state"] == "timeout":
        return result | {"outcome": "timeout", "tone": "", "note": ""}
    draft = result["draft"] or "(no reply)"
    if result["state"] == "escalated":
        draft = f"[HAND-OVER: {result['hand_over']}: {result['hand_over_reason']}] {draft}"
    user = (f"Guest: {result['guest']}\n\nHost actually replied: {result['host']}\n\n"
            f"Assistant draft: {draft}")  # fmt: skip
    verdict = _model_call(model, JUDGE_SYSTEM, user, JUDGE_SCHEMA, "replay_judgment")
    return result | verdict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--threads", type=Path, required=True)
    parser.add_argument("--api", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--home", default="buttercup-beauty")
    parser.add_argument("--since", default="2025-09-27")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    if "onrender.com" in args.api:
        sys.exit("run the replay against a local Homes Prime, not production")
    api = Api(args.api, args.token_file.read_text(encoding="utf-8").strip())
    todo = cases(args.threads, args.home, args.since)
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} guest messages to replay")
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda c: judge(run_case(api, c, args.home), args.model), todo))

    stem = args.out / f"replay-{args.home}"
    fields = ["at", "thread", "guest", "host", "state", "draft", "gate", "gate_notes",
              "hand_over", "hand_over_reason", "attempts", "outcome", "tone", "note",
              "turn"]  # fmt: skip
    with stem.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    outcomes = collections.Counter(r["outcome"] for r in results)
    tones = collections.Counter(r["tone"] for r in results if r["tone"])
    states = collections.Counter(r["state"] for r in results)
    gates = collections.Counter(r["gate"] for r in results if r["gate"])
    lines = [f"# Guest Lucy replay: {args.home}, {len(results)} guest messages since {args.since}",
             "", f"- Outcomes: {dict(outcomes.most_common())}", f"- Tone: {dict(tones)}",
             f"- Turn states: {dict(states)}", f"- Gate on last draft: {dict(gates)}", "",
             "## Wrong or missing", ""]  # fmt: skip
    for r in results:
        if r["outcome"] in ("wrong", "missing_info", "needless_hand_over"):
            lines += [f"**{r['outcome']}** ({r['at'][:10]}): {r['note']}",
                      f"> Guest: {r['guest'][:300]}", f"> Host: {r['host'][:300]}",
                      f"> Lucy: {r['draft'][:300] or '[hand-over] ' + r['hand_over_reason']}",
                      ""]  # fmt: skip
    stem.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:6]))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
