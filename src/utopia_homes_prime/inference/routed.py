"""Homes' model routing: a primary route plus fallback routes on other providers.

Each route is a complete backend (one model, its own provider list and price ceilings, the same
zero-data-retention and no-data-collection rules). A call starts on the first healthy route; if it
fails, or hasn't answered within `hedge_after_ms`, the next route starts too, and the first valid
answer wins (the others are cancelled). A route that keeps failing is skipped for a while, so a
provider outage costs one slow answer, not every answer. With a single route this is just that
route.

The rest rule (Ray, 2026-10-07): a strike is an error, a timeout, an invalid answer, or losing
the race to another route. Two strikes in a row and the route rests at the back of the line for
2 minutes. Then it gets a try: a miss sends it back for twice as long (4, 8, 16, 32, then at most
64 minutes); a success earns a second try, and two successes in a row restore it to its place
with the ladder reset.

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
    strikes: int = 0  # misses in a row while healthy
    level: int = 0  # 0 = healthy; n = resting 2**n minutes after its last miss
    open_until: float = 0.0  # resting until then (monotonic seconds)
    trial_wins: int = 0  # successes in a row since its rest ended


STRIKES_TO_REST = 2
FIRST_REST_LEVEL = 1  # 2 minutes
MAX_REST_LEVEL = 6  # 64 minutes


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
        minute_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not routes:
            raise ValueError("at least one route is required")
        self._routes = [Route(name, backend) for name, backend in routes]
        self._hedge_after = hedge_after_ms / 1000
        self._minute = minute_seconds
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
        health = route.health
        now = self._monotonic()
        if health.open_until > now:
            return  # resting (tried only because everything else failed): no change
        if health.level == 0:  # healthy
            if ok:
                health.strikes = 0
                return
            health.strikes += 1
            if health.strikes >= STRIKES_TO_REST:
                self._rest(route, FIRST_REST_LEVEL)
            return
        # Back from a rest, on trial.
        if ok:
            health.trial_wins += 1
            if health.trial_wins >= 2:
                route.health = _Health()
                _log.info("model route restored: route=%s", route.name)
            return
        self._rest(route, min(health.level + 1, MAX_REST_LEVEL))

    def _rest(self, route: Route, level: int) -> None:
        minutes = 2**level
        route.health = _Health(level=level, open_until=self._monotonic() + minutes * self._minute)
        _log.warning("model route resting: route=%s minutes=%d", route.name, minutes)

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
