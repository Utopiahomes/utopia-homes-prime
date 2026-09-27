"""Guest Lucy's tools: read one guest turn and answer it through Homes Prime's gate.

Guest Lucy runs from a webhook: Homes Prime wakes her for one guest message (a *turn*), and the
turn id becomes her session's id. Every tool reads the turn id from the session, never from the
model, so nothing she is told can point her at another reservation, home, or guest.

She has four tools and no others: read the turn, submit a reply (checked by the gate, then queued
for a person), escalate to a person, or propose work. None of them reaches a guest directly.

Environment:
    UTOPIA_BUSINESS_API_URL      base URL of Homes Prime, e.g. http://utopia-homes-prime:10000
    UTOPIA_BUSINESS_GUEST_TOKEN  guest Lucy's token (accepted only by /guest/v1 routes)
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

_ENV = ["UTOPIA_BUSINESS_API_URL", "UTOPIA_BUSINESS_GUEST_TOKEN"]
_TURN = re.compile(r"(gt-[a-z0-9]{12})$")

CONTEXT_SCHEMA = {
    "name": "guest_context",
    "description": (
        "Read this guest conversation: the reservation (home, dates, group size), the "
        "conversation so far, the guest's new message, the home's record, and every piece of "
        "guest-safe knowledge about the home. Call this first."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

REPLY_SCHEMA = {
    "name": "send_guest_reply",
    "description": (
        "Submit your reply to the guest. It is checked, then queued for Ray or Meghan to approve "
        "before it is sent. Cite the knowledge ids you used, and 'record' for facts from the "
        "home record. If it comes back blocked, fix what the notes say and submit again."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 2000},
            "cited_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
                "description": "Knowledge ids (ki-...) and/or 'record'.",
            },
        },
        "required": ["text", "cited_ids"],
        "additionalProperties": False,
    },
}

ESCALATE_SCHEMA = {
    "name": "escalate_to_host",
    "description": (
        "Hand this conversation to Ray or Meghan instead of replying: money, refunds, damage, "
        "safety, complaints, exceptions to policy (early check-in, extra guests or pets), "
        "anything you cannot answer from the knowledge, or anything that feels off."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": [
                    "money",
                    "exception",
                    "complaint",
                    "maintenance",
                    "safety",
                    "unknown",
                    "other",
                ],
            },  # fmt: skip
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "proposed_reply": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
                "description": (
                    "The reply you propose the hosts send. Either a full answer with [brackets] "
                    'where only the hosts know the detail ("Hi! Yes, you can check in at '
                    '[time]."), or a one-line holding reply ("Hi! Let me check with the team '
                    'and get back to you shortly.").'
                ),
            },
        },
        "required": ["category", "reason", "proposed_reply"],
        "additionalProperties": False,
    },
}

PROPOSE_WORK_SCHEMA = {
    "name": "propose_work",
    "description": (
        "Suggest work for the team when the guest reports something that needs doing (a repair, "
        "a missing item, a cleaning issue). A person decides whether to start it; never tell the "
        "guest it is scheduled or done."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["maintenance", "cleaning", "guest_request"]},
            "title": {"type": "string", "minLength": 1, "maxLength": 160},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "required": ["type", "title", "purpose"],
        "additionalProperties": False,
    },
}


def _turn_id() -> str | None:
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return None
    match = _TURN.search(get_session_env("HERMES_SESSION_CHAT_ID", ""))
    return match.group(1) if match else None


def _call(method: str, action: str, payload: dict[str, Any] | None = None) -> str:
    turn = _turn_id()
    if turn is None:
        return json.dumps({"ok": False, "error": "this session is not a guest turn"})
    base = os.environ["UTOPIA_BUSINESS_API_URL"].rstrip("/")
    request = urllib.request.Request(
        f"{base}/guest/v1/turns/{turn}/{action}",
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['UTOPIA_BUSINESS_GUEST_TOKEN']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read()[:1000].decode("utf-8", "replace")
        return json.dumps({"ok": False, "status": exc.code, "response": body})
    except (urllib.error.URLError, TimeoutError) as exc:
        return json.dumps({"ok": False, "error": f"Homes Prime unreachable: {exc}"})


def _context(args: dict[str, Any], **_: Any) -> str:
    return _call("GET", "context")


def _reply(args: dict[str, Any], **_: Any) -> str:
    cited = [str(c) for c in args.get("cited_ids") or []][:20]
    return _call("POST", "reply", {"text": str(args.get("text") or ""), "cited_ids": cited})


def _escalate(args: dict[str, Any], **_: Any) -> str:
    body = {"category": str(args.get("category") or "other"), "reason": str(args.get("reason"))}
    if args.get("proposed_reply"):
        body["holding_reply"] = str(args["proposed_reply"])[:1000]
    return _call("POST", "escalate", body)


def _propose(args: dict[str, Any], **_: Any) -> str:
    keys = ("type", "title", "purpose")
    return _call("POST", "propose_work", {k: str(args.get(k) or "") for k in keys})


TOOLS = (
    (CONTEXT_SCHEMA, _context, "📖"),
    (REPLY_SCHEMA, _reply, "✉️"),
    (ESCALATE_SCHEMA, _escalate, "🙋"),
    (PROPOSE_WORK_SCHEMA, _propose, "🧰"),
)


def register(ctx: Any) -> None:
    for schema, handler, emoji in TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset="utopia_guest",
            schema=schema,
            handler=handler,
            requires_env=_ENV,
            description=schema["description"],
            emoji=emoji,
        )
