"""The inference seam Homes owns.

Everything before it (knowledge, retrieval, prompts, context assembly) and after it (validation,
sources, actions, the final answer, failure selection) is Homes business logic and is identical
whichever backend runs. Homes selects the backend in its own configuration:

- `direct-openrouter` (inference/direct_openrouter.py): Homes' own provider route, credentials,
  limits, and accounting. It needs no Tiamat account, grant, service, or network connection.
- `tiamat` (inference/tiamat.py): optional. Shared Model Execution through its private
  `inference.execute@1.0` contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from utopia_homes_prime.inference.structured_output import ExecutionMessage, JsonSchemaOutput

FailureCategory = Literal[
    "deadline", "rate_limited", "unsupported_output", "unavailable", "too_large"
]


class InferenceFailure(Exception):
    """An inference produced no usable candidate. Content-free by construction: it carries only a
    category, a backend code when there is one, and retry information."""

    def __init__(
        self,
        category: FailureCategory,
        *,
        code: str | None = None,
        retry_after_seconds: int | None = None,
        attempts: int = 0,
    ) -> None:
        super().__init__(f"{category}:{code or 'none'}")
        self.category = category
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts


@dataclass(frozen=True, slots=True)
class Deadline:
    """Absolute monotonic deadline in seconds."""

    at: float

    def remaining_ms(self, now: float) -> int:
        return int((self.at - now) * 1000)


@dataclass(frozen=True, slots=True)
class InferenceCall:
    """One logical inference. `route` is the Homes route name; the Tiamat backend uses it as the
    execution profile ID, and the direct backend's configuration maps every route to its model."""

    route: str
    messages: tuple[ExecutionMessage, ...]
    output: JsonSchemaOutput
    max_output_tokens: int
    max_cost_microusd: int
    ceiling_ms: int


class InferenceBackend(Protocol):
    @property
    def name(self) -> str: ...

    async def infer(self, call: InferenceCall, *, deadline: Deadline) -> dict[str, Any]:
        """Returns output content already validated against `call.output.schema`, or raises
        InferenceFailure."""
        ...
