"""Utopia Lucy's business tools: property records, knowledge, and work items in Utopia's
business core (Homes Prime).

The records are the single source of truth. The public guest assistant answers from them, so
a correction saved here reaches guests within about a minute. Every change is attributed to the
operator and carries a reason.

Environment:
    UTOPIA_BUSINESS_API_URL     base URL of Homes Prime, e.g. http://utopia-homes-prime:10000
    UTOPIA_BUSINESS_LUCY_TOKEN  bearer token for its /internal/v1 routes
    UTOPIA_LUCY_OPERATOR_NAME   who Lucy is working for in this deployment, e.g. "Ray"
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_ENV = ["UTOPIA_BUSINESS_API_URL", "UTOPIA_BUSINESS_LUCY_TOKEN"]
_TIMEOUT_SECONDS = 20

_SLUG = {
    "type": "string",
    "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$",
    "description": "The property's slug, e.g. 'the-shamrock' (see utopia_list_properties).",
}

LIST_SCHEMA = {
    "name": "utopia_list_properties",
    "description": "List every Utopia Homes property with its slug, status, city, and capacity.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

GET_SCHEMA = {
    "name": "utopia_get_property",
    "description": (
        "Read a property's record: what guests are told. Pass `fields` for just what you need "
        "(e.g. parking); omit it only when you need the whole record."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": _SLUG,
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "e.g. max_guests, beds, beds_by_room, parking, pet_policy, "
                "check_in_time, check_out_time, min_age, amenities, full_description, status.",
            },
        },
        "required": ["slug"],
        "additionalProperties": False,
    },
}

UPDATE_SCHEMA = {
    "name": "utopia_update_property",
    "description": (
        "Correct or update fields of a property record. Saved changes are what the website "
        "assistant tells guests, so change only what the operator actually stated. Editable "
        "fields: name, status (active/hidden/draft/archived), city, state, short_description, "
        "full_description, max_guests, bedrooms, beds, bathrooms, amenities (list of "
        "{name, amenities[]} groups; send the whole list), unique_features (list of strings; "
        "send the whole list), pet_policy, parking (must state a number of cars), accessibility."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": _SLUG,
            "changes": {
                "type": "object",
                "description": 'Field name to new value, e.g. {"parking": "Parking for 5 cars."}.',
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "Why, in the operator's words, e.g. 'Ray: garage fits 5 now.'",
            },
        },
        "required": ["slug", "changes", "reason"],
        "additionalProperties": False,
    },
}

HISTORY_SCHEMA = {
    "name": "utopia_property_history",
    "description": "Recent changes to a property record: field, old and new, who, why, when.",
    "parameters": {
        "type": "object",
        "properties": {
            "slug": _SLUG,
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["slug"],
        "additionalProperties": False,
    },
}


_AUDIENCE = {
    "type": "string",
    "enum": ["public", "booked_guest", "internal"],
    "description": (
        "public: anyone, incl. website visitors. booked_guest: only guests with a booking "
        "(e.g. trash day, checkout steps). internal: Ray, Meghan and Lucy only (e.g. breaker "
        "location, vendors, business decisions)."
    ),
}

SEARCH_KNOWLEDGE_SCHEMA = {
    "name": "utopia_search_knowledge",
    "description": (
        "Search Utopia's knowledge items (everything beyond page facts: how things work, local "
        "tips, internal notes, business decisions, and proposed items awaiting review)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {**_SLUG, "description": "Limit to one property (omit for all and general)."},
            "query": {"type": "string", "maxLength": 100, "description": "Words to match."},
            "status": {"type": "string", "enum": ["proposed", "active", "withdrawn"]},
            "audience": _AUDIENCE,
        },
        "additionalProperties": False,
    },
}

ADD_KNOWLEDGE_SCHEMA = {
    "name": "utopia_add_knowledge",
    "description": (
        "Save a piece of knowledge the operator told you, active and confirmed. Use for "
        "anything that is not a property page field. Never save door/lock codes, Wi-Fi "
        "passwords, phone numbers, emails, or guest personal details."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {**_SLUG, "description": "The property, or omit for Utopia in general."},
            "audience": _AUDIENCE,
            "kind": {"type": "string", "enum": ["fact", "policy", "faq", "place", "playbook"]},
            "topic": {"type": "string", "minLength": 1, "maxLength": 60},
            "title": {"type": "string", "minLength": 1, "maxLength": 160},
            "text": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        "required": ["audience", "kind", "topic", "title", "text"],
        "additionalProperties": False,
    },
}

UPDATE_KNOWLEDGE_SCHEMA = {
    "name": "utopia_update_knowledge",
    "description": (
        "Change, approve (status active), or withdraw (status withdrawn) a knowledge item, as "
        "the operator decided. The change is recorded as confirmed by the operator."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "pattern": "^ki-[a-z0-9]{12}$"},
            "changes": {
                "type": "object",
                "description": "Any of: status, audience, kind, topic, title, text.",
            },
        },
        "required": ["item_id", "changes"],
        "additionalProperties": False,
    },
}

CONFIRM_SCHEMA = {
    "name": "utopia_confirm_property",
    "description": (
        "Record that the operator checked property fields and they are correct as they are "
        "(no change). Use when Ray says a fact shown to him is right."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": _SLUG,
            "fields": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["slug", "fields"],
        "additionalProperties": False,
    },
}


_WORK_ID = {"type": "string", "pattern": "^wi-[a-z0-9]{12}$"}

OPEN_WORK_SCHEMA = {
    "name": "utopia_open_work",
    "description": (
        "Open a work item when Ray asks for something to be DONE (call a vendor, fix something, "
        "follow up, reconcile, send, buy, schedule), instead of doing it or promising it. A "
        "person (Ray or Meghan) does the work for now. Write the purpose so whoever does it "
        "needs no other context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": [
                    "maintenance",
                    "vendor_followup",
                    "guest_request",
                    "cleaning",
                    "finance",
                    "owner",
                    "marketing",
                    "other",
                ],
            },  # fmt: skip
            "title": {"type": "string", "minLength": 1, "maxLength": 160},
            "purpose": {"type": "string", "minLength": 1, "maxLength": 2000},
            "slug": {**_SLUG, "description": "The property, if the work is about one."},
            "due_at": {"type": "string", "description": "ISO date or datetime, if Ray gave one."},
        },
        "required": ["type", "title", "purpose"],
        "additionalProperties": False,
    },
}

LIST_WORK_SCHEMA = {
    "name": "utopia_list_work",
    "description": "List work items: open ones by default (not done or cancelled).",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["open", "proposed", "in_progress", "waiting", "done", "cancelled"],
            },
            "slug": _SLUG,
        },
        "additionalProperties": False,
    },
}

UPDATE_WORK_SCHEMA = {
    "name": "utopia_update_work",
    "description": (
        "Record progress on a work item as Ray reports it: add a note, change status "
        "(open, in_progress, waiting, done, cancelled), or record the result when done."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "work_id": _WORK_ID,
            "status": {
                "type": "string",
                "enum": ["open", "in_progress", "waiting", "done", "cancelled"],
            },
            "note": {"type": "string", "minLength": 1, "maxLength": 1000},
            "result": {"type": "string", "maxLength": 2000},
        },  # fmt: skip
        "required": ["work_id"],
        "additionalProperties": False,
    },
}


_TURN_ID = {"type": "string", "pattern": "^gt-[a-z0-9]{12}$"}

GUEST_QUEUE_SCHEMA = {
    "name": "utopia_guest_queue",
    "description": (
        "Show guest messages waiting on a person: drafts guest Lucy wrote (with the gate's "
        "verdict and notes) and conversations she handed over (with her reason)."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

CARD_ON_SCREEN_SCHEMA = {
    "name": "utopia_guest_card",
    "description": (
        "Show the guest card on Ray's screen right now (Homes Prime sends cards to Telegram "
        "directly, so you never see them in this chat): the guest's message, the proposed reply "
        "and its version, priority, and how many are waiting. Check it whenever Ray's message "
        "could be an answer to a card."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

ANSWER_CARD_SCHEMA = {
    "name": "utopia_answer_guest_card",
    "description": (
        "Record Ray's answer to a guest card or a 📚 learning card (send = save it). It "
        "applies to the card on screen (the latest one sent) unless Ray replied to a specific "
        "card: then pass that card's id and version from the [gt-... vN] or [lc-... vN] line. "
        "send: approve the wording shown. revise: the "
        "new wording (Ray's exact words, or your rewrite of his instruction); the card is sent "
        "back to him and only his 'send' finalizes it. reject. undo: reopen the last answered "
        "card."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["send", "revise", "reject", "undo"]},
            "text": {"type": "string", "maxLength": 2000},
            "turn_id": {"type": "string", "pattern": "^(gt|lc)-[a-z0-9]{12}$"},
            "version": {"type": "integer", "minimum": 1},
            "categories": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["fact", "tone", "length", "policy", "missing_info", "other"],
                },
            },  # fmt: skip
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

TRY_GUEST_SCHEMA = {
    "name": "utopia_try_guest_message",
    "description": (
        "TEST ONLY: send a pretend guest message for a home (a TEST reservation next week) so "
        "Ray can see what guest Lucy drafts. The draft appears in utopia_guest_queue within a "
        "minute."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": _SLUG,
            "message": {"type": "string", "minLength": 1, "maxLength": 1000},
            "guests": {"type": "integer", "minimum": 1, "maximum": 40},
        },
        "required": ["slug", "message"],
        "additionalProperties": False,
    },
}


def _operator() -> str:
    return f"{os.environ.get('UTOPIA_LUCY_OPERATOR_NAME', 'operator')} via Utopia Lucy"


def _call(method: str, path: str, payload: dict[str, Any] | None = None) -> str:
    base = os.environ["UTOPIA_BUSINESS_API_URL"].rstrip("/")
    request = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['UTOPIA_BUSINESS_LUCY_TOKEN']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # The business core's error bodies are safe to show: {"error": {"code", "message"}}.
        body = exc.read()[:2000].decode("utf-8", "replace")
        return json.dumps({"ok": False, "status": exc.code, "response": body})
    except (urllib.error.URLError, TimeoutError) as exc:
        return json.dumps({"ok": False, "error": f"business core unreachable: {exc}"})


def _list(args: dict[str, Any], **_: Any) -> str:
    return _call("GET", "/internal/v1/properties")


def _get(args: dict[str, Any], **_: Any) -> str:
    raw = _call("GET", f"/internal/v1/properties/{urllib.parse.quote(str(args['slug']))}")
    fields = [str(f) for f in args.get("fields") or []]
    if not fields:
        return raw
    try:
        data = json.loads(raw)
        record = data["property"]
    except (ValueError, KeyError, TypeError):
        return raw
    return json.dumps(
        {
            "property": {"slug": record.get("slug"), **{f: record.get(f) for f in fields}},
            "unconfirmed_fields": [f for f in data.get("unconfirmed_fields", []) if f in fields],
        }
    )


def _compact_items(raw: str) -> str:
    """Only what Lucy needs to talk about an item; evidence and timestamps stay in the store."""
    try:
        items = json.loads(raw)["items"]
    except (ValueError, KeyError, TypeError):
        return raw
    keep = ("id", "property_slug", "audience", "kind", "status", "title", "text", "confidence",
            "relation", "source_note")  # fmt: skip
    return json.dumps({"items": [{k: i.get(k) for k in keep if i.get(k) is not None}
                                 for i in items]})  # fmt: skip


def _update(args: dict[str, Any], **_: Any) -> str:
    return _call(
        "PATCH",
        f"/internal/v1/properties/{urllib.parse.quote(str(args['slug']))}",
        {
            "changes": args.get("changes") or {},
            "changed_by": _operator(),
            "reason": str(args.get("reason") or "")[:500],
        },
    )


def _history(args: dict[str, Any], **_: Any) -> str:
    limit = int(args.get("limit") or 10)
    slug = urllib.parse.quote(str(args["slug"]))
    return _call("GET", f"/internal/v1/properties/{slug}/history?limit={limit}")


def _search_knowledge(args: dict[str, Any], **_: Any) -> str:
    params = {
        "property": args.get("slug"),
        "q": args.get("query"),
        "status": args.get("status"),
        "audience": args.get("audience"),
        "limit": 40,
    }
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    return _compact_items(_call("GET", f"/internal/v1/knowledge?{query}"))


def _add_knowledge(args: dict[str, Any], **_: Any) -> str:
    item = {
        "property_slug": args.get("slug"),
        "audience": args.get("audience"),
        "kind": args.get("kind"),
        "topic": args.get("topic"),
        "title": args.get("title"),
        "text": args.get("text"),
        "status": "active",
        "source_note": "stated by the operator in Telegram",
    }
    return _call(
        "POST",
        "/internal/v1/knowledge",
        {"item": item, "created_by": _operator(), "confirm": True},
    )


def _update_knowledge(args: dict[str, Any], **_: Any) -> str:
    return _call(
        "PATCH",
        f"/internal/v1/knowledge/{urllib.parse.quote(str(args['item_id']))}",
        {"changes": args.get("changes") or {}, "changed_by": _operator(), "confirm": True},
    )


def _confirm(args: dict[str, Any], **_: Any) -> str:
    return _call(
        "POST",
        f"/internal/v1/properties/{urllib.parse.quote(str(args['slug']))}/confirm",
        {"fields": list(args.get("fields") or []), "confirmed_by": _operator()},
    )


def _compact_work(raw: str) -> str:
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    keep = ("id", "type", "title", "property_slug", "status", "assigned_executor", "due_at",
            "result", "updated_at")  # fmt: skip

    def one(w: dict[str, Any]) -> dict[str, Any]:
        brief = {k: w.get(k) for k in keep if w.get(k)}
        if w.get("notes"):
            brief["last_note"] = w["notes"][-1]["text"]
        return brief

    if isinstance(data.get("work"), list):
        return json.dumps({"work": [one(w) for w in data["work"]]})
    if isinstance(data.get("work"), dict):
        return json.dumps({"work": one(data["work"])})
    return raw


def _open_work(args: dict[str, Any], **_: Any) -> str:
    item = {
        "type": args.get("type"),
        "title": args.get("title"),
        "purpose": args.get("purpose"),
        "property_slug": args.get("slug"),
        "due_at": args.get("due_at"),
        "status": "open",
    }
    return _compact_work(
        _call(
            "POST",
            "/internal/v1/work",
            {"item": {k: v for k, v in item.items() if v}, "requested_by": _operator()},
        )
    )


def _list_work(args: dict[str, Any], **_: Any) -> str:
    params = {"status": args.get("status") or "open", "property": args.get("slug"), "limit": 30}
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    return _compact_work(_call("GET", f"/internal/v1/work?{query}"))


def _update_work(args: dict[str, Any], **_: Any) -> str:
    changes = {k: args[k] for k in ("status", "result") if args.get(k)}
    body: dict[str, Any] = {"changes": changes, "changed_by": _operator()}
    if args.get("note"):
        body["note"] = str(args["note"])[:1000]
    return _compact_work(
        _call("PATCH", f"/internal/v1/work/{urllib.parse.quote(str(args['work_id']))}", body)
    )


def _guest_queue(args: dict[str, Any], **_: Any) -> str:
    raw = _call("GET", "/internal/v1/guest/turns?limit=20")
    try:
        turns = json.loads(raw)["turns"]
    except (ValueError, KeyError, TypeError):
        return raw
    keep = ("id", "property_slug", "state", "guest_message", "draft", "gate", "gate_notes",
            "escalation", "proposed_work")  # fmt: skip
    return json.dumps({"turns": [{k: t.get(k) for k in keep if t.get(k)} for t in turns]})


def _card_on_screen(args: dict[str, Any], **_: Any) -> str:
    return _call("GET", "/internal/v1/guest/card")


def _answer_card(args: dict[str, Any], **_: Any) -> str:
    body: dict[str, Any] = {"action": args.get("action"), "by": _operator()}
    for key in ("text", "turn_id", "version", "categories"):
        if args.get(key):
            body[key] = args[key]
    return _call("POST", "/internal/v1/guest/card", body)


def _try_guest(args: dict[str, Any], **_: Any) -> str:
    from datetime import date, timedelta

    start = date.today() + timedelta(days=7)
    reservation = _call(
        "POST",
        "/internal/v1/reservations",
        {
            "property_slug": args.get("slug"),
            "check_in": start.isoformat(),
            "check_out": (start + timedelta(days=3)).isoformat(),
            "guests": int(args.get("guests") or 10),
            "label": "TEST from Telegram",
        },
    )
    try:
        reservation_id = json.loads(reservation)["reservation"]["id"]
    except (ValueError, KeyError, TypeError):
        return reservation
    return _call(
        "POST",
        "/internal/v1/guest/turns",
        {"reservation_id": reservation_id, "message": str(args.get("message") or "")},
    )


def register(ctx: Any) -> None:
    for schema, handler, emoji in (
        (LIST_SCHEMA, _list, "🏠"),
        (GET_SCHEMA, _get, "🔎"),
        (UPDATE_SCHEMA, _update, "✏️"),
        (HISTORY_SCHEMA, _history, "📜"),
        (SEARCH_KNOWLEDGE_SCHEMA, _search_knowledge, "📚"),
        (ADD_KNOWLEDGE_SCHEMA, _add_knowledge, "➕"),
        (UPDATE_KNOWLEDGE_SCHEMA, _update_knowledge, "🗂️"),
        (CONFIRM_SCHEMA, _confirm, "✅"),
        (OPEN_WORK_SCHEMA, _open_work, "🧰"),
        (LIST_WORK_SCHEMA, _list_work, "📋"),
        (UPDATE_WORK_SCHEMA, _update_work, "🔧"),
        (GUEST_QUEUE_SCHEMA, _guest_queue, "📥"),
        (CARD_ON_SCREEN_SCHEMA, _card_on_screen, "🃏"),
        (ANSWER_CARD_SCHEMA, _answer_card, "✅"),
        (TRY_GUEST_SCHEMA, _try_guest, "🧪"),
    ):
        ctx.register_tool(
            name=schema["name"],
            toolset="utopia_business",
            schema=schema,
            handler=handler,
            requires_env=_ENV,
            description=schema["description"],
            emoji=emoji,
        )
