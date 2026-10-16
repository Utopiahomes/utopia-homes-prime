"""Explicit Homes/Stoin feedback to Tiamat, independent of business records and chat history."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from uuid import UUID, uuid4

_ENV = ["UTOPIA_TIAMAT_FEEDBACK_ORIGIN", "UTOPIA_TIAMAT_FEEDBACK_TOKEN"]
_TEXT = {
    "topic": (1, 120),
    "text": (1, 2000),
    "expected_behavior": (0, 1000),
    "observed_version": (0, 120),
    "approved_summary": (0, 2000),
}
_ENUMS = {
    "target": ("homes_dragon", "stoin"),
    "category": ("other", "problem", "request", "praise"),
    "evidence_kind": ("operator_observation", "customer_report", "dragon_inference"),
    "sharing": ("private", "summary", "quotation"),
    "public_topic": ("other", "usability", "quality", "capability", "reliability"),
}
_PROPERTIES: dict[str, Any] = {
    key: {"type": "string", "minLength": lo, "maxLength": hi} for key, (lo, hi) in _TEXT.items()
}
_PROPERTIES.update(
    {key: {"type": "string", "enum": list(values)} for key, values in _ENUMS.items()}
)
_PROPERTIES.update(
    {
        "report_id": {
            "type": "string",
            "format": "uuid",
            "description": (
                "Omit for new feedback; reuse the returned ID and identical fields on retry."
            ),
        },
        "experience_id": {
            "type": "string",
            "format": "uuid",
            "description": "Optional shared UUID linking separate Homes and Stoin reports.",
        },
        "synthetic": {"type": "boolean", "description": "True for examples, tests or practice."},
    }
)
SCHEMA = {
    "name": "utopia_record_feedback",
    "description": (
        "Record explicitly requested feedback about Homes Dragon or Stoin in Tiamat. "
        "Do not send chat history or infer permission. Sharing defaults to private. "
        "Summary sharing needs separately approved summary text; quotation needs permission "
        "to share the submitted text. Permission also covers category, evidence kind and the "
        "public grouping label. Private topic and expected behavior never enter product digests. "
        "Mark tests synthetic. A receipt means recorded in Tiamat, not delivered to a product "
        "owner or fixed. On an unknown outcome retry only with the returned ID and same fields."
    ),
    "parameters": {
        "type": "object",
        "properties": _PROPERTIES,
        "required": ["target", "topic", "text"],
        "additionalProperties": False,
    },
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _payload(args: dict[str, Any]) -> dict[str, Any]:
    if set(args) - set(_PROPERTIES):
        raise ValueError("unexpected_fields")
    body: dict[str, Any] = {}
    for key, (lo, hi) in _TEXT.items():
        value = args.get(key, "")
        if not isinstance(value, str) or not lo <= len(value.strip()) <= hi:
            raise ValueError("invalid_feedback")
        body[key] = value.strip()
    for key, values in _ENUMS.items():
        value = args.get(key, values[0])
        if value not in values:
            raise ValueError("invalid_feedback")
        body[key] = value
    if "target" not in args:
        raise ValueError("target_required")
    body["target"] = "dragon" if body["target"] == "homes_dragon" else "product"
    synthetic = args.get("synthetic", False)
    if not isinstance(synthetic, bool):
        raise ValueError("invalid_synthetic_marker")
    body["synthetic"] = synthetic
    for key in ("report_id", "experience_id"):
        if key in args:
            if not isinstance(args[key], str):
                raise ValueError("invalid_report_id")
            body[key] = str(UUID(args[key]))
    body.setdefault("report_id", str(uuid4()))
    if body["sharing"] == "summary" and not body["approved_summary"]:
        raise ValueError("approved_summary_required")
    if body["sharing"] != "summary":
        body["approved_summary"] = ""
    return body


def _record(args: dict[str, Any], **_: Any) -> str:
    origin = os.environ.get(_ENV[0], "").strip()
    token = os.environ.get(_ENV[1], "")
    if not origin or not token:
        return json.dumps({"outcome": "not_recorded", "error": "feedback_not_configured"})
    try:
        parsed = urllib.parse.urlsplit(origin)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and local))
            or len(token) < 32
            or not token.isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            raise ValueError("invalid_configuration")
        body = _payload(args)
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 16_384:
            raise ValueError("feedback_too_large")
    except (ValueError, TypeError):
        return json.dumps({"outcome": "not_recorded", "error": "invalid_feedback_or_configuration"})
    report_id = body["report_id"]
    request = urllib.request.Request(
        origin.rstrip("/") + "/api/field/intake/homes",
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=10) as response:
            if response.status not in {200, 201}:
                raise ValueError("unexpected_status")
            raw = response.read(16_385)
            if len(raw) > 16_384:
                raise ValueError("invalid_receipt")
            receipt = json.loads(raw)
            if (
                not isinstance(receipt, dict)
                or receipt.get("report_id") != report_id
                or receipt.get("accepted") is not True
                or not isinstance(receipt.get("duplicate"), bool)
            ):
                raise ValueError("invalid_receipt")
            return json.dumps(
                {
                    "outcome": "recorded",
                    "report_id": report_id,
                    "duplicate": receipt["duplicate"],
                    "product_owner_delivery": "not_sent",
                }
            )
    except urllib.error.HTTPError as error:
        if error.code in {401, 403, 409, 413, 422}:
            return json.dumps(
                {
                    "outcome": "rejected",
                    "report_id": report_id,
                    "error": "feedback_rejected",
                    "http_status": error.code,
                }
            )
    except (OSError, ValueError):
        pass
    # A transport failure or server error does not prove the report was not saved.
    return json.dumps(
        {
            "outcome": "unknown",
            "report_id": report_id,
            "error": "feedback_outcome_unknown",
            "retry": "same_id_and_identical_fields",
        }
    )


def register(ctx: Any) -> None:
    ctx.register_tool(
        name=SCHEMA["name"],
        toolset="utopia_business",
        schema=SCHEMA,
        handler=_record,
        requires_env=_ENV,
        description=SCHEMA["description"],
        emoji="📣",
    )
