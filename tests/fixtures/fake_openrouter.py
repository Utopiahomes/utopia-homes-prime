"""A fake OpenRouter chat-completions endpoint for Homes' direct inference route, as an httpx
transport. It never calls a model: results are scripted per structured-output name. It checks the
routing guarantees Homes relies on (zero data retention, data collection denied, no fallbacks,
price ceilings, strict JSON schema) and refuses any other host, so a test that passes proves no
other network destination (such as Tiamat) was needed."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from fixtures.homes_prime import HomesPrimeHarness, build_homes_prime_env

HOST = "openrouter.ai"
PATH = "/api/v1/chat/completions"
API_KEY = "sk-or-test-" + "x" * 32
MODEL = "openai/gpt-oss-120b"


@dataclass
class Reply:
    content: dict[str, Any] | str | None = None
    status: int = 200
    cost_usd: float = 0.0004
    model: str | None = None
    finish_reason: str = "stop"
    retry_after: str | None = None
    raw: bytes | None = None


@dataclass
class FakeOpenRouter:
    scripts: dict[str, list[Reply]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def script(self, schema_name: str, *replies: Reply) -> None:
        self.scripts.setdefault(schema_name, []).extend(replies)

    def transport(self) -> httpx.AsyncBaseTransport:
        return _Transport(self)

    def handle(self, request: httpx.Request, body: bytes) -> httpx.Response:
        if request.url.host != HOST or request.url.path != PATH or request.method != "POST":
            self.violations.append(f"unexpected destination {request.method} {request.url}")
            raise httpx.ConnectError("fake OpenRouter refuses other destinations", request=request)
        document = json.loads(body)
        self.requests.append({"headers": dict(request.headers), "body": document})
        if request.headers.get("authorization") != f"Bearer {API_KEY}":
            self.violations.append("wrong credential")
        provider = document.get("provider", {})
        expected = {"zdr": True, "data_collection": "deny", "allow_fallbacks": False}
        if {k: provider.get(k) for k in expected} != expected or "max_price" not in provider:
            self.violations.append("routing guarantees missing")
        schema = document["response_format"]["json_schema"]
        if schema.get("strict") is not True:
            self.violations.append("schema not strict")
        queue = self.scripts.get(schema["name"], [])
        if not queue:
            self.violations.append(f"unscripted call for {schema['name']}")
            return httpx.Response(500, request=request)
        reply = queue.pop(0)
        headers = {"content-type": "application/json"}
        if reply.retry_after is not None:
            headers["retry-after"] = reply.retry_after
        if reply.raw is not None:
            return httpx.Response(reply.status, headers=headers, content=reply.raw, request=request)
        if reply.status != 200:
            return httpx.Response(reply.status, headers=headers, content=b"{}", request=request)
        content = reply.content if isinstance(reply.content, str) else json.dumps(reply.content)
        payload = {
            "id": f"gen-{uuid.uuid4().hex}",
            "model": reply.model or document["model"],
            "provider": "FakeProvider",
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": reply.finish_reason,
                }
            ],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 150, "cost": reply.cost_usd},
        }
        return httpx.Response(200, headers=headers, json=payload, request=request)


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, fake: FakeOpenRouter) -> None:
        self._fake = fake

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._fake.handle(request, await request.aread())


TIAMAT_ENV_MARKERS = ("EXECUTION_", "TIAMAT", "SHARED_MODEL")


def make_direct(env: dict[str, str]) -> dict[str, str]:
    """Moves a Homes Prime environment to the direct route and removes every Tiamat setting."""
    prefix = "GUEST_ANSWER_PROVIDER_HOMES_PRIME_"
    for name in [n for n in env if any(m in n for m in TIAMAT_ENV_MARKERS)]:
        del env[name]
    env.update(
        {
            f"{prefix}INFERENCE_BACKEND": "direct-openrouter",
            f"{prefix}OPENROUTER_API_KEY": API_KEY,
            f"{prefix}OPENROUTER_MODEL": MODEL,
            f"{prefix}OPENROUTER_MAX_PROMPT_USD_PER_MILLION": "0.2",
            f"{prefix}OPENROUTER_MAX_COMPLETION_USD_PER_MILLION": "0.8",
        }
    )
    return env


def build_direct_env(tmp_path: Path, **kwargs: Any) -> HomesPrimeHarness:
    """A Homes Prime environment on the direct route with no Tiamat setting of any kind."""
    harness = build_homes_prime_env(tmp_path, **kwargs)
    make_direct(harness.env)
    return harness
