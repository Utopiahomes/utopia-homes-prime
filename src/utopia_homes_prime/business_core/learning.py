"""Learning: Lucy learns from what the hosts did, automatically.

Every time a host answers something Lucy could not (a guest card she handed over, or a reply the
host rewrote) or finishes a piece of work, there may be something durable to learn: "the pool is
open through mid-October", "the breaker for the pool pump is in the garage". Lucy saves it as
active knowledge straight away (Ray's call, 2026-09-30: no confirmation step), marked as learned
and unconfirmed, so she uses it next time. If it corrects something she already knew, she updates
that item instead of adding a second one.

Public facts she learns reach the website chat too (within its refresh window, about 15 s): Ray
would rather the website learn and occasionally be slightly off than never learn. Guest-only and
internal details keep their audience. Hosts see what she learned by asking her ("what have you
learned lately?") and correct it the same way they correct any knowledge.

An optional review mode (`review_by_card=True`) instead saves proposals as *proposed* and asks via
a learning card in the Telegram queue ("save", a change, or "skip"); it is off by default.

The proposer is a model call (OpenRouter, zero data retention). It is told to propose only lasting
facts and policies, never one-off situations, guest details, codes, contact details, or prices;
code then drops anything that still looks like a secret or contact detail.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from utopia_homes_prime.business_core.knowledge_items import KnowledgeItemStore
from utopia_homes_prime.business_core.scrub import leaks, scrub
from utopia_homes_prime.business_core.store import PropertyStore

_log = logging.getLogger(__name__)
MAX_PROPOSALS = 2
Audience = Literal["public", "booked_guest", "internal"]


class LearningCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^lc-[a-z0-9]{12}$")
    item_id: str
    """The proposed knowledge item this card asks about."""
    property_slug: str | None
    source_ref: str
    """What it was learned from: `guest-turn:gt-...` or `work:wi-...`."""
    source_summary: str
    audience: Audience
    title: str
    versions: list[str]
    """The proposed text, then each host revision; the last is what "save" saves."""
    state: Literal["open", "saved", "skipped"] = "open"
    decided_by: str | None = None
    priority: Literal["learn"] = "learn"
    card_sent_at: datetime | None = None
    card_seq: int = 0
    host_active_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class LearningStore(Protocol):
    def put(self, card: LearningCard) -> None: ...
    def get(self, card_id: str) -> LearningCard: ...
    def recent(self, limit: int = 200) -> list[LearningCard]: ...


class LearningNotFound(LookupError):
    pass


class MemoryLearningStore:
    def __init__(self) -> None:
        self._cards: dict[str, LearningCard] = {}
        self._lock = threading.Lock()

    def put(self, card: LearningCard) -> None:
        with self._lock:
            self._cards[card.id] = card

    def get(self, card_id: str) -> LearningCard:
        try:
            return self._cards[card_id]
        except KeyError:
            raise LearningNotFound(card_id) from None

    def recent(self, limit: int = 200) -> list[LearningCard]:
        return sorted(self._cards.values(), key=lambda c: c.updated_at, reverse=True)[:limit]


LEARNING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS learning_cards (
    id          text PRIMARY KEY,
    doc         jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
"""


class PostgresLearningStore:
    def __init__(self, dsn: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        with self._connect() as conn:
            conn.execute(LEARNING_SCHEMA_SQL)

    def _connect(self) -> Any:
        return self._psycopg.connect(self._dsn, autocommit=False, connect_timeout=10)

    def put(self, card: LearningCard) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO learning_cards (id, doc, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc, updated_at = now()",
                (card.id, json.dumps(card.model_dump(mode="json"))),
            )

    def get(self, card_id: str) -> LearningCard:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT doc FROM learning_cards WHERE id = %s", (card_id,)
            ).fetchone()
        if row is None:
            raise LearningNotFound(card_id)
        return LearningCard.model_validate(row[0])

    def recent(self, limit: int = 200) -> list[LearningCard]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT doc FROM learning_cards ORDER BY updated_at DESC LIMIT %s", (limit,)
            ).fetchall()
        return [LearningCard.model_validate(r[0]) for r in rows]


Proposer = Callable[[str], list[dict[str, Any]]]
"""Given the learning context (plain text), the knowledge it suggests (schema below)."""

PROPOSER_SYSTEM = """You help a vacation-rental business keep its knowledge base current.
You are shown something the hosts just did: a guest's question and the reply the host approved
(sometimes with the assistant's earlier draft), or a finished piece of work and its notes. You
also see what the business already knows about the home.

Propose at most two knowledge items worth keeping for the future: lasting facts about the home,
house policies, or answers future guests will need. Write each as a plain statement in the third
person about the home (not addressed to a guest), one to three sentences.

Do NOT propose:
- one-off situations or updates ("a plumber is on the way", "the cleaners finished early",
  "you can check in at noon this Friday") unless they reveal a standing rule;
- anything the existing knowledge already says;
- guest names, dates of a specific stay, door or lock codes, Wi-Fi passwords, phone numbers,
  emails, or prices.
If there is nothing durable to learn, return an empty list. Most of the time that is right.

If what the host said corrects or updates something the business already knows, set `replaces` to
that item's exact title (as listed) and write the corrected text; otherwise set it to "".

audience: public (anyone may know it), booked_guest (only guests with a booking, e.g. how things
work in the house), internal (hosts only, e.g. where the breaker is, which vendor to call)."""

PROPOSER_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "audience": {"type": "string", "enum": ["public", "booked_guest", "internal"]},
                    "kind": {"type": "string", "enum": ["fact", "policy", "faq", "place"]},
                    "topic": {"type": "string"},
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                    "why": {"type": "string"},
                    "replaces": {"type": "string"},
                },
                "required": ["audience", "kind", "topic", "title", "text", "why", "replaces"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def openrouter_proposer(api_key: str, model: str) -> Proposer:
    def propose(context: str) -> list[dict[str, Any]]:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": PROPOSER_SYSTEM},
                {"role": "user", "content": context},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "knowledge_proposals",
                    "strict": True,
                    "schema": PROPOSER_SCHEMA,
                },
            },  # fmt: skip
            "provider": {"zdr": True, "data_collection": "deny", "require_parameters": True},
            "max_tokens": 4000,
            "reasoning": {"effort": "low"},
        }
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        for attempt in range(3):  # providers occasionally return an error body; try again
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.load(response)
            if payload.get("choices"):
                items = json.loads(payload["choices"][0]["message"]["content"]).get("items", [])
                return [i for i in items if isinstance(i, dict)]
            _log.warning("knowledge proposer returned no choices (attempt %d)", attempt + 1)
        return []

    return propose


def _now() -> datetime:
    return datetime.now(UTC)


class LearningDesk:
    def __init__(
        self,
        store: LearningStore,
        items: KnowledgeItemStore,
        properties: PropertyStore,
        propose: Proposer,
        clock: Callable[[], datetime] = _now,
        *,
        review_by_card: bool = False,
    ) -> None:
        self.review_by_card = review_by_card
        self.store = store
        self._items = items
        self._properties = properties
        self._propose = propose
        self._clock = clock
        self.on_new_card: Callable[[], None] | None = None

    # Learning ------------------------------------------------------------------------------

    def learn_in_background(self, source_ref: str, property_slug: str | None, about: str) -> None:
        threading.Thread(
            target=self._learn_safely, args=(source_ref, property_slug, about), daemon=True
        ).start()

    def _learn_safely(self, source_ref: str, property_slug: str | None, about: str) -> None:
        try:
            self.learn(source_ref, property_slug, about)
        except Exception:  # learning is a bonus; it must never break the flow that triggered it
            _log.warning("learning from %s failed", source_ref, exc_info=True)

    def learn(self, source_ref: str, property_slug: str | None, about: str) -> list[Any]:
        """Learn from one thing the hosts did. Returns the knowledge items written (or, in review
        mode, the learning cards created)."""
        known = self._items.search(property_slug=property_slug, statuses=("active", "proposed"),
                                   limit=500)  # fmt: skip
        if any(source_ref in i.evidence_ids for i in known) or any(
            c.source_ref == source_ref for c in self.store.recent(300)
        ):
            return []  # already learned from this (e.g. an answer that was undone and re-sent)
        home = self._properties.get(property_slug).name if property_slug else "Utopia Homes"
        known_text = "\n".join(f"- {i.title}: {i.text}" for i in known) or "(nothing yet)"
        context = f"Home: {home}\n\n{about}\n\nWhat the business already knows:\n{known_text}"
        by_title = {i.title.strip().lower(): i for i in known if i.status == "active"}
        written: list[Any] = []
        for proposal in self._propose(context)[:MAX_PROPOSALS]:
            text = scrub(str(proposal.get("text") or "")).strip()
            title = str(proposal.get("title") or "").strip()[:160]
            if not text or not title or leaks(text) or "[" in text or len(text) > 1500:
                continue
            why = f"learned from {source_ref}: {proposal.get('why', '')}"[:500]
            if self.review_by_card:
                written.append(self._card_for(proposal, title, text, why, source_ref,
                                              property_slug, about))  # fmt: skip
                continue
            target = by_title.get(str(proposal.get("replaces") or "").strip().lower())
            if target is not None:
                written.append(self._items.update(
                    target.id,
                    {"text": text, "evidence_ids": [*target.evidence_ids, source_ref][-50:],
                     "source_note": f"updated by Lucy: {why}"[:500], "relation": "confirms"},
                    changed_by="Lucy (learned automatically)", confirm=False,
                ))  # fmt: skip
                continue
            written.append(self._items.create(
                {
                    "property_slug": property_slug,
                    "audience": proposal.get("audience", "booked_guest"),
                    "kind": proposal.get("kind", "fact"),
                    "topic": str(proposal.get("topic") or "general")[:60],
                    "title": title,
                    "text": text,
                    "status": "active",
                    "confidence": 0.7,
                    "evidence_ids": [source_ref],
                    "source_note": why,
                    "relation": "new",
                },
                created_by="Lucy (learned automatically)",
            ))  # fmt: skip
        if written and self.review_by_card and self.on_new_card is not None:
            self.on_new_card()
        return written

    def _card_for(
        self,
        proposal: dict[str, Any],
        title: str,
        text: str,
        why: str,
        source_ref: str,
        property_slug: str | None,
        about: str,
    ) -> LearningCard:
        item = self._items.create(
            {
                "property_slug": property_slug,
                "audience": proposal.get("audience", "booked_guest"),
                "kind": proposal.get("kind", "fact"),
                "topic": str(proposal.get("topic") or "general")[:60],
                "title": title,
                "text": text,
                "status": "proposed",
                "evidence_ids": [source_ref],
                "source_note": why,
                "relation": "new",
            },
            created_by="Lucy (learning)",
        )
        now = self._clock()
        card = LearningCard(
            id="lc-" + uuid.uuid4().hex[:12], item_id=item.id, property_slug=property_slug,
            source_ref=source_ref, source_summary=about[:1200], audience=item.audience,
            title=title, versions=[text], created_at=now, updated_at=now,
        )  # fmt: skip
        self.store.put(card)
        return card

    # Host answers ----------------------------------------------------------------------------

    def answer(
        self, card: LearningCard, action: str, *, by: str, text: str | None, version: int | None
    ) -> dict[str, Any]:
        now = self._clock()
        if card.state != "open":
            raise ValueError(f"that learning card is already {card.state}")
        if action == "send":
            if version is not None and version != len(card.versions):
                raise ValueError(f"the latest wording is v{len(card.versions)}; save that one")
            final = card.versions[-1]
            self._items.update(card.item_id, {"status": "active", "text": final},
                               changed_by=by, confirm=True)  # fmt: skip
            self._save(card, state="saved", decided_by=by, host_active_at=None)
            return {"status": "saved", "card_id": card.id, "knowledge": final}
        if action == "reject":
            self._items.update(card.item_id, {"status": "withdrawn"}, changed_by=by, confirm=False)
            self._save(card, state="skipped", decided_by=by, host_active_at=None)
            return {"status": "skipped", "card_id": card.id}
        if action == "revise":
            clean = scrub((text or "").strip())
            if not clean:
                raise ValueError("a change needs the new wording")
            if leaks(clean):
                raise ValueError("knowledge cannot hold codes, passwords, or contact details")
            card = self._save(card, versions=[*card.versions, clean], host_active_at=now)
            return {"status": "revised", "card_id": card.id, "version": len(card.versions),
                    "next": "the updated card was sent; save finalizes it"}  # fmt: skip
        raise ValueError("action must be send (save), revise, or reject (skip)")

    def _save(self, card: LearningCard, **changes: Any) -> LearningCard:
        updated = card.model_copy(update={**changes, "updated_at": self._clock()})
        self.store.put(updated)
        return updated

    def mark_card_sent(self, card_id: str, now: datetime, seq: int) -> LearningCard:
        card = self.store.get(card_id)
        updated = card.model_copy(update={"card_sent_at": now, "card_seq": seq})
        self.store.put(updated)
        return updated


def learning_card_text(card: LearningCard, home: str) -> str:
    audience = {"public": "anyone", "booked_guest": "booked guests", "internal": "hosts only"}
    source = (
        "From a guest message you just answered"
        if card.source_ref.startswith("guest-turn:")
        else "From work you just finished"
    )
    version = len(card.versions)
    label = "Your updated wording" if version > 1 else "Proposed knowledge"
    lines = [
        f"📚 Learn · {home}",
        "",
        f"{source}:",
        card.source_summary,
        "",
        f"Save this for next time? (visible to {audience[card.audience]})",
        f"{label} (v{version}) · {card.title}:",
        card.versions[-1],
        "",
        'Reply "save", your changes, or "skip".',
        f"[{card.id} v{version}]",
    ]
    return "\n".join(lines)
