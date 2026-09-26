"""Guest knowledge built live from the business core's property records.

When Ray corrects a property through Utopia Lucy, the record changes and the next guest answer is
built from it. There is no separate release step: an authorized edit to the record is the
approval. The digest check still runs on every rebuild, over exactly the corpus that was built.

If the records cannot be read, the last good knowledge keeps serving. Guest answering fails
closed only when no knowledge has ever been built.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from utopia_homes_prime.business_core.knowledge_items import KnowledgeItemStore
from utopia_homes_prime.business_core.records import public_feed
from utopia_homes_prime.business_core.store import PropertyStore
from utopia_homes_prime.knowledge.builder import build_corpus
from utopia_homes_prime.knowledge.projection import (
    EffectiveKnowledge,
    KnowledgeProjection,
    KnowledgeUnavailable,
    canonical_corpus_digest,
)

LIVE_RELEASE_PREFIX = "homes-knowledge:live"
_log = logging.getLogger(__name__)


class LiveKnowledgeProjection:
    def __init__(
        self,
        store: PropertyStore,
        base_document: dict[str, Any],
        *,
        approved_hostnames: frozenset[str],
        items: KnowledgeItemStore | None = None,
        refresh_seconds: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self._store = store
        self._items = items
        self._base = base_document
        self.approved_hostnames = approved_hostnames
        self._refresh_seconds = refresh_seconds
        self._monotonic = monotonic
        self._today = today
        self._lock = threading.Lock()
        self._current: KnowledgeProjection | None = None
        self._corpus: dict[str, Any] | None = None
        self._version: str | None = None
        self._checked_at = float("-inf")

    def _projection(self) -> KnowledgeProjection:
        with self._lock:
            now = self._monotonic()
            if self._current is not None and now - self._checked_at < self._refresh_seconds:
                return self._current
            try:
                version = self._store.version() + (
                    "|" + self._items.version() if self._items is not None else ""
                )
                if self._current is None or version != self._version:
                    feed = public_feed(self._store.all())
                    approved = (
                        [
                            i.model_dump(mode="json")
                            for i in self._items.search(
                                audiences=("public",), statuses=("active",), limit=100_000
                            )
                        ]
                        if self._items is not None
                        else []
                    )
                    corpus = build_corpus(feed, self._base, self._corpus, self._today(), approved)
                    digest = canonical_corpus_digest(corpus)
                    self._current = KnowledgeProjection.from_document(
                        corpus,
                        release_id=f"{LIVE_RELEASE_PREFIX}:{digest[:12]}",
                        allowed_corpus_digests=frozenset({digest}),
                        withdrawn_ids=frozenset(),
                        approved_hostnames=self.approved_hostnames,
                    )
                    self._corpus, self._version = corpus, version
                self._checked_at = now
            except Exception as exc:
                if self._current is None:
                    raise KnowledgeUnavailable("property records are unavailable") from exc
                _log.warning("live knowledge refresh failed; serving the last good knowledge")
            return self._current

    @property
    def release_id(self) -> str:
        return self._projection().release_id

    @property
    def eligibility_token(self) -> str:
        return self._projection().eligibility_token

    def effective(self, observed_at: datetime, *, enforce_cap: bool = True) -> EffectiveKnowledge:
        return self._projection().effective(observed_at, enforce_cap=enforce_cap)

    def approved_destinations(self) -> frozenset[str]:
        return self._projection().approved_destinations()
