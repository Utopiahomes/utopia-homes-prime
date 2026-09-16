"""Maps between RC2's guest.answer@1.0 shape and the legacy FAQ-snapshot upstream's flatter shape.

This is a PRECONFORMANT compatibility stage, not the conformant Homes Prime provider — see
docs/guest-answer-preview-rollout.md. Every successful response carries a fixed limitations[]
entry disclosing that status on the wire itself (build_limitations() below); this is not
optional and must never be dropped.

Every mapping decision here is a genuine ambiguity RC2 leaves open by design — it specifies the
wire contract, not how a strangler-mode provider must source its answers. Each is logged in
docs/implementation-notes.md with its reasoning; see that file for the full write-up. In short:
content outside legacy's narrower length bound is never truncated (would silently change the
guest's question) and never invalid_request (the request IS valid); it becomes
answer_validation_failed. Legacy has no sources/actions concept, so those stay empty rather than
being fabricated in a way that could violate I-B08's exact-destination invariant.
"""

from __future__ import annotations

import httpx

from guest_answer_provider.config import LegacyUpstreamConfig
from guest_answer_provider.errors import AnswerValidationFailedError, TemporarilyUnavailableError
from guest_answer_provider.legacy_upstream import LegacyUpstreamUnavailable, ask_legacy_lucy
from guest_answer_provider.patterns import ANSWER_TEXT_MAX, ANSWER_TEXT_MIN

LEGACY_QUESTION_MIN = 2
LEGACY_QUESTION_MAX = 500

_PRECONFORMANT_LIMITATION = (
    "This response comes from a preconformant compatibility stage that delegates to a legacy FAQ "
    "lookup; it does not yet satisfy guest.answer@1.0's grounding and knowledge requirements "
    "(RC2 §§13-18)."
)
_SOURCE_LIMITATION = (
    "This answer is generated from a static FAQ reference and does not cite a specific source page."
)
_HISTORY_IGNORED_LIMITATION = "This answer does not take earlier conversation turns into account."

assert len(_PRECONFORMANT_LIMITATION) <= 240  # RC2 common.defs.json limitationText bound


def content_to_legacy_question(content: str) -> str | None:
    """Returns the legacy-bound question, or None if `content` (already RC2-valid, 1-2000 chars)
    falls outside legacy's narrower 2-500 char bound. Never truncates or pads."""
    if len(content) < LEGACY_QUESTION_MIN or len(content) > LEGACY_QUESTION_MAX:
        return None
    return content


def build_limitations(*, history_present: bool) -> list[str]:
    """Every response from this preconformant stage carries a fixed limitation disclosing that
    status on the wire itself, per the Control-side correction recorded in
    docs/guest-answer-preview-rollout.md — this must never be silently dropped or made
    conditional, since doing so would let a caller receive an answer with no signal that it
    didn't come from a conformant provider."""
    limitations = [_PRECONFORMANT_LIMITATION, _SOURCE_LIMITATION]
    if history_present:
        limitations.append(_HISTORY_IGNORED_LIMITATION)
    return limitations


async def answer_via_legacy(
    content: str,
    session_id: str,
    *,
    history_present: bool,
    config: LegacyUpstreamConfig,
    client: httpx.AsyncClient,
) -> tuple[str, list[str]]:
    """Returns (answer_text, limitations) for a successful `outcome: "answered"` response.

    Raises AnswerValidationFailedError (503, non-retryable) when the request's content falls
    outside what the legacy engine can accept, or the legacy answer itself falls outside RC2's
    tighter answerText bound. Raises TemporarilyUnavailableError (503, retryable) for any opaque
    legacy transport/schema/digest failure.
    """
    question = content_to_legacy_question(content)
    if question is None:
        raise AnswerValidationFailedError()

    try:
        answer = await ask_legacy_lucy(question, session_id, config=config, client=client)
    except LegacyUpstreamUnavailable as exc:
        raise TemporarilyUnavailableError() from exc

    if not (ANSWER_TEXT_MIN <= len(answer) <= ANSWER_TEXT_MAX):
        raise AnswerValidationFailedError()

    return answer, build_limitations(history_present=history_present)
