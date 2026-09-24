"""A fake Shared Model Execution provider for Homes-side tests (SME RC1).

Deliberately independent of utopia_homes_prime.inference.sme_wire: request verification here
re-derives the RC1 rules (digest, `req` binding, JWT claims, `jti` replay, message ordering,
headers) with hashlib/base64/PyJWT directly, so a bug in the client's request construction cannot
be masked by the same bug in the verifier.

The fake is only as smart as Homes tests need: scripted per-profile behaviors, the idempotency
state machine the caller can observe (admission, long-poll duplicates, replay, conflict,
invalidation after a successor release), receipts, and response loss. It never calls a model.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

PATH = "/execution/v1/inference"
UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

ERROR_MESSAGES = {
    "invalid_request": (400, "The execution request is invalid.", False),
    "authentication_failed": (401, "Service authentication failed.", False),
    "execution_aborted": (409, "The execution ended before model dispatch.", False),
    "idempotency_conflict": (409, "The idempotency key conflicts with an earlier request.", False),
    "request_in_progress": (409, "The execution request is already in progress.", True),
    "idempotency_recovery_unavailable": (
        409,
        "The earlier execution result is no longer available.",
        False,
    ),
    "execution_outcome_unknown": (409, "The execution outcome could not be determined.", False),
    "execution_invalidated": (409, "The earlier execution result is no longer eligible.", False),
    "cost_ceiling_insufficient": (422, "The execution cost ceiling is insufficient.", False),
    "rate_limited": (429, "Execution capacity is temporarily limited.", True),
    "provider_response_invalid": (502, "The model returned an unusable result.", False),
    "output_limit_reached": (502, "The model reached its output limit.", False),
    "content_filtered": (502, "The model response was filtered.", False),
    "provider_execution_failed": (502, "The model execution failed.", False),
    "privacy_route_unavailable": (503, "No approved private execution route is available.", False),
    "state_store_unavailable": (503, "Execution state is temporarily unavailable.", True),
    "spending_authority_exhausted": (503, "Execution spending authority is unavailable.", False),
    "temporarily_unavailable": (503, "Model execution is temporarily unavailable.", True),
    "deadline_exceeded": (504, "Model execution exceeded its deadline.", False),
}

POST_RECORD_FAILED = {
    "provider_response_invalid",
    "output_limit_reached",
    "content_filtered",
    "provider_execution_failed",
}
"""Scripted post-dispatch errors: the fake commits them as terminal `failed` records."""
PRE_RECORD = {
    "rate_limited",
    "privacy_route_unavailable",
    "spending_authority_exhausted",
    "temporarily_unavailable",
    "state_store_unavailable",
    "cost_ceiling_insufficient",
}
"""Scripted pre-record errors: no idempotency record is created (RC1 §6.3 steps 10-14)."""


def b64url_sha256(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


@dataclass
class Behavior:
    """One scripted outcome for one new execution."""

    content: dict[str, Any] | None = None
    error: str | None = None
    delay_s: float = 0.0
    retry_after: int | None = None
    settle: str = "settled"


def success(content: dict[str, Any], **kwargs: Any) -> Behavior:
    return Behavior(content=content, **kwargs)


def error(code: str, **kwargs: Any) -> Behavior:
    return Behavior(error=code, **kwargs)


@dataclass
class Record:
    body_digest: str
    execution_id: str
    profile_id: str
    profile_release: str
    max_cost: int
    task: asyncio.Task[tuple[str, Behavior]]
    state: str = "dispatched"


@dataclass
class Attempt:
    request_id: str
    idempotency_key: str
    timeout_ms: int
    jti: str
    token: str
    profile_id: str
    body: bytes
    document: dict[str, Any]


@dataclass
class FakeSharedModelExecution:
    public_key_pem: str
    issuer: str
    subject: str = "stoin:synth:utopia-homes-prime"
    kid: str = "homes-prime-execution-test"
    scripts: dict[str, list[Behavior]] = field(default_factory=dict)
    releases: dict[str, str] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    records: dict[str, Record] = field(default_factory=dict)
    dispatches: dict[str, int] = field(default_factory=dict)
    seen_jti: set[str] = field(default_factory=set)
    lose_next_responses: int = 0

    def script(self, profile_id: str, *behaviors: Behavior) -> None:
        self.scripts.setdefault(profile_id, []).extend(behaviors)

    def activate_successor_release(self, profile_id: str) -> None:
        """Routine rollout: replay from the predecessor release becomes ineligible (RC1 §11)."""
        self.releases[profile_id] = f"profiles-successor-{uuid.uuid4().hex[:8]}"

    def release_for(self, profile_id: str) -> str:
        return self.releases.setdefault(profile_id, "profiles-test.1")

    # --- verification ---------------------------------------------------------------------------

    def _violation(self, message: str) -> None:
        self.violations.append(message)

    def _verify(self, headers: dict[str, str], body: bytes) -> dict[str, Any] | None:
        required = (
            "authorization",
            "x-request-id",
            "idempotency-key",
            "x-execution-timeout-ms",
            "x-content-sha256",
            "content-type",
            "accept",
        )
        for name in required:
            if name not in headers:
                self._violation(f"missing header {name}")
                return None
        if headers.get("accept-encoding") != "identity":
            self._violation("compression was not disabled")
        if not UUID_V4.fullmatch(headers["x-request-id"]) or not UUID_V4.fullmatch(
            headers["idempotency-key"]
        ):
            self._violation("request id or idempotency key is not a UUID v4")
        if headers["content-type"] != "application/json" or headers["accept"] != "application/json":
            self._violation("media type headers are wrong")
        timeout = headers["x-execution-timeout-ms"]
        if not timeout.isdigit() or not 1000 <= int(timeout) <= 18000:
            self._violation("X-Execution-Timeout-Ms out of bounds")
        if b64url_sha256(body) != headers["x-content-sha256"]:
            self._violation("X-Content-SHA256 does not match the body")

        token = headers["authorization"].removeprefix("Bearer ")
        try:
            header = jwt.get_unverified_header(token)
            claims = jwt.decode(
                token,
                self.public_key_pem,
                algorithms=["EdDSA"],
                audience="stoin:shared-model-execution",
                issuer=self.issuer,
                options={"require": ["iss", "sub", "aud", "iat", "nbf", "exp", "jti"]},
            )
        except jwt.PyJWTError as exc:
            self._violation(f"jwt rejected: {type(exc).__name__}")
            return None
        if header.get("kid") != self.kid or header.get("alg") != "EdDSA":
            self._violation("jwt header kid/alg is wrong")
        if claims.get("sub") != self.subject or claims.get("scope") != "inference.execute":
            self._violation("jwt subject or scope is wrong")
        if claims["exp"] - claims["iat"] > 300 or claims["nbf"] > claims["exp"]:
            self._violation("jwt lifetime is wrong")
        if claims["jti"] in self.seen_jti:
            self._violation("jti reused")
        self.seen_jti.add(claims["jti"])
        expected_req = b64url_sha256(
            f"POST\n{PATH}\n{headers['idempotency-key']}\n{headers['x-content-sha256']}".encode()
        )
        if claims.get("req") != expected_req:
            self._violation("jwt req binding is wrong")

        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._violation("body is not UTF-8 JSON")
            return None
        if set(document) != {"contract", "execution_profile_id", "messages", "output", "limits"}:
            self._violation("request members are wrong")
        if document.get("contract") != "stoin.inference.execute.request.v1":
            self._violation("request contract is wrong")
        roles = [m.get("role") for m in document.get("messages", [])]
        expected_roles = ["system"] + [
            "user" if i % 2 == 0 else "assistant" for i in range(len(roles) - 1)
        ]
        if roles != expected_roles or not roles or roles[-1] != "user":
            self._violation("message role ordering is wrong")
        if document.get("output", {}).get("mode") != "json_schema":
            self._violation("output mode is wrong")
        self.attempts.append(
            Attempt(
                request_id=headers["x-request-id"],
                idempotency_key=headers["idempotency-key"],
                timeout_ms=int(timeout) if timeout.isdigit() else -1,
                jti=claims["jti"],
                token=token,
                profile_id=document.get("execution_profile_id", ""),
                body=body,
                document=document,
            )
        )
        return document

    # --- responses ------------------------------------------------------------------------------

    def _headers(
        self, request_id: str, profile_id: str | None, *, error_body: bool
    ) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "cache-control": "no-store",
            "x-request-id": request_id,
            "x-stoin-execution-release": "execution-test.1",
            "x-stoin-execution-policy-release": "policy-test.1",
        }
        if error_body:
            headers["x-correlation-id"] = str(uuid.uuid4())
        return headers

    def _error(
        self,
        code: str,
        request_id: str,
        *,
        record: Record | None = None,
        state: str | None = None,
        settle: str = "settled",
        retry_after: int | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        status, message, retryable = ERROR_MESSAGES[code]
        document: dict[str, Any] = {
            "contract": "stoin.inference.execute.error.v1",
            "correlation_id": str(uuid.uuid4()),
            "request_id": request_id,
            "error": {"code": code, "message": message, "retryable": retryable},
        }
        if record is not None and state is not None:
            document["execution"] = {"execution_id": record.execution_id, "state": state}
            reserved = min(1000, record.max_cost)
            document["cost"] = (
                {
                    "reserved_microusd": reserved,
                    "settled_microusd": None,
                    "settlement_status": "pending_reconciliation",
                }
                if settle == "pending_reconciliation"
                else {
                    "reserved_microusd": reserved,
                    "settled_microusd": 0 if code == "execution_aborted" else reserved // 2,
                    "settlement_status": "settled",
                }
            )
        headers = self._headers(request_id, None, error_body=True)
        headers["x-correlation-id"] = document["correlation_id"]
        if retryable:
            headers["retry-after"] = str(retry_after or 1)
        return status, headers, json.dumps(document).encode()

    def _success(
        self, record: Record, behavior: Behavior, request_id: str, *, replayed: bool
    ) -> tuple[int, dict[str, str], bytes]:
        reserved = min(1000, record.max_cost)
        document = {
            "contract": "stoin.inference.execute.response.v1",
            "request_id": request_id,
            "execution_id": record.execution_id,
            "replayed": replayed,
            "execution_profile_id": record.profile_id,
            "profile_release_id": record.profile_release,
            "output": {"mode": "json_schema", "content": behavior.content},
            "finish_reason": "stop",
            "usage": {
                "input_tokens": 1200,
                "generated_tokens": 150,
                "output_tokens": 120,
                "reasoning_tokens": 30,
            },
            "cost": {
                "reserved_microusd": reserved,
                "settled_microusd": None if behavior.settle != "settled" else reserved // 3,
                "settlement_status": behavior.settle,
            },
        }
        return (
            200,
            self._headers(request_id, record.profile_id, error_body=False),
            json.dumps(document).encode(),
        )

    async def _run(self, behavior: Behavior) -> tuple[str, Behavior]:
        if behavior.delay_s:
            await asyncio.sleep(behavior.delay_s)
        return ("completed" if behavior.error is None else "failed"), behavior

    def _result_for(
        self, record: Record, request_id: str, *, replayed: bool
    ) -> tuple[int, dict[str, str], bytes]:
        state, behavior = record.task.result()
        record.state = state
        if state == "completed":
            if replayed and self.release_for(record.profile_id) != record.profile_release:
                return self._error(
                    "execution_invalidated", request_id, record=record, state="completed"
                )
            return self._success(record, behavior, request_id, replayed=replayed)
        assert behavior.error is not None
        return self._error(behavior.error, request_id, record=record, state="failed")

    async def handle(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        if path != PATH:
            return 404, {"content-type": "text/plain"}, b"not found"
        if method != "POST":
            return 405, {"content-type": "text/plain"}, b"method not allowed"
        request_id = headers.get("x-request-id", str(uuid.uuid4()))
        document = self._verify(headers, body)
        if document is None or self.violations:
            return self._error("invalid_request", request_id)

        key = headers["idempotency-key"]
        timeout_s = int(headers["x-execution-timeout-ms"]) / 1000
        digest = hashlib.sha256(body).hexdigest()
        record = self.records.get(key)

        if record is None:
            profile_id = document["execution_profile_id"]
            queue = self.scripts.get(profile_id, [])
            if not queue:
                self._violation(f"unscripted execution for {profile_id}")
                return self._error("invalid_request", request_id)
            behavior = queue.pop(0)
            if behavior.error in PRE_RECORD:
                return self._error(behavior.error, request_id, retry_after=behavior.retry_after)
            self.dispatches[profile_id] = self.dispatches.get(profile_id, 0) + 1
            record = Record(
                body_digest=digest,
                execution_id=str(uuid.uuid4()),
                profile_id=profile_id,
                profile_release=self.release_for(profile_id),
                max_cost=document["limits"]["max_cost_microusd"],
                task=asyncio.get_running_loop().create_task(self._run(behavior)),
            )
            self.records[key] = record
            replayed = False
        elif record.body_digest != digest:
            return self._error("idempotency_conflict", request_id)
        else:
            replayed = True

        try:
            await asyncio.wait_for(asyncio.shield(record.task), timeout=timeout_s)
        except TimeoutError:
            if replayed:
                return self._error("request_in_progress", request_id, retry_after=1)
            record.state = "outcome_unknown"
            status, response_headers, response_body = self._error(
                "deadline_exceeded",
                request_id,
                record=record,
                state="outcome_unknown",
                settle="pending_reconciliation",
            )
            return status, response_headers, response_body
        if record.state == "outcome_unknown":
            return self._error(
                "execution_outcome_unknown",
                request_id,
                record=record,
                state="outcome_unknown",
                settle="pending_reconciliation",
            )
        return self._result_for(record, request_id, replayed=replayed)

    # --- adapters -------------------------------------------------------------------------------

    def transport(self) -> httpx.AsyncBaseTransport:
        return _FakeTransport(self)


class _FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, fake: FakeSharedModelExecution) -> None:
        self._fake = fake

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        headers = {name.lower(): value for name, value in request.headers.items()}
        status, response_headers, response_body = await self._fake.handle(
            request.method, request.url.path, headers, body
        )
        if self._fake.lose_next_responses > 0:
            self._fake.lose_next_responses -= 1
            raise httpx.ReadError("simulated lost response", request=request)
        return httpx.Response(
            status,
            headers=response_headers,
            stream=httpx.ByteStream(response_body),
            request=request,
        )
