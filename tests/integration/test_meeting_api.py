"""Homes Dragon meeting operations end to end: identity, respond, and draft, over the in-process app
with the fake Shared Model Execution provider. Covers the adapter's workload JWT, exact material
resolution, strict schemas, Homes-side checks on model output, idempotency, the content-free error
format, and the retention statement."""

from __future__ import annotations

import json
import logging
import time
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient
from fixtures.fake_sme import error, success
from fixtures.keys import sign_token
from fixtures.meeting import (
    BRIEF,
    CHECKLIST,
    MEETING_PROFILE,
    MeetingHarness,
    build_meeting_env,
    build_meeting_harness,
    draft_body,
    next_steps,
    reply,
    respond_body,
)

from utopia_homes_prime.business_api.api import create_app
from utopia_homes_prime.config import Config, ConfigError
from utopia_homes_prime.meeting_assist.meeting_materials import MaterialsUnavailable

IDENTITY = "/business/v1/meeting/identity"
RESPOND = "/business/v1/meeting/respond"
DRAFT = "/business/v1/meeting/draft"
BRIEF_SHA = "dd5fcd2211a657a28a6b00d2a50097496379b811554577b60c97a73c36f4687e"
CHECKLIST_SHA = "33407208407932fc651547e2f1f02210a71b37c987101d4ef08183c6309194ec"


@pytest.fixture()
def dragon(tmp_path) -> MeetingHarness:
    return build_meeting_harness(tmp_path)


def _error_of(response) -> tuple[int, str, bool]:
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "retryable"}
    return response.status_code, body["error"]["code"], body["error"]["retryable"]


def _messages(dragon: MeetingHarness) -> list[dict]:
    return dragon.fake.attempts[-1].document["messages"]


# --- identity ------------------------------------------------------------------------------------


def test_identity_reports_homes_dragon_and_exact_materials(dragon):
    with TestClient(dragon.app()) as client:
        response = client.get(IDENTITY, headers=dragon.headers(post=False))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "contract_version": "1.0",
        "synth_id": "stoin:synth:utopia-homes-prime",
        "display_name": "Lucy",
        "software_version": "homes-business:release:test.1",
        "approved_materials": [
            {
                "id": BRIEF["id"],
                "version": 1,
                "sha256": BRIEF_SHA,
                "title": "Utopia Homes Owner Onboarding Brief — Demo v1",
                "kind": "brief",
            },
            {
                "id": CHECKLIST["id"],
                "version": 1,
                "sha256": CHECKLIST_SHA,
                "title": "Utopia Homes Owner Onboarding Checklist — Demo v1",
                "kind": "checklist",
            },
        ],
    }


# --- authentication ------------------------------------------------------------------------------


def _claims_token(dragon: MeetingHarness, **overrides) -> str:
    """Callable overrides receive the signing time, so time-relative claims stay exact."""
    now = int(time.time())
    claims = {
        "iss": dragon.adapter.issuer,
        "sub": dragon.adapter.subject,
        "aud": dragon.adapter.audience,
        "scope": "meeting.assist",
        "iat": now,
        "nbf": now,
        "exp": now + 120,
        "jti": str(uuid.uuid4()),
    }
    claims.update({k: v(now) if callable(v) else v for k, v in overrides.items()})
    return jwt.encode(
        claims, dragon.adapter.private_pem, algorithm="EdDSA", headers={"kid": dragon.adapter.kid}
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "stoin:business:someone-else"},
        {"scope": "guest.answer"},
        {"scope": "meeting.assist guest.answer"},
        {"nbf": lambda now: now - 5},
        {"exp": lambda now: now + 301},
        {"sub": "stoin:service:another-adapter"},
        {"jti": "not-a-uuid"},
    ],
)
def test_identity_refuses_tokens_outside_the_workload_rules(dragon, overrides):
    headers = dragon.headers(post=False)
    headers["Authorization"] = f"Bearer {_claims_token(dragon, **overrides)}"
    with TestClient(dragon.app()) as client:
        response = client.get(IDENTITY, headers=headers)
    assert _error_of(response) == (401, "authentication_failed", False)


def test_a_reused_jti_is_refused(dragon):
    token = _claims_token(dragon)
    with TestClient(dragon.app()) as client:
        first = dict(dragon.headers(post=False), Authorization=f"Bearer {token}")
        second = dict(dragon.headers(post=False), Authorization=f"Bearer {token}")
        assert client.get(IDENTITY, headers=first).status_code == 200
        assert _error_of(client.get(IDENTITY, headers=second)) == (
            401,
            "authentication_failed",
            False,
        )


def test_capabilities_are_per_key_and_do_not_cross_contracts(dragon):
    guest_meeting_token = sign_token(dragon.prime.guest_keypair, scope="meeting.assist")
    adapter_guest_token = dragon.token(scope="guest.answer")
    with TestClient(dragon.app()) as client:
        # The website's guest.answer key holds no meeting capability.
        headers = dict(dragon.headers(post=False), Authorization=f"Bearer {guest_meeting_token}")
        assert _error_of(client.get(IDENTITY, headers=headers)) == (
            403,
            "capability_forbidden",
            False,
        )
        # The adapter's key cannot call guest.answer.
        guest = client.post(
            "/business/v1/guest/answer",
            json={
                "contract_version": "1.0",
                "session_id": str(uuid.uuid4()),
                "message": {"turn_id": str(uuid.uuid4()), "content": "Hello"},
                "locale": "en-US",
            },
            headers=dict(dragon.headers(), Authorization=f"Bearer {adapter_guest_token}"),
        )
        assert guest.status_code == 403
        assert guest.json()["error"]["code"] == "capability_forbidden"
    assert dragon.fake.attempts == []


# --- respond -------------------------------------------------------------------------------------


def test_respond_answers_from_the_named_material_only(dragon):
    dragon.fake.script(MEETING_PROFILE, success(reply()))
    body = respond_body()
    with TestClient(dragon.app()) as client:
        response = client.post(RESPOND, json=body, headers=dragon.headers())
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {
        "contract_version",
        "response_id",
        "turn_id",
        "outcome",
        "answer",
        "display_material",
        "limitations",
    }
    assert result["turn_id"] == body["turn_id"]
    assert result["outcome"] == "answered"
    assert result["answer"].startswith("Before listing")
    assert result["display_material"] == CHECKLIST
    assert result["limitations"] == []

    attempt = dragon.fake.attempts[-1]
    assert attempt.profile_id == MEETING_PROFILE
    system, user = (message["content"] for message in _messages(dragon))
    assert "Owner Onboarding Checklist" in system
    assert "Owner Onboarding Brief" not in system  # not named for this meeting
    assert user.startswith("MEETING_TURN=")
    turn = json.loads(user.removeprefix("MEETING_TURN="))
    assert turn["requester"] == {"display_name": "Sample Owner", "is_host": False}
    assert turn["context"] == body["context"]
    schema = attempt.document["output"]["schema"]
    assert schema["properties"]["display_material"]["enum"] == [
        "homes-owner-onboarding-checklist-demo-v1@1",
        None,
    ]


@pytest.mark.parametrize(
    "materials",
    [
        [{"id": CHECKLIST["id"], "version": 2}],
        [{"id": "homes-private-owner-ledger", "version": 1}],
        [dict(CHECKLIST), {"id": BRIEF["id"], "version": 3}],
    ],
)
def test_an_identifier_alone_grants_nothing(dragon, materials):
    with TestClient(dragon.app()) as client:
        response = client.post(
            RESPOND, json=respond_body(materials=materials), headers=dragon.headers()
        )
    assert _error_of(response) == (403, "material_not_permitted", False)
    assert dragon.fake.attempts == []


def test_declined_requests_carry_no_answer_or_material(dragon):
    dragon.fake.script(
        MEETING_PROFILE,
        success(
            reply(
                "Another owner's income was 500 last year.",
                outcome="declined",
                reason="unrelated_private_data",
            )
        ),
    )
    body = respond_body("What did the owner on Example Street earn last year?")
    with TestClient(dragon.app()) as client:
        response = client.post(RESPOND, json=body, headers=dragon.headers())
    result = response.json()
    assert response.status_code == 200
    assert result["outcome"] == "declined"
    assert result["answer"] == ""
    assert "display_material" not in result
    assert result["limitations"] == [
        "I can't share private information that isn't part of this meeting."
    ]


@pytest.mark.parametrize(
    "content",
    [
        reply("Our management fee is 25 percent."),  # a number no source supports
        reply("See https://example.com for the checklist."),  # not speakable plain text
        reply("   "),
    ],
)
def test_output_breaking_homes_rules_twice_becomes_an_honest_line(dragon, content):
    dragon.fake.script(MEETING_PROFILE, success(content), success(content))
    with TestClient(dragon.app()) as client:
        response = client.post(RESPOND, json=respond_body(), headers=dragon.headers())
    assert response.status_code == 200
    result = response.json()
    assert result["outcome"] == "answered"
    assert result["answer"].startswith("Sorry, I couldn't find a reliable answer")
    assert result["limitations"] == []
    assert "display_material" not in result
    assert len(dragon.fake.attempts) == 2  # one corrected retry before giving up


def test_numbers_from_the_conversation_or_materials_are_allowed(dragon):
    dragon.fake.script(
        MEETING_PROFILE,
        success(reply("You mentioned 12 guests, and the checklist asks for maximum capacity.")),
    )
    body = respond_body(
        "It sleeps 12, what else do you need?",
        context=[{"speaker": "Sample Owner", "text": "The house sleeps 12."}],
    )
    with TestClient(dragon.app()) as client:
        response = client.post(RESPOND, json=body, headers=dragon.headers())
    assert response.json()["outcome"] == "answered"


@pytest.mark.parametrize("code", ["provider_execution_failed", "privacy_route_unavailable"])
def test_execution_failure_is_an_unavailable_turn(dragon, code):
    dragon.fake.script(MEETING_PROFILE, error(code))
    with TestClient(dragon.app()) as client:
        response = client.post(RESPOND, json=respond_body(), headers=dragon.headers())
    assert response.status_code == 200
    assert response.json()["outcome"] == "unavailable"


def _mutate(body: dict, path: str, value: object) -> dict:
    target = body
    *parents, last = path.split(".")
    for name in parents:
        target = target[int(name)] if name.isdigit() else target[name]
    if value is _DELETE:
        del target[last]
    else:
        target[int(last) if last.isdigit() else last] = value
    return body


_DELETE = object()


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        ("contract_version", "2.0", "unsupported_version"),
        ("unexpected", True, "invalid_request"),
        ("requester.role", "host", "invalid_request"),
        ("requester.is_host", "true", "invalid_request"),
        ("materials.0.version", "1", "invalid_request"),
        ("materials.0.extra", 1, "invalid_request"),
        ("turn_id", "not-a-uuid", "invalid_request"),
        ("turn_id", str(uuid.uuid1()), "invalid_request"),
        ("meeting_id", "has spaces", "invalid_request"),
        ("locale", "fr-FR", "invalid_request"),
        ("message", "x" * 2001, "invalid_request"),
        ("message", "   ", "invalid_request"),
        ("message", "hi\u0000there", "invalid_request"),
        ("context", [{"speaker": "Ray", "text": "x"}] * 25, "invalid_request"),
        ("context", [{"speaker": "Ray", "text": "x" * 4000}] * 4, "invalid_request"),
        ("materials", [dict(CHECKLIST), dict(CHECKLIST)], "invalid_request"),
        ("locale", _DELETE, "invalid_request"),
    ],
)
def test_respond_schema_is_strict(dragon, path, value, code):
    body = _mutate(respond_body(), path, value)
    with TestClient(dragon.app()) as client:
        response = client.post(RESPOND, json=body, headers=dragon.headers())
    assert _error_of(response) == (400, code, False)
    assert dragon.fake.attempts == []


def test_duplicate_members_and_bad_headers_are_refused(dragon):
    raw = json.dumps(respond_body())[:-1] + ', "locale": "en-US"}'
    with TestClient(dragon.app()) as client:
        duplicate = client.post(RESPOND, content=raw, headers=dragon.headers())
        assert _error_of(duplicate) == (400, "invalid_request", False)
        no_key = dragon.headers()
        del no_key["Idempotency-Key"]
        assert client.post(RESPOND, json=respond_body(), headers=no_key).status_code == 400
        html = dict(dragon.headers(), Accept="text/html")
        assert client.post(RESPOND, json=respond_body(), headers=html).status_code == 400
        query = client.post(RESPOND + "?debug=1", json=respond_body(), headers=dragon.headers())
        assert query.status_code == 400
        wrong_method = client.post(IDENTITY, json={}, headers=dragon.headers())
        assert _error_of(wrong_method) == (400, "invalid_request", False)
        unknown = client.get("/business/v1/meeting/memory", headers=dragon.headers(post=False))
        assert _error_of(unknown) == (404, "not_found", False)


def test_idempotent_replay_and_conflict(dragon):
    dragon.fake.script(MEETING_PROFILE, success(reply()))
    key = str(uuid.uuid4())
    body = respond_body()
    with TestClient(dragon.app()) as client:
        first = client.post(RESPOND, json=body, headers=dragon.headers(key=key))
        again = client.post(RESPOND, json=body, headers=dragon.headers(key=key))
        other = client.post(
            RESPOND, json=respond_body("Different"), headers=dragon.headers(key=key)
        )
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert dragon.fake.dispatches[MEETING_PROFILE] == 1
    assert _error_of(other) == (409, "idempotency_conflict", False)


# --- draft ---------------------------------------------------------------------------------------


def test_draft_keeps_decisions_apart_and_unknowns_null(dragon):
    dragon.fake.script(
        MEETING_PROFILE,
        success(
            next_steps(
                action_items=[
                    {"description": "Send the HOA rules.", "owner": "Sample Owner", "timing": None},
                    # Guessed by the model: neither appears in the notes, so Homes returns null.
                    {"description": "Book photography.", "owner": "Dana", "timing": "next week"},
                    {"description": "Hold the walkthrough.", "owner": "Ray", "timing": "Friday"},
                ]
            )
        ),
    )
    body = draft_body()
    with TestClient(dragon.app()) as client:
        response = client.post(DRAFT, json=body, headers=dragon.headers())
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {"contract_version", "response_id", "request_id", "draft"}
    assert result["request_id"] == body["request_id"]
    draft = result["draft"]
    assert draft["confirmed_decisions"] == ["Walkthrough on Friday."]
    assert draft["decisions"] == ["Consider a March listing start."]
    assert draft["action_items"] == [
        {"description": "Send the HOA rules.", "owner": "Sample Owner", "timing": None},
        {"description": "Book photography.", "owner": None, "timing": None},
        {"description": "Hold the walkthrough.", "owner": "Ray", "timing": "Friday"},
    ]
    system, user = (message["content"] for message in _messages(dragon))
    assert "Owner Onboarding Brief" in system and "Owner Onboarding Checklist" in system
    assert json.loads(user.removeprefix("MEETING_NOTES="))[0] == {
        "speaker": "Ray",
        "text": "We agreed the walkthrough is on Friday.",
    }


def test_draft_with_invented_numbers_fails_closed(dragon):
    dragon.fake.script(
        MEETING_PROFILE, success(next_steps(decisions=["Agree a 15 percent management fee."]))
    )
    with TestClient(dragon.app()) as client:
        response = client.post(DRAFT, json=draft_body(), headers=dragon.headers())
    assert _error_of(response) == (503, "temporarily_unavailable", True)
    assert 1 <= int(response.headers["retry-after"]) <= 30


def test_draft_deadline_is_reported(tmp_path):
    dragon = build_meeting_harness(
        tmp_path, meeting={"GUEST_ANSWER_PROVIDER_MEETING_DRAFT_CEILING_MS": "1000"}
    )
    dragon.fake.script(MEETING_PROFILE, success(next_steps(), delay_s=1.5))
    with TestClient(dragon.app()) as client:
        response = client.post(DRAFT, json=draft_body(), headers=dragon.headers())
    assert _error_of(response) == (504, "deadline_exceeded", True)


def test_draft_notes_bound(dragon):
    notes = [("Ray", "x" * 4000)] * 12 + [("Ray", "x")]  # 48,001 characters
    with TestClient(dragon.app()) as client:
        response = client.post(DRAFT, json=draft_body(notes), headers=dragon.headers())
    assert _error_of(response) == (400, "invalid_request", False)
    assert dragon.fake.attempts == []


# --- retention -----------------------------------------------------------------------------------


def test_no_meeting_content_reaches_logs_or_error_bodies(dragon, caplog):
    secret_message = "Zanzibar-owner-question-7731"
    secret_context = "Quokka-context-line-4410"
    secret_answer = "Marmalade-dragon-answer"
    dragon.fake.script(MEETING_PROFILE, success(reply(secret_answer)))
    dragon.fake.script(MEETING_PROFILE, error("provider_execution_failed"))
    body = respond_body(secret_message, context=[{"speaker": "Ray", "text": secret_context}])
    with caplog.at_level(logging.DEBUG), TestClient(dragon.app()) as client:
        client.post(RESPOND, json=body, headers=dragon.headers())
        refused = client.post(
            RESPOND,
            json=respond_body(secret_message, materials=[{"id": "x-secret-4410", "version": 1}]),
            headers=dragon.headers(),
        )
        failed = client.post(
            DRAFT,
            json=draft_body([("Ray", secret_context)]),
            headers=dragon.headers(),
        )
    for secret in (secret_message, secret_context, secret_answer, "x-secret-4410"):
        assert secret not in caplog.text
        assert secret not in refused.text
        assert secret not in failed.text


def test_answers_leave_memory_after_the_replay_window(tmp_path):
    dragon = build_meeting_harness(
        tmp_path, meeting={"GUEST_ANSWER_PROVIDER_MEETING_IDEMPOTENCY_TTL_SECONDS": "1"}
    )
    dragon.fake.script(MEETING_PROFILE, success(reply()), success(reply()))
    app = dragon.app()
    with TestClient(app) as client:
        client.post(RESPOND, json=respond_body(), headers=dragon.headers())
        assert len(app.state.meeting_idempotency) == 1
        time.sleep(1.1)
        client.post(RESPOND, json=respond_body(), headers=dragon.headers())
        # Only the newest call remains; the earlier answer has been dropped from memory.
        assert len(app.state.meeting_idempotency) == 1


# --- configuration -------------------------------------------------------------------------------


def test_meeting_operations_are_absent_unless_enabled(tmp_path):
    prime, _ = build_meeting_env(
        tmp_path, meeting={"GUEST_ANSWER_PROVIDER_MEETING_ENABLED": "false"}
    )
    app = create_app(config=Config.from_environment(prime.env))
    with TestClient(app) as client:
        assert client.get(IDENTITY).status_code == 404


def test_meeting_operations_require_the_homes_prime_engine(tmp_path):
    prime, _ = build_meeting_env(tmp_path)
    prime.env["GUEST_ANSWER_PROVIDER_ANSWER_ENGINE"] = "legacy-bridge"
    prime.env.update(
        {
            "LUCY_PUBLIC_ENABLED": "true",
            "LUCY_PUBLIC_API_URL": "http://127.0.0.1:9/unused",
            "LUCY_PUBLIC_API_TOKEN": "x" * 40,
            "LUCY_PUBLIC_SITE_HOSTNAME": "www.example.com",
            "LUCY_PUBLIC_SNAPSHOT_DIGEST": "0" * 64,
        }
    )
    with pytest.raises(ConfigError, match="homes-prime"):
        Config.from_environment(prime.env)


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("RESPOND_MAX_COST_MICROUSD", "", "RESPOND_MAX_COST_MICROUSD"),
        ("RESPOND_CEILING_MS", "9500", "exceeds 10000"),
        ("DRAFT_CEILING_MS", "18001", "within"),
        ("MATERIALS_ALLOWED_DIGESTS", "ABC", "SHA-256"),
    ],
)
def test_meeting_config_fails_closed(tmp_path, name, value, match):
    prime, _ = build_meeting_env(tmp_path, meeting={f"GUEST_ANSWER_PROVIDER_MEETING_{name}": value})
    with pytest.raises(ConfigError, match=match):
        Config.from_environment(prime.env)


def test_startup_refuses_materials_outside_the_allowlist(tmp_path):
    prime, _ = build_meeting_env(tmp_path, allowed_digest="0" * 64)
    with pytest.raises(MaterialsUnavailable, match="allowlisted"):
        create_app(config=Config.from_environment(prime.env))


def test_answers_use_the_public_knowledge_and_its_numbers(dragon):
    dragon.fake.script(
        MEETING_PROFILE,
        success(reply("Harbor Light sleeps 12 guests and has parking for 3 cars.", display=None)),
    )
    body = respond_body("How many guests can Harbor Light take?")
    with TestClient(dragon.app()) as client:
        response = client.post(RESPOND, json=body, headers=dragon.headers())
    assert response.json()["outcome"] == "answered"
    system = next(iter(message["content"] for message in _messages(dragon)))
    assert "PUBLIC_CONTEXT=" in system and "Harbor Light welcomes up to 12 guests" in system


def _answer(dragon, *contents, body=None):
    dragon.fake.script(MEETING_PROFILE, *(success(content) for content in contents))
    with TestClient(dragon.app()) as client:
        response = client.post(RESPOND, json=body or respond_body(), headers=dragon.headers())
    return response.json()


def test_a_claimed_display_is_made_true_when_the_material_is_clear(dragon):
    result = _answer(dragon, reply("I've put the onboarding checklist on screen.", display=None))
    assert result["outcome"] == "answered"
    assert result["display_material"] == CHECKLIST


def test_a_claim_with_nothing_to_show_is_dropped_and_the_rest_is_spoken(dragon):
    body = respond_body(materials=[dict(CHECKLIST), dict(BRIEF)])
    result = _answer(
        dragon,
        reply("The owner portal handles that. I've put it on screen for you.", display=None),
        body=body,
    )
    assert result["outcome"] == "answered"
    assert result["answer"] == "The owner portal handles that."
    assert "display_material" not in result


def test_offers_to_show_are_fine(dragon):
    result = _answer(dragon, reply("I can show you the checklist if that helps.", display=None))
    assert (result["outcome"], result["answer"]) == (
        "answered",
        "I can show you the checklist if that helps.",
    )


def test_a_rejected_answer_is_retried_once_with_the_reason(dragon):
    result = _answer(
        dragon,
        reply("Our management fee is 25 percent."),
        reply("I don't have the fee in front of me; let's note it as an open question."),
    )
    assert result["outcome"] == "answered"
    assert result["answer"].startswith("I don't have the fee")
    assert len(dragon.fake.attempts) == 2
    user = dragon.fake.attempts[-1].document["messages"][-1]["content"]
    assert "CORRECTION=" in user and "number" in user.split("CORRECTION=")[1]


def test_two_rejected_answers_become_an_honest_line_not_silence(dragon):
    result = _answer(
        dragon,
        reply("Our management fee is 25 percent."),
        reply("Actually it is 30 percent."),
    )
    assert result["outcome"] == "answered"
    assert result["answer"].startswith("Sorry, I couldn't find a reliable answer")


LEASE_PAGES = [
    {"name": "Lease.pdf", "page": 1, "text": "Residential lease between the owner and the tenant."},
    {"name": "Lease.pdf", "page": 4, "text": "The security deposit is $2,400, due at signing."},
]


def test_shared_files_reach_lucy_page_by_page_with_the_page_on_screen(dragon):
    body = respond_body(
        "What's the deposit on this page?",
        documents=LEASE_PAGES,
        on_screen={"name": "Lease.pdf", "page": 4},
    )
    result = _answer(
        dragon, reply("Page 4 of the lease says the deposit is $2,400.", display=None), body=body
    )
    # The deposit's number comes from the shared file, so the Homes number check accepts it.
    assert result["outcome"] == "answered" and "2,400" in result["answer"]
    system, user = (message["content"] for message in _messages(dragon))
    assert "SHARED_DOCUMENTS=" in system and "The security deposit is $2,400" in system
    assert '"on_screen":{"file":"Lease.pdf","page":4}' in user


def test_shared_files_are_bounded(dragon):
    page = {"name": "Big.pdf", "page": 1, "text": "x" * 8_000}
    body = respond_body(documents=[dict(page, page=n) for n in range(1, 12)])  # 88,000 chars
    with TestClient(dragon.app()) as client:
        response = client.post(RESPOND, json=body, headers=dragon.headers())
    assert _error_of(response) == (400, "invalid_request", False)
