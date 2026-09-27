"""Work items: the handoff between what the company knows and work someone has to do.

Homes Prime is the company's truth: it answers from knowledge and records. When a request asks
for something to be *done* ("call the pool company", "reconcile September payouts"), Prime does
not grow tools for it; it opens a work item and hands it to an execution provider.

- A work item says what is needed and why, for which home, who asked, and who is doing it.
- `proposed` items are suggestions (from a guest conversation, or a worker) that a person has not
  started. `open` items are real work. Only an operator opens work.
- The execution provider is whoever performs the work. Today that is `manual`: Ray or Meghan see
  the item in Telegram and do it. Later it can be Anton or another executor; the item carries the
  executor's own reference so its status can be followed.
- Workers never change company truth directly. What they learn comes back as a proposed knowledge
  item for a person to approve.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

WorkStatus = Literal["proposed", "open", "in_progress", "waiting", "done", "cancelled"]
Authority = Literal["none", "operator", "owner"]
_REF = r"^[a-z][a-z0-9_-]*:[A-Za-z0-9._:/-]{1,120}$"


class WorkNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: datetime
    by: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=1000)


class WorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^wi-[a-z0-9]{12}$")
    type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    """What kind of work: maintenance, vendor_followup, guest_request, finance, owner, other..."""
    title: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=2000)
    """What is needed and why, so whoever does it needs no other context."""
    property_slug: str | None = Field(default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    status: WorkStatus = "open"
    requested_by: str = Field(min_length=1, max_length=120)
    assigned_executor: str = Field(default="manual", pattern=r"^[a-z][a-z0-9_-]{1,39}$")
    executor_ref: str | None = Field(default=None, max_length=200)
    required_authority: Authority = "operator"
    input_refs: list[str] = Field(default_factory=list, max_length=20)
    """What the work is about: `reservation:…`, `airbnb-thread:…`, `ki-…` items, `vendor:…`."""
    result_refs: list[str] = Field(default_factory=list, max_length=20)
    result: str = Field(default="", max_length=2000)
    due_at: datetime | None = None
    notes: list[WorkNote] = Field(default_factory=list, max_length=200)
    created_at: datetime
    updated_at: datetime


EDITABLE_WORK_FIELDS = frozenset(
    {
        "type", "title", "purpose", "property_slug", "status", "assigned_executor",
        "executor_ref", "required_authority", "input_refs", "result_refs", "result", "due_at",
    }
)  # fmt: skip
OPEN_STATUSES = ("proposed", "open", "in_progress", "waiting")


class WorkNotFound(LookupError):
    pass


class InvalidWork(ValueError):
    pass


def _validate(data: dict[str, Any]) -> WorkItem:
    try:
        return WorkItem.model_validate(data)
    except ValidationError as exc:
        error = exc.errors()[0]
        raise InvalidWork(f"{'.'.join(map(str, error['loc']))}: {error['msg']}") from None


class ExecutionProvider(Protocol):
    """Whoever performs work. `submit` is called when an item is opened for this executor and may
    return the executor's own reference; `cancel` when it is cancelled."""

    name: str

    def submit(self, item: WorkItem) -> str | None: ...
    def cancel(self, item: WorkItem) -> None: ...


class ManualExecution:
    """A person does the work: they see open items in Telegram and update them there."""

    name = "manual"

    def submit(self, item: WorkItem) -> str | None:
        return None

    def cancel(self, item: WorkItem) -> None:
        return None


def _new(data: dict[str, Any], requested_by: str) -> WorkItem:
    now = datetime.now(UTC)
    return _validate(
        {
            **data,
            "id": "wi-" + uuid.uuid4().hex[:12],
            "requested_by": requested_by,
            "notes": [],
            "created_at": now,
            "updated_at": now,
        }
    )


def _apply(current: WorkItem, changes: dict[str, Any], note: str | None, by: str) -> WorkItem:
    unknown = set(changes) - EDITABLE_WORK_FIELDS
    if unknown:
        raise InvalidWork(f"these fields cannot be changed: {', '.join(sorted(unknown))}")
    now = datetime.now(UTC)
    data = {**current.model_dump(), **changes, "updated_at": now}
    if note:
        data["notes"] = [*data["notes"], {"at": now, "by": by, "text": note}]
    return _validate(data)


def _matches(
    item: WorkItem,
    statuses: tuple[str, ...] | None,
    property_slug: str | None,
    executor: str | None,
) -> bool:
    return (
        (statuses is None or item.status in statuses)
        and (property_slug is None or item.property_slug == property_slug)
        and (executor is None or item.assigned_executor == executor)
    )


class WorkStore(Protocol):
    def create(self, data: dict[str, Any], *, requested_by: str) -> WorkItem: ...
    def get(self, work_id: str) -> WorkItem: ...
    def update(
        self, work_id: str, changes: dict[str, Any], *, changed_by: str, note: str | None = None
    ) -> WorkItem: ...
    def search(
        self,
        *,
        statuses: tuple[str, ...] | None = None,
        property_slug: str | None = None,
        executor: str | None = None,
        limit: int = 50,
    ) -> list[WorkItem]: ...


def _sorted(items: list[WorkItem], limit: int) -> list[WorkItem]:
    return sorted(items, key=lambda w: w.updated_at, reverse=True)[:limit]


class MemoryWorkStore:
    def __init__(self) -> None:
        self._items: dict[str, WorkItem] = {}
        self._lock = threading.Lock()

    def create(self, data: dict[str, Any], *, requested_by: str) -> WorkItem:
        with self._lock:
            item = _new(data, requested_by)
            self._items[item.id] = item
            return item

    def get(self, work_id: str) -> WorkItem:
        try:
            return self._items[work_id]
        except KeyError:
            raise WorkNotFound(work_id) from None

    def update(
        self, work_id: str, changes: dict[str, Any], *, changed_by: str, note: str | None = None
    ) -> WorkItem:
        with self._lock:
            item = _apply(self.get(work_id), changes, note, changed_by)
            self._items[work_id] = item
            return item

    def search(
        self,
        *,
        statuses: tuple[str, ...] | None = None,
        property_slug: str | None = None,
        executor: str | None = None,
        limit: int = 50,
    ) -> list[WorkItem]:
        found = [w for w in self._items.values() if _matches(w, statuses, property_slug, executor)]
        return _sorted(found, limit)


WORK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS work_items (
    id          text PRIMARY KEY,
    item        jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS work_item_changes (
    id          bigserial PRIMARY KEY,
    work_id     text NOT NULL REFERENCES work_items(id),
    before      jsonb,
    after       jsonb NOT NULL,
    changed_by  text NOT NULL,
    changed_at  timestamptz NOT NULL DEFAULT now()
);
"""


class PostgresWorkStore:
    """Stored as JSON documents like knowledge items: the shape will change as workflows arrive."""

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        with self._connect() as conn:
            conn.execute(WORK_SCHEMA_SQL)

    def _connect(self) -> Any:
        return self._psycopg.connect(self._dsn, autocommit=False, connect_timeout=10)

    def _write(self, conn: Any, item: WorkItem, before: WorkItem | None, by: str) -> None:
        doc = json.dumps(item.model_dump(mode="json"))
        conn.execute(
            "INSERT INTO work_items (id, item, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (id) DO UPDATE SET item = EXCLUDED.item, updated_at = now()",
            (item.id, doc),
        )
        conn.execute(
            "INSERT INTO work_item_changes (work_id, before, after, changed_by) "
            "VALUES (%s, %s, %s, %s)",
            (item.id, json.dumps(before.model_dump(mode="json")) if before else None, doc, by),
        )

    def create(self, data: dict[str, Any], *, requested_by: str) -> WorkItem:
        item = _new(data, requested_by)
        with self._connect() as conn:
            self._write(conn, item, None, requested_by)
        return item

    def get(self, work_id: str) -> WorkItem:
        with self._connect() as conn:
            row = conn.execute("SELECT item FROM work_items WHERE id = %s", (work_id,)).fetchone()
        if row is None:
            raise WorkNotFound(work_id)
        return WorkItem.model_validate(row[0])

    def update(
        self, work_id: str, changes: dict[str, Any], *, changed_by: str, note: str | None = None
    ) -> WorkItem:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT item FROM work_items WHERE id = %s FOR UPDATE", (work_id,)
            ).fetchone()
            if row is None:
                raise WorkNotFound(work_id)
            before = WorkItem.model_validate(row[0])
            item = _apply(before, changes, note, changed_by)
            self._write(conn, item, before, changed_by)
        return item

    def search(
        self,
        *,
        statuses: tuple[str, ...] | None = None,
        property_slug: str | None = None,
        executor: str | None = None,
        limit: int = 50,
    ) -> list[WorkItem]:
        with self._connect() as conn:
            rows = conn.execute("SELECT item FROM work_items").fetchall()
        found = [
            w
            for w in (WorkItem.model_validate(r[0]) for r in rows)
            if _matches(w, statuses, property_slug, executor)
        ]
        return _sorted(found, limit)


class WorkDesk:
    """Opens, updates, and hands off work. The only place that talks to execution providers."""

    def __init__(self, store: WorkStore, providers: list[ExecutionProvider] | None = None) -> None:
        self.store = store
        self._providers = {p.name: p for p in (providers or [ManualExecution()])}

    def _provider(self, name: str) -> ExecutionProvider:
        try:
            return self._providers[name]
        except KeyError:
            known = ", ".join(sorted(self._providers))
            raise InvalidWork(f"no executor named {name!r} (known: {known})") from None

    def submit(self, data: dict[str, Any], *, requested_by: str) -> WorkItem:
        self._provider(str(data.get("assigned_executor") or "manual"))
        item = self.store.create(data, requested_by=requested_by)
        return self._hand_off(item, requested_by) if item.status == "open" else item

    def update(
        self, work_id: str, changes: dict[str, Any], *, changed_by: str, note: str | None = None
    ) -> WorkItem:
        before = self.store.get(work_id)
        if "assigned_executor" in changes:
            self._provider(str(changes["assigned_executor"]))
        item = self.store.update(work_id, changes, changed_by=changed_by, note=note)
        if before.status == "proposed" and item.status == "open":
            item = self._hand_off(item, changed_by)
        elif item.status == "cancelled" and before.status != "cancelled":
            self._provider(item.assigned_executor).cancel(item)
        return item

    def _hand_off(self, item: WorkItem, by: str) -> WorkItem:
        ref = self._provider(item.assigned_executor).submit(item)
        if ref:
            item = self.store.update(item.id, {"executor_ref": ref}, changed_by=by)
        return item
