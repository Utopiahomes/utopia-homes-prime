"""Utopia Lucy's business tools: the property records in Utopia's business core (Homes Prime).

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
        "Read one property's full record: descriptions, capacity, rooms, amenities, highlights, "
        "pet policy, parking, accessibility, and status. This is what guests are told."
    ),
    "parameters": {
        "type": "object",
        "properties": {"slug": _SLUG},
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
    return _call("GET", f"/internal/v1/properties/{urllib.parse.quote(str(args['slug']))}")


def _update(args: dict[str, Any], **_: Any) -> str:
    operator = os.environ.get("UTOPIA_LUCY_OPERATOR_NAME", "operator")
    return _call(
        "PATCH",
        f"/internal/v1/properties/{urllib.parse.quote(str(args['slug']))}",
        {
            "changes": args.get("changes") or {},
            "changed_by": f"{operator} via Utopia Lucy",
            "reason": str(args.get("reason") or "")[:500],
        },
    )


def _history(args: dict[str, Any], **_: Any) -> str:
    limit = int(args.get("limit") or 10)
    slug = urllib.parse.quote(str(args["slug"]))
    return _call("GET", f"/internal/v1/properties/{slug}/history?limit={limit}")


def register(ctx: Any) -> None:
    for schema, handler, emoji in (
        (LIST_SCHEMA, _list, "🏠"),
        (GET_SCHEMA, _get, "🔎"),
        (UPDATE_SCHEMA, _update, "✏️"),
        (HISTORY_SCHEMA, _history, "📜"),
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
