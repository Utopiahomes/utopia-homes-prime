"""Shared builders for Homes Prime (Stage 2 candidate) tests: config, fake execution service, and
model-output content that matches the synthetic knowledge fixture."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fixtures.env import build_env
from fixtures.fake_sme import FakeSharedModelExecution
from fixtures.homes_knowledge import write_synthetic_corpus
from fixtures.keys import TestKeypair, generate_test_keypair

GENERATE = "utopia-homes.public-answer.generate.v1"
REVIEW = "utopia-homes.public-answer.support-review.v1"
EXECUTION_ISSUER = "stoin:deployment:utopia-homes-prime-test"
EXECUTION_URL = "http://127.0.0.1:9/execution/v1/inference"


@dataclass(frozen=True)
class ExecutionKeypair:
    private_pem: str
    public_pem: str


def generate_execution_keypair() -> ExecutionKeypair:
    key = Ed25519PrivateKey.generate()
    return ExecutionKeypair(
        private_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        public_pem=key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    )


@dataclass(frozen=True)
class HomesPrimeHarness:
    env: dict[str, str]
    guest_keypair: TestKeypair
    execution_keypair: ExecutionKeypair
    corpus_path: Path
    corpus_digest: str

    def fake(self) -> FakeSharedModelExecution:
        return FakeSharedModelExecution(
            public_key_pem=self.execution_keypair.public_pem, issuer=EXECUTION_ISSUER
        )


def build_homes_prime_env(
    tmp_path: Path,
    *,
    generate_ceiling_ms: int = 3000,
    review_ceiling_ms: int = 2000,
    transit_ms: int = 100,
    reserve_ms: int = 500,
    withdrawn: tuple[str, ...] = (),
    execution_url: str = EXECUTION_URL,
    extra: dict[str, str] | None = None,
) -> HomesPrimeHarness:
    guest_keypair = generate_test_keypair()
    execution_keypair = generate_execution_keypair()
    corpus_path, digest = write_synthetic_corpus(tmp_path)
    env = build_env(keypair=guest_keypair)
    for legacy in [name for name in env if name.startswith("LUCY_PUBLIC_")]:
        del env[legacy]
    prefix = "GUEST_ANSWER_PROVIDER_HOMES_PRIME_"
    env.update(
        {
            "GUEST_ANSWER_PROVIDER_ANSWER_ENGINE": "homes-prime",
            f"{prefix}EXECUTION_URL": execution_url,
            f"{prefix}EXECUTION_KEY_ID": "homes-prime-execution-test",
            f"{prefix}EXECUTION_ISSUER": EXECUTION_ISSUER,
            f"{prefix}EXECUTION_PRIVATE_KEY_PEM": execution_keypair.private_pem,
            f"{prefix}GENERATE_CEILING_MS": str(generate_ceiling_ms),
            f"{prefix}GENERATE_MAX_COST_MICROUSD": "40000",
            f"{prefix}REVIEW_CEILING_MS": str(review_ceiling_ms),
            f"{prefix}REVIEW_MAX_COST_MICROUSD": "20000",
            f"{prefix}TRANSIT_ALLOWANCE_MS": str(transit_ms),
            f"{prefix}RESERVE_MS": str(reserve_ms),
            f"{prefix}KNOWLEDGE_PATH": str(corpus_path),
            f"{prefix}KNOWLEDGE_ALLOWED_DIGESTS": digest,
            f"{prefix}KNOWLEDGE_WITHDRAWN_IDS_JSON": json.dumps(list(withdrawn)),
        }
    )
    env.update(extra or {})
    return HomesPrimeHarness(env, guest_keypair, execution_keypair, corpus_path, digest)


def draft(
    *segments: tuple[str, str, list[str]],
    outcome: str = "answered",
    link_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "segments": [{"kind": k, "text": t, "evidence_ids": e} for k, t, e in segments],
        "link_ids": link_ids or [],
    }


HARBOR_CAPACITY_DRAFT = draft(
    (
        "business_claim",
        "Harbor Light welcomes up to 12 guests and has parking for 3 cars.",
        ["harbor-light-capacity"],
    ),
    ("conversation", "Would you like to compare it with another home?", []),
    link_ids=["harbor-light-page-link"],
)

SUPPORTED = {"supported": True, "unsupported_segment_indexes": [], "reason_codes": []}
UNSUPPORTED = {
    "supported": False,
    "unsupported_segment_indexes": [0],
    "reason_codes": ["inverted_negation"],
}
