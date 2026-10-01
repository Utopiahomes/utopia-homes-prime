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
import re
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from utopia_homes_prime.business_core.host_cards import (
    OPEN_STATES,
    CardDispatcher,
    priority_for,
)
from utopia_homes_prime.business_core.knowledge_items import KnowledgeItemStore
from utopia_homes_prime.business_core.learning import LearningCard, LearningDesk
from utopia_homes_prime.business_core.scrub import scrub
from utopia_homes_prime.business_core.store import PropertyStore
from utopia_homes_prime.business_core.work import WorkDesk, WorkItem
from utopia_homes_prime.guest_reply.gate import Channel, Draft, check_reply, gather_context

TurnState = Literal["awaiting_draft", "queued", "escalated", "approved", "rejected"]
_PLACEHOLDER = re.compile(r"\[[^\]]{1,80}\]")
MAX_DRAFT_ATTEMPTS = 3
ANSWER_PAUSE_SECONDS = 12.0
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
    author: Literal["lucy", "host"] = "lucy"
    """`host` for a version written from the host's changes; only its "send" finalizes it."""


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "edit", "reject"]
    decided_by: str = Field(min_length=1, max_length=120)
    final_text: str | None = Field(default=None, max_length=4000)
    edit_categories: list[str] = Field(default_factory=list, max_length=10)
    """What the edit fixed: fact, tone, length, policy, missing_info, other."""
    reason: str = Field(default="", max_length=1000)
    edited: bool = False
    """True when the sent wording was the host's version rather than Lucy's."""
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
    undone: list[Decision] = Field(default_factory=list)
    priority: Literal["urgent", "today", "normal"] = "normal"
    card_sent_at: datetime | None = None
    """When this turn's card last went to the hosts."""
    card_seq: int = 0
    """Send order across all cards: the highest is the card on screen (ties are impossible)."""
    host_active_at: datetime | None = None
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
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._clock = clock
        self.cards: CardDispatcher | None = None
        self.learning: LearningDesk | None = None
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
        now = self._clock()
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
        """The API form of a host's answer for a named turn: approve (send the latest wording),
        edit (a new version, sent back for a "send"), or reject."""
        action = {"approve": "send", "edit": "revise", "reject": "reject"}.get(
            str(data.get("action")), ""
        )
        if not action:
            raise GuestError("action must be approve, edit, or reject")
        self.answer_card(
            action,
            by=str(data.get("decided_by") or "host"),
            text=data.get("final_text"),
            turn_id=turn_id,
            categories=list(data.get("edit_categories") or []),
            reason=str(data.get("reason") or ""),
        )
        return self.store.turn(turn_id)

    def on_screen(self) -> GuestTurn | LearningCard | None:
        """The card the hosts see last in their chat (a guest card or a learning card), if it
        still needs an answer."""
        shown: list[GuestTurn | LearningCard] = [
            t for t in self.store.turns(None, limit=300) if t.card_sent_at is not None
        ]
        if self.learning is not None:
            shown += [c for c in self.learning.store.recent(200) if c.card_sent_at is not None]
        if not shown:
            return None
        latest = max(shown, key=lambda c: c.card_seq)
        if isinstance(latest, LearningCard):
            return latest if latest.state == "open" else None
        return latest if latest.state in OPEN_STATES else None

    def next_card_seq(self) -> int:
        seqs = [t.card_seq for t in self.store.turns(None, limit=300)]
        if self.learning is not None:
            seqs += [c.card_seq for c in self.learning.store.recent(200)]
        return max(seqs, default=0) + 1

    def answer_card(
        self,
        action: str,
        *,
        by: str,
        text: str | None = None,
        turn_id: str | None = None,
        version: int | None = None,
        categories: list[str] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """A host's answer to a card. Without `turn_id` it applies to the card on screen; the
        code picks the card, never the model. Only `send` finalizes, and only the latest wording
        the host was shown."""
        now = self._clock()
        if action == "undo":
            return self._undo(by, now)
        if turn_id and turn_id.startswith("lc-"):
            if self.learning is None:
                raise GuestError("learning is not enabled")
            return self._answer_lesson(self.learning.store.get(turn_id), action, by, text, version)
        if turn_id:
            turn = self.store.turn(turn_id)
        else:
            current = self.on_screen()
            if current is None:
                raise GuestError("no card is waiting for an answer")
            if isinstance(current, LearningCard):
                return self._answer_lesson(current, action, by, text, version)
            turn = current
        if turn.state not in OPEN_STATES:
            raise GuestError(f"that guest message is already {turn.state}")
        latest = turn.attempts[-1] if turn.attempts else None
        n = len(turn.attempts)
        if action == "send":
            if version is not None and version != n:
                raise GuestError(
                    f"that was version {version}; the latest wording is v{n}. Say send to it."
                )
            if latest is None or latest.decision == "block":
                raise GuestError("there is no sendable reply yet; give the wording you want")
            if _PLACEHOLDER.search(latest.text):
                raise GuestError("the reply still has [blanks]; give the missing detail")
            decision = Decision(action="approve", decided_by=by, final_text=latest.text,
                                edited=latest.author == "host", edit_categories=categories or [],
                                reason=reason, at=now)  # fmt: skip
            done = self._save(turn, state="approved", decision=decision, host_active_at=None)
            self._learn_from(done)
            return {"status": "approved", "turn_id": turn.id, "reply": latest.text,
                    "sending": "not connected yet: approved replies are recorded"}  # fmt: skip
        if action == "reject":
            decision = Decision(action="reject", decided_by=by, reason=reason, at=now)
            self._save(turn, state="rejected", decision=decision, host_active_at=None)
            return {"status": "rejected", "turn_id": turn.id}
        if action == "revise":
            if not text or not text.strip():
                raise GuestError("a change needs the new wording")
            record = self._check(turn, text, [])
            # The host is the source for their own wording: only never-send findings matter.
            kept = [f for f in record.findings if f["severity"] == "block"]
            record = record.model_copy(update={"author": "host", "findings": kept})
            turn = self._save(turn, attempts=[*turn.attempts, record], host_active_at=now)
            if self.cards is not None:
                self.cards.send_card(turn)  # the new version goes back for a "send"
            return {"status": "revised", "turn_id": turn.id, "version": len(turn.attempts),
                    "gate": record.decision, "notes": [f["detail"] for f in record.findings],
                    "next": "the updated card was sent; send finalizes it"}  # fmt: skip
        raise GuestError("action must be send, revise, reject, or undo")

    def _answer_lesson(
        self, card: LearningCard, action: str, by: str, text: str | None, version: int | None
    ) -> dict[str, Any]:
        assert self.learning is not None
        try:
            result = self.learning.answer(card, action, by=by, text=text, version=version)
        except ValueError as exc:
            raise GuestError(str(exc)) from None
        if self.cards is not None:
            if result["status"] == "revised":
                self.cards.send_card(self.learning.store.get(card.id))
            else:
                self.cards.poke(delay=ANSWER_PAUSE_SECONDS)
        return result

    def _learn_from(self, turn: GuestTurn) -> None:
        """A host answered something Lucy could not, or rewrote her reply: maybe there is
        something durable to learn. Runs in the background; never blocks the answer."""
        if self.learning is None or turn.decision is None:
            return
        if not (turn.decision.edited or turn.escalation):
            return
        first = next((a.text for a in turn.attempts if a.author == "lucy"), "")
        about = "\n".join(
            line
            for line in (
                f"Guest asked: {turn.guest_message}",
                f"Lucy's draft: {first}" if first else "",
                f"Lucy handed it to the hosts because: {turn.escalation['reason']}"
                if turn.escalation
                else "",
                f"The host sent: {turn.decision.final_text}",
            )
            if line
        )
        self.learning.learn_in_background(f"guest-turn:{turn.id}", turn.property_slug, about)

    def _undo(self, by: str, now: datetime) -> dict[str, Any]:
        decided = [t for t in self.store.turns(("approved", "rejected"), 50) if t.decision]
        recent = [t for t in decided if t.decision and now - t.decision.at < timedelta(hours=1)]
        if not recent:
            raise GuestError("nothing was decided in the last hour to undo")
        turn = max(recent, key=lambda t: t.decision.at if t.decision else now)
        assert turn.decision is not None
        turn = self._save(turn, state="queued" if turn.attempts else "escalated",
                          undone=[*turn.undone, turn.decision], decision=None,
                          host_active_at=now)  # fmt: skip
        if self.cards is not None:
            self.cards.send_card(turn)
        return {"status": "reopened", "turn_id": turn.id}

    def reopen(self, turn_id: str) -> GuestTurn:
        """Operator repair: put a decided turn back in the queue (its decision is kept)."""
        turn = self.store.turn(turn_id)
        if turn.decision is None:
            raise GuestError("that turn has no decision to reopen")
        turn = self._save(turn, state="queued" if turn.attempts else "escalated",
                          undone=[*turn.undone, turn.decision], decision=None)  # fmt: skip
        return turn

    def home_name(self, slug: str) -> str:
        return self._properties.get(slug).name

    def mark_card_sent(self, turn_id: str, now: datetime, seq: int | None = None) -> GuestTurn:
        seq = seq if seq is not None else self.next_card_seq()
        turn = self.store.turn(turn_id)
        updated = turn.model_copy(update={"card_sent_at": now, "card_seq": seq})
        self.store.put_turn(updated)
        return updated

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

    def _priority(self, turn: GuestTurn, suggested: str | None) -> str:
        r = self.store.reservation(turn.reservation_id)
        return priority_for(suggested, turn.guest_message, r, self._clock())

    def reply(
        self, turn_id: str, text: str, cited_ids: list[str], urgency: str | None = None
    ) -> dict[str, Any]:
        turn = self._open_turn(turn_id)
        turn = turn.model_copy(update={"priority": self._priority(turn, urgency)})
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
        self,
        turn_id: str,
        category: str,
        reason: str,
        holding_reply: str | None = None,
        urgency: str | None = None,
    ) -> dict[str, Any]:
        """Hand the turn to a person with a proposed reply: a holding line, or an answer with
        [brackets] for what only the hosts know."""
        turn = self._open_turn(turn_id)
        turn = turn.model_copy(update={"priority": self._priority(turn, urgency)})
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
        opened_or_closed = (updated.state in OPEN_STATES) != (turn.state in OPEN_STATES)
        if self.cards is not None and opened_or_closed:
            # A closed card: wait for Lucy's confirmation to land before the next card.
            self.cards.poke(delay=0.0 if updated.state in OPEN_STATES else ANSWER_PAUSE_SECONDS)
        return updated

    def notify_hosts(self, turn: GuestTurn, problem: str) -> None:
        """Send a turn's card now with a warning (e.g. guest Lucy could not be reached)."""
        if self.cards is None:
            return
        try:
            self.cards.send_card(turn, problem=problem)
        except Exception:  # a notification must never break the guest flow
            import logging

            logging.getLogger(__name__).warning("host notification failed", exc_info=True)
