"""Python port of utopia-homes-web/lib/lucy/server.ts's askPublicLucy().

Calls the SAME already-working legacy FAQ-snapshot endpoint the website currently calls (same env
vars, same headers, same 10s timeout, same exact snapshot_digest check) — this is the "accepted
current guest-answer behavior" RC2 §20.2's strangler step 2 requires wrapping, not rebuilding.
Faithfully mirrors the TypeScript original including discarding the upstream `source` field
(askPublicLucy returns only `{answer}` — the source is validated as part of the upstream
contract's shape, but never surfaced past this layer, exactly as in the original).
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from guest_answer_provider.config import LegacyUpstreamConfig

DEFAULT_TIMEOUT_SECONDS = 10.0


class LegacyUpstreamUnavailable(Exception):
    """Mirrors PublicLucyUnavailable — collapses transport failure, timeout, non-2xx status,
    malformed/non-conforming JSON, and snapshot_digest mismatch into one generic signal."""


class _LegacyAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str = Field(min_length=1, max_length=8_000)
    source: str = Field(min_length=1, max_length=2_000)
    version: int = Field(gt=0)
    snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


async def ask_legacy_lucy(
    question: str,
    session_id: str,
    *,
    config: LegacyUpstreamConfig,
    client: httpx.AsyncClient,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Returns the validated answer text. Raises LegacyUpstreamUnavailable on any failure."""
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.token}",
        "Content-Type": "application/json",
        "Origin": f"https://{config.site_hostname}",
        "X-Lucy-Public-Host": config.site_hostname,
        "X-Lucy-Public-Session": session_id,
    }

    try:
        response = await client.post(
            config.endpoint,
            headers=headers,
            json={"question": question},
            timeout=timeout_seconds,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise LegacyUpstreamUnavailable() from exc

    if response.status_code // 100 != 2:
        raise LegacyUpstreamUnavailable()

    try:
        payload = response.json()
    except ValueError as exc:
        raise LegacyUpstreamUnavailable() from exc

    try:
        legacy_answer = _LegacyAnswer.model_validate(payload)
    except ValidationError as exc:
        raise LegacyUpstreamUnavailable() from exc

    if legacy_answer.snapshot_digest != config.snapshot_digest:
        raise LegacyUpstreamUnavailable()

    return legacy_answer.answer
