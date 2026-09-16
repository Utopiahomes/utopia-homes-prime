"""FastAPI app factory for POST /business/v1/guest/answer (RC2 §§9-12, 17) plus /healthz.

Request pipeline order is load-bearing (see docs/implementation-notes.md and the top-level plan):
header preflight -> body size cap -> authenticate -> strict JSON decode (duplicate-key rejection)
-> schema validation + I-B01..I-B03 -> trim message.content -> canonical digest -> idempotency
decision -> (new_execution only) call the legacy upstream, build the response, and durably
complete the idempotency record.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from guest_answer_provider import legacy_bridge, patterns, schema_validation
from guest_answer_provider.auth import AuthenticationFailure, KeyAllowlist, authenticate
from guest_answer_provider.bundle_tools import check_invariants_impl
from guest_answer_provider.canonicalization import canonical_digest
from guest_answer_provider.config import Config
from guest_answer_provider.errors import (
    ERROR_CLASSES,
    AnswerValidationFailedError,
    AuthenticationFailedError,
    CapabilityForbiddenError,
    GuestAnswerError,
    IdempotencyConflictError,
    IdempotencyRecoveryUnavailableError,
    InvalidRequestError,
    RateLimitedError,
    RequestInProgressError,
    RequestTooLargeError,
    ResponseInvalidatedError,
    TemporarilyUnavailableError,
)
from guest_answer_provider.idempotency import IdempotencyScopeKey, IdempotencyStore
from guest_answer_provider.jti_replay import JtiReplayStore
from guest_answer_provider.logging_utils import (
    access_log,
    digest_idempotency_key,
    digest_session_id,
)
from guest_answer_provider.models import ErrorBodyV1, ErrorResponseV1, GuestAnswerResponseV1

_ERROR_CLASS_BY_CODE = {cls.code: cls for cls in ERROR_CLASSES}


class _DuplicateMemberError(ValueError):
    pass


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMemberError(f"duplicate member name: {key}")
        result[key] = value
    return result


class _RateLimiter:
    """Fixed one-minute window per authenticated principal."""

    def __init__(self, limit_per_minute: int) -> None:
        self._limit = limit_per_minute
        self._state: dict[str, tuple[int, int]] = {}

    def check(self, principal_kid: str) -> int | None:
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
class _HeaderPreflight:
    request_id: str
    idempotency_key: str


def _accept_ok(accept_header: str | None) -> bool:
    """RC2 §8 headers.008 requires exactly `Accept: application/json` — unlike the (GET-only,
    browser/curl-convenience-tolerant) Management Contract adapter, this contract's own vector
    tests only accept the literal media type, not a `*/*` wildcard, so no leniency is added here."""
    if accept_header is None:
        return False
    return accept_header.split(",")[0].split(";")[0].strip().lower() == "application/json"


def _content_type_ok(content_type_header: str | None) -> bool:
    if content_type_header is None:
        return False
    return content_type_header.split(";")[0].strip().lower() == "application/json"


def _header_preflight(request: Request) -> _HeaderPreflight:
    raw_request_id = request.headers.get("x-request-id")
    if raw_request_id is not None and re.fullmatch(patterns.UUID_V4_RE, raw_request_id):
        request.state.request_id = raw_request_id
    else:
        request.state.request_id = None
        raise InvalidRequestError()

    raw_idempotency_key = request.headers.get("idempotency-key")
    if raw_idempotency_key is None or not re.fullmatch(patterns.UUID_V4_RE, raw_idempotency_key):
        raise InvalidRequestError()
    request.state.idempotency_key = raw_idempotency_key

    if request.query_params:
        raise InvalidRequestError()
    if not _accept_ok(request.headers.get("accept")):
        raise InvalidRequestError()
    if not _content_type_ok(request.headers.get("content-type")):
        raise InvalidRequestError()

    return _HeaderPreflight(request_id=raw_request_id, idempotency_key=raw_idempotency_key)


def _error_response(request: Request, exc: GuestAnswerError) -> JSONResponse:
    correlation_id = str(uuid4())
    valid_request_id = getattr(request.state, "request_id", None)

    body = ErrorResponseV1(
        contract_version="1.0",
        error=ErrorBodyV1(
            code=exc.code,  # type: ignore[arg-type]
            message=exc.default_message,
            correlation_id=correlation_id,
            retryable=exc.retryable,
        ),
    )

    headers = {"Cache-Control": "no-store", "X-Correlation-ID": correlation_id}
    if valid_request_id is not None:
        headers["X-Request-ID"] = valid_request_id
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)

    raw_idempotency_key = getattr(request.state, "idempotency_key", None)
    access_log(
        endpoint="/business/v1/guest/answer",
        http_class=f"{exc.http_status // 100}xx",
        latency_ms=getattr(request.state, "latency_ms", 0.0),
        request_id=valid_request_id,
        correlation_id=correlation_id,
        idempotency_key_digest=digest_idempotency_key(raw_idempotency_key)
        if raw_idempotency_key
        else None,
        error_category=exc.code,
        principal_kid=getattr(request.state, "principal_kid", None),
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


def _success_response(
    request: Request, *, body: dict[str, Any], request_id: str, config: Config
) -> JSONResponse:
    headers = {
        "Cache-Control": "no-store",
        "X-Request-ID": request_id,
        "X-Utopia-Business-Release": config.business_release_id,
        "X-Utopia-Knowledge-Release": config.knowledge_release_id,
    }
    raw_idempotency_key = getattr(request.state, "idempotency_key", None)
    access_log(
        endpoint="/business/v1/guest/answer",
        http_class="2xx",
        latency_ms=getattr(request.state, "latency_ms", 0.0),
        request_id=request_id,
        response_id=body.get("response_id"),
        idempotency_key_digest=digest_idempotency_key(raw_idempotency_key)
        if raw_idempotency_key
        else None,
        session_id_keyed_digest=digest_session_id(str(body.get("session_id", ""))),
        outcome=body.get("outcome"),
        principal_kid=getattr(request.state, "principal_kid", None),
    )
    return JSONResponse(status_code=200, content=body, headers=headers)


def create_app(*, config: Config) -> FastAPI:
    allowlist = KeyAllowlist(config.jwt_keys)
    rate_limiter = _RateLimiter(config.rate_limit_per_minute)
    jti_replay_store = JtiReplayStore()
    idempotency_store = IdempotencyStore(
        ttl_seconds=config.idempotency_ttl_seconds,
        in_progress_ceiling_seconds=config.idempotency_in_progress_ceiling_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient() as client:
            app.state.legacy_client = client
            yield

    app = FastAPI(
        title="Utopia Homes guest.answer Provider",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def _timing_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        request.state.latency_ms = (time.perf_counter() - started) * 1000
        return response

    @app.exception_handler(GuestAnswerError)
    async def _handle_guest_answer_error(request: Request, exc: GuestAnswerError) -> JSONResponse:
        return _error_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 405:
            return _error_response(request, InvalidRequestError())
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers or {}
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return _error_response(request, TemporarilyUnavailableError())

    @app.get("/healthz")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/business/v1/guest/answer")
    async def post_guest_answer(request: Request) -> JSONResponse:
        preflight = _header_preflight(request)

        raw_body = await request.body()
        if len(raw_body) > patterns.REQUEST_BODY_MAX_BYTES:
            raise RequestTooLargeError()

        try:
            principal = await authenticate(
                request.headers.get("authorization"),
                allowlist=allowlist,
                provider_environment=config.environment,
                jti_replay_store=jti_replay_store,
            )
        except AuthenticationFailure:
            raise AuthenticationFailedError() from None
        request.state.principal_kid = principal.kid

        if "guest.answer" not in principal.capabilities:
            raise CapabilityForbiddenError()

        retry_after = rate_limiter.check(principal.kid)
        if retry_after is not None:
            raise RateLimitedError(retry_after_seconds=max(1, retry_after))

        try:
            body = json.loads(raw_body, object_pairs_hook=_reject_duplicate_keys)
        except (ValueError, _DuplicateMemberError):
            raise InvalidRequestError() from None
        if not isinstance(body, dict):
            raise InvalidRequestError()

        try:
            schema_validation.validate_request(body)
        except schema_validation.SchemaValidationError:
            raise InvalidRequestError() from None

        try:
            check_invariants_impl.check_i_b01({"request": body})
            check_invariants_impl.check_i_b02({"request": body})
            check_invariants_impl.check_i_b03({"request": body})
        except AssertionError:
            raise InvalidRequestError() from None

        canonical_body = dict(body)
        message = dict(body["message"])
        trimmed_content = message["content"].strip()
        if not (
            patterns.MESSAGE_CONTENT_MIN <= len(trimmed_content) <= patterns.MESSAGE_CONTENT_MAX
        ):
            raise InvalidRequestError()
        message["content"] = trimmed_content
        canonical_body["message"] = message

        digest = canonical_digest(canonical_body)

        scope_key = IdempotencyScopeKey(
            principal_subject=principal.subject,
            environment=principal.environment,
            idempotency_key=preflight.idempotency_key,
        )
        decision = await idempotency_store.decide_and_admit(
            scope_key,
            canonical_digest=digest,
            current_snapshot_digest=config.legacy_upstream.snapshot_digest,
        )

        if decision == "idempotency_conflict":
            raise IdempotencyConflictError()
        if decision == "request_in_progress":
            raise RequestInProgressError()
        if decision == "idempotency_recovery_unavailable":
            raise IdempotencyRecoveryUnavailableError()
        if decision == "response_invalidated":
            raise ResponseInvalidatedError()
        if decision == "replay":
            cached = await idempotency_store.get_replay_response(scope_key)
            assert cached is not None
            if cached["kind"] == "success":
                return _success_response(
                    request,
                    body=cast(dict[str, Any], cached["body"]),
                    request_id=preflight.request_id,
                    config=config,
                )
            raise _ERROR_CLASS_BY_CODE[cast(str, cached["code"])]()

        assert decision == "new_execution"

        session_id = body["session_id"]
        turn_id = message["turn_id"]
        history = body.get("history", [])

        try:
            answer_text, limitations = await legacy_bridge.answer_via_legacy(
                trimmed_content,
                session_id,
                history_present=bool(history),
                config=config.legacy_upstream,
                client=request.app.state.legacy_client,
            )
        except AnswerValidationFailedError:
            await idempotency_store.complete(
                scope_key,
                response={"kind": "error", "code": "answer_validation_failed"},
                snapshot_digest=config.legacy_upstream.snapshot_digest,
                failed=False,
            )
            raise
        except TemporarilyUnavailableError:
            await idempotency_store.complete(
                scope_key,
                response={"kind": "error", "code": "temporarily_unavailable"},
                snapshot_digest=config.legacy_upstream.snapshot_digest,
                failed=False,
            )
            raise

        response_model = GuestAnswerResponseV1(
            contract_version="1.0",
            response_id=str(uuid4()),
            session_id=session_id,
            assistant_turn_id=turn_id,
            outcome="answered",
            answer=answer_text,
            sources=[],
            actions=[],
            limitations=limitations,
        )
        dumped = response_model.model_dump(mode="json")
        schema_validation.validate_response(dumped)

        await idempotency_store.complete(
            scope_key,
            response={"kind": "success", "body": dumped},
            snapshot_digest=config.legacy_upstream.snapshot_digest,
            failed=False,
        )

        return _success_response(
            request, body=dumped, request_id=preflight.request_id, config=config
        )

    return app
