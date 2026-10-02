"""Builders for Homes Dragon meeting-operation tests: a Homes Prime config with the meeting
operations enabled, the Workspaces adapter's own registered key, and the vendored demo materials.
Every key here is ephemeral and test-only."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fixtures.fake_sme import FakeSharedModelExecution
from fixtures.homes_prime import HomesPrimeHarness, build_homes_prime_env
from fixtures.keys import TestKeypair, generate_test_keypair, sign_token
from utopia_homes_prime.business_api.api import create_app
from utopia_homes_prime.config import Config

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "materials" / "homes-dragon" / "manifest.json"
MEETING_PROFILE = "utopia-homes.meeting-assist.v1"
BRIEF = {"id": "homes-owner-onboarding-brief-demo-v1", "version": 1}
CHECKLIST = {"id": "homes-owner-onboarding-checklist-demo-v1", "version": 1}
ADAPTER_ISSUER = "stoin:application:utopia-workspaces"
ADAPTER_SUBJECT = "stoin:service:utopia-workspaces-synth-adapter"
AUDIENCE = "stoin:business:utopia-homes-prime"


def manifest_digest(path: Path = MANIFEST) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def adapter_key_entry(keypair: TestKeypair, capabilities: list[str]) -> dict[str, Any]:
    return {
        "kid": keypair.kid,
        "public_key_pem": keypair.public_pem,
        "environment": "preview",
        "issuer": keypair.issuer,
        "subject": keypair.subject,
        "audience": keypair.audience,
        "capabilities": capabilities,
        "status": "active",
    }


@dataclass
class MeetingHarness:
    prime: HomesPrimeHarness
    adapter: TestKeypair
    fake: FakeSharedModelExecution
    config: Config

    def app(self) -> Any:
        return create_app(config=self.config, execution_transport=self.fake.transport())

    def token(self, **kwargs: Any) -> str:
        return sign_token(self.adapter, scope=kwargs.pop("scope", "meeting.assist"), **kwargs)

    def headers(self, *, post: bool = True, key: str | None = None, **token: Any) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token(**token)}",
            "X-Request-ID": str(uuid.uuid4()),
            "Accept": "application/json",
        }
        if post:
            headers["Idempotency-Key"] = key or str(uuid.uuid4())
            headers["Content-Type"] = "application/json"
        return headers


def build_meeting_env(
    tmp_path: Path,
    *,
    manifest: Path = MANIFEST,
    allowed_digest: str | None = None,
    meeting: dict[str, str] | None = None,
    prime: dict[str, Any] | None = None,
) -> tuple[HomesPrimeHarness, TestKeypair]:
    adapter = generate_test_keypair(
        kid="workspaces-adapter-test-1",
        issuer=ADAPTER_ISSUER,
        subject=ADAPTER_SUBJECT,
        audience=AUDIENCE,
    )
    harness = build_homes_prime_env(tmp_path, **(prime or {}))
    keys = json.loads(harness.env["GUEST_ANSWER_PROVIDER_JWT_PUBLIC_KEYS_JSON"])
    keys.append(adapter_key_entry(adapter, ["meeting.assist"]))
    harness.env["GUEST_ANSWER_PROVIDER_JWT_PUBLIC_KEYS_JSON"] = json.dumps(keys)
    prefix = "GUEST_ANSWER_PROVIDER_MEETING_"
    harness.env.update(
        {
            f"{prefix}ENABLED": "true",
            f"{prefix}RESPOND_MAX_COST_MICROUSD": "20000",
            f"{prefix}DRAFT_MAX_COST_MICROUSD": "40000",
            f"{prefix}MATERIALS_PATH": str(manifest),
            f"{prefix}MATERIALS_ALLOWED_DIGESTS": allowed_digest or manifest_digest(manifest),
        }
    )
    harness.env.update(meeting or {})
    return harness, adapter


def build_meeting_harness(tmp_path: Path, **kwargs: Any) -> MeetingHarness:
    prime, adapter = build_meeting_env(tmp_path, **kwargs)
    return MeetingHarness(prime, adapter, prime.fake(), Config.from_environment(prime.env))


def respond_body(message: str = "What do you need from me before listing?", **extra: Any) -> dict:
    body: dict[str, Any] = {
        "contract_version": "1.0",
        "meeting_id": "meeting-1",
        "turn_id": str(uuid.uuid4()),
        "requester": {"display_name": "Sample Owner", "is_host": False},
        "message": message,
        "context": [{"speaker": "Ray", "text": "Welcome, let's go through onboarding."}],
        "materials": [dict(CHECKLIST)],
        "locale": "en-US",
    }
    body.update(extra)
    return body


def draft_body(notes: list[tuple[str, str]] | None = None, **extra: Any) -> dict:
    body: dict[str, Any] = {
        "contract_version": "1.0",
        "meeting_id": "meeting-1",
        "request_id": str(uuid.uuid4()),
        "meeting_notes": [
            {"speaker": s, "text": t}
            for s, t in notes
            or [
                ("Ray", "We agreed the walkthrough is on Friday."),
                ("Sample Owner", "Maybe we start listing in March?"),
                ("Ray", "Sample Owner will send the HOA rules."),
            ]
        ],
        "materials": [dict(BRIEF), dict(CHECKLIST)],
    }
    body.update(extra)
    return body


def reply(
    answer: str = "Before listing, we'll need the items on the onboarding checklist.",
    *,
    outcome: str = "answered",
    display: str | None = "homes-owner-onboarding-checklist-demo-v1@1",
    reason: str | None = None,
    kind: str = "utopia",
    search_query: str = "",
    show_file: str | None = None,
    show_page: int | None = None,
    action_item: str = "",
    action_context: str = "",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "outcome": outcome,
        "answer": answer,
        "search_query": search_query,
        "show_file": show_file,
        "show_page": show_page,
        "display_material": display,
        "decline_reason": reason,
        "action_item": action_item,
        "action_context": action_context,
    }


def next_steps(**overrides: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "confirmed_decisions": ["Walkthrough on Friday."],
        "decisions": ["Consider a March listing start."],
        "open_questions": ["Who sends the management agreement?"],
        "action_items": [
            {"description": "Send the HOA rules.", "owner": "Sample Owner", "timing": None}
        ],
        "next_steps": ["Hold the walkthrough."],
    }
    content.update(overrides)
    return content
