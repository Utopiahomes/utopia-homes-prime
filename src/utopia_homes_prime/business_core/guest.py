"""Booked-guest conversations: reservations, guest turns, and the approval queue.

"Lucy writes, a gate checks, a person approves":

1. A guest message arrives for a reservation. Prime records a *turn* (the message, scrubbed, and
   the conversation so far) and wakes guest Lucy with the turn's id.
2. Guest Lucy reads the turn's context: the reservation's home, its guest-safe knowledge, and the
   conversation. Everything is scoped here, from the turn: she cannot ask about another home or
   see internal knowledge, whatever she is told.
3. Her only way to answer is `reply`: the gate checks the draft, and a blocked draft comes back
   with reasons so she can fix it (twice at most, then a person takes over). She may also
   `escalate` or `propose_work`; she can never open work or reach a guest herself.
4. A person approves, edits, or rejects each draft. Their decision and edits are kept next to the
   gate's verdict: that is the measure of how good Lucy is, and the bar for any future auto-send.

Guest names, phone numbers, emails, and codes are scrubbed before anything is stored.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from utopia_homes_prime.business_core.knowledge_items import KnowledgeItemStore
from utopia_homes_prime.business_core.scrub import scrub
from utopia_homes_prime.business_core.store import PropertyStore
from utopia_homes_prime.business_core.work import WorkDesk, WorkItem
from utopia_homes_prime.guest_reply.gate import Channel, Draft, check_reply, gather_context

TurnState = Literal["awaiting_draft", "queued", "escalated", "approved", "rejected"]
MAX_DRAFT_ATTEMPTS = 3
GUEST_AUDIENCES = ("public", "booked_guest")


class GuestError(ValueError):
    pass


class NotFound(LookupError):
    pass


class Reservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^rs-[a-z0-9]{12}$")
    property_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    channel: Channel = "airbnb"
    check_in: date
    check_out: date
    guests: int = Field(ge=1, le=100)
    label: str = Field(default="", max_length=120)
    """Who this is without personal details, e.g. "family reunion, 3 dogs" or "TEST"."""
    external_ref: str | None = Field(default=None, max_length=120)
    created_at: datetime


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["guest", "host"]
    text: str = Field(min_length=1, max_length=4000)


class DraftRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    cited_ids: list[str]
    decision: Literal["send", "review", "block"]
    findings: list[dict[str, str]]
    at: datetime


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "edit", "reject"]
    decided_by: str = Field(min_length=1, max_length=120)
    final_text: str | None = Field(default=None, max_length=4000)
    edit_categories: list[str] = Field(default_factory=list, max_length=10)
    """What the edit fixed: fact, tone, length, policy, missing_info, other."""
    reason: str = Field(default="", max_length=1000)
    at: datetime


class GuestTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^gt-[a-z0-9]{12}$")
    reservation_id: str
    property_slug: str
    guest_message: str
    history: list[Message] = Field(default_factory=list, max_length=100)
    state: TurnState = "awaiting_draft"
    attempts: list[DraftRecord] = Field(default_factory=list)
    """Every draft Lucy submitted, blocked ones included; the last one is what a person sees."""
    escalation: dict[str, str] | None = None
    proposed_work: list[str] = Field(default_factory=list)
    decision: Decision | None = None
    created_at: datetime
    updated_at: datetime


_M = TypeVar("_M", bound=BaseModel)


def _validate(model: type[_M], data: dict[str, Any]) -> _M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        error = exc.errors()[0]
        raise GuestError(f"{'.'.join(map(str, error['loc']))}: {error['msg']}") from None


class GuestStore(Protocol):
    def put_reservation(self, r: Reservation) -> None: ...
    def reservation(self, reservation_id: str) -> Reservation: ...
    def reservations(self) -> list[Reservation]: ...
    def put_turn(self, t: GuestTurn) -> None: ...
    def turn(self, turn_id: str) -> GuestTurn: ...
    def turns(self, states: tuple[str, ...] | None = None, limit: int = 50) -> list[GuestTurn]: ...


class MemoryGuestStore:
    def __init__(self) -> None:
        self._reservations: dict[str, Reservation] = {}
        self._turns: dict[str, GuestTurn] = {}
        self._lock = threading.Lock()

    def put_reservation(self, r: Reservation) -> None:
        with self._lock:
            self._reservations[r.id] = r

    def reservation(self, reservation_id: str) -> Reservation:
        try:
            return self._reservations[reservation_id]
        except KeyError:
            raise NotFound(f"no reservation {reservation_id!r}") from None

    def reservations(self) -> list[Reservation]:
        return sorted(self._reservations.values(), key=lambda r: r.check_in)

    def put_turn(self, t: GuestTurn) -> None:
        with self._lock:
            self._turns[t.id] = t

    def turn(self, turn_id: str) -> GuestTurn:
        try:
            return self._turns[turn_id]
        except KeyError:
            raise NotFound(f"no guest turn {turn_id!r}") from None

    def turns(self, states: tuple[str, ...] | None = None, limit: int = 50) -> list[GuestTurn]:
        found = [t for t in self._turns.values() if states is None or t.state in states]
        return sorted(found, key=lambda t: t.updated_at, reverse=True)[:limit]


GUEST_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reservations (
    id          text PRIMARY KEY,
    doc         jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS guest_turns (
    id          text PRIMARY KEY,
    doc         jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
"""


class PostgresGuestStore:
    def __init__(self, dsn: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        with self._connect() as conn:
            conn.execute(GUEST_SCHEMA_SQL)

    def _connect(self) -> Any:
        return self._psycopg.connect(self._dsn, autocommit=False, connect_timeout=10)

    def _put(self, table: str, key: str, doc: BaseModel) -> None:
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {table} (id, doc, updated_at) VALUES (%s, %s, now()) "  # noqa: S608
                "ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc, updated_at = now()",
                (key, json.dumps(doc.model_dump(mode="json"))),
            )

    def _get(self, table: str, key: str) -> Any:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT doc FROM {table} WHERE id = %s",  # noqa: S608
                (key,),
            ).fetchone()
        return None if row is None else row[0]

    def put_reservation(self, r: Reservation) -> None:
        self._put("reservations", r.id, r)

    def reservation(self, reservation_id: str) -> Reservation:
        doc = self._get("reservations", reservation_id)
        if doc is None:
            raise NotFound(f"no reservation {reservation_id!r}")
        return Reservation.model_validate(doc)

    def reservations(self) -> list[Reservation]:
        with self._connect() as conn:
            rows = conn.execute("SELECT doc FROM reservations").fetchall()
        return sorted((Reservation.model_validate(r[0]) for r in rows), key=lambda r: r.check_in)

    def put_turn(self, t: GuestTurn) -> None:
        self._put("guest_turns", t.id, t)

    def turn(self, turn_id: str) -> GuestTurn:
        doc = self._get("guest_turns", turn_id)
        if doc is None:
            raise NotFound(f"no guest turn {turn_id!r}")
        return GuestTurn.model_validate(doc)

    def turns(self, states: tuple[str, ...] | None = None, limit: int = 50) -> list[GuestTurn]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT doc FROM guest_turns ORDER BY updated_at DESC LIMIT 500"
            ).fetchall()
        found = [GuestTurn.model_validate(r[0]) for r in rows]
        return [t for t in found if states is None or t.state in states][:limit]


def _now() -> datetime:
    return datetime.now(UTC)


class GuestDesk:
    """Every guest-conversation operation. Guest Lucy's calls all start from a turn id, and the
    turn decides the reservation, the home, and what she may see."""

    def __init__(
        self,
        store: GuestStore,
        properties: PropertyStore,
        items: KnowledgeItemStore,
        work: WorkDesk,
        *,
        approved_hostnames: tuple[str, ...] = ("www.utopiahomes.com",),
        support_phone: str | None = None,
    ) -> None:
        self.store = store
        self._properties = properties
        self._items = items
        self._work = work
        self._hostnames = approved_hostnames
        self._support_phone = support_phone

    # Operator side ---------------------------------------------------------------------------

    def create_reservation(self, data: dict[str, Any]) -> Reservation:
        self._properties.get(str(data.get("property_slug")))  # the home must exist
        r = _validate(Reservation, {**data, "id": "rs-" + uuid.uuid4().hex[:12],
                                    "label": scrub(str(data.get("label") or "")),
                                    "created_at": _now()})  # fmt: skip
        if r.check_out <= r.check_in:
            raise GuestError("check_out must be after check_in")
        self.store.put_reservation(r)
        return r

    def receive(
        self, reservation_id: str, message: str, history: list[dict[str, str]] | None = None
    ) -> GuestTurn:
        r = self.store.reservation(reservation_id)
        now = _now()
        clean = [{"role": m.get("role"), "text": scrub(str(m.get("text") or ""))}
                 for m in (history or [])]  # fmt: skip
        turn = _validate(GuestTurn, {
            "id": "gt-" + uuid.uuid4().hex[:12], "reservation_id": r.id,
            "property_slug": r.property_slug, "guest_message": scrub(message),
            "history": [m for m in clean if m["text"]][-40:], "created_at": now, "updated_at": now,
        })  # fmt: skip
        self.store.put_turn(turn)
        return turn

    def decide(self, turn_id: str, data: dict[str, Any]) -> GuestTurn:
        turn = self.store.turn(turn_id)
        if turn.state not in ("queued", "escalated"):
            raise GuestError(f"turn is {turn.state}; only queued or escalated turns are decided")
        decision = _validate(Decision, {**data, "at": _now()})
        if decision.action == "edit" and not decision.final_text:
            raise GuestError("an edit needs final_text")
        if decision.action == "approve":
            if not turn.attempts or turn.attempts[-1].decision == "block":
                raise GuestError("there is no sendable draft to approve; edit or reject instead")
            decision.final_text = turn.attempts[-1].text
        state: TurnState = "rejected" if decision.action == "reject" else "approved"
        return self._save(turn, state=state, decision=decision)

    # Guest Lucy's side (everything scoped by the turn) ---------------------------------------

    def _open_turn(self, turn_id: str) -> GuestTurn:
        turn = self.store.turn(turn_id)
        if turn.state != "awaiting_draft":
            raise GuestError(f"this turn is already {turn.state}")
        return turn

    def context(self, turn_id: str) -> dict[str, Any]:
        turn = self.store.turn(turn_id)
        r = self.store.reservation(turn.reservation_id)
        home = self._properties.get(turn.property_slug)
        items = [
            i
            for slug in (turn.property_slug, None)
            for i in self._items.search(
                property_slug=slug, audiences=GUEST_AUDIENCES, statuses=("active",), limit=500
            )
            if slug is not None or i.property_slug is None
        ]
        return {
            "turn_id": turn.id,
            "state": turn.state,
            "reservation": {"home": home.name, "channel": r.channel,
                            "check_in": r.check_in.isoformat(),
                            "check_out": r.check_out.isoformat(), "guests": r.guests},
            "conversation": [m.model_dump() for m in turn.history],
            "guest_message": turn.guest_message,
            "home_record": home.model_dump(
                mode="json", exclude={"status", "short_description", "full_description"}
            ),
            "knowledge": [{"id": i.id, "title": i.title, "text": i.text} for i in items],
        }  # fmt: skip

    def _check(self, turn: GuestTurn, text: str, cited_ids: list[str]) -> DraftRecord:
        r = self.store.reservation(turn.reservation_id)
        draft = Draft(text.strip(), tuple(dict.fromkeys(cited_ids))[:20])
        ctx = gather_context(
            draft, property_slug=turn.property_slug, channel=r.channel,
            properties=self._properties, items=self._items, guest_message=turn.guest_message,
            reservation_facts={"guests": str(r.guests), "check_in": r.check_in.isoformat(),
                               "check_out": r.check_out.isoformat()},
            approved_hostnames=self._hostnames, support_phone=self._support_phone,
        )  # fmt: skip
        verdict = check_reply(draft, ctx)
        return DraftRecord(
            text=draft.text, cited_ids=list(draft.cited_ids), decision=verdict.decision,
            findings=[{"code": f.code, "severity": f.severity, "detail": f.detail}
                      for f in verdict.findings], at=_now(),
        )  # fmt: skip

    def reply(self, turn_id: str, text: str, cited_ids: list[str]) -> dict[str, Any]:
        turn = self._open_turn(turn_id)
        record = self._check(turn, text, cited_ids)
        attempts = [*turn.attempts, record]
        if record.decision != "block":
            self._save(turn, state="queued", attempts=attempts)
            return {"status": "queued_for_approval", "gate": record.decision,
                    "notes": record.findings}  # fmt: skip
        if len(attempts) >= MAX_DRAFT_ATTEMPTS:
            self._save(turn, state="escalated", attempts=attempts,
                       escalation={"category": "gate_blocked",
                                   "reason": "every draft was blocked by the gate"})  # fmt: skip
            return {"status": "escalated", "gate": "block", "notes": record.findings}
        self._save(turn, attempts=attempts)
        return {"status": "blocked_try_again", "gate": "block", "notes": record.findings,
                "attempts_left": MAX_DRAFT_ATTEMPTS - len(attempts)}  # fmt: skip

    def escalate(
        self, turn_id: str, category: str, reason: str, holding_reply: str | None = None
    ) -> dict[str, Any]:
        """Hand the turn to a person, optionally with a short holding reply ("Let me check with
        the team") that the gate checks and a person can approve in one tap."""
        turn = self._open_turn(turn_id)
        attempts = list(turn.attempts)
        result: dict[str, Any] = {"status": "escalated"}
        if holding_reply and holding_reply.strip():
            record = self._check(turn, holding_reply, [])
            attempts.append(record)
            result |= {"holding_reply_gate": record.decision, "notes": record.findings}
        self._save(turn, state="escalated", attempts=attempts,
                   escalation={"category": category[:40], "reason": reason[:500]})  # fmt: skip
        return result

    def propose_work(self, turn_id: str, data: dict[str, Any]) -> WorkItem:
        turn = self.store.turn(turn_id)
        item = self._work.submit(
            {
                "type": str(data.get("type") or "guest_request"),
                "title": str(data.get("title") or "")[:160],
                "purpose": scrub(str(data.get("purpose") or ""))[:2000],
                "property_slug": turn.property_slug,
                "status": "proposed",
                "input_refs": [f"reservation:{turn.reservation_id}", f"guest-turn:{turn.id}"],
            },
            requested_by=f"guest Lucy (turn {turn.id})",
        )
        self._save(turn, proposed_work=[*turn.proposed_work, item.id])
        return item

    def _save(self, turn: GuestTurn, **changes: Any) -> GuestTurn:
        updated = turn.model_copy(update={**changes, "updated_at": _now()})
        self.store.put_turn(updated)
        return updated
