"""In-memory jti replay rejection (RC2 §7: "jti replay rejected").

Single-process, matching the bundle's documented single-coordinator v1.0 replica profile (the
same scope the idempotency store operates under — see idempotency.py). TTL = the maximum possible
token lifetime the JWT layer will ever accept (300s max lifetime + 30s clock skew = 330s); a jti
older than that could never pass auth.py's own exp check anyway, so it's safe to forget.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from utopia_homes_prime.guest_answer.patterns import (
    JWT_CLOCK_SKEW_SECONDS,
    JWT_MAX_LIFETIME_SECONDS,
)

TTL_SECONDS = JWT_MAX_LIFETIME_SECONDS + JWT_CLOCK_SKEW_SECONDS


class JtiReplayStore:
    def __init__(
        self, *, ttl_seconds: float = TTL_SECONDS, now: Callable[[], float] = time.monotonic
    ) -> None:
        self._seen: dict[str, float] = {}
        self._ttl_seconds = ttl_seconds
        self._now = now
        self._lock = asyncio.Lock()

    async def check_and_record(self, jti: str) -> bool:
        """Returns True if `jti` is new (and records it), False if it's a replay."""
        async with self._lock:
            now = self._now()
            expired = [key for key, deadline in self._seen.items() if deadline <= now]
            for key in expired:
                del self._seen[key]

            if jti in self._seen:
                return False

            self._seen[jti] = now + self._ttl_seconds
            return True
