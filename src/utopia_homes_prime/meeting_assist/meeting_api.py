"""HTTP routes for the Homes Dragon meeting operations.

Pipeline order mirrors guest.answer: header preflight -> body size cap -> authenticate (scope
`meeting.assist`) -> key capability -> rate limit -> strict JSON decode -> version and schema ->
exact material resolution -> idempotency -> one execution. Error bodies are exactly
`{"error": {"code", "retryable"}}` and never carry meeting content; access logs carry only the
allowlisted, content-free fields.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any, Final, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from utopia_homes_prime.business_api.auth import AuthenticationFailure, KeyAllowlist, authenticate
from utopia_homes_prime.business_api.canonicalization import canonical_digest
from utopia_homes_prime.business_api.idempotency import IdempotencyScopeKey, IdempotencyStore
from utopia_homes_prime.business_api.jti_replay import JtiReplayStore
from utopia_homes_prime.business_api.logging_utils import access_log, digest_idempotency_key
from utopia_homes_prime.config import (
    MEETING_DRAFT_BUDGET_MS,
    MEETING_RESPOND_BUDGET_MS,
    Config,
)
from utopia_homes_prime.guest_answer import patterns
from utopia_homes_prime.meeting_assist import meeting
from utopia_homes_prime.meeting_assist.meeting import MeetingEngine, MeetingError
from utopia_homes_prime.meeting_assist.meeting_materials import (
    MaterialNotPermitted,
    MaterialRegistry,
)

MEETING_PREFIX: Final = "/business/v1/meeting/"
IDENTITY_PATH: Final = "/business/v1/meeting/identity"
RESPOND_PATH: Final = "/business/v1/meeting/respond"
DRAFT_PATH: Final = "/business/v1/meeting/draft"
DEFAULT_RETRY_AFTER_SECONDS: Final = 2
IN_PROGRESS_CEILING_SECONDS: Final = MEETING_DRAFT_BUDGET_MS // 1000 + 5
"""The longest meeting call plus grace, before an in-progress record is treated as unresolved."""


def is_meeting_path(path: str) -> bool:
    return path.startswith(MEETING_PREFIX)


def meeting_error_response(request: Request, exc: MeetingError) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    request_id = getattr(request.state, "request_id", None)
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    if exc.retryable:
        seconds = exc.retry_after_seconds or DEFAULT_RETRY_AFTER_SECONDS
        headers["Retry-After"] = str(min(30, max(1, seconds)))
    key = getattr(request.state, "idempotency_key", None)
    access_log(
        endpoint=request.url.path,
        http_class=f"{exc.http_status // 100}xx",
        latency_ms=getattr(request.state, "latency_ms", 0.0),
        request_id=request_id,
        idempotency_key_digest=digest_idempotency_key(key) if key else None,
        error_category=exc.code,
        principal_kid=getattr(request.state, "principal_kid", None),
    )
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": {"code": exc.code, "retryable": exc.retryable}},
        headers=headers,
    )


def _success(request: Request, body: dict[str, Any]) -> JSONResponse:
    key = getattr(request.state, "idempotency_key", None)
    access_log(
        endpoint=request.url.path,
        http_class="2xx",
        latency_ms=getattr(request.state, "latency_ms", 0.0),
        request_id=request.state.request_id,
        response_id=body.get("response_id"),
        idempotency_key_digest=digest_idempotency_key(key) if key else None,
        outcome=body.get("outcome"),
        principal_kid=getattr(request.state, "principal_kid", None),
    )
    return JSONResponse(
        status_code=200,
        content=body,
        headers={"Cache-Control": "no-store", "X-Request-ID": request.state.request_id},
    )


def _media_type(value: str | None) -> str | None:
    return None if value is None else value.split(",")[0].split(";")[0].strip().lower()


def _preflight(request: Request, *, post: bool) -> None:
    request_id = request.headers.get("x-request-id")
    if request_id is None or not re.fullmatch(patterns.UUID_V4_RE, request_id):
        request.state.request_id = None
        raise meeting.InvalidRequest()
    request.state.request_id = request_id
    if request.query_params or _media_type(request.headers.get("accept")) != "application/json":
        raise meeting.InvalidRequest()
    if not post:
        return
    key = request.headers.get("idempotency-key")
    if key is None or not re.fullmatch(patterns.UUID_V4_RE, key):
        raise meeting.InvalidRequest()
    request.state.idempotency_key = key
    if _media_type(request.headers.get("content-type")) != "application/json":
        raise meeting.InvalidRequest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


def register_meeting_routes(
    app: FastAPI,
    *,
    config: Config,
    allowlist: KeyAllowlist,
    jti_replay_store: JtiReplayStore,
    registry: MaterialRegistry,
    rate_limit: Callable[[str], int | None],
) -> None:
    """`rate_limit(kid)` returns seconds to wait when the principal is over its limit."""
    assert config.meeting is not None
    idempotency = IdempotencyStore(
        ttl_seconds=config.meeting.idempotency_ttl_seconds,
        in_progress_ceiling_seconds=IN_PROGRESS_CEILING_SECONDS,
    )
    app.state.meeting_idempotency = idempotency

    async def admit(request: Request, *, post: bool, max_bytes: int = 0) -> bytes:
        _preflight(request, post=post)
        raw = await request.body()
        if post and len(raw) > max_bytes:
            raise meeting.RequestTooLarge()
        try:
            principal = await authenticate(
                request.headers.get("authorization"),
                allowlist=allowlist,
                provider_environment=config.environment,
                jti_replay_store=jti_replay_store,
                required_scope=meeting.REQUIRED_SCOPE,
            )
        except AuthenticationFailure:
            raise meeting.AuthenticationFailed() from None
        request.state.principal_kid = principal.kid
        request.state.principal_subject = principal.subject
        if meeting.REQUIRED_SCOPE not in principal.capabilities:
            raise meeting.CapabilityForbidden()
        retry_after = rate_limit(principal.kid)
        if retry_after is not None:
            raise meeting.RateLimited(retry_after_seconds=max(1, retry_after))
        return raw

    def decode(raw: bytes) -> Any:
        try:
            return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except ValueError:
            raise meeting.InvalidRequest() from None

    async def run_once(
        request: Request,
        *,
        operation: str,
        canonical: dict[str, Any],
        budget_ms: int,
        call: Any,
    ) -> JSONResponse:
        scope = IdempotencyScopeKey(
            principal_subject=request.state.principal_subject,
            environment=config.environment,
            idempotency_key=request.state.idempotency_key,
            operation=operation,
        )
        decision = await idempotency.decide_and_admit(
            scope,
            canonical_digest=canonical_digest(canonical),
            current_snapshot_digest=registry.manifest_sha256,
        )
        if decision == "idempotency_conflict":
            raise meeting.IdempotencyConflict()
        if decision == "request_in_progress":
            raise meeting.RequestInProgress()
        if decision in ("idempotency_recovery_unavailable", "response_invalidated"):
            raise meeting.IdempotencyRecoveryUnavailable()
        if decision == "replay":
            cached = await idempotency.get_replay_response(scope)
            assert cached is not None
            if cached["kind"] == "success":
                return _success(request, cast(dict[str, Any], cached["body"]))
            raise meeting.ERRORS_BY_CODE[cast(str, cached["code"])]()

        error: MeetingError
        try:
            body = await asyncio.wait_for(call(), timeout=budget_ms / 1000)
        except TimeoutError:
            error = meeting.DeadlineExceeded()
        except MeetingError as exc:
            error = exc
        else:
            await idempotency.complete(
                scope,
                response={"kind": "success", "body": body},
                snapshot_digest=registry.manifest_sha256,
                failed=False,
            )
            return _success(request, body)
        await idempotency.complete(
            scope,
            response={"kind": "error", "code": error.code},
            snapshot_digest=registry.manifest_sha256,
            failed=False,
        )
        raise error

    def resolve(requested: list[meeting.MaterialRef]) -> Any:
        try:
            return registry.resolve({"id": m.id, "version": m.version} for m in requested)
        except MaterialNotPermitted:
            raise meeting.MaterialNotPermittedError() from None

    @app.get(IDENTITY_PATH)
    async def meeting_identity(request: Request) -> JSONResponse:
        await admit(request, post=False)
        return _success(
            request,
            {
                "contract_version": meeting.CONTRACT_VERSION,
                "synth_id": meeting.SYNTH_ID,
                "display_name": meeting.DISPLAY_NAME,
                "software_version": config.business_release_id,
                "approved_materials": registry.identity_entries(),
            },
        )

    @app.post(RESPOND_PATH)
    async def meeting_respond(request: Request) -> JSONResponse:
        raw = await admit(request, post=True, max_bytes=meeting.RESPOND_BODY_MAX_BYTES)
        parsed: meeting.RespondRequest = meeting.parse_request(decode(raw), meeting.RespondRequest)
        materials = resolve(parsed.materials)
        engine: MeetingEngine = request.app.state.meeting_engine
        canonical = parsed.model_dump(mode="json")
        canonical["message"] = parsed.message.strip()
        return await run_once(
            request,
            operation="meeting.respond",
            canonical=canonical,
            budget_ms=MEETING_RESPOND_BUDGET_MS,
            call=lambda: engine.respond(parsed, materials),
        )

    @app.post(DRAFT_PATH)
    async def meeting_draft(request: Request) -> JSONResponse:
        raw = await admit(request, post=True, max_bytes=meeting.DRAFT_BODY_MAX_BYTES)
        parsed: meeting.DraftRequest = meeting.parse_request(decode(raw), meeting.DraftRequest)
        materials = resolve(parsed.materials)
        engine: MeetingEngine = request.app.state.meeting_engine
        return await run_once(
            request,
            operation="meeting.draft",
            canonical=parsed.model_dump(mode="json"),
            budget_ms=MEETING_DRAFT_BUDGET_MS,
            call=lambda: engine.draft(parsed, materials),
        )
