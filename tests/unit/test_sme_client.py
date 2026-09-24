"""The private inference.execute client against the independent fake provider: authentication and
request binding, per-attempt deadlines, idempotent retry after a lost response, the one-retry
budget rule, and rollout-lost-response behavior (RC1 §§6.1, 7, 11, 12)."""

from __future__ import annotations

import time
import uuid

import httpx
import jwt
import pytest
from fixtures.fake_sme import error, success
from fixtures.homes_prime import (
    EXECUTION_ISSUER,
    REVIEW,
    SUPPORTED,
    generate_execution_keypair,
)

from utopia_homes_prime.guest_answer import homes_prime
from utopia_homes_prime.inference import sme_wire
from utopia_homes_prime.inference.sme_client import (
    Deadline,
    ExecutionFailure,
    ExecutionIdentity,
    SharedModelExecutionClient,
    classify_error_code,
)
from utopia_homes_prime.inference.sme_wire import ExecutionMessage, JsonSchemaOutput

URL = "http://127.0.0.1:9/execution/v1/inference"


class _Clock:
    """Real monotonic time plus virtual sleep, so backoff never slows the suite."""

    def __init__(self) -> None:
        self.offset = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.offset += seconds


@pytest.fixture()
def harness():
    keys = generate_execution_keypair()
    from fixtures.fake_sme import FakeSharedModelExecution

    fake = FakeSharedModelExecution(public_key_pem=keys.public_pem, issuer=EXECUTION_ISSUER)
    identity = ExecutionIdentity.from_pem(
        kid="homes-prime-execution-test",
        issuer=EXECUTION_ISSUER,
        subject="stoin:synth:utopia-homes-prime",
        private_key_pem=keys.private_pem,
    )
    clock = _Clock()
    return fake, identity, clock


def _prepared():
    return sme_wire.prepare_request(
        execution_profile_id=REVIEW,
        idempotency_key=str(uuid.uuid4()),
        messages=(ExecutionMessage("system", "policy"), ExecutionMessage("user", "payload")),
        output=JsonSchemaOutput("homes-support-verdict", homes_prime.verdict_schema()),
        max_output_tokens=300,
        max_cost_microusd=20_000,
    )


async def _execute(harness, prepared=None, *, ceiling_ms=2000, budget_ms=14_000, transit_ms=100):
    fake, identity, clock = harness
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        client = SharedModelExecutionClient(
            endpoint_url=URL,
            identity=identity,
            http=http,
            transit_allowance_ms=transit_ms,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        deadline = Deadline(clock.monotonic() + budget_ms / 1000)
        return await client.execute(
            prepared or _prepared(), profile_ceiling_ms=ceiling_ms, deadline=deadline
        )


async def test_success_is_authenticated_bound_and_validated(harness):
    fake, identity, _ = harness
    fake.script(REVIEW, success(SUPPORTED))
    prepared = _prepared()
    result = await _execute(harness, prepared)

    assert fake.violations == []
    assert result.content == SUPPORTED and result.replayed is False
    [attempt] = fake.attempts
    token = jwt.decode(
        attempt.token,
        identity.private_key.public_key(),
        algorithms=["EdDSA"],
        audience="stoin:shared-model-execution",
    )
    assert token["scope"] == "inference.execute"
    assert token["sub"] == "stoin:synth:utopia-homes-prime"
    assert token["iat"] == token["nbf"] and token["exp"] - token["iat"] <= 300
    assert token["req"] == prepared.request_binding
    assert attempt.idempotency_key == prepared.idempotency_key
    assert attempt.body == prepared.body
    assert attempt.timeout_ms == 2000


async def test_lost_response_retry_observes_original_execution(harness):
    fake, _, clock = harness
    fake.script(REVIEW, success(SUPPORTED))
    fake.lose_next_responses = 1

    result = await _execute(harness)

    assert fake.violations == []
    assert fake.dispatches[REVIEW] == 1, "the retry must not start a second billable dispatch"
    assert result.replayed is True
    first, second = fake.attempts
    assert first.idempotency_key == second.idempotency_key
    assert first.body == second.body
    assert first.jti != second.jti
    assert first.request_id != second.request_id
    assert second.timeout_ms <= first.timeout_ms
    assert len(clock.sleeps) == 1 and 0.25 <= clock.sleeps[0] <= 0.75


async def test_rollout_during_lost_response_is_not_regenerated(harness):
    """RC1 §11: a response lost during a routine rollout becomes unrecoverable and is never
    regenerated; the client surfaces the terminal invalidation instead of a new execution."""
    fake, _, _ = harness
    fake.script(REVIEW, success(SUPPORTED))
    fake.lose_next_responses = 1
    original_transport = fake.transport

    class _RolloutTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self._inner = original_transport()

        async def handle_async_request(self, request):
            try:
                return await self._inner.handle_async_request(request)
            except httpx.ReadError:
                fake.activate_successor_release(REVIEW)
                raise

    fake.transport = _RolloutTransport  # type: ignore[method-assign]
    with pytest.raises(ExecutionFailure) as failure:
        await _execute(harness)

    assert failure.value.code == "execution_invalidated"
    assert failure.value.category == "unavailable"
    assert fake.dispatches[REVIEW] == 1
    assert len(fake.attempts) == 2


async def test_non_retryable_errors_are_never_retried(harness):
    fake, _, clock = harness
    fake.script(REVIEW, error("content_filtered"))
    with pytest.raises(ExecutionFailure) as failure:
        await _execute(harness)
    assert (failure.value.category, failure.value.code, failure.value.attempts) == (
        "unsupported_output",
        "content_filtered",
        1,
    )
    assert len(fake.attempts) == 1 and clock.sleeps == []


async def test_retryable_error_retries_once_honoring_retry_after(harness):
    fake, _, clock = harness
    fake.script(REVIEW, error("temporarily_unavailable", retry_after=2), success(SUPPORTED))
    result = await _execute(harness)
    assert result.content == SUPPORTED
    assert clock.sleeps == [2.0]
    assert len(fake.attempts) == 2


async def test_at_most_one_retry(harness):
    fake, _, _ = harness
    fake.script(
        REVIEW,
        error("state_store_unavailable"),
        error("state_store_unavailable"),
        success(SUPPORTED),
    )
    with pytest.raises(ExecutionFailure) as failure:
        await _execute(harness)
    assert failure.value.code == "state_store_unavailable"
    assert len(fake.attempts) == 2


async def test_retry_skipped_when_complete_ceiling_does_not_fit(harness):
    """RC1 §12: Retry-After + transit + the retry's complete profile ceiling must fit."""
    fake, _, _ = harness
    fake.script(REVIEW, error("rate_limited", retry_after=5), success(SUPPORTED))
    with pytest.raises(ExecutionFailure) as failure:
        await _execute(harness, ceiling_ms=2000, budget_ms=6_000)
    assert (failure.value.category, failure.value.retry_after_seconds) == ("rate_limited", 5)
    assert len(fake.attempts) == 1


async def test_timeout_header_is_bounded_by_remaining_budget(harness):
    fake, _, _ = harness
    fake.script(REVIEW, success(SUPPORTED))
    await _execute(harness, ceiling_ms=9000, budget_ms=3_000, transit_ms=200)
    assert 2_500 <= fake.attempts[0].timeout_ms <= 2_800


async def test_budget_below_minimum_header_fails_without_sending(harness):
    fake, _, _ = harness
    with pytest.raises(ExecutionFailure) as failure:
        await _execute(harness, ceiling_ms=2000, budget_ms=1_050, transit_ms=100)
    assert failure.value.category == "deadline"
    assert fake.attempts == []


async def test_provider_deadline_is_terminal(harness):
    fake, _, _ = harness
    fake.script(REVIEW, success(SUPPORTED, delay_s=1.5))
    with pytest.raises(ExecutionFailure) as failure:
        await _execute(harness, ceiling_ms=1000, budget_ms=10_000)
    assert (failure.value.category, failure.value.code) == ("deadline", "deadline_exceeded")
    assert fake.dispatches[REVIEW] == 1 and len(fake.attempts) == 1


async def test_malformed_provider_response_fails_closed(harness):
    fake, _, _ = harness
    fake.script(REVIEW, success({"supported": "yes"}))
    with pytest.raises(ExecutionFailure) as failure:
        await _execute(harness)
    assert (failure.value.category, failure.value.code) == ("unavailable", None)


def test_every_rc1_code_has_a_failure_category():
    expected = {
        "deadline_exceeded": "deadline",
        "rate_limited": "rate_limited",
        "provider_response_invalid": "unsupported_output",
        "provider_response_too_large": "unsupported_output",
        "output_limit_reached": "unsupported_output",
        "content_filtered": "unsupported_output",
    }
    for code in sme_wire.ERROR_TABLE:
        assert classify_error_code(code) == expected.get(code, "unavailable")
