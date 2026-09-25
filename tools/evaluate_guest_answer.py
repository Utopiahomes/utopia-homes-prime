#!/usr/bin/env python
"""Runs the Homes acceptance set (knowledge/evaluation/homes-guest-answer-acceptance.v1.json)
against Homes Prime on its direct inference route, in process, with a real provider.

Each conversation is played turn by turn the way the website does it: the browser's bounded
history (earlier questions and Homes' actual answers) is sent with every request, and Homes keeps
nothing. Every turn is scored against its expectations. Latency and the provider-reported cost of
each call are recorded. The report goes to evaluation-results/ (git-ignored).

This makes paid provider calls. It needs a Homes OpenRouter key, read from the environment
(GUEST_ANSWER_PROVIDER_HOMES_PRIME_OPENROUTER_API_KEY) or from a key file
(`OPENROUTER_API_KEY=...`, default "API Keys.txt"). It never prints the key.

    python tools/evaluate_guest_answer.py --model google/gemini-3.1-flash-lite \
        --providers google-vertex --prompt-price 0.30 --completion-price 1.80
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from utopia_homes_prime.business_api.api import create_app
from utopia_homes_prime.config import Config

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "knowledge" / "evaluation" / "homes-guest-answer-acceptance.v1.json"
# In-process mode serves the latest approved knowledge release, as production does.
REGISTER = ROOT / "knowledge" / "releases.json"
ISSUER = "stoin:application:homes-evaluation"
SUBJECT = "stoin:service:homes-evaluation"
AUDIENCE = "stoin:business:utopia-homes-prime"
KID = "homes-evaluation-local"
# Remote mode signs exactly as the website adapter does.
WEB_ISSUER = "stoin:application:utopia-homes-web"
WEB_SUBJECT = "stoin:service:utopia-homes-web-guest-adapter"


def normalize(text: str) -> str:
    return text.casefold().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def score(turn: dict[str, Any], status: int, body: dict[str, Any]) -> list[str]:
    """Returns the list of failed expectations (empty means the turn passed)."""
    if status != 200:
        return [f"http_{status}:{body.get('error', {}).get('code')}"]
    failures = []
    answer = normalize(body["answer"])
    if body["outcome"] not in turn["allowed_outcomes"]:
        failures.append(f"outcome {body['outcome']} not in {turn['allowed_outcomes']}")
    for group in turn["required_term_groups"]:
        if not any(normalize(term) in answer for term in group):
            failures.append(f"missing one of {group[:6]}{'…' if len(group) > 6 else ''}")
    for term in turn["forbidden_terms"]:
        if normalize(term) in answer:
            failures.append(f"forbidden term {term!r}")
    sources = {s["source_id"].removeprefix("public-source:") for s in body["sources"]}
    for source in turn["required_source_ids"]:
        if source not in sources:
            failures.append(f"missing source {source}")
    for source in turn["forbidden_source_ids"]:
        if source in sources:
            failures.append(f"forbidden source {source}")
    if len(body["answer"]) > turn["max_characters"]:
        failures.append(f"answer longer than {turn['max_characters']}")
    return failures


class Recorder(httpx.AsyncBaseTransport):
    """Real network. Records only status, model, provider, token counts, and cost per call."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.calls: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        entry: dict[str, Any] = {"status": response.status_code}
        try:
            document = json.loads(body)
            usage = document.get("usage") or {}
            entry.update(
                model=document.get("model"),
                provider=document.get("provider"),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                cost=usage.get("cost"),
                # The model's raw structured output (public content only), so a rejected draft can
                # be diagnosed. Reports stay local and git-ignored.
                output=((document.get("choices") or [{}])[0].get("message") or {}).get("content"),
            )
        except ValueError:
            pass
        self.calls.append(entry)
        # aread() decoded the body, so drop the encoding headers to avoid decoding it twice.
        headers = [
            (k, v)
            for k, v in response.headers.items()
            if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        ]
        return httpx.Response(response.status_code, headers=headers, content=body, request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def read_key(path: Path) -> str:
    env_key = os.environ.get("GUEST_ANSWER_PROVIDER_HOMES_PRIME_OPENROUTER_API_KEY")
    if env_key:
        return env_key.strip()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("OPENROUTER_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"no OPENROUTER_API_KEY= line in {path}")


def build_app(args: argparse.Namespace, recorder: Recorder) -> tuple[Any, Ed25519PrivateKey]:
    signing = Ed25519PrivateKey.generate()
    public_pem = (
        signing.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    p, h = "GUEST_ANSWER_PROVIDER_", "GUEST_ANSWER_PROVIDER_HOMES_PRIME_"
    env = {
        f"{p}ENVIRONMENT": "preview",
        f"{p}JWT_PUBLIC_KEYS_JSON": json.dumps(
            [
                {
                    "kid": KID,
                    "public_key_pem": public_pem,
                    "environment": "preview",
                    "issuer": ISSUER,
                    "subject": SUBJECT,
                    "audience": AUDIENCE,
                    "capabilities": ["guest.answer"],
                }
            ]
        ),
        f"{p}RATE_LIMIT_PER_MINUTE": "600",
        f"{p}BUSINESS_RELEASE_ID": "homes-business:release:evaluation",
        f"{p}KNOWLEDGE_RELEASE_ID": "latest-approved",
        f"{p}ANSWER_ENGINE": "homes-prime",
        f"{h}INFERENCE_BACKEND": "direct-openrouter",
        f"{h}OPENROUTER_API_KEY": read_key(args.key_file),
        f"{h}OPENROUTER_MODEL": args.model,
        f"{h}OPENROUTER_ALLOWED_PROVIDERS": args.providers,
        f"{h}OPENROUTER_MAX_PROMPT_USD_PER_MILLION": str(args.prompt_price),
        f"{h}OPENROUTER_MAX_COMPLETION_USD_PER_MILLION": str(args.completion_price),
        f"{h}GENERATE_MAX_COST_MICROUSD": str(args.max_call_microusd),
        f"{h}REVIEW_MAX_COST_MICROUSD": str(args.max_call_microusd),
        f"{h}KNOWLEDGE_REGISTER": str(REGISTER),
    }
    return create_app(config=Config.from_environment(env), execution_transport=recorder), signing


def headers(
    signing: Ed25519PrivateKey, kid: str = KID, issuer: str = ISSUER, subject: str = SUBJECT
) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": issuer,
            "sub": subject,
            "aud": AUDIENCE,
            "scope": "guest.answer",
            "iat": now,
            "nbf": now,
            "exp": now + 120,
            "jti": str(uuid.uuid4()),
        },
        signing,
        algorithm="EdDSA",
        headers={"kid": kid},
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": str(uuid.uuid4()),
        "Idempotency-Key": str(uuid.uuid4()),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def bounded(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """The website's rule: keep the most recent turns within RC2's 12-entry, 10,000-character bound,
    starting with a user turn."""
    kept = list(history[-12:])
    while kept and (kept[0]["role"] != "user" or sum(len(t["content"]) for t in kept) > 10_000):
        kept = kept[1:]
    return kept


def run(args: argparse.Namespace) -> dict[str, Any]:
    document = json.loads(CASES.read_text(encoding="utf-8"))
    conversations = [
        c
        for c in document["conversations"]
        if not args.only or any(re.search(p, c["id"]) for p in args.only)
    ]
    results = []
    if args.url:
        # A deployed Homes Prime, called over HTTPS exactly as the website calls it.
        remote_key = serialization.load_pem_private_key(
            args.signing_key.read_bytes(), password=None
        )
        assert isinstance(remote_key, Ed25519PrivateKey)
        signing, kid, app, recorder = remote_key, args.kid, None, None
        http = httpx.Client(timeout=30, follow_redirects=False)

        def post(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            reply = http.post(
                args.url, json=body, headers=headers(signing, kid, WEB_ISSUER, WEB_SUBJECT)
            )
            return reply.status_code, reply.json()

        context: Any = http
    else:
        recorder = Recorder()
        app, signing = build_app(args, recorder)
        context = TestClient(app)

        def post(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            reply = context.post("/business/v1/guest/answer", json=body, headers=headers(signing))
            return reply.status_code, reply.json()

    with context:
        for conversation in conversations:
            # One browser session per conversation; history entries carry their turn IDs.
            session_id = str(uuid.uuid4())
            history = [{"turn_id": str(uuid.uuid4()), **t} for t in conversation["initial_history"]]
            for index, turn in enumerate(conversation["turns"]):
                turn_id = str(uuid.uuid4())
                body: dict[str, Any] = {
                    "contract_version": "1.0",
                    "session_id": session_id,
                    "message": {"turn_id": turn_id, "content": turn["message"]},
                    "locale": "en-US",
                }
                if history:
                    body["history"] = bounded(history)
                if turn["page_context"] is not None:
                    body["page_context"] = turn["page_context"]
                before = len(recorder.calls) if recorder else 0
                started = time.perf_counter()
                status, payload = post(body)
                latency = time.perf_counter() - started
                calls = recorder.calls[before:] if recorder else []
                failures = score(turn, status, payload)
                telemetry = app.state.homes_prime.last_telemetry if app else None
                results.append(
                    {
                        "conversation": conversation["id"],
                        "category": conversation["category"],
                        "turn": index,
                        "message": turn["message"],
                        "status": status,
                        "outcome": payload.get("outcome"),
                        "answer": payload.get("answer"),
                        "sources": [s["source_id"] for s in payload.get("sources", [])],
                        "error": payload.get("error", {}).get("code"),
                        "stage": None
                        if telemetry is None
                        else f"{telemetry.stage}:{telemetry.category}",
                        "latency_s": round(latency, 3),
                        "cost_usd": round(sum(c.get("cost") or 0 for c in calls), 6),
                        "calls": calls,
                        "passed": not failures,
                        "failures": failures,
                    }
                )
                mark = "PASS" if not failures else "FAIL"
                print(
                    f"{mark} {conversation['id']}[{index}] {latency:.1f}s {'; '.join(failures)}",
                    flush=True,
                )
                if status != 200:
                    break  # the conversation cannot continue without an answer
                history += [
                    {"turn_id": turn_id, "role": "user", "content": turn["message"]},
                    {
                        "turn_id": payload["assistant_turn_id"],
                        "role": "assistant",
                        "content": payload["answer"],
                    },
                ]

    latencies = [r["latency_s"] for r in results if r["status"] == 200]
    categories: dict[str, list[bool]] = {}
    for r in results:
        categories.setdefault(r["category"], []).append(r["passed"])
    summary = {
        "model": args.model,
        "providers": args.providers,
        "target": args.url or "in-process",
        "knowledge": document["knowledge"],
        "cases_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "turns": len(results),
        "passed": sum(r["passed"] for r in results),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(categories.items())},
        "latency_p50_s": round(statistics.median(latencies), 2) if latencies else None,
        "latency_p95_s": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 2)
        if latencies
        else None,
        "total_cost_usd": round(sum(r["cost_usd"] for r in results), 6),
        "provider_calls": sum(len(r["calls"]) for r in results),
    }
    return {"summary": summary, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", help="a deployed Homes Prime guest.answer URL (remote mode)")
    parser.add_argument("--signing-key", type=Path, help="remote mode: the caller's Ed25519 PEM")
    parser.add_argument("--kid", help="remote mode: the caller key's registered kid")
    parser.add_argument("--model", help="in-process mode: the OpenRouter model")
    parser.add_argument(
        "--providers", default="", help="comma-separated OpenRouter provider allowlist"
    )
    parser.add_argument("--prompt-price", type=float, help="USD per million input tokens ceiling")
    parser.add_argument(
        "--completion-price",
        type=float,
        help="USD per million output tokens ceiling",
    )
    parser.add_argument("--max-call-microusd", type=int, default=20_000)
    parser.add_argument("--key-file", type=Path, default=ROOT / "API Keys.txt")
    parser.add_argument("--only", nargs="*", help="regular expressions selecting conversation IDs")
    parser.add_argument("--out", type=Path, default=ROOT / "evaluation-results")
    args = parser.parse_args()
    if args.url and not (args.signing_key and args.kid):
        parser.error("--url needs --signing-key and --kid")
    if not args.url and not (args.model and args.prompt_price and args.completion_price):
        parser.error("in-process mode needs --model, --prompt-price and --completion-price")

    report = run(args)
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    label = args.model.replace("/", "_") if args.model else "deployed"
    target = args.out / f"{label}-{stamp}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report["summary"], indent=1))
    print(f"report: {target}")


if __name__ == "__main__":
    sys.exit(main())
