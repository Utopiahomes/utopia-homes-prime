"""The Homes independence proof: Utopia Homes Prime answers guests and meetings with no Tiamat
URL, credential, grant, key, or service configured, over its own direct inference route.

The fake OpenRouter transport refuses every other destination, and the environment holds no
Tiamat setting of any kind, so a passing test shows nothing on the Tiamat side was needed."""

from __future__ import annotations

import pathlib
import re
import tomllib
import uuid

import pytest
from fastapi.testclient import TestClient
from fixtures.fake_openrouter import (
    MODEL,
    TIAMAT_ENV_MARKERS,
    FakeOpenRouter,
    Reply,
    build_direct_env,
    make_direct,
)
from fixtures.homes_prime import HARBOR_CAPACITY_DRAFT, SUPPORTED, UNSUPPORTED
from fixtures.keys import sign_token
from fixtures.meeting import build_meeting_env, draft_body, next_steps, reply, respond_body

from utopia_homes_prime.business_api.api import create_app
from utopia_homes_prime.config import Config
from utopia_homes_prime.guest_answer import schema_validation

REPO = pathlib.Path(__file__).resolve().parents[2]
GUEST = "/business/v1/guest/answer"


def _guest_headers(keypair) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {sign_token(keypair)}",
        "X-Request-ID": str(uuid.uuid4()),
        "Idempotency-Key": str(uuid.uuid4()),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _guest_body() -> dict:
    return {
        "contract_version": "1.0",
        "session_id": str(uuid.uuid4()),
        "message": {
            "turn_id": str(uuid.uuid4()),
            "content": "How many guests fit at Harbor Light?",
        },
        "locale": "en-US",
        "page_context": {
            "path": "/stays/harbor-light",
            "subject_type": "property",
            "subject_id": "harbor-light",
        },
    }


def _no_tiamat_setting(env: dict[str, str]) -> None:
    assert not [name for name in env if any(m in name for m in TIAMAT_ENV_MARKERS)]
    assert not [value for value in env.values() if "/execution/v1/inference" in value]


def test_guest_answer_with_no_tiamat_configured(tmp_path):
    harness = build_direct_env(tmp_path)
    _no_tiamat_setting(harness.env)
    config = Config.from_environment(harness.env)
    assert config.homes_prime is not None
    assert config.homes_prime.tiamat is None
    assert config.homes_prime.inference_backend == "direct-openrouter"

    fake = FakeOpenRouter()
    fake.script("homes-guest-answer-draft", Reply(HARBOR_CAPACITY_DRAFT))
    fake.script("homes-support-verdict", Reply(SUPPORTED))
    app = create_app(config=config, execution_transport=fake.transport())
    with TestClient(app) as client:
        response = client.post(
            GUEST, json=_guest_body(), headers=_guest_headers(harness.guest_keypair)
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    schema_validation.validate_response(payload)
    assert payload["outcome"] == "answered"
    assert payload["answer"].startswith("Harbor Light welcomes up to 12 guests")
    assert [s["source_id"] for s in payload["sources"]] == ["public-source:harbor-light-page"]
    assert fake.violations == []
    generate, review = fake.requests
    assert generate["body"]["model"] == review["body"]["model"] == MODEL
    assert "PUBLIC_CONTEXT=" in generate["body"]["messages"][0]["content"]
    # Nothing about the route or provider crosses into the guest-facing contract.
    assert "FakeProvider" not in response.text and MODEL not in response.text


def test_homes_rules_still_decide_on_the_direct_route(tmp_path):
    harness = build_direct_env(tmp_path)
    fake = FakeOpenRouter()
    fake.script("homes-guest-answer-draft", Reply(HARBOR_CAPACITY_DRAFT))
    fake.script("homes-support-verdict", Reply(UNSUPPORTED))
    app = create_app(
        config=Config.from_environment(harness.env), execution_transport=fake.transport()
    )
    with TestClient(app) as client:
        response = client.post(
            GUEST, json=_guest_body(), headers=_guest_headers(harness.guest_keypair)
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "answer_validation_failed"


def test_meeting_operations_with_no_tiamat_configured(tmp_path):
    prime, adapter = build_meeting_env(tmp_path)
    make_direct(prime.env)
    _no_tiamat_setting(prime.env)
    fake = FakeOpenRouter()
    fake.script("homes-dragon-meeting-reply", Reply(reply()))
    fake.script("homes-dragon-next-steps-draft", Reply(next_steps()))
    app = create_app(
        config=Config.from_environment(prime.env), execution_transport=fake.transport()
    )

    def headers() -> dict[str, str]:
        return {
            "Authorization": f"Bearer {sign_token(adapter, scope='meeting.assist')}",
            "X-Request-ID": str(uuid.uuid4()),
            "Idempotency-Key": str(uuid.uuid4()),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    with TestClient(app) as client:
        answered = client.post(
            "/business/v1/meeting/respond", json=respond_body(), headers=headers()
        )
        drafted = client.post("/business/v1/meeting/draft", json=draft_body(), headers=headers())
    assert answered.status_code == 200 and answered.json()["outcome"] == "answered"
    assert drafted.status_code == 200 and drafted.json()["draft"]["confirmed_decisions"]
    assert fake.violations == []


def test_the_selected_backend_is_explicit(tmp_path):
    harness = build_direct_env(tmp_path)
    del harness.env["GUEST_ANSWER_PROVIDER_HOMES_PRIME_INFERENCE_BACKEND"]
    with pytest.raises(Exception, match="INFERENCE_BACKEND"):
        Config.from_environment(harness.env)


# --- source independence -------------------------------------------------------------------------

FOREIGN_PACKAGES = ("tiamat", "lucy", "personal_lucy", "utopia_studio", "workspaces_synth_adapter")


def test_homes_prime_imports_no_other_system():
    pattern = re.compile(rf"^\s*(?:from|import)\s+({'|'.join(FOREIGN_PACKAGES)})\b", re.MULTILINE)
    offenders = [
        str(path.relative_to(REPO))
        for root in ("src", "tests", "tools", "deploy", "contracts")
        if (REPO / root).is_dir()
        for path in (REPO / root).rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_homes_prime_depends_on_no_other_system():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = [*project["dependencies"], *project["optional-dependencies"]["dev"]]
    for requirement in requirements:
        name = re.split(r"[<>=\[ ;]", requirement, maxsplit=1)[0].lower()
        assert not any(foreign.replace("_", "-") in name for foreign in FOREIGN_PACKAGES), name
    for text in (REPO / "pyproject.toml", REPO / "requirements.lock", REPO / "Dockerfile"):
        content = text.read_text(encoding="utf-8")
        assert "cloud-hermes-lucy" not in content and "../" not in content
