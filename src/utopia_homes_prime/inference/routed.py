"""Homes' model routing: a primary route plus fallback routes on other providers.

Each route is a complete backend (one model, its own provider list and price ceilings, the same
zero-data-retention and no-data-collection rules). A call starts on the first healthy route; if it
fails, or hasn't answered within `hedge_after_ms`, the next route starts too, and the first valid
answer wins (the others are cancelled). A route that keeps failing is skipped for a while, so a
provider outage costs one slow answer, not every answer. With a single route this is just that
route.

2026-10-07: Google Vertex slowed down for hours and every meeting answer timed out; nothing outside
Google could take over.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from utopia_homes_prime.inference.backend import (
    Deadline,
    InferenceBackend,
    InferenceCall,
    InferenceFailure,
)

_log = logging.getLogger(__name__)


@dataclass
class _Health:
    failures: int = 0
    open_until: float = 0.0


@dataclass
class Route:
    name: str
    backend: InferenceBackend
    health: _Health = field(default_factory=_Health)


class RoutedBackend:
    def __init__(
        self,
        routes: Sequence[tuple[str, InferenceBackend]],
        *,
        hedge_after_ms: int,
        breaker_failures: int = 3,
        breaker_seconds: float = 120.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not routes:
            raise ValueError("at least one route is required")
        self._routes = [Route(name, backend) for name, backend in routes]
        self._hedge_after = hedge_after_ms / 1000
        self._breaker_failures = breaker_failures
        self._breaker_seconds = breaker_seconds
        self._monotonic = monotonic

    @property
    def name(self) -> str:
        return "routed"

    def _order(self) -> list[Route]:
        """Healthy routes first, in configured order; routes resting after failures go last, so
        they still get a turn when everything else fails."""
        now = self._monotonic()
        healthy = [r for r in self._routes if r.health.open_until <= now]
        resting = [r for r in self._routes if r.health.open_until > now]
        return healthy + resting

    def _record(self, route: Route, ok: bool) -> None:
        if ok:
            route.health = _Health()
            return
        route.health.failures += 1
        if route.health.failures >= self._breaker_failures:
            route.health.open_until = self._monotonic() + self._breaker_seconds
            _log.warning(
                "model route resting after failures: route=%s failures=%d seconds=%d",
                route.name,
                route.health.failures,
                int(self._breaker_seconds),
            )

    async def infer(self, call: InferenceCall, *, deadline: Deadline) -> dict[str, Any]:
        order = self._order()
        running: dict[asyncio.Task[dict[str, Any]], Route] = {}
        last: InferenceFailure | None = None
        next_index = 0

        def start_next() -> bool:
            nonlocal next_index
            if next_index >= len(order):
                return False
            route = order[next_index]
            next_index += 1
            task = asyncio.ensure_future(route.backend.infer(call, deadline=deadline))
            running[task] = route
            return True

        start_next()
        try:
            while running:
                more_waiting = next_index < len(order)
                done, _ = await asyncio.wait(
                    running,
                    timeout=self._hedge_after if more_waiting else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    # Nothing back in time: start the next route alongside the slow one.
                    _log.info("model route hedged: started=%s", order[next_index].name)
                    start_next()
                    continue
                for task in done:
                    route = running.pop(task)
                    try:
                        result = task.result()
                    except InferenceFailure as failure:
                        if failure.category == "too_large":
                            raise  # the request itself is too big; another model won't help
                        self._record(route, ok=False)
                        last = failure
                        _log.info(
                            "model route failed: route=%s category=%s code=%s",
                            route.name,
                            failure.category,
                            failure.code,
                        )
                        if not running:
                            start_next()
                        continue
                    self._record(route, ok=True)
                    # A route still running lost the race: count it, so a consistently slow route
                    # rests too and later calls don't wait for it before trying the next.
                    for loser in running.values():
                        self._record(loser, ok=False)
                    if route is not self._routes[0]:
                        _log.info("model route answered by fallback: route=%s", route.name)
                    return result
            raise last or InferenceFailure("unavailable", code="no_route")
        finally:
            for task in running:
                task.cancel()
