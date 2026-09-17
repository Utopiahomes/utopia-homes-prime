"""Homes-owned approved public knowledge projection (RC2 §§13-14).

Homes Prime loads an approved `lucy-public-knowledge-v1` corpus from a configured path and admits
it only when its canonical digest is in the configured allowlist, so exact approved bytes — not
whatever happens to be on disk — define what the model may see. Withdrawn entries and entries
outside their effective window are excluded before any context is assembled, and replay
eligibility is tied to the knowledge release plus the withdrawal set, so a withdrawal or rollback
can never resurrect superseded information through a cached answer.

The canonical digest reproduces utopia-homes-web's `digestPublicLucyKnowledgeSnapshot()`: entries
sorted by id, absent `property_slug`/`property_facts`/`effective_until` normalized to null, keys
sorted, compact JSON, UTF-8, SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from guest_answer_provider import patterns

KNOWLEDGE_SCHEMA = "lucy-public-knowledge-v1"
CONTEXT_SCHEMA = "homes-public-context-v1"

_LOCAL_ID_RE = re.compile(r"^(?=[a-z0-9-]{1,95}$)[a-z0-9]+(?:-[a-z0-9]+)*$")
"""The local component of an RC2 §12.2 namespaced identifier. Every source and link id must fit
it so Homes can build `source_id`/`action_id` values without rewriting approved identifiers."""


class KnowledgeUnavailable(RuntimeError):
    """The approved projection cannot be established. Callers fail closed."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Reference(_Strict):
    id: str
    label: str = Field(min_length=1, max_length=160)
    href: str = Field(max_length=2048)


class PropertyFacts(_Strict):
    max_guests: int = Field(gt=0, le=100)
    parking_spaces: int = Field(ge=0, le=50)
    has_pool: bool
    has_hot_tub: bool
    bedrooms: int = Field(ge=0, le=100)
    bathrooms: int | float = Field(ge=0, le=100)
    pets_allowed: bool


class KnowledgeEntry(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    service_line: Literal["homes", "design", "general"]
    kind: Literal["fact", "description", "policy", "navigation", "call_to_action"]
    title: str = Field(min_length=1, max_length=200)
    approved_text: str = Field(min_length=1, max_length=2_000)
    aliases: tuple[str, ...] = Field(default=(), max_length=24)
    topics: tuple[str, ...] = Field(min_length=1, max_length=24)
    route: str = Field(pattern=r"^[a-z_]{1,32}$")
    property_slug: str | None = Field(default=None, pattern=patterns.SUBJECT_ID_RE)
    property_facts: PropertyFacts | None = None
    source: Reference
    links: tuple[Reference, ...] = Field(default=(), max_length=8)
    effective_from: datetime
    effective_until: datetime | None = None
    direct_answer: bool = False

    @model_validator(mode="after")
    def _scope_and_time(self) -> KnowledgeEntry:
        if (self.route == "property") != (self.property_slug is not None):
            raise ValueError("property knowledge requires exactly one property slug")
        if (self.route == "property") != (self.property_facts is not None):
            raise ValueError("structured property facts belong to property knowledge only")
        if self.effective_from.tzinfo is None or (
            self.effective_until is not None and self.effective_until.tzinfo is None
        ):
            raise ValueError("effective times must be timezone-aware")
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("knowledge expiration must follow its effective time")
        return self

    def effective_at(self, observed_at: datetime) -> bool:
        return self.effective_from <= observed_at and (
            self.effective_until is None or observed_at < self.effective_until
        )


def canonical_corpus_digest(document: dict[str, object]) -> str:
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise KnowledgeUnavailable("knowledge corpus has no entries array")
    normalized: list[dict[str, object]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise KnowledgeUnavailable("knowledge entries must be objects")
        entry = dict(raw)
        for optional in ("property_slug", "property_facts", "effective_until"):
            entry.setdefault(optional, None)
        normalized.append(entry)
    normalized.sort(key=lambda item: str(item.get("id")))
    canonical = json.dumps(
        {"schema": document.get("schema"), "entries": normalized},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovedLink:
    id: str
    label: str
    url: str
    kind: Literal["open_internal_link", "contact_utopia"]


@dataclass(frozen=True, slots=True)
class EffectiveKnowledge:
    """The exact evidence set for one answer: effective, non-withdrawn entries at one instant."""

    release_id: str
    eligibility_token: str
    entries_by_id: dict[str, KnowledgeEntry]
    links_by_id: dict[str, ApprovedLink]

    @property
    def property_labels(self) -> dict[str, str]:
        """property_slug -> approved display label, for wrong-property checks."""
        labels: dict[str, str] = {}
        for entry in self.entries_by_id.values():
            if entry.property_slug is not None:
                labels.setdefault(entry.property_slug, entry.source.label)
        return labels

    def context_packet(self) -> str:
        packet_entries = []
        for entry in sorted(self.entries_by_id.values(), key=lambda item: item.id):
            packet_entries.append(
                {
                    "id": entry.id,
                    "service_line": entry.service_line,
                    "kind": entry.kind,
                    "title": entry.title,
                    "approved_text": entry.approved_text,
                    "aliases": list(entry.aliases),
                    "topics": list(entry.topics),
                    "route": entry.route,
                    "property_slug": entry.property_slug,
                    "property_facts": (
                        entry.property_facts.model_dump(mode="json")
                        if entry.property_facts is not None
                        else None
                    ),
                    "source_label": entry.source.label,
                    "links": [{"id": link.id, "label": link.label} for link in entry.links],
                }
            )
        return json.dumps(
            {"schema": CONTEXT_SCHEMA, "entries": packet_entries},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class KnowledgeProjection:
    def __init__(
        self,
        *,
        release_id: str,
        corpus_digest: str,
        entries: tuple[KnowledgeEntry, ...],
        withdrawn_ids: frozenset[str],
        approved_hostnames: frozenset[str],
    ) -> None:
        self.release_id = release_id
        self.corpus_digest = corpus_digest
        self._entries = entries
        self._withdrawn = withdrawn_ids
        self.approved_hostnames = approved_hostnames
        withdrawal_digest = hashlib.sha256(
            "\n".join(sorted(withdrawn_ids)).encode("utf-8")
        ).hexdigest()
        self.eligibility_token = f"{release_id}|{corpus_digest}|{withdrawal_digest}"

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        release_id: str,
        allowed_corpus_digests: frozenset[str],
        withdrawn_ids: frozenset[str],
        approved_hostnames: frozenset[str],
    ) -> KnowledgeProjection:
        try:
            document = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KnowledgeUnavailable("approved knowledge corpus cannot be read") from exc
        if not isinstance(document, dict) or document.get("schema") != KNOWLEDGE_SCHEMA:
            raise KnowledgeUnavailable("approved knowledge corpus has the wrong schema")
        if set(document) != {"schema", "entries"}:
            raise KnowledgeUnavailable("approved knowledge corpus has unknown members")

        digest = canonical_corpus_digest(document)
        if digest not in allowed_corpus_digests:
            raise KnowledgeUnavailable("knowledge corpus digest is not approved")

        try:
            entries = tuple(KnowledgeEntry.model_validate(raw) for raw in document["entries"])
        except ValidationError as exc:
            raise KnowledgeUnavailable("approved knowledge corpus is malformed") from exc
        if len({entry.id for entry in entries}) != len(entries):
            raise KnowledgeUnavailable("knowledge entry identifiers are not unique")
        unknown_withdrawals = withdrawn_ids - {entry.id for entry in entries}
        if unknown_withdrawals:
            raise KnowledgeUnavailable("withdrawal names an entry outside the corpus")

        projection = cls(
            release_id=release_id,
            corpus_digest=digest,
            entries=entries,
            withdrawn_ids=withdrawn_ids,
            approved_hostnames=approved_hostnames,
        )
        # Validate every destination and identifier once, at load, so no answer path ever has to
        # truncate, rewrite, or silently drop an approved-but-unrenderable reference.
        for entry in entries:
            projection._validate_reference(entry.source, max_label=patterns.SOURCE_TITLE_MAX)
            for link in entry.links:
                projection._validate_reference(link, max_label=patterns.ACTION_LABEL_MAX)
        return projection

    def _validate_reference(self, reference: Reference, *, max_label: int) -> None:
        if not _LOCAL_ID_RE.fullmatch(reference.id):
            raise KnowledgeUnavailable("approved reference id cannot form an RC2 identifier")
        if len(reference.label) > max_label:
            raise KnowledgeUnavailable("approved reference label exceeds its display bound")
        if not re.fullmatch(patterns.HTTPS_URL_RE, reference.href):
            raise KnowledgeUnavailable("approved reference URL is not an HTTPS URL")
        parsed = urlsplit(reference.href)
        if parsed.hostname not in self.approved_hostnames or parsed.fragment or parsed.username:
            raise KnowledgeUnavailable("approved reference URL uses an unapproved destination")

    def effective(self, observed_at: datetime) -> EffectiveKnowledge:
        entries = {
            entry.id: entry
            for entry in self._entries
            if entry.id not in self._withdrawn and entry.effective_at(observed_at)
        }
        if not entries:
            raise KnowledgeUnavailable("no effective public knowledge is available")
        links: dict[str, ApprovedLink] = {}
        for entry in sorted(entries.values(), key=lambda item: item.id):
            kind: Literal["open_internal_link", "contact_utopia"] = (
                "contact_utopia" if entry.kind == "call_to_action" else "open_internal_link"
            )
            for link in entry.links:
                candidate = ApprovedLink(link.id, link.label, link.href, kind)
                existing = links.get(link.id)
                if existing is not None and existing != candidate:
                    raise KnowledgeUnavailable("approved link identifier is ambiguous")
                links[link.id] = candidate
        return EffectiveKnowledge(
            release_id=self.release_id,
            eligibility_token=self.eligibility_token,
            entries_by_id=entries,
            links_by_id=links,
        )

    def approved_destinations(self) -> frozenset[str]:
        urls = set()
        for entry in self._entries:
            urls.add(entry.source.href)
            urls.update(link.href for link in entry.links)
        return frozenset(urls)
