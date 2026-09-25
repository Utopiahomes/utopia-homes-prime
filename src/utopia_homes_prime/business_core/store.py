"""Where property records live: Postgres in deployment, memory in tests.

Every update records who changed which field, from what, to what, and why, so the business can
always answer "who told Lucy that?" and undo a mistake.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError

from utopia_homes_prime.business_core.records import EDITABLE_FIELDS, PropertyRecord


class PropertyNotFound(LookupError):
    pass


class InvalidChange(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Change:
    slug: str
    field: str
    old: Any
    new: Any
    changed_by: str
    reason: str
    changed_at: datetime


def apply_changes(
    current: PropertyRecord, changes: dict[str, Any]
) -> tuple[PropertyRecord, dict[str, tuple[Any, Any]]]:
    """The updated record and the fields that actually changed. Rejects unknown or read-only
    fields and anything that would make the record invalid."""
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise InvalidChange(f"these fields cannot be changed: {', '.join(sorted(unknown))}")
    before = current.model_dump(mode="json")
    try:
        updated = PropertyRecord.model_validate({**before, **changes})
    except ValidationError as exc:
        raise InvalidChange(exc.errors()[0]["msg"]) from None
    after = updated.model_dump(mode="json")
    # Guest answers are built from this record, so it must still make sense to Lucy's knowledge
    # builder (for example, parking must state a number of cars).
    from utopia_homes_prime.knowledge.builder import FeedError, property_entries

    try:
        property_entries({k: v for k, v in after.items() if k != "status"}, updated.slug)
    except FeedError as exc:
        raise InvalidChange(str(exc)) from None
    diff = {f: (before[f], after[f]) for f in changes if before[f] != after[f]}
    return updated, diff


class PropertyStore(Protocol):
    def all(self) -> list[PropertyRecord]: ...
    def get(self, slug: str) -> PropertyRecord: ...
    def update(
        self, slug: str, changes: dict[str, Any], *, changed_by: str, reason: str
    ) -> tuple[PropertyRecord, list[Change]]: ...
    def history(self, slug: str, limit: int = 20) -> list[Change]: ...
    def seed_if_empty(self, records: list[PropertyRecord]) -> int: ...
    def version(self) -> str: ...


class MemoryPropertyStore:
    def __init__(self) -> None:
        self._records: dict[str, PropertyRecord] = {}
        self._changes: list[Change] = []
        self._lock = threading.Lock()

    def all(self) -> list[PropertyRecord]:
        return sorted(self._records.values(), key=lambda r: r.name)

    def get(self, slug: str) -> PropertyRecord:
        try:
            return self._records[slug]
        except KeyError:
            raise PropertyNotFound(slug) from None

    def update(
        self, slug: str, changes: dict[str, Any], *, changed_by: str, reason: str
    ) -> tuple[PropertyRecord, list[Change]]:
        with self._lock:
            updated, diff = apply_changes(self.get(slug), changes)
            now = datetime.now(UTC)
            made = [Change(slug, f, o, n, changed_by, reason, now) for f, (o, n) in diff.items()]
            self._records[slug] = updated
            self._changes.extend(made)
            return updated, made

    def history(self, slug: str, limit: int = 20) -> list[Change]:
        self.get(slug)
        return [c for c in reversed(self._changes) if c.slug == slug][:limit]

    def seed_if_empty(self, records: list[PropertyRecord]) -> int:
        with self._lock:
            if self._records:
                return 0
            self._records = {r.slug: r for r in records}
            return len(records)

    def version(self) -> str:
        return str(len(self._changes)) + ":" + ",".join(sorted(self._records))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS properties (
    slug        text PRIMARY KEY,
    record      jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL
);
CREATE TABLE IF NOT EXISTS property_changes (
    id          bigserial PRIMARY KEY,
    slug        text NOT NULL REFERENCES properties(slug),
    field       text NOT NULL,
    old_value   jsonb,
    new_value   jsonb,
    changed_by  text NOT NULL,
    reason      text NOT NULL,
    changed_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS property_changes_slug ON property_changes (slug, id DESC);
"""


class PostgresPropertyStore:
    """Small and synchronous: FastAPI runs these calls in its thread pool, and a connection per
    call is plenty for a handful of editors and one guest-answer cache refresh."""

    def __init__(self, dsn: str) -> None:
        import psycopg  # imported here so the rest of Homes Prime runs without it

        self._psycopg = psycopg
        self._dsn = dsn
        with self._connect() as conn:
            conn.execute(SCHEMA_SQL)

    def _connect(self) -> Any:
        return self._psycopg.connect(self._dsn, autocommit=False, connect_timeout=10)

    def all(self) -> list[PropertyRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT record FROM properties").fetchall()
        return sorted((PropertyRecord.model_validate(r[0]) for r in rows), key=lambda r: r.name)

    def get(self, slug: str) -> PropertyRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT record FROM properties WHERE slug = %s", (slug,)).fetchone()
        if row is None:
            raise PropertyNotFound(slug)
        return PropertyRecord.model_validate(row[0])

    def update(
        self, slug: str, changes: dict[str, Any], *, changed_by: str, reason: str
    ) -> tuple[PropertyRecord, list[Change]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT record FROM properties WHERE slug = %s FOR UPDATE", (slug,)
            ).fetchone()
            if row is None:
                raise PropertyNotFound(slug)
            updated, diff = apply_changes(PropertyRecord.model_validate(row[0]), changes)
            if not diff:
                return updated, []
            now = conn.execute("SELECT now()").fetchone()[0]
            conn.execute(
                "UPDATE properties SET record = %s, updated_at = %s, updated_by = %s "
                "WHERE slug = %s",
                (json.dumps(updated.model_dump(mode="json")), now, changed_by, slug),
            )
            made = []
            for field, (old, new) in diff.items():
                conn.execute(
                    "INSERT INTO property_changes "
                    "(slug, field, old_value, new_value, changed_by, reason, changed_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (slug, field, json.dumps(old), json.dumps(new), changed_by, reason, now),
                )
                made.append(Change(slug, field, old, new, changed_by, reason, now))
        return updated, made

    def history(self, slug: str, limit: int = 20) -> list[Change]:
        self.get(slug)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT slug, field, old_value, new_value, changed_by, reason, changed_at "
                "FROM property_changes WHERE slug = %s ORDER BY id DESC LIMIT %s",
                (slug, limit),
            ).fetchall()
        return [Change(*row) for row in rows]

    def seed_if_empty(self, records: list[PropertyRecord]) -> int:
        with self._connect() as conn:
            conn.execute("LOCK TABLE properties IN EXCLUSIVE MODE")
            if conn.execute("SELECT 1 FROM properties LIMIT 1").fetchone():
                return 0
            for r in records:
                conn.execute(
                    "INSERT INTO properties (slug, record, updated_by) VALUES (%s, %s, %s)",
                    (r.slug, json.dumps(r.model_dump(mode="json")), "seed: website content"),
                )
        return len(records)

    def version(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*), coalesce(max(updated_at)::text, '') FROM properties"
            ).fetchone()
        return f"{row[0]}:{row[1]}"
