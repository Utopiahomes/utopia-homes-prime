"""guest.answer@1.0 end to end on the Stage 2 candidate engine: the website-facing contract is
unchanged while Homes Prime owns context, validation, sources, actions, and failure selection, and
Shared Model Execution is reached only through the private RC1 client (here, the fake provider)."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from fixtures.fake_sme import error, success
from fixtures.homes_prime import (
    GENERATE,
    HARBOR_CAPACITY_DRAFT,
    REVIEW,
    SUPPORTED,
    UNSUPPORTED,
    build_homes_prime_env,
    draft,
)
from fixtures.keys import sign_token

from utopia_homes_prime.business_api.api import create_app
from utopia_homes_prime.config import Config, ConfigError
from utopia_homes_prime.guest_answer import schema_validation

ENDPOINT = "/business/v1/guest/answer"


class Stage2:
    def __init__(self, tmp_path, **env_kwargs):
        self.harness = build_homes_prime_env(tmp_path, **env_kwargs)
        self.fake = self.harness.fake()
        self.config = Config.from_environment(self.harness.env)
        self.app = create_app(config=self.config, execution_transport=self.fake.transport())

    def headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {sign_token(self.harness.guest_keypair)}",
            "X-Request-ID": str(uuid.uuid4()),
            "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }


def _body(content="How many guests fit at Harbor Light?", **extra):
    body = {
        "contract_version": "1.0",
        "session_id": str(uuid.uuid4()),
        "message": {"turn_id": str(uuid.uuid4()), "content": content},
        "locale": "en-US",
    }
    body.update(extra)
    return body


@pytest.fixture()
def stage2(tmp_path):
    return Stage2(tmp_path)


def _post(stage, client, body=None, key=None):
    return client.post(ENDPOINT, json=body or _body(), headers=stage.headers(key))


def test_grounded_answer_end_to_end(stage2):
    stage2.fake.script(GENERATE, success(HARBOR_CAPACITY_DRAFT))
    stage2.fake.script(REVIEW, success(SUPPORTED))
    body = _body(
        page_context={
            "path": "/stays/harbor-light",
            "subject_type": "property",
            "subject_id": "harbor-light",
        }
    )
    with TestClient(stage2.app) as client:
        response = _post(stage2, client, body)

    assert response.status_code == 200, response.text
    payload = response.json()
    schema_validation.validate_response(payload)
    assert payload["session_id"] == body["session_id"]
    assert payload["answer"].startswith("Harbor Light welcomes up to 12 guests")
    assert [s["source_id"] for s in payload["sources"]] == ["public-source:harbor-light-page"]
    assert [a["action_id"] for a in payload["actions"]] == ["public-link:harbor-light-page-link"]
    assert response.headers["X-Utopia-Preview-Mode"] == "homes-prime-candidate"
    assert response.headers["X-Utopia-Knowledge-Release"] == stage2.config.knowledge_release_id

    fake = stage2.fake
    assert fake.violations == []
    assert fake.dispatches == {GENERATE: 1, REVIEW: 1}
    generate_attempt, review_attempt = fake.attempts
    assert generate_attempt.idempotency_key != review_attempt.idempotency_key
    # Nothing from the private execution seam reaches the website-facing contract.
    record_ids = [record.execution_id for record in fake.records.values()]
    assert not any(execution_id in response.text for execution_id in record_ids)
    assert not any(name.lower().startswith("x-stoin") for name in response.headers)


def test_generation_request_carries_only_homes_assembled_context(stage2):
    stage2.fake.script(GENERATE, success(HARBOR_CAPACITY_DRAFT))
    stage2.fake.script(REVIEW, success(SUPPORTED))
    with TestClient(stage2.app) as client:
        _post(stage2, client)
    document = stage2.fake.attempts[0].document
    system = document["messages"][0]["content"]
    assert "PUBLIC_CONTEXT=" in system and "harbor-light-capacity" in system
    assert "expired-promotion" not in system
    assert document["limits"] == {"max_output_tokens": 900, "max_cost_microusd": 40000}
    schema = document["output"]["schema"]
    assert schema["additionalProperties"] is False


def test_factually_false_candidate_is_rejected_before_review(stage2):
    invented = draft(
        ("business_claim", "Harbor Light welcomes up to 30 guests.", ["harbor-light-capacity"])
    )
    # One bounded repair is allowed, so the invented number is produced twice here.
    stage2.fake.script(GENERATE, success(invented), success(invented))
    with TestClient(stage2.app) as client:
        response = _post(stage2, client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "answer_validation_failed"
    assert response.json()["error"]["retryable"] is False
    assert "Retry-After" not in response.headers
    assert stage2.fake.dispatches == {GENERATE: 2}
    # The rejected candidate never reaches the website (checked by phrase: random UUIDs in the
    # error body can legitimately contain the digits).
    assert "30 guests" not in response.text and "Harbor Light" not in response.text


def test_support_review_rejection_withholds_the_candidate(stage2):
    negation = draft(
        ("business_claim", "Harbor Light has parking for 3 cars.", ["harbor-light-capacity"])
    )
    stage2.fake.script(GENERATE, success(negation))
    stage2.fake.script(REVIEW, success(UNSUPPORTED))
    key = str(uuid.uuid4())
    body = _body()
    with TestClient(stage2.app) as client:
        first = _post(stage2, client, body, key)
        retry = _post(stage2, client, body, key)
    for response in (first, retry):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "answer_validation_failed"
        assert "Harbor Light" not in response.text
    assert stage2.fake.dispatches == {GENERATE: 1, REVIEW: 1}, (
        "a duplicate never re-runs the pipeline"
    )


@pytest.mark.parametrize(
    ("behavior", "status", "code", "retryable"),
    [
        (error("content_filtered"), 503, "answer_validation_failed", False),
        (error("output_limit_reached"), 503, "answer_validation_failed", False),
        (error("provider_execution_failed"), 503, "temporarily_unavailable", True),
        (error("privacy_route_unavailable"), 503, "temporarily_unavailable", True),
        (error("spending_authority_exhausted"), 503, "temporarily_unavailable", True),
        (error("cost_ceiling_insufficient"), 503, "temporarily_unavailable", True),
    ],
)
def test_execution_failures_map_to_rc2_errors(stage2, behavior, status, code, retryable):
    stage2.fake.script(GENERATE, behavior)
    with TestClient(stage2.app) as client:
        response = _post(stage2, client)
    assert response.status_code == status
    error_body = response.json()
    schema_validation.validate_error_response(error_body)
    assert (error_body["error"]["code"], error_body["error"]["retryable"]) == (code, retryable)
    assert ("Retry-After" in response.headers) is retryable
    assert stage2.fake.violations == []
    for private in ("execution", "provider", "privacy", "spending", "cost"):
        assert private not in error_body["error"]["message"].lower()


def test_rate_limited_execution_that_cannot_retry_in_budget_is_guest_rate_limited(stage2):
    stage2.fake.script(
        GENERATE, error("rate_limited", retry_after=30), success(HARBOR_CAPACITY_DRAFT)
    )
    with TestClient(stage2.app) as client:
        response = _post(stage2, client)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.headers["Retry-After"] == "30"
    assert len(stage2.fake.attempts) == 1


def test_transient_failure_retries_once_within_budget(stage2):
    stage2.fake.script(
        GENERATE, error("temporarily_unavailable", retry_after=1), success(HARBOR_CAPACITY_DRAFT)
    )
    stage2.fake.script(REVIEW, success(SUPPORTED))
    with TestClient(stage2.app) as client:
        response = _post(stage2, client)
    assert response.status_code == 200
    generate_attempts = [a for a in stage2.fake.attempts if a.profile_id == GENERATE]
    assert len(generate_attempts) == 2
    assert generate_attempts[0].idempotency_key == generate_attempts[1].idempotency_key


def test_lost_generation_response_is_recovered_by_replay_without_second_dispatch(stage2):
    stage2.fake.script(GENERATE, success(HARBOR_CAPACITY_DRAFT))
    stage2.fake.script(REVIEW, success(SUPPORTED))
    stage2.fake.lose_next_responses = 1
    with TestClient(stage2.app) as client:
        response = _post(stage2, client)
    assert response.status_code == 200
    assert stage2.fake.dispatches == {GENERATE: 1, REVIEW: 1}
    assert stage2.fake.violations == []


def test_response_lost_during_rollout_is_never_regenerated(stage2):
    """RC1 §11 + criterion 80: the lost generate response becomes execution_invalidated after a
    routine successor release. Homes fails this guest attempt; it never starts a replacement
    generation under a new idempotency key."""
    fake = stage2.fake
    fake.script(GENERATE, success(HARBOR_CAPACITY_DRAFT), success(HARBOR_CAPACITY_DRAFT))
    fake.lose_next_responses = 1
    original_handle = fake.handle

    async def handle_with_rollout(method, path, headers, body):
        result = await original_handle(method, path, headers, body)
        if len(fake.attempts) == 1:
            fake.activate_successor_release(GENERATE)
        return result

    fake.handle = handle_with_rollout  # type: ignore[method-assign]
    key = str(uuid.uuid4())
    body = _body()
    with TestClient(stage2.app) as client:
        response = _post(stage2, client, body, key)
        website_retry = _post(stage2, client, body, key)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "temporarily_unavailable"
    assert fake.dispatches == {GENERATE: 1}
    assert len({a.idempotency_key for a in fake.attempts}) == 1
    assert len(fake.attempts) == 2
    # The website's permitted retry replays Homes' recorded outcome; still no new execution.
    assert website_retry.json()["error"]["code"] == "temporarily_unavailable"
    assert fake.dispatches == {GENERATE: 1}


def test_execution_deadline_yields_guest_deadline_exceeded(tmp_path):
    stage = Stage2(tmp_path, generate_ceiling_ms=1000)
    stage.fake.script(GENERATE, success(HARBOR_CAPACITY_DRAFT, delay_s=1.4))
    with TestClient(stage.app) as client:
        response = _post(stage, client)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "deadline_exceeded"
    assert "Retry-After" in response.headers
    assert stage.fake.dispatches == {GENERATE: 1}


def test_review_not_started_without_its_complete_ceiling(tmp_path):
    """RC1 §18: support review begins only when its complete profile ceiling still fits. With a
    slow generation eating the attempt budget, Homes stops before a second paid execution."""
    stage = Stage2(tmp_path, generate_ceiling_ms=9000, review_ceiling_ms=4000, reserve_ms=1500)
    stage.fake.script(GENERATE, success(HARBOR_CAPACITY_DRAFT, delay_s=0.1))
    engine_budget = {}

    with TestClient(stage.app) as client:
        engine = client.app.state.homes_prime  # type: ignore[attr-defined]
        real_monotonic = engine._monotonic
        calls = {"n": 0}

        def skewed():
            calls["n"] += 1
            # After generation returns, pretend 10 seconds elapsed: 13.5s usable budget leaves
            # 3.5s, less than the review's 4s ceiling plus transit.
            return real_monotonic() + (10.0 if calls["n"] > 1 else 0.0)

        engine._monotonic = skewed
        response = _post(stage, client)
        engine_budget["telemetry"] = engine.last_telemetry

    assert response.status_code == 504
    assert stage.fake.dispatches == {GENERATE: 1}
    assert engine_budget["telemetry"].category == "insufficient_budget"


def test_guest_replay_returns_identical_answer_without_new_executions(stage2):
    stage2.fake.script(GENERATE, success(HARBOR_CAPACITY_DRAFT))
    stage2.fake.script(REVIEW, success(SUPPORTED))
    key = str(uuid.uuid4())
    body = _body()
    with TestClient(stage2.app) as client:
        first = _post(stage2, client, body, key)
        second = _post(stage2, client, body, key)
        conflict = _post(stage2, client, _body("Something else?"), key)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert stage2.fake.dispatches == {GENERATE: 1, REVIEW: 1}


def test_withdrawn_knowledge_is_absent_from_model_context(tmp_path):
    stage = Stage2(tmp_path, withdrawn=("harbor-light-capacity",))
    stage.fake.script(GENERATE, success(HARBOR_CAPACITY_DRAFT))
    with TestClient(stage.app) as client:
        response = _post(stage, client)
    # The draft cites withdrawn evidence, which the Homes output schema enum no longer allows; a
    # provider result that violates the requested schema is a malformed execution response.
    assert response.status_code == 503
    system = stage.fake.attempts[0].document["messages"][0]["content"]
    assert "harbor-light-capacity" not in system
    assert response.json()["error"]["code"] == "temporarily_unavailable"


def test_oversized_evidence_packet_fails_closed_without_execution(tmp_path):
    from fixtures.homes_knowledge import padded_corpus, write_corpus

    stage = Stage2(tmp_path)
    oversized = tmp_path / "oversized"
    oversized.mkdir()
    path, digest = write_corpus(oversized, padded_corpus(66))
    env = dict(stage.harness.env)
    env["GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_PATH"] = str(path)
    env["GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_ALLOWED_DIGESTS"] = digest
    app = create_app(
        config=Config.from_environment(env), execution_transport=stage.fake.transport()
    )
    with TestClient(app) as client:
        response = client.post(ENDPOINT, json=_body(), headers=stage.headers())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "temporarily_unavailable"
    assert stage.fake.attempts == []


def test_homes_prime_engine_is_refused_outside_preview(tmp_path):
    harness = build_homes_prime_env(
        tmp_path, extra={"GUEST_ANSWER_PROVIDER_ENVIRONMENT": "production"}
    )
    with pytest.raises(ConfigError, match="only in preview"):
        Config.from_environment(harness.env)


@pytest.mark.parametrize(
    "missing",
    [
        "GUEST_ANSWER_PROVIDER_HOMES_PRIME_GENERATE_MAX_COST_MICROUSD",
        "GUEST_ANSWER_PROVIDER_HOMES_PRIME_REVIEW_MAX_COST_MICROUSD",
        "GUEST_ANSWER_PROVIDER_HOMES_PRIME_EXECUTION_PRIVATE_KEY_PEM",
        "GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_ALLOWED_DIGESTS",
    ],
)
def test_spending_credentials_and_knowledge_pins_are_never_defaulted(tmp_path, missing):
    harness = build_homes_prime_env(tmp_path)
    del harness.env[missing]
    with pytest.raises(ConfigError):
        Config.from_environment(harness.env)


def test_unapproved_knowledge_digest_fails_at_startup(tmp_path):
    harness = build_homes_prime_env(
        tmp_path, extra={"GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_ALLOWED_DIGESTS": "0" * 64}
    )
    from utopia_homes_prime.knowledge.projection import KnowledgeUnavailable

    with pytest.raises(KnowledgeUnavailable):
        create_app(config=Config.from_environment(harness.env))


def test_timing_invariant_violation_fails_at_startup(tmp_path):
    harness = build_homes_prime_env(tmp_path, generate_ceiling_ms=11_000, review_ceiling_ms=5_000)
    with pytest.raises(ValueError, match="15s"):
        create_app(config=Config.from_environment(harness.env))


def test_non_loopback_http_execution_endpoint_is_rejected(tmp_path):
    harness = build_homes_prime_env(
        tmp_path, execution_url="http://sme.internal/execution/v1/inference"
    )
    with pytest.raises(ValueError, match="HTTPS"):
        create_app(config=Config.from_environment(harness.env))


def test_legacy_bridge_remains_the_default_engine(test_environment):
    config = Config.from_environment(test_environment.env)
    assert config.answer_engine == "legacy-bridge"
    assert config.homes_prime is None
    assert json.loads(json.dumps(config.legacy_upstream.snapshot_digest))


def test_a_rejected_draft_is_repaired_once_with_a_content_free_note(stage2):
    invented = draft(
        ("business_claim", "Harbor Light welcomes up to 30 guests.", ["harbor-light-capacity"])
    )
    stage2.fake.script(GENERATE, success(invented), success(HARBOR_CAPACITY_DRAFT))
    stage2.fake.script(REVIEW, success(SUPPORTED))
    with TestClient(stage2.app) as client:
        response = _post(stage2, client)
    assert response.status_code == 200, response.text
    assert response.json()["answer"].startswith("Harbor Light welcomes up to 12 guests")
    assert stage2.fake.dispatches == {GENERATE: 2, REVIEW: 1}
    first, second = (a.document["messages"][0]["content"] for a in stage2.fake.attempts[:2])
    assert "REVISION_REQUIRED" not in first
    assert "REVISION_REQUIRED=Your previous draft stated a number" in second
    assert "30 guests" not in second


def test_a_support_review_rejection_is_never_repaired(stage2):
    stage2.fake.script(GENERATE, success(HARBOR_CAPACITY_DRAFT))
    stage2.fake.script(REVIEW, success(UNSUPPORTED))
    with TestClient(stage2.app) as client:
        response = _post(stage2, client)
    assert response.json()["error"]["code"] == "answer_validation_failed"
    assert stage2.fake.dispatches == {GENERATE: 1, REVIEW: 1}


def test_no_repair_without_enough_budget(stage2, monkeypatch):
    from utopia_homes_prime.guest_answer import homes_prime

    monkeypatch.setattr(homes_prime, "MIN_REPAIR_BUDGET_MS", 60_000)
    invented = draft(
        ("business_claim", "Harbor Light welcomes up to 30 guests.", ["harbor-light-capacity"])
    )
    stage2.fake.script(GENERATE, success(invented))
    with TestClient(stage2.app) as client:
        response = _post(stage2, client)
    assert response.json()["error"]["code"] == "answer_validation_failed"
    assert stage2.fake.dispatches == {GENERATE: 1}
