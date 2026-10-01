"""Knowledge items: what Utopia knows beyond a property's page facts.

Each item is one piece of knowledge ("beds are not made on arrival; sheets are in the top dresser
drawer"), tied to a property or to Utopia in general, with:

- audience: who may ever see it. `public` (website), `booked_guest` (after booking), `internal`
  (Ray, Meghan, and internal Lucy only). Secrets such as door codes are never knowledge items.
- status: `proposed` (extracted or suggested, awaiting review), `active` (approved), `withdrawn`.
- evidence: where it came from (source note, evidence ids such as Airbnb thread ids), how
  confident the extraction was, and how it relates to what we already know (`relation`).
- time: effective dates for facts that change, and `last_confirmed`, set when a person confirms it.

Historical messages are evidence, not truth: an extracted item stays `proposed` until a person
approves it, and evidence can challenge an active item but never overwrite it.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Audience = Literal["public", "booked_guest", "internal"]
ItemStatus = Literal["proposed", "active", "withdrawn"]
ItemKind = Literal["fact", "policy", "faq", "place", "playbook"]


class KnowledgeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^ki-[a-z0-9]{12}$")
    property_slug: str | None = Field(default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    audience: Audience
    kind: ItemKind = "fact"
    topic: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=2000)
    status: ItemStatus = "proposed"
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    source_note: str = Field(default="", max_length=500)
    relation: Literal["new", "confirms", "conflicts", "obsolete"] | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    last_confirmed: datetime | None = None
    created_by: str = Field(min_length=1, max_length=120)
    updated_at: datetime | None = None


EDITABLE_ITEM_FIELDS = frozenset(
    {
        "audience", "kind", "topic", "title", "text", "status", "confidence", "evidence_ids",
        "source_note", "relation", "effective_from", "effective_until",
    }
)  # fmt: skip


class ItemNotFound(LookupError):
    pass


class InvalidItem(ValueError):
    pass


def new_item_id() -> str:
    return "ki-" + uuid.uuid4().hex[:12]


def validate_item(data: dict[str, Any]) -> KnowledgeItem:
    try:
        return KnowledgeItem.model_validate(data)
    except ValidationError as exc:
        error = exc.errors()[0]
        raise InvalidItem(f"{'.'.join(map(str, error['loc']))}: {error['msg']}") from None


class KnowledgeItemStore(Protocol):
    def create(
        self, data: dict[str, Any], *, created_by: str, confirm: bool = False
    ) -> KnowledgeItem: ...
    def get(self, item_id: str) -> KnowledgeItem: ...
    def update(
        self, item_id: str, changes: dict[str, Any], *, changed_by: str, confirm: bool
    ) -> KnowledgeItem: ...
    def search(
        self,
        *,
        property_slug: str | None = None,
        audiences: tuple[str, ...] | None = None,
        statuses: tuple[str, ...] | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[KnowledgeItem]: ...
    def version(self) -> str: ...


def _matches(
    item: KnowledgeItem,
    property_slug: str | None,
    audiences: tuple[str, ...] | None,
    statuses: tuple[str, ...] | None,
    query: str | None,
) -> bool:
    if property_slug is not None and item.property_slug != property_slug:
        return False
    if audiences is not None and item.audience not in audiences:
        return False
    if statuses is not None and item.status not in statuses:
        return False
    if query:
        haystack = f"{item.topic} {item.title} {item.text} {item.source_note}".lower()
        return all(word in haystack for word in query.lower().split())
    return True


def _new(data: dict[str, Any], created_by: str, confirm: bool) -> KnowledgeItem:
    """A person stating a fact (confirm=True) confirms it; an extracted item is not confirmed."""
    now = datetime.now(UTC)
    return validate_item(
        {
            **data,
            "id": new_item_id(),
            "created_by": created_by,
            "updated_at": now,
            "last_confirmed": now if confirm else None,
        }
    )


def _apply(current: KnowledgeItem, changes: dict[str, Any], *, confirm: bool) -> KnowledgeItem:
    unknown = set(changes) - EDITABLE_ITEM_FIELDS
    if unknown:
        raise InvalidItem(f"these fields cannot be changed: {', '.join(sorted(unknown))}")
    now = datetime.now(UTC)
    data = {**current.model_dump(), **changes, "updated_at": now}
    if confirm:
        data["last_confirmed"] = now
    return validate_item(data)


class MemoryKnowledgeItemStore:
    def __init__(self) -> None:
        self._items: dict[str, KnowledgeItem] = {}
        self._writes = 0
        self._lock = threading.Lock()

    def create(
        self, data: dict[str, Any], *, created_by: str, confirm: bool = False
    ) -> KnowledgeItem:
        with self._lock:
            item = _new(data, created_by, confirm)
            self._items[item.id] = item
            self._writes += 1
            return item

    def get(self, item_id: str) -> KnowledgeItem:
        try:
            return self._items[item_id]
        except KeyError:
            raise ItemNotFound(item_id) from None

    def update(
        self, item_id: str, changes: dict[str, Any], *, changed_by: str, confirm: bool
    ) -> KnowledgeItem:
        with self._lock:
            item = _apply(self.get(item_id), changes, confirm=confirm)
            self._items[item_id] = item
            self._writes += 1
            return item

    def search(
        self,
        *,
        property_slug: str | None = None,
        audiences: tuple[str, ...] | None = None,
        statuses: tuple[str, ...] | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[KnowledgeItem]:
        found = [
            i
            for i in self._items.values()
            if _matches(i, property_slug, audiences, statuses, query)
        ]
        return sorted(found, key=lambda i: (i.property_slug or "", i.topic, i.id))[:limit]

    def version(self) -> str:
        return f"items:{self._writes}"


KNOWLEDGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_items (
    id          text PRIMARY KEY,
    item        jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS knowledge_item_changes (
    id          bigserial PRIMARY KEY,
    item_id     text NOT NULL REFERENCES knowledge_items(id),
    before      jsonb,
    after       jsonb NOT NULL,
    changed_by  text NOT NULL,
    changed_at  timestamptz NOT NULL DEFAULT now()
);
"""


class PostgresKnowledgeItemStore:
    """Items are stored as JSON documents and filtered in Python: a few hundred items per home is
    small, and it keeps the schema free to evolve while the knowledge model settles."""

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        with self._connect() as conn:
            conn.execute(KNOWLEDGE_SCHEMA_SQL)

    def _connect(self) -> Any:
        return self._psycopg.connect(self._dsn, autocommit=False, connect_timeout=10)

    def _write(self, conn: Any, item: KnowledgeItem, before: KnowledgeItem | None, by: str) -> None:
        doc = json.dumps(item.model_dump(mode="json"))
        conn.execute(
            "INSERT INTO knowledge_items (id, item, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (id) DO UPDATE SET item = EXCLUDED.item, updated_at = now()",
            (item.id, doc),
        )
        conn.execute(
            "INSERT INTO knowledge_item_changes (item_id, before, after, changed_by) "
            "VALUES (%s, %s, %s, %s)",
            (item.id, json.dumps(before.model_dump(mode="json")) if before else None, doc, by),
        )

    def create(
        self, data: dict[str, Any], *, created_by: str, confirm: bool = False
    ) -> KnowledgeItem:
        item = _new(data, created_by, confirm)
        with self._connect() as conn:
            self._write(conn, item, None, created_by)
        return item

    def get(self, item_id: str) -> KnowledgeItem:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT item FROM knowledge_items WHERE id = %s", (item_id,)
            ).fetchone()
        if row is None:
            raise ItemNotFound(item_id)
        return KnowledgeItem.model_validate(row[0])

    def update(
        self, item_id: str, changes: dict[str, Any], *, changed_by: str, confirm: bool
    ) -> KnowledgeItem:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT item FROM knowledge_items WHERE id = %s FOR UPDATE", (item_id,)
            ).fetchone()
            if row is None:
                raise ItemNotFound(item_id)
            before = KnowledgeItem.model_validate(row[0])
            item = _apply(before, changes, confirm=confirm)
            self._write(conn, item, before, changed_by)
        return item

    def search(
        self,
        *,
        property_slug: str | None = None,
        audiences: tuple[str, ...] | None = None,
        statuses: tuple[str, ...] | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[KnowledgeItem]:
        with self._connect() as conn:
            rows = conn.execute("SELECT item FROM knowledge_items").fetchall()
        found = [
            i
            for i in (KnowledgeItem.model_validate(r[0]) for r in rows)
            if _matches(i, property_slug, audiences, statuses, query)
        ]
        return sorted(found, key=lambda i: (i.property_slug or "", i.topic, i.id))[:limit]

    def version(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*), coalesce(max(updated_at)::text, '') FROM knowledge_items"
            ).fetchone()
        return f"items:{row[0]}:{row[1]}"
