"""FastAPI app factory for POST /business/v1/guest/answer (RC2 §§9-12, 17) plus /healthz.

Request pipeline order is load-bearing (see docs/implementation-notes.md and the top-level plan):
header preflight -> body size cap -> authenticate -> strict JSON decode (duplicate-key rejection)
-> schema validation + I-B01..I-B03 -> trim message.content -> canonical digest -> idempotency
decision -> (new_execution only) call the legacy upstream, build the response, and durably
complete the idempotency record.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from utopia_homes_prime.business_api.auth import AuthenticationFailure, KeyAllowlist, authenticate
from utopia_homes_prime.business_api.canonicalization import canonical_digest
from utopia_homes_prime.business_api.idempotency import IdempotencyScopeKey, IdempotencyStore
from utopia_homes_prime.business_api.jti_replay import JtiReplayStore
from utopia_homes_prime.business_api.logging_utils import (
    access_log,
    digest_idempotency_key,
    digest_session_id,
)
from utopia_homes_prime.config import (
    MEETING_DRAFT_BUDGET_MS,
    MEETING_RESPOND_BUDGET_MS,
    Config,
    ExecutionProfileConfig,
    HomesPrimeConfig,
)
from utopia_homes_prime.guest_answer import legacy_bridge, patterns, schema_validation
from utopia_homes_prime.guest_answer.bundle_tools import check_invariants_impl
from utopia_homes_prime.guest_answer.errors import (
    ERROR_CLASSES,
    AnswerValidationFailedError,
    AuthenticationFailedError,
    CapabilityForbiddenError,
    DeadlineExceededError,
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
from utopia_homes_prime.guest_answer.homes_prime import (
    GUEST_ATTEMPT_BUDGET_MS,
    ExecutionProfileSettings,
    HomesPrimeEngine,
    HomesPrimeSettings,
)
from utopia_homes_prime.guest_answer.models import (
    ErrorBodyV1,
    ErrorResponseV1,
    GuestAnswerResponseV1,
)
from utopia_homes_prime.inference.backend import InferenceBackend
from utopia_homes_prime.inference.direct_openrouter import (
    DirectOpenRouterBackend,
    DirectProviderSettings,
)
from utopia_homes_prime.inference.sme_client import (
    ExecutionIdentity,
    SharedModelExecutionClient,
    validate_endpoint_url,
)
from utopia_homes_prime.inference.tiamat import TiamatBackend
from utopia_homes_prime.knowledge.projection import KnowledgeProjection
from utopia_homes_prime.meeting_assist.meeting import InvalidRequest as MeetingInvalidRequest
from utopia_homes_prime.meeting_assist.meeting import (
    MeetingEngine,
    MeetingError,
    NotFound,
    OperationSettings,
)
from utopia_homes_prime.meeting_assist.meeting import TemporarilyUnavailable as MeetingUnavailable
from utopia_homes_prime.meeting_assist.meeting_api import (
    is_meeting_path,
    meeting_error_response,
    register_meeting_routes,
)
from utopia_homes_prime.meeting_assist.meeting_materials import MaterialRegistry

_ERROR_CLASS_BY_CODE = {cls.code: cls for cls in ERROR_CLASSES}

PREVIEW_MODE_HEADER_NAME = "X-Utopia-Preview-Mode"
PREVIEW_MODE_HEADER_VALUE = "legacy-bridge"
"""Diagnostic-only, out-of-band signal that this service is the preconformant compatibility stage
(delegates to the legacy FAQ engine) — see docs/guest-answer-preview-rollout.md. Deliberately NOT
part of RC2's wire contract and never referenced by schema_validation.py: RC2 §12.2 requires
`limitations[]` to stay customer-relevant and prohibits it from revealing internal provider,
prompt, policy, security, or infrastructure details, so this status is carried here instead, on
every response regardless of outcome (see the timing middleware below). Consumers must not read
this header into anything customer-facing or into analytics."""

HOMES_PRIME_PREVIEW_MODE_HEADER_VALUE = "homes-prime-candidate"
"""Stage 2 candidate marker. The Homes Prime engine is implemented locally against Shared Model
Execution RC1 but has no Tier B evidence, conformance acceptance, or activation approval, so it is
flagged out of band exactly like the legacy bridge."""

DEFAULT_RETRY_AFTER_SECONDS = 2
"""RC2 §17: every retryable error carries an integer Retry-After of 1-30 seconds."""


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
        headers["Retry-After"] = str(min(30, max(1, exc.retry_after_seconds)))
    elif exc.retryable:
        headers["Retry-After"] = str(DEFAULT_RETRY_AFTER_SECONDS)

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


def _load_homes_prime(config: Config) -> tuple[KnowledgeProjection, HomesPrimeSettings]:
    """Startup-time, fail-closed assembly of the Stage 2 candidate's Homes-owned inputs."""
    prime = config.homes_prime
    assert prime is not None
    settings = HomesPrimeSettings(
        generate=ExecutionProfileSettings(
            prime.generate.profile_id,
            prime.generate.ceiling_ms,
            prime.generate.max_output_tokens,
            prime.generate.max_cost_microusd,
        ),
        review=ExecutionProfileSettings(
            prime.review.profile_id,
            prime.review.ceiling_ms,
            prime.review.max_output_tokens,
            prime.review.max_cost_microusd,
        ),
        transit_allowance_ms=prime.transit_allowance_ms,
        prime_reserve_ms=prime.prime_reserve_ms,
    )
    if prime.tiamat is not None:
        validate_endpoint_url(prime.tiamat.url, allow_loopback_http=config.environment == "preview")
    projection = KnowledgeProjection.load(
        Path(prime.knowledge_path),
        release_id=config.knowledge_release_id,
        allowed_corpus_digests=prime.knowledge_allowed_digests,
        withdrawn_ids=prime.knowledge_withdrawn_ids,
        approved_hostnames=prime.approved_hostnames,
    )
    return projection, settings


def _profile_fields(profile: ExecutionProfileConfig) -> tuple[str, int, int, int]:
    return (
        profile.profile_id,
        profile.ceiling_ms,
        profile.max_output_tokens,
        profile.max_cost_microusd,
    )


def _inference_backend(
    prime: HomesPrimeConfig, http: httpx.AsyncClient, transit_allowance_ms: int
) -> InferenceBackend:
    """Homes selects its own backend. The direct route needs no Tiamat configuration at all."""
    if prime.tiamat is None:
        assert prime.direct is not None
        direct = prime.direct
        return DirectOpenRouterBackend(
            settings=DirectProviderSettings(
                api_key=direct.api_key,
                model=direct.model,
                allowed_providers=direct.allowed_providers,
                max_prompt_usd_per_million=direct.max_prompt_usd_per_million,
                max_completion_usd_per_million=direct.max_completion_usd_per_million,
                referer=direct.referer,
                transit_allowance_ms=transit_allowance_ms,
            ),
            http=http,
        )
    tiamat = prime.tiamat
    identity = ExecutionIdentity.from_pem(
        kid=tiamat.key_id,
        issuer=tiamat.issuer,
        subject=tiamat.subject,
        private_key_pem=tiamat.private_key_pem,
    )
    return TiamatBackend(
        SharedModelExecutionClient(
            endpoint_url=tiamat.url,
            identity=identity,
            http=http,
            transit_allowance_ms=transit_allowance_ms,
        )
    )


def create_app(
    *,
    config: Config,
    execution_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """`execution_transport` exists only so tests can route the selected inference backend
    client to an in-process fake; runtime wiring always uses the default network transport."""
    allowlist = KeyAllowlist(config.jwt_keys)
    homes_prime_parts = _load_homes_prime(config) if config.answer_engine == "homes-prime" else None
    preview_mode_value = (
        HOMES_PRIME_PREVIEW_MODE_HEADER_VALUE
        if config.answer_engine == "homes-prime"
        else PREVIEW_MODE_HEADER_VALUE
    )
    rate_limiter = _RateLimiter(config.rate_limit_per_minute)
    jti_replay_store = JtiReplayStore()
    # Startup fails closed unless every meeting material matches its allowlisted manifest.
    meeting_registry = (
        MaterialRegistry.load(
            Path(config.meeting.materials_path),
            allowed_manifest_digests=config.meeting.materials_allowed_digests,
        )
        if config.meeting is not None
        else None
    )
    idempotency_store = IdempotencyStore(
        ttl_seconds=config.idempotency_ttl_seconds,
        in_progress_ceiling_seconds=config.idempotency_in_progress_ceiling_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if homes_prime_parts is None:
            async with httpx.AsyncClient() as client:
                app.state.legacy_client = client
                app.state.homes_prime = None
                yield
            return

        prime = config.homes_prime
        assert prime is not None
        projection, settings = homes_prime_parts
        # trust_env=False: no ambient proxy or netrc configuration can intercept inference calls.
        async with httpx.AsyncClient(
            transport=execution_transport, trust_env=False, follow_redirects=False
        ) as inference_http:
            backend = _inference_backend(prime, inference_http, settings.transit_allowance_ms)
            app.state.legacy_client = None
            app.state.homes_prime = HomesPrimeEngine(
                settings=settings, projection=projection, backend=backend
            )
            if config.meeting is not None:
                app.state.meeting_engine = MeetingEngine(
                    respond=OperationSettings(
                        *_profile_fields(config.meeting.respond), MEETING_RESPOND_BUDGET_MS
                    ),
                    draft=OperationSettings(
                        *_profile_fields(config.meeting.draft), MEETING_DRAFT_BUDGET_MS
                    ),
                    transit_allowance_ms=settings.transit_allowance_ms,
                    reserve_ms=settings.prime_reserve_ms,
                    backend=backend,
                )
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
        # Applied centrally, to every response regardless of outcome or future new routes, so the
        # preconformant signal can never be forgotten on one code path while present on another.
        response.headers[PREVIEW_MODE_HEADER_NAME] = preview_mode_value
        return response

    @app.exception_handler(GuestAnswerError)
    async def _handle_guest_answer_error(request: Request, exc: GuestAnswerError) -> JSONResponse:
        return _error_response(request, exc)

    @app.exception_handler(MeetingError)
    async def _handle_meeting_error(request: Request, exc: MeetingError) -> JSONResponse:
        return meeting_error_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if config.meeting is not None and is_meeting_path(request.url.path):
            if exc.status_code == 405:
                return meeting_error_response(request, MeetingInvalidRequest())
            if exc.status_code == 404:
                return meeting_error_response(request, NotFound())
        if exc.status_code == 405:
            return _error_response(request, InvalidRequestError())
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers or {}
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        if config.meeting is not None and is_meeting_path(request.url.path):
            return meeting_error_response(request, MeetingUnavailable())
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
        engine: HomesPrimeEngine | None = request.app.state.homes_prime
        if engine is not None:
            eligibility_token = engine.eligibility_token()
        else:
            assert config.legacy_upstream is not None
            eligibility_token = config.legacy_upstream.snapshot_digest
        decision = await idempotency_store.decide_and_admit(
            scope_key,
            canonical_digest=digest,
            current_snapshot_digest=eligibility_token,
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

        if engine is not None:
            return await _answer_with_homes_prime(
                request,
                engine=engine,
                body=canonical_body,
                message_content=trimmed_content,
                scope_key=scope_key,
                eligibility_token=eligibility_token,
                request_id=preflight.request_id,
            )

        assert config.legacy_upstream is not None
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

    if config.meeting is not None:
        assert meeting_registry is not None
        meeting_limiter = _RateLimiter(config.rate_limit_per_minute)
        register_meeting_routes(
            app,
            config=config,
            allowlist=allowlist,
            jti_replay_store=jti_replay_store,
            registry=meeting_registry,
            rate_limit=meeting_limiter.check,
        )

    async def _answer_with_homes_prime(
        request: Request,
        *,
        engine: HomesPrimeEngine,
        body: dict[str, Any],
        message_content: str,
        scope_key: IdempotencyScopeKey,
        eligibility_token: str,
        request_id: str,
    ) -> JSONResponse:
        """One guest.answer attempt is one Homes Prime pipeline, hard-capped at RC2 §6's 15s.
        Every modeled outcome durably completes the idempotency record so a duplicate replays the
        same definitive result instead of starting a second paid pipeline (RC2 §11)."""
        error: GuestAnswerError
        try:
            answer_body = await asyncio.wait_for(
                engine.answer(body, message_content), timeout=GUEST_ATTEMPT_BUDGET_MS / 1000
            )
        except TimeoutError:
            error = DeadlineExceededError()
        except GuestAnswerError as exc:
            error = exc
        else:
            await idempotency_store.complete(
                scope_key,
                response={"kind": "success", "body": answer_body},
                snapshot_digest=eligibility_token,
                failed=False,
            )
            return _success_response(
                request, body=answer_body, request_id=request_id, config=config
            )

        await idempotency_store.complete(
            scope_key,
            response={"kind": "error", "code": error.code},
            snapshot_digest=eligibility_token,
            failed=False,
        )
        raise error

    return app
