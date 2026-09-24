"""Homes' own inference route: one exact OpenRouter model with zero-data-retention,
data-collection-denied, no-fallback routing and per-token price ceilings.

Adapted from cloud-hermes-lucy `src/lucy/public_openrouter.py` (ba461b7), the adapter the legacy
Public Lucy service already uses in production, onto Homes' InferenceBackend seam: async,
deadline-bounded, a bounded streaming read, and Homes' own re-validation of the returned content
against the requested schema. It needs no Tiamat account, grant, service, or network connection.

Deliberately simple (RC1-style recovery and accounting are not copied here): one attempt, no
retry, and a per-call cost check against the incurred charge OpenRouter reports.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from utopia_homes_prime.inference.backend import Deadline, InferenceCall, InferenceFailure
from utopia_homes_prime.inference.structured_output import OutputViolation, validate_instance

ENDPOINT: Final = "https://openrouter.ai/api/v1/chat/completions"
REQUEST_MAX_BYTES: Final = 262_144
RESPONSE_MAX_BYTES: Final = 1_000_000
MINIMUM_TIMEOUT_MS: Final = 1_000


@dataclass(frozen=True, slots=True)
class DirectProviderSettings:
    api_key: str
    model: str
    allowed_providers: tuple[str, ...]
    max_prompt_usd_per_million: float
    max_completion_usd_per_million: float
    referer: str
    transit_allowance_ms: int


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cost: float = Field(ge=0)


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    content: str = Field(min_length=1)


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    message: _Message
    finish_reason: str | None = None


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    choices: tuple[_Choice, ...] = Field(min_length=1)
    usage: _Usage


class DirectOpenRouterBackend:
    name: Final = "direct-openrouter"

    def __init__(
        self,
        *,
        settings: DirectProviderSettings,
        http: httpx.AsyncClient,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http = http
        self._monotonic = monotonic

    def request_body(self, call: InferenceCall) -> dict[str, Any]:
        settings = self._settings
        provider: dict[str, Any] = {
            "zdr": True,
            "data_collection": "deny",
            "allow_fallbacks": False,
            "max_price": {
                "prompt": settings.max_prompt_usd_per_million,
                "completion": settings.max_completion_usd_per_million,
            },
        }
        if settings.allowed_providers:
            provider["only"] = list(settings.allowed_providers)
        body: dict[str, Any] = {
            "model": settings.model,
            "messages": [{"role": m.role, "content": m.content} for m in call.messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": call.output.name,
                    "strict": True,
                    "schema": call.output.schema,
                },
            },
            "provider": provider,
        }
        if settings.model.startswith("openai/gpt-5"):
            body["max_completion_tokens"] = call.max_output_tokens
            body["reasoning"] = {"effort": "low", "exclude": True}
        else:
            body["max_tokens"] = call.max_output_tokens
        return body

    async def infer(self, call: InferenceCall, *, deadline: Deadline) -> dict[str, Any]:
        remaining = deadline.remaining_ms(self._monotonic())
        timeout_ms = min(call.ceiling_ms, remaining - self._settings.transit_allowance_ms)
        if timeout_ms < MINIMUM_TIMEOUT_MS:
            raise InferenceFailure("deadline")
        raw = json.dumps(
            self.request_body(call), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(raw) > REQUEST_MAX_BYTES:
            raise InferenceFailure("too_large")

        seconds = timeout_ms / 1000
        try:
            status, retry_after, data = await asyncio.wait_for(
                self._send(raw, seconds), timeout=seconds
            )
        except (TimeoutError, httpx.TimeoutException):
            raise InferenceFailure("deadline", code="timeout", attempts=1) from None
        except httpx.HTTPError:
            raise InferenceFailure("unavailable", code="transport", attempts=1) from None

        if status == 429:
            raise InferenceFailure(
                "rate_limited", code="http_429", retry_after_seconds=retry_after, attempts=1
            )
        if status != 200:
            raise InferenceFailure("unavailable", code=f"http_{status}", attempts=1)
        try:
            response = _Response.model_validate_json(data)
        except (ValidationError, ValueError):
            raise InferenceFailure("unsupported_output", code="response_invalid") from None
        if response.model != self._settings.model:
            raise InferenceFailure("unavailable", code="model_mismatch")
        if math.ceil(response.usage.cost * 1_000_000) > call.max_cost_microusd:
            raise InferenceFailure("unavailable", code="cost_exceeded")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise InferenceFailure("unsupported_output", code="output_limit_reached")
        try:
            content = json.loads(choice.message.content)
        except ValueError:
            raise InferenceFailure("unsupported_output", code="content_not_json") from None
        if not isinstance(content, dict):
            raise InferenceFailure("unsupported_output", code="content_not_object")
        try:
            # Homes re-validates regardless of what the provider claims to enforce.
            validate_instance(call.output.schema, content)
        except OutputViolation:
            raise InferenceFailure("unsupported_output", code="schema_mismatch") from None
        return content

    async def _send(self, body: bytes, seconds: float) -> tuple[int, int | None, bytes]:
        settings = self._settings
        request = self._http.build_request(
            "POST",
            ENDPOINT,
            content=body,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "HTTP-Referer": settings.referer,
                "X-Title": "Utopia Homes Prime",
            },
            timeout=httpx.Timeout(seconds),
        )
        response = await self._http.send(request, stream=True, follow_redirects=False)
        try:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > RESPONSE_MAX_BYTES:
                    raise InferenceFailure("unsupported_output", code="response_too_large")
                chunks.append(chunk)
        finally:
            await response.aclose()
        return (
            response.status_code,
            _retry_after(response.headers.get("retry-after")),
            b"".join(chunks),
        )


def _retry_after(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    return min(30, max(1, int(value)))
