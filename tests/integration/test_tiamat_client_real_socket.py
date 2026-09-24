"""Drives SharedModelExecutionClient over a genuine loopback TCP socket against
fixtures/fake_sme_server.py, not the in-process httpx transport substitution every other test
uses (fixtures/fake_sme.py's `.transport()`). Every other Stage 2 test proves sme_wire.py and
sme_client.py build/parse the RC1 wire format correctly; this is the one test proving that also
holds when real bytes cross a real socket -- header casing as actually transmitted, a body read
back off the wire, a real connection lifecycle -- which a transport substitution never exercises.

This is deliberately the smallest possible proof (one successful execution end to end), not a
second copy of test_sme_client.py's behavior matrix: the retry/backoff/error-mapping logic is
already fully covered against the in-process fake, and none of it is socket-specific.
"""

from __future__ import annotations

import time
import uuid

import httpx
from fixtures.fake_sme import FakeSharedModelExecution, success
from fixtures.fake_sme_server import run as run_fake_tiamat
from fixtures.homes_prime import EXECUTION_ISSUER, GENERATE, generate_execution_keypair

from utopia_homes_prime.inference import sme_wire
from utopia_homes_prime.inference.sme_client import (
    Deadline,
    ExecutionIdentity,
    SharedModelExecutionClient,
)
from utopia_homes_prime.inference.sme_wire import ExecutionMessage, JsonSchemaOutput


def _prepared():
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string", "minLength": 1, "maxLength": 200}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    return sme_wire.prepare_request(
        execution_profile_id=GENERATE,
        idempotency_key=str(uuid.uuid4()),
        messages=(
            ExecutionMessage("system", "policy"),
            ExecutionMessage("user", "How many guests fit at Harbor Light?"),
        ),
        output=JsonSchemaOutput("homes-guest-answer-draft", schema),
        max_output_tokens=300,
        max_cost_microusd=20_000,
    )


async def test_execution_succeeds_over_a_real_loopback_socket():
    keys = generate_execution_keypair()
    fake = FakeSharedModelExecution(public_key_pem=keys.public_pem, issuer=EXECUTION_ISSUER)
    fake.script(GENERATE, success({"answer": "Harbor Light sleeps six guests."}))
    identity = ExecutionIdentity.from_pem(
        kid="homes-prime-execution-test",
        issuer=EXECUTION_ISSUER,
        subject="stoin:synth:utopia-homes-prime",
        private_key_pem=keys.private_pem,
    )

    async with run_fake_tiamat(fake) as endpoint_url:
        assert endpoint_url.startswith("http://127.0.0.1:")
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as http:
            client = SharedModelExecutionClient(
                endpoint_url=endpoint_url,
                identity=identity,
                http=http,
                transit_allowance_ms=200,
            )
            result = await client.execute(
                _prepared(),
                profile_ceiling_ms=3000,
                deadline=Deadline(time.monotonic() + 5),
            )

    assert result.content == {"answer": "Harbor Light sleeps six guests."}
    assert not fake.violations
    assert len(fake.attempts) == 1
