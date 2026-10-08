"""Clara's character (set by the company in Stoin Spaces) colors her small talk, never her facts."""

import pytest
from pydantic import ValidationError

from utopia_homes_prime.meeting_assist.meeting import (
    CHARACTER_POLICY,
    PERSONA_MAX,
    RespondRequest,
    build_respond_messages,
)

BEACH = "Clara works from the beach in Wildwood, by the boardwalk and the Ferris wheel."


def _request(**extra) -> RespondRequest:
    return RespondRequest.model_validate(
        {
            "contract_version": "1.0",
            "meeting_id": "meeting-1",
            "turn_id": "6f1c1c7e-2f0a-4c55-9a39-0f1e8f2b9b11",
            "requester": {"display_name": "Ray", "is_host": True},
            "message": "How's the beach today?",
            "context": [],
            "materials": [],
            "locale": "en-US",
            **extra,
        }
    )


def test_a_character_joins_the_prompt_as_data_after_the_rules():
    system, _ = build_respond_messages(_request(persona=BEACH), ())
    assert CHARACTER_POLICY in system.content
    assert 'CHARACTER="Clara works from the beach' in system.content


def test_without_a_character_the_prompt_is_unchanged():
    for persona in (None, "  "):
        system, _ = build_respond_messages(_request(persona=persona), ())
        assert CHARACTER_POLICY not in system.content


def test_a_character_is_bounded():
    with pytest.raises(ValidationError):
        _request(persona="x" * (PERSONA_MAX + 1))
