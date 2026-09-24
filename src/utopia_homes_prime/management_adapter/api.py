"""FastAPI app factory for the four RC3 §§9-12 endpoints plus the §13 error envelope.

Every request runs through one `_preflight()` step before any endpoint-specific logic, so the
framing/auth/rate-limit rules apply identically to all four resources (§7, §13, §18 criteria
5/6/18). A single `_error_response()` builds every non-success body, guaranteeing the
correlation-id/request-id-echo/header rules (§13) can't drift between error paths.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from utopia_homes_prime.management_adapter import patterns
from utopia_homes_prime.management_adapter.auth import (
    AuthenticationFailure,
    KeyAllowlist,
    authenticate,
)
from utopia_homes_prime.management_adapter.config import REALM_ID, SYNTH_CLASS, SYNTH_ID, Config
from utopia_homes_prime.management_adapter.errors import (
    AuthenticationFailedError,
    InternalError,
    InvalidRequestError,
    ManagementAdapterError,
    MethodNotAllowedError,
    RateLimitedError,
    RequestTooLargeError,
)
from utopia_homes_prime.management_adapter.health import assemble_health
from utopia_homes_prime.management_adapter.logging_utils import access_log
from utopia_homes_prime.management_adapter.models import (
    CapabilitiesResponseV1,
    CapabilityEntryV1,
    ErrorBodyV1,
    ErrorResponseV1,
    IdentityResponseV1,
    ManagedSynthV1,
    ManagementProviderV1,
    VersionResponseV1,
    format_observed_at,
)

_MAX_HEADER_BYTES = 16 * 1024
_ACCEPT_JSON_RE = re.compile(r"application/json|\*/\*")


class _RateLimiter:
    """Fixed one-minute window per principal. Generous default; see implementation-notes.md."""

    def __init__(self, limit_per_minute: int) -> None:
        self._limit = limit_per_minute
        self._state: dict[str, tuple[int, int]] = {}

    def check(self, principal_kid: str) -> int | None:
        """Returns None if allowed, or the number of seconds to wait if rate-limited."""
        minute = int(time.time() // 60)
        window, count = self._state.get(principal_kid, (minute, 0))
        if window != minute:
            window, count = minute, 0
        count += 1
        self._state[principal_kid] = (window, count)
        if count > self._limit:
            return 60 - int(time.time() % 60)
        return None


@dataclass(frozen=True, slots=True)
class _RequestContext:
    request_id: str
    principal_kid: str


def _accept_ok(accept_header: str | None) -> bool:
    return accept_header is not None and bool(_ACCEPT_JSON_RE.search(accept_header))


async def _preflight(
    request: Request, *, allowlist: KeyAllowlist, rate_limiter: _RateLimiter
) -> _RequestContext:
    # 1. X-Request-ID is validated and stashed FIRST, so it is echoed on every later failure too.
    raw_request_id = request.headers.get("x-request-id")
    if raw_request_id is not None and re.fullmatch(patterns.UUID_V4_RE, raw_request_id):
        request.state.request_id = raw_request_id
    else:
        request.state.request_id = None
        raise InvalidRequestError()

    # 2. No query parameters are ever accepted.
    if request.query_params:
        raise InvalidRequestError()

    # 3. GET requests must not carry a body.
    content_length = request.headers.get("content-length")
    if content_length not in (None, "0"):
        raise InvalidRequestError()

    # 4. Reject requests whose headers exceed a bounded size.
    header_bytes = sum(len(k) + len(v) + 4 for k, v in request.headers.items())
    if header_bytes > _MAX_HEADER_BYTES:
        raise RequestTooLargeError()

    # 5. Accept must indicate JSON is acceptable.
    if not _accept_ok(request.headers.get("accept")):
        raise InvalidRequestError()

    # 6. Authenticate. Any failure -> one generic AuthenticationFailedError, no detail leaked.
    try:
        principal_kid = authenticate(request.headers.get("authorization"), allowlist=allowlist)
    except AuthenticationFailure:
        raise AuthenticationFailedError() from None

    # 7. Rate limit, keyed by the authenticated principal.
    retry_after = rate_limiter.check(principal_kid)
    if retry_after is not None:
        raise RateLimitedError(retry_after_seconds=max(1, retry_after))

    return _RequestContext(request_id=raw_request_id, principal_kid=principal_kid)


def _error_response(request: Request, exc: ManagementAdapterError) -> JSONResponse:
    correlation_id = str(uuid4())
    valid_request_id = getattr(request.state, "request_id", None)

    body = ErrorResponseV1(
        contract_version="1.0",
        error=ErrorBodyV1(
            code=exc.code,
            message=exc.default_message,
            correlation_id=correlation_id,
            request_id=valid_request_id,
            retryable=exc.retryable,
        ),
    )

    headers = {"Cache-Control": "no-store", "X-Correlation-ID": correlation_id}
    if valid_request_id is not None:
        headers["X-Request-ID"] = valid_request_id
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)

    access_log(
        endpoint=request.url.path,
        http_class=f"{exc.http_status // 100}xx",
        latency_ms=getattr(request.state, "latency_ms", 0.0),
        contract_version="1.0",
        request_id=valid_request_id,
        correlation_id=correlation_id,
        principal_id=getattr(request.state, "principal_kid", None),
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=body.model_dump(mode="json", exclude_none=True),
        headers=headers,
    )


def _success_response(
    request: Request,
    *,
    endpoint: str,
    model_dump: dict[str, object],
    request_id: str,
    release_id: str | None = None,
    status: str | None = None,
    reason_codes: list[str] | None = None,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "X-Request-ID": request_id}
    access_log(
        endpoint=endpoint,
        http_class="2xx",
        latency_ms=getattr(request.state, "latency_ms", 0.0),
        contract_version="1.0",
        synth_id=SYNTH_ID,
        release_id=release_id,
        status=status,
        reason_codes=reason_codes,
        request_id=request_id,
        principal_id=getattr(request.state, "principal_kid", None),
    )
    return JSONResponse(status_code=200, content=model_dump, headers=headers)


def create_app(*, config: Config) -> FastAPI:
    app = FastAPI(
        title="Utopia Homes Management Adapter",
        version=config.software_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    allowlist = KeyAllowlist(config.jwt_keys)
    rate_limiter = _RateLimiter(config.rate_limit_per_minute)

    @app.middleware("http")
    async def _timing_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        request.state.latency_ms = (time.perf_counter() - started) * 1000
        return response

    @app.exception_handler(ManagementAdapterError)
    async def _handle_management_error(
        request: Request, exc: ManagementAdapterError
    ) -> JSONResponse:
        return _error_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 405:
            return _error_response(request, MethodNotAllowedError())
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers or {},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return _error_response(request, InternalError())

    @app.get("/healthz")
    async def liveness() -> dict[str, str]:
        # Deliberately outside /management/v1 and unauthenticated: §5 permits a separate
        # process-liveness endpoint that "does not represent Homes business health."
        return {"status": "ok"}

    @app.get("/management/v1/identity")
    async def get_identity(request: Request) -> JSONResponse:
        ctx = await _preflight(request, allowlist=allowlist, rate_limiter=rate_limiter)
        request.state.principal_kid = ctx.principal_kid
        body = IdentityResponseV1(
            contract_version="1.0",
            observed_at=format_observed_at(datetime.now(UTC)),
            synth_id=SYNTH_ID,
            realm_id=REALM_ID,
            synth_class=SYNTH_CLASS,
            display_name=config.display_name,
        )
        return _success_response(
            request,
            endpoint="/management/v1/identity",
            model_dump=body.model_dump(mode="json", exclude_none=True),
            request_id=ctx.request_id,
        )

    @app.get("/management/v1/health")
    async def get_health(request: Request) -> JSONResponse:
        ctx = await _preflight(request, allowlist=allowlist, rate_limiter=rate_limiter)
        request.state.principal_kid = ctx.principal_kid
        body = assemble_health(config=config, observed_at=datetime.now(UTC))
        return _success_response(
            request,
            endpoint="/management/v1/health",
            model_dump=body.model_dump(mode="json", exclude_none=True),
            request_id=ctx.request_id,
            release_id=body.management_provider_release_id,
            status=body.status,
            reason_codes=list(body.reason_codes),
        )

    @app.get("/management/v1/version")
    async def get_version(request: Request) -> JSONResponse:
        ctx = await _preflight(request, allowlist=allowlist, rate_limiter=rate_limiter)
        request.state.principal_kid = ctx.principal_kid
        body = VersionResponseV1(
            contract_version="1.0",
            observed_at=format_observed_at(datetime.now(UTC)),
            management_provider=ManagementProviderV1(
                deployment_id=config.deployment_id,
                runtime_id=config.runtime_id,
                release_id=config.release_id,
                software_version=config.software_version,
                artifact_digest=config.artifact_digest,
                deployed_at=config.deployed_at,
            ),
            managed_synth=ManagedSynthV1(
                synth_id=SYNTH_ID,
                observed_release_id=config.managed_synth_observed_release_id,
            ),
            supported_management_contracts=["1.0"],
        )
        return _success_response(
            request,
            endpoint="/management/v1/version",
            model_dump=body.model_dump(mode="json", exclude_none=True),
            request_id=ctx.request_id,
            release_id=body.management_provider.release_id,
        )

    @app.get("/management/v1/capabilities")
    async def get_capabilities(request: Request) -> JSONResponse:
        ctx = await _preflight(request, allowlist=allowlist, rate_limiter=rate_limiter)
        request.state.principal_kid = ctx.principal_kid
        body = CapabilitiesResponseV1(
            contract_version="1.0",
            observed_at=format_observed_at(datetime.now(UTC)),
            capabilities=[
                CapabilityEntryV1(
                    capability_id=c.capability_id,
                    contract_version=c.contract_version,
                    state=c.state,
                    business_contract_id=c.business_contract_id,
                )
                for c in config.capabilities
            ],
        )
        return _success_response(
            request,
            endpoint="/management/v1/capabilities",
            model_dump=body.model_dump(mode="json", exclude_none=True),
            request_id=ctx.request_id,
        )

    return app
