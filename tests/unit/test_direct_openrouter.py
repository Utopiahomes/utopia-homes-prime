"""Homes' direct inference route: the request it sends and how every provider outcome maps onto
the backend-neutral failure categories. Adapted from cloud-hermes-lucy's public_openrouter tests'
intent (exact model, ZDR routing, price ceilings, bounded charge), for the async seam."""

from __future__ import annotations

import dataclasses
import time

import httpx
import pytest
from fixtures.fake_openrouter import API_KEY, MODEL, FakeOpenRouter, Reply

from utopia_homes_prime.inference.backend import Deadline, InferenceCall, InferenceFailure
from utopia_homes_prime.inference.direct_openrouter import (
    DirectOpenRouterBackend,
    DirectProviderSettings,
)
from utopia_homes_prime.inference.structured_output import ExecutionMessage, JsonSchemaOutput

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "maxLength": 50}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _call(**changes) -> InferenceCall:
    values = {
        "route": "utopia-homes.public-answer.generate.v1",
        "messages": (ExecutionMessage("system", "policy"), ExecutionMessage("user", "hello")),
        "output": JsonSchemaOutput("homes-test-output", SCHEMA),
        "max_output_tokens": 300,
        "max_cost_microusd": 2_000,
        "ceiling_ms": 3_000,
    }
    values.update(changes)
    return InferenceCall(**values)


def _backend(fake: FakeOpenRouter, http: httpx.AsyncClient, model: str = MODEL):
    return DirectOpenRouterBackend(
        settings=DirectProviderSettings(
            api_key=API_KEY,
            model=model,
            allowed_providers=("fireworks",),
            max_prompt_usd_per_million=0.2,
            max_completion_usd_per_million=0.8,
            referer="https://www.utopiahomes.com",
            transit_allowance_ms=100,
        ),
        http=http,
    )


async def _infer(fake: FakeOpenRouter, call: InferenceCall | None = None, **kwargs):
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        return await _backend(fake, http, **kwargs).infer(
            call or _call(), deadline=Deadline(time.monotonic() + 10)
        )


async def test_request_carries_the_routing_guarantees():
    fake = FakeOpenRouter()
    fake.script("homes-test-output", Reply({"answer": "hi"}))
    assert await _infer(fake) == {"answer": "hi"}
    assert fake.violations == []
    body = fake.requests[0]["body"]
    assert body["model"] == MODEL
    assert body["provider"] == {
        "zdr": True,
        "data_collection": "deny",
        "allow_fallbacks": False,
        "max_price": {"prompt": 0.2, "completion": 0.8},
        "only": ["fireworks"],
    }
    assert body["response_format"]["json_schema"] == {
        "name": "homes-test-output",
        "strict": True,
        "schema": SCHEMA,
    }
    assert body["max_tokens"] == 300
    assert fake.requests[0]["headers"]["x-title"] == "Utopia Homes Prime"


async def test_reasoning_models_use_completion_token_limits():
    fake = FakeOpenRouter()
    fake.script("homes-test-output", Reply({"answer": "hi"}))
    await _infer(fake, model="openai/gpt-5-mini")
    body = fake.requests[0]["body"]
    assert body["max_completion_tokens"] == 300 and "max_tokens" not in body
    assert body["reasoning"] == {"effort": "low", "exclude": True}


@pytest.mark.parametrize(
    ("reply", "category", "code"),
    [
        (Reply(status=429, retry_after="7"), "rate_limited", "http_429"),
        (Reply(status=502), "unavailable", "http_502"),
        (Reply({"answer": "hi"}, model="other/model"), "unavailable", "model_mismatch"),
        (Reply({"answer": "hi"}, cost_usd=0.01), "unavailable", "cost_exceeded"),
        (
            Reply({"answer": "hi"}, finish_reason="length"),
            "unsupported_output",
            "output_limit_reached",
        ),
        (Reply("not json"), "unsupported_output", "content_not_json"),
        (Reply("[1, 2]"), "unsupported_output", "content_not_object"),
        (Reply({"answer": "x" * 51}), "unsupported_output", "schema_mismatch"),
        (Reply({"answer": "hi", "extra": 1}), "unsupported_output", "schema_mismatch"),
        (Reply(raw=b"{bad"), "unsupported_output", "response_invalid"),
    ],
)
async def test_provider_outcomes_map_to_categories(reply, category, code):
    fake = FakeOpenRouter()
    fake.script("homes-test-output", reply)
    with pytest.raises(InferenceFailure) as failure:
        await _infer(fake)
    assert (failure.value.category, failure.value.code) == (category, code)
    if category == "rate_limited":
        assert failure.value.retry_after_seconds == 7


async def test_no_time_left_is_a_deadline_before_any_request():
    fake = FakeOpenRouter()
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        with pytest.raises(InferenceFailure) as failure:
            await _backend(fake, http).infer(_call(), deadline=Deadline(time.monotonic() + 0.5))
    assert failure.value.category == "deadline"
    assert fake.requests == []


async def test_oversized_requests_are_refused_locally():
    fake = FakeOpenRouter()
    huge = (ExecutionMessage("system", "p"), ExecutionMessage("user", "x" * 300_000))
    with pytest.raises(InferenceFailure) as failure:
        await _infer(fake, _call(messages=huge))
    assert failure.value.category == "too_large"
    assert fake.requests == []


async def test_transport_failure_is_unavailable():
    class Broken(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("down", request=request)

    async with httpx.AsyncClient(transport=Broken()) as http:
        with pytest.raises(InferenceFailure) as failure:
            await _backend(FakeOpenRouter(), http).infer(
                _call(), deadline=Deadline(time.monotonic() + 10)
            )
    assert (failure.value.category, failure.value.code) == ("unavailable", "transport")


async def test_a_search_route_asks_openrouter_for_one_web_search():
    fake = FakeOpenRouter()
    fake.script("homes-test-output", Reply({"answer": "hi"}))
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        backend = _backend(fake, http)
        backend._settings = dataclasses.replace(
            backend._settings, web_search_engine="native", web_search_max_results=3
        )
        await backend.infer(_call(), deadline=Deadline(time.monotonic() + 10))
    assert fake.requests[0]["body"]["plugins"] == [
        {"id": "web", "engine": "native", "max_results": 3}
    ]
