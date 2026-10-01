"""HTTP routes for booked-guest conversations.

- `/internal/v1/reservations`, `/internal/v1/guest/turns...`: operator side (Utopia Lucy's token):
  create test reservations, record a guest message, see the approval queue, decide on drafts.
- `/guest/v1/turns/{turn_id}/...`: guest Lucy's side (the guest token, and nothing else). Every
  call names a turn, and the turn alone decides which reservation and home she may see.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from utopia_homes_prime.business_core.guest import GuestDesk, GuestError, GuestTurn, NotFound
from utopia_homes_prime.business_core.learning import LearningCard, LearningNotFound
from utopia_homes_prime.business_core.store import PropertyNotFound
from utopia_homes_prime.business_core.work import InvalidWork

GUEST_PREFIX = "/guest/v1/"
_log = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends a plain-text message to the hosts' Telegram chat through Utopia Lucy's bot, so the
    hosts can answer it there (Lucy sees the message they reply to)."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    def __call__(self, text: str) -> None:
        threading.Thread(target=self._send, args=(text,), daemon=True).start()

    def _send(self, text: str) -> None:
        body = json.dumps(
            {"chat_id": self._chat_id, "text": text[:4000], "disable_web_page_preview": True}
        ).encode()
        request = urllib.request.Request(
            self._url, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read()
        except Exception:
            _log.warning("could not notify the hosts on Telegram")  # never log the bot URL


class GuestWaker:
    """Wakes guest Lucy for a new turn: a signed POST to her Hermes webhook (V2 signature: HMAC of
    "<timestamp>.<body>"). The turn id travels as X-Request-ID, which becomes her session's id,
    so her tools know the turn without the model ever saying it."""

    def __init__(
        self, url: str, secret: str, on_failure: Callable[[str], None] | None = None
    ) -> None:
        self._url = url
        self._secret = secret.encode()
        self._on_failure = on_failure

    def wake(self, turn: GuestTurn) -> None:
        threading.Thread(target=self._post, args=(turn.id,), daemon=True).start()

    def _post(self, turn_id: str) -> None:
        body = json.dumps({"turn_id": turn_id}).encode()
        ts = str(int(time.time()))
        signature = hmac.new(self._secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": turn_id,
                "X-Webhook-Timestamp": ts,
                "X-Webhook-Signature-V2": signature,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read()
        except Exception:  # the turn waits in awaiting_draft; /wake retries it
            _log.warning("could not wake guest Lucy for %s", turn_id, exc_info=True)
            if self._on_failure is not None:
                self._on_failure(turn_id)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


class ReservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_slug: str
    channel: str = "airbnb"
    check_in: str
    check_out: str
    guests: int
    label: str = ""
    external_ref: str | None = None


class GuestMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservation_id: str
    message: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=100)
    wake: bool = True


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    decided_by: str
    final_text: str | None = None
    edit_categories: list[str] = Field(default_factory=list)
    reason: str = ""


_URGENCY = r"^(urgent|today|normal)$"


class CardAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(pattern=r"^(send|revise|reject|undo)$")
    by: str = Field(min_length=1, max_length=120)
    text: str | None = Field(default=None, max_length=2000)
    turn_id: str | None = Field(default=None, pattern=r"^(gt|lc)-[a-z0-9]{12}$")
    """Only when the host replied to a specific card; otherwise the card on screen."""
    version: int | None = Field(default=None, ge=1)
    categories: list[str] = Field(default_factory=list, max_length=10)
    reason: str = Field(default="", max_length=1000)


class ReplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    cited_ids: list[str] = Field(default_factory=list, max_length=20)
    urgency: str | None = Field(default=None, pattern=_URGENCY)


class EscalateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=500)
    holding_reply: str | None = Field(default=None, max_length=1000)
    urgency: str | None = Field(default=None, pattern=_URGENCY)


class ProposeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "guest_request"
    title: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=2000)


def _turn_summary(t: GuestTurn) -> dict[str, Any]:
    last = t.attempts[-1] if t.attempts else None
    return {
        "id": t.id,
        "reservation_id": t.reservation_id,
        "property_slug": t.property_slug,
        "state": t.state,
        "priority": t.priority,
        "version": len(t.attempts),
        "guest_message": t.guest_message,
        "draft": last.text if last else None,
        "gate": last.decision if last else None,
        "gate_notes": [f["detail"] for f in last.findings] if last else [],
        "escalation": t.escalation,
        "proposed_work": t.proposed_work,
        "decision": t.decision.model_dump(mode="json") if t.decision else None,
        "updated_at": t.updated_at.isoformat(),
    }


async def _run(fn: Any) -> JSONResponse | Any:
    try:
        return await run_in_threadpool(fn)
    except (NotFound, PropertyNotFound, LearningNotFound) as exc:
        return _error(404, "not_found", str(exc))
    except (GuestError, InvalidWork) as exc:
        return _error(422, "invalid_request", str(exc))


def register_guest_routes(
    app: FastAPI, desk: GuestDesk, *, guest_token: str | None, waker: GuestWaker | None
) -> None:
    def guest_authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        return (
            guest_token is not None
            and header.startswith("Bearer ")
            and hmac.compare_digest(header[len("Bearer ") :].encode(), guest_token.encode())
        )

    @app.middleware("http")
    async def _guest_auth(request: Request, call_next: Any) -> Any:
        if request.url.path.startswith(GUEST_PREFIX) and not guest_authorized(request):
            return _error(401, "unauthorized", "a valid guest token is required")
        return await call_next(request)

    # Operator side -----------------------------------------------------------------------------

    @app.post("/internal/v1/reservations")
    async def create_reservation(body: ReservationCreate) -> JSONResponse:
        result = await _run(lambda: desk.create_reservation(body.model_dump()))
        if isinstance(result, JSONResponse):
            return result
        return JSONResponse({"reservation": result.model_dump(mode="json")}, status_code=201)

    @app.get("/internal/v1/reservations")
    async def list_reservations() -> JSONResponse:
        found = await run_in_threadpool(desk.store.reservations)
        return JSONResponse({"reservations": [r.model_dump(mode="json") for r in found]})

    @app.post("/internal/v1/guest/turns")
    async def receive_message(body: GuestMessage) -> JSONResponse:
        result = await _run(lambda: desk.receive(body.reservation_id, body.message, body.history))
        if isinstance(result, JSONResponse):
            return result
        if body.wake and waker is not None:
            waker.wake(result)
        return JSONResponse(
            {"turn": _turn_summary(result), "woke_guest_lucy": bool(body.wake and waker)},
            status_code=201,
        )

    @app.get("/internal/v1/guest/turns")
    async def queue(state: str | None = None, limit: int = 30) -> JSONResponse:
        states = tuple(state.split(",")) if state else ("queued", "escalated", "awaiting_draft")
        found = await run_in_threadpool(lambda: desk.store.turns(states, max(1, min(limit, 200))))
        return JSONResponse({"turns": [_turn_summary(t) for t in found]})

    @app.get("/internal/v1/guest/turns/{turn_id}")
    async def turn_detail(turn_id: str) -> JSONResponse:
        result = await _run(lambda: desk.store.turn(turn_id))
        if isinstance(result, JSONResponse):
            return result
        return JSONResponse({"turn": result.model_dump(mode="json")})

    @app.get("/internal/v1/guest/card")
    async def card_on_screen() -> JSONResponse:
        """The guest card the hosts see last in their chat, if it still needs an answer."""
        current = await run_in_threadpool(desk.on_screen)
        if isinstance(current, LearningCard):
            return JSONResponse({"card": {
                "kind": "learning", "id": current.id, "property_slug": current.property_slug,
                "version": len(current.versions), "proposed_knowledge": current.versions[-1],
                "title": current.title, "audience": current.audience,
                "learned_from": current.source_summary,
            }})  # fmt: skip
        return JSONResponse(
            {"card": _turn_summary(current) | {"kind": "guest"} if current else None}
        )

    @app.post("/internal/v1/guest/card")
    async def answer_card(body: CardAnswer) -> JSONResponse:
        result = await _run(
            lambda: desk.answer_card(
                body.action, by=body.by, text=body.text, turn_id=body.turn_id,
                version=body.version, categories=body.categories, reason=body.reason,
            )
        )  # fmt: skip
        return result if isinstance(result, JSONResponse) else JSONResponse(result)

    @app.post("/internal/v1/guest/turns/{turn_id}/reopen")
    async def reopen(turn_id: str) -> JSONResponse:
        result = await _run(lambda: desk.reopen(turn_id))
        if isinstance(result, JSONResponse):
            return result
        if desk.cards is not None:
            desk.cards.poke()
        return JSONResponse({"turn": _turn_summary(result)})

    @app.post("/internal/v1/guest/turns/{turn_id}/decision")
    async def decide(turn_id: str, body: DecisionBody) -> JSONResponse:
        result = await _run(lambda: desk.decide(turn_id, body.model_dump()))
        if isinstance(result, JSONResponse):
            return result
        return JSONResponse({"turn": _turn_summary(result)})

    @app.post("/internal/v1/guest/turns/{turn_id}/wake")
    async def wake_again(turn_id: str) -> JSONResponse:
        result = await _run(lambda: desk.store.turn(turn_id))
        if isinstance(result, JSONResponse):
            return result
        if waker is None:
            return _error(409, "no_guest_worker", "no guest worker is configured")
        if result.state != "awaiting_draft":
            return _error(409, "not_waiting", f"turn is {result.state}")
        waker.wake(result)
        return JSONResponse({"woke_guest_lucy": True})

    # Guest Lucy's side ---------------------------------------------------------------------------

    @app.get("/guest/v1/turns/{turn_id}/context")
    async def guest_context(turn_id: str) -> JSONResponse:
        result = await _run(lambda: desk.context(turn_id))
        return result if isinstance(result, JSONResponse) else JSONResponse(result)

    @app.post("/guest/v1/turns/{turn_id}/reply")
    async def guest_reply(turn_id: str, body: ReplyBody) -> JSONResponse:
        result = await _run(lambda: desk.reply(turn_id, body.text, body.cited_ids, body.urgency))
        return result if isinstance(result, JSONResponse) else JSONResponse(result)

    @app.post("/guest/v1/turns/{turn_id}/escalate")
    async def guest_escalate(turn_id: str, body: EscalateBody) -> JSONResponse:
        result = await _run(
            lambda: desk.escalate(
                turn_id, body.category, body.reason, body.holding_reply, body.urgency
            )
        )
        return result if isinstance(result, JSONResponse) else JSONResponse(result)

    @app.post("/guest/v1/turns/{turn_id}/propose_work")
    async def guest_propose(turn_id: str, body: ProposeBody) -> JSONResponse:
        result = await _run(lambda: desk.propose_work(turn_id, body.model_dump()))
        if isinstance(result, JSONResponse):
            return result
        return JSONResponse({"proposed_work": result.id, "status": "proposed"})
