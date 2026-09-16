"""Builds a full test Config (JWT allowlist + legacy upstream config) from ephemeral test-only
material, reused by unit, integration, and contract tests so the environment shape only needs to
be defined once.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from fixtures.keys import TestKeypair, generate_test_keypair
from guest_answer_provider.config import Config

DEFAULT_SNAPSHOT_DIGEST = hashlib.sha256(b"fixture-snapshot").hexdigest()


@dataclass(frozen=True, slots=True)
class TestEnvironment:
    env: dict[str, str]
    keypair: TestKeypair


def build_env(
    *,
    keypair: TestKeypair,
    port: int = 0,
    upstream_url: str = "http://127.0.0.1:9/unused-upstream",
    snapshot_digest: str = DEFAULT_SNAPSHOT_DIGEST,
    provider_environment: str = "preview",
    key_environment: str | None = None,
    capabilities: tuple[str, ...] = ("guest.answer",),
) -> dict[str, str]:
    jwt_keys = [
        {
            "kid": keypair.kid,
            "public_key_pem": keypair.public_pem,
            "environment": key_environment or provider_environment,
            "issuer": keypair.issuer,
            "subject": keypair.subject,
            "audience": keypair.audience,
            "capabilities": list(capabilities),
            "status": "active",
        }
    ]
    return {
        "PORT": str(port),
        "GUEST_ANSWER_PROVIDER_ENVIRONMENT": provider_environment,
        "GUEST_ANSWER_PROVIDER_JWT_PUBLIC_KEYS_JSON": json.dumps(jwt_keys),
        "GUEST_ANSWER_PROVIDER_BUSINESS_RELEASE_ID": "homes-business:release:test.1",
        "GUEST_ANSWER_PROVIDER_KNOWLEDGE_RELEASE_ID": "homes-knowledge:release:test.1",
        "LUCY_PUBLIC_ENABLED": "true",
        "LUCY_PUBLIC_API_URL": upstream_url,
        "LUCY_PUBLIC_API_TOKEN": "x" * 40,
        "LUCY_PUBLIC_SITE_HOSTNAME": "www.example.com",
        "LUCY_PUBLIC_SNAPSHOT_DIGEST": snapshot_digest,
    }


def build_test_environment(**kwargs) -> TestEnvironment:
    keypair = kwargs.pop("keypair", None) or generate_test_keypair()
    env = build_env(keypair=keypair, **kwargs)
    return TestEnvironment(env=env, keypair=keypair)


def build_config(**kwargs) -> Config:
    test_env = build_test_environment(**kwargs)
    return Config.from_environment(test_env.env)
