"""Optional backend: Tiamat Shared Model Execution through its private inference.execute@1.0
contract (pinned under contracts/stoin-shared-model-execution-v1-rc1/). Homes selects it in its own
configuration; nothing else in Homes requires it."""

from __future__ import annotations

import uuid
from typing import Any, Final

from utopia_homes_prime.inference import sme_wire
from utopia_homes_prime.inference.backend import Deadline, InferenceCall, InferenceFailure
from utopia_homes_prime.inference.sme_client import SharedModelExecutionClient


class TiamatBackend:
    name: Final = "tiamat"

    def __init__(self, client: SharedModelExecutionClient) -> None:
        self._client = client

    async def infer(self, call: InferenceCall, *, deadline: Deadline) -> dict[str, Any]:
        # Inputs inside a Homes contract's bounds can still exceed one execution's bounds, for
        # example non-ASCII text measured in bytes. That is the caller's request being too large.
        encoded = sum(len(m.content.encode("utf-8")) for m in call.messages)
        if encoded > sme_wire.MESSAGES_TOTAL_MAX_BYTES or any(
            len(m.content) > sme_wire.MESSAGE_CONTENT_MAX_SCALARS for m in call.messages
        ):
            raise InferenceFailure("too_large")
        try:
            prepared = sme_wire.prepare_request(
                execution_profile_id=call.route,
                idempotency_key=str(uuid.uuid4()),
                messages=call.messages,
                output=call.output,
                max_output_tokens=call.max_output_tokens,
                max_cost_microusd=call.max_cost_microusd,
            )
        except sme_wire.WireViolation:
            # Any other construction failure is a Homes defect; it fails closed.
            raise InferenceFailure("unavailable", code="request_construction") from None
        result = await self._client.execute(
            prepared, profile_ceiling_ms=call.ceiling_ms, deadline=deadline
        )
        return result.content
