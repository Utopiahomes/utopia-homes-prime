"""Legacy-upstream wrapper and RC2<->legacy mapping, against a fake upstream (httpx.MockTransport)
rather than the real network — covers every branch documented in legacy_bridge.py's ambiguity
write-up: length-bound mismatches, oversized legacy answers, snapshot digest mismatch, and
transport failure/timeout.
"""

from __future__ import annotations

import json

import httpx
import pytest

from utopia_homes_prime.config import LegacyUpstreamConfig
from utopia_homes_prime.guest_answer.errors import (
    AnswerValidationFailedError,
    TemporarilyUnavailableError,
)
from utopia_homes_prime.guest_answer.legacy_bridge import (
    answer_via_legacy,
    content_to_legacy_question,
)
from utopia_homes_prime.guest_answer.legacy_upstream import (
    LegacyUpstreamUnavailable,
    ask_legacy_lucy,
)

SNAPSHOT_DIGEST = "a" * 64
CONFIG = LegacyUpstreamConfig(
    endpoint="https://legacy.example.com/api/lucy",
    token="x" * 40,
    site_hostname="www.example.com",
    snapshot_digest=SNAPSHOT_DIGEST,
)


def _client_with_handler(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok_handler(answer: str = "The check-in time is 4pm.", snapshot_digest: str = SNAPSHOT_DIGEST):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answer": answer,
                "source": "faq-1",
                "version": 1,
                "snapshot_digest": snapshot_digest,
            },
        )

    return handler


@pytest.mark.parametrize("content", ["", "x", "y" * 501, "z" * 2000])
async def test_content_to_legacy_question_bounds(content):
    if 2 <= len(content) <= 500:
        assert content_to_legacy_question(content) == content
    else:
        assert content_to_legacy_question(content) is None


async def test_answer_via_legacy_success():
    async with _client_with_handler(_ok_handler()) as client:
        answer, limitations = await answer_via_legacy(
            "What time is check-in?",
            "session-1",
            history_present=False,
            config=CONFIG,
            client=client,
        )
        assert answer == "The check-in time is 4pm."
        assert len(limitations) == 1
        # RC2 Section 12.2: limitations must never reveal internal provider/infrastructure detail.
        for forbidden in ("preconformant", "legacy", "Cloud Lucy", "cloud-hermes", "RC2"):
            assert forbidden not in limitations[0]


async def test_answer_via_legacy_includes_history_limitation_when_history_present():
    async with _client_with_handler(_ok_handler()) as client:
        _, limitations = await answer_via_legacy(
            "What time is check-in?",
            "session-1",
            history_present=True,
            config=CONFIG,
            client=client,
        )
        assert len(limitations) == 2


async def test_content_too_long_for_legacy_raises_answer_validation_failed():
    async with _client_with_handler(_ok_handler()) as client:
        with pytest.raises(AnswerValidationFailedError):
            await answer_via_legacy(
                "x" * 501, "session-1", history_present=False, config=CONFIG, client=client
            )


async def test_content_too_short_for_legacy_raises_answer_validation_failed():
    async with _client_with_handler(_ok_handler()) as client:
        with pytest.raises(AnswerValidationFailedError):
            await answer_via_legacy(
                "x", "session-1", history_present=False, config=CONFIG, client=client
            )


async def test_oversized_legacy_answer_raises_answer_validation_failed():
    async with _client_with_handler(_ok_handler(answer="a" * 4001)) as client:
        with pytest.raises(AnswerValidationFailedError):
            await answer_via_legacy(
                "What time is check-in?",
                "session-1",
                history_present=False,
                config=CONFIG,
                client=client,
            )


async def test_snapshot_digest_mismatch_raises_temporarily_unavailable():
    async with _client_with_handler(_ok_handler(snapshot_digest="b" * 64)) as client:
        with pytest.raises(TemporarilyUnavailableError):
            await answer_via_legacy(
                "What time is check-in?",
                "session-1",
                history_present=False,
                config=CONFIG,
                client=client,
            )


async def test_non_2xx_status_raises_temporarily_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async with _client_with_handler(handler) as client:
        with pytest.raises(LegacyUpstreamUnavailable):
            await ask_legacy_lucy("hi there", "session-1", config=CONFIG, client=client)


async def test_malformed_json_raises_temporarily_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    async with _client_with_handler(handler) as client:
        with pytest.raises(LegacyUpstreamUnavailable):
            await ask_legacy_lucy("hi there", "session-1", config=CONFIG, client=client)


async def test_extra_field_in_upstream_response_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answer": "hi",
                "source": "faq-1",
                "version": 1,
                "snapshot_digest": SNAPSHOT_DIGEST,
                "unexpected_extra_field": True,
            },
        )

    async with _client_with_handler(handler) as client:
        with pytest.raises(LegacyUpstreamUnavailable):
            await ask_legacy_lucy("hi there", "session-1", config=CONFIG, client=client)


async def test_transport_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client_with_handler(handler) as client:
        with pytest.raises(LegacyUpstreamUnavailable):
            await ask_legacy_lucy("hi there", "session-1", config=CONFIG, client=client)


async def test_request_shape_matches_legacy_wire_contract():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "answer": "hi",
                "source": "faq-1",
                "version": 1,
                "snapshot_digest": SNAPSHOT_DIGEST,
            },
        )

    async with _client_with_handler(handler) as client:
        await ask_legacy_lucy("What time is check-in?", "session-xyz", config=CONFIG, client=client)

    assert captured["body"] == {"question": "What time is check-in?"}
    assert captured["headers"]["authorization"] == f"Bearer {CONFIG.token}"
    assert captured["headers"]["x-lucy-public-host"] == CONFIG.site_hostname
    assert captured["headers"]["x-lucy-public-session"] == "session-xyz"
    assert captured["headers"]["origin"] == f"https://{CONFIG.site_hostname}"
