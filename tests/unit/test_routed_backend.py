"""Model routing: fallbacks on other providers take over when the primary is slow or down."""

from __future__ import annotations

import asyncio
import time

import pytest

from utopia_homes_prime.inference.backend import Deadline, InferenceCall, InferenceFailure
from utopia_homes_prime.inference.routed import RoutedBackend
from utopia_homes_prime.inference.structured_output import JsonSchemaOutput


class Fake:
    def __init__(self, name: str, *, delay: float = 0.0, fail: str | None = None) -> None:
        self.name = name
        self.delay = delay
        self.fail = fail
        self.calls = 0

    async def infer(self, call, *, deadline):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise InferenceFailure(self.fail, code="test")  # type: ignore[arg-type]
        return {"from": self.name}


def call() -> InferenceCall:
    return InferenceCall(
        route="test",
        messages=(),
        output=JsonSchemaOutput(name="x", schema={"type": "object"}),
        max_output_tokens=10,
        max_cost_microusd=10,
        ceiling_ms=6000,
    )


def run(backend: RoutedBackend) -> dict:
    deadline = Deadline(time.monotonic() + 5)
    return asyncio.run(backend.infer(call(), deadline=deadline))


def test_a_fast_primary_answers_alone():
    primary, fallback = Fake("gemini"), Fake("gpt")
    backend = RoutedBackend([("gemini", primary), ("gpt", fallback)], hedge_after_ms=200)
    assert run(backend) == {"from": "gemini"}
    assert fallback.calls == 0


def test_a_slow_primary_is_hedged_and_the_fallback_wins():
    primary, fallback = Fake("gemini", delay=1.0), Fake("gpt", delay=0.01)
    backend = RoutedBackend([("gemini", primary), ("gpt", fallback)], hedge_after_ms=200)
    started = time.monotonic()
    assert run(backend) == {"from": "gpt"}
    assert time.monotonic() - started < 0.6


def test_a_failing_primary_moves_on_at_once():
    primary, fallback = Fake("gemini", fail="unavailable"), Fake("gpt")
    backend = RoutedBackend([("gemini", primary), ("gpt", fallback)], hedge_after_ms=5000)
    started = time.monotonic()
    assert run(backend) == {"from": "gpt"}
    assert time.monotonic() - started < 0.5


def test_a_route_that_keeps_failing_rests_then_returns():
    clock = [100.0]
    primary, fallback = Fake("gemini", fail="deadline"), Fake("gpt")
    backend = RoutedBackend(
        [("gemini", primary), ("gpt", fallback)],
        hedge_after_ms=5000,
        breaker_failures=2,
        breaker_seconds=60,
        monotonic=lambda: clock[0],
    )
    run(backend)
    run(backend)
    assert primary.calls == 2
    run(backend)  # resting: the fallback goes first and answers, the primary isn't called
    assert primary.calls == 2
    clock[0] += 61
    primary.fail = None
    assert run(backend) == {"from": "gemini"}


def test_everything_failing_reports_a_failure():
    backend = RoutedBackend(
        [("gemini", Fake("gemini", fail="deadline")), ("gpt", Fake("gpt", fail="unavailable"))],
        hedge_after_ms=5000,
    )
    with pytest.raises(InferenceFailure):
        run(backend)


def test_a_request_too_large_is_not_retried_elsewhere():
    fallback = Fake("gpt")
    backend = RoutedBackend(
        [("gemini", Fake("gemini", fail="too_large")), ("gpt", fallback)], hedge_after_ms=5000
    )
    with pytest.raises(InferenceFailure):
        run(backend)
    assert fallback.calls == 0
