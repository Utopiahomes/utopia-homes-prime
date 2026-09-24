"""Private `inference.execute@1.0` client used by Homes Prime (RC1 §§6-7, 12).

One `execute()` call is one logical execution: one Idempotency-Key and one exact body, with at
most one explicit retry that observes the original operation rather than authorizing another
dispatch. HTTP-library retries, redirects, and compression are all disabled; the only retry is
the state machine below.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from utopia_homes_prime.inference import sme_wire
from utopia_homes_prime.inference.backend import Deadline as Deadline
from utopia_homes_prime.inference.backend import FailureCategory, InferenceFailure
from utopia_homes_prime.inference.sme_wire import (
    ExecutionError,
    ExecutionSuccess,
    PreparedRequest,
    WireViolation,
)

_UNSUPPORTED_OUTPUT_CODES = frozenset(
    {
        "provider_response_invalid",
        "provider_response_too_large",
        "output_limit_reached",
        "content_filtered",
    }
)
RETRY_BACKOFF_MS = (250, 750)
TOKEN_LIFETIME_SECONDS = 60


ExecutionFailure = InferenceFailure
"""The Tiamat client raises the backend-neutral failure; this name is kept for RC1 readers."""


def classify_error_code(code: str) -> FailureCategory:
    if code == "deadline_exceeded":
        return "deadline"
    if code == "rate_limited":
        return "rate_limited"
    if code in _UNSUPPORTED_OUTPUT_CODES:
        return "unsupported_output"
    return "unavailable"


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    """Homes Prime's dedicated execution workload identity (RC1 §7.1). The key must never be
    shared with the guest.answer, Management Contract, deployment, or policy-signing keys."""

    kid: str
    issuer: str
    subject: str
    private_key: Ed25519PrivateKey

    @classmethod
    def from_pem(
        cls, *, kid: str, issuer: str, subject: str, private_key_pem: str
    ) -> ExecutionIdentity:
        loaded = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ValueError("execution identity key must be Ed25519")
        return cls(kid=kid, issuer=issuer, subject=subject, private_key=loaded)


def validate_endpoint_url(url: str, *, allow_loopback_http: bool) -> str:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("execution endpoint must not carry userinfo, query, or fragment")
    if parsed.path != sme_wire.EXECUTION_PATH:
        raise ValueError(f"execution endpoint path must be exactly {sme_wire.EXECUTION_PATH}")
    loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not (
        allow_loopback_http and loopback and parsed.scheme == "http"
    ):
        raise ValueError("execution endpoint must use HTTPS (loopback HTTP only in tests/preview)")
    return url


class SharedModelExecutionClient:
    def __init__(
        self,
        *,
        endpoint_url: str,
        identity: ExecutionIdentity,
        http: httpx.AsyncClient,
        transit_allowance_ms: int,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._identity = identity
        self._http = http
        self._transit_ms = transit_allowance_ms
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._rng = rng or random.SystemRandom()

    def sign_token(self, prepared: PreparedRequest) -> str:
        now = int(self._wall_clock())
        claims = {
            "iss": self._identity.issuer,
            "sub": self._identity.subject,
            "aud": sme_wire.JWT_AUDIENCE,
            "scope": sme_wire.JWT_SCOPE,
            "iat": now,
            "nbf": now,
            "exp": now + TOKEN_LIFETIME_SECONDS,
            "jti": str(uuid.uuid4()),
            "req": prepared.request_binding,
        }
        return jwt.encode(
            claims,
            self._identity.private_key,
            algorithm="EdDSA",
            headers={"kid": self._identity.kid, "typ": "JWT"},
        )

    async def execute(
        self, prepared: PreparedRequest, *, profile_ceiling_ms: int, deadline: Deadline
    ) -> ExecutionSuccess:
        previous_timeout_ms: int | None = None
        attempts = 0
        while True:
            remaining = deadline.remaining_ms(self._monotonic())
            timeout_ms = min(
                profile_ceiling_ms,
                sme_wire.TIMEOUT_HEADER_MAX_MS,
                remaining - self._transit_ms,
            )
            if previous_timeout_ms is not None:
                timeout_ms = min(timeout_ms, previous_timeout_ms)  # §6.1: never increase on retry
            if timeout_ms < sme_wire.TIMEOUT_HEADER_MIN_MS:
                raise ExecutionFailure("deadline", attempts=attempts)

            attempts += 1
            previous_timeout_ms = timeout_ms
            request_id = str(uuid.uuid4())
            outcome = await self._attempt(prepared, request_id=request_id, timeout_ms=timeout_ms)

            if isinstance(outcome, ExecutionSuccess):
                return outcome

            if isinstance(outcome, ExecutionError):
                if not outcome.retryable:
                    raise ExecutionFailure(
                        classify_error_code(outcome.code),
                        code=outcome.code,
                        retry_after_seconds=outcome.retry_after_seconds,
                        attempts=attempts,
                    )
                exhausted_category: FailureCategory = (
                    "deadline"
                    if outcome.code == "request_in_progress"
                    else classify_error_code(outcome.code)
                )
                delay_ms = (
                    outcome.retry_after_seconds * 1000
                    if outcome.retry_after_seconds is not None
                    else self._rng.randint(*RETRY_BACKOFF_MS)
                )
                failure = ExecutionFailure(
                    exhausted_category,
                    code=outcome.code,
                    retry_after_seconds=outcome.retry_after_seconds,
                    attempts=attempts,
                )
            else:
                # Transport failure: the response may have been lost after the provider
                # completed. The retry observes the original operation under the same key.
                exhausted_category = "deadline" if outcome == "timeout" else "unavailable"
                delay_ms = self._rng.randint(*RETRY_BACKOFF_MS)
                failure = ExecutionFailure(exhausted_category, attempts=attempts)

            if attempts >= 2:
                raise failure
            # §12: skip the retry unless the delay, transit allowance, and the retry's complete
            # profile ceiling all fit inside the remaining enclosing budget.
            remaining_after = deadline.remaining_ms(self._monotonic())
            if delay_ms + self._transit_ms + profile_ceiling_ms > remaining_after:
                raise failure
            await self._sleep(delay_ms / 1000)

    async def _attempt(
        self, prepared: PreparedRequest, *, request_id: str, timeout_ms: int
    ) -> ExecutionSuccess | ExecutionError | Literal["timeout", "transport"]:
        headers = sme_wire.attempt_headers(
            prepared, token=self.sign_token(prepared), request_id=request_id, timeout_ms=timeout_ms
        )
        total_seconds = (timeout_ms + self._transit_ms) / 1000
        try:
            status, response_headers, body = await asyncio.wait_for(
                self._send(prepared.body, headers, total_seconds), timeout=total_seconds
            )
        except (TimeoutError, httpx.TimeoutException):
            return "timeout"
        except httpx.HTTPError:
            return "transport"

        try:
            if status == 200:
                return sme_wire.parse_success(
                    status=status,
                    headers=response_headers,
                    body=body,
                    request_id=request_id,
                    prepared=prepared,
                )
            return sme_wire.parse_error(
                status=status, headers=response_headers, body=body, request_id=request_id
            )
        except WireViolation as exc:
            raise ExecutionFailure("unavailable", code=None) from exc

    async def _send(
        self, body: bytes, headers: dict[str, str], total_seconds: float
    ) -> tuple[int, dict[str, str], bytes]:
        request = self._http.build_request(
            "POST",
            self._endpoint_url,
            content=body,
            headers=headers,
            timeout=httpx.Timeout(total_seconds),
        )
        response = await self._http.send(request, stream=True, follow_redirects=False)
        try:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_raw():
                size += len(chunk)
                if size > sme_wire.RESPONSE_BODY_MAX_BYTES:
                    # Oversized bodies are malformed; never buffer beyond the bound.
                    raise ExecutionFailure("unavailable")
                chunks.append(chunk)
        finally:
            await response.aclose()
        lowered = {name.lower(): value for name, value in response.headers.items()}
        return response.status_code, lowered, b"".join(chunks)
