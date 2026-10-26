"""Lyra's character (set by the company in Stoin Spaces) colors her small talk, never her facts."""

import pytest
from pydantic import ValidationError

from utopia_homes_prime.meeting_assist.meeting import (
    CHARACTER_POLICY,
    PERSONA_MAX,
    RespondRequest,
    build_respond_messages,
)

BEACH = "Lyra works from the beach in Wildwood, by the boardwalk and the Ferris wheel."


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
    assert 'CHARACTER="Lyra works from the beach' in system.content


def test_without_a_character_the_prompt_is_unchanged():
    for persona in (None, "  "):
        system, _ = build_respond_messages(_request(persona=persona), ())
        assert CHARACTER_POLICY not in system.content


def test_a_character_is_bounded():
    with pytest.raises(ValidationError):
        _request(persona="x" * (PERSONA_MAX + 1))


def test_homes_meeting_policies_share_lyras_identity():
    from utopia_homes_prime.meeting_assist.meeting import (
        DISPLAY_NAME,
        DRAFT_POLICY,
        RESPOND_POLICY,
        SEARCH_POLICY,
    )

    assert DISPLAY_NAME == "Lyra"
    for policy in (RESPOND_POLICY, SEARCH_POLICY, DRAFT_POLICY):
        assert policy.startswith("You are Lyra, Utopia Homes' corporate dragon")
        assert "Clara" not in policy
    for policy in (RESPOND_POLICY, SEARCH_POLICY):
        assert "LYE-ruh" in policy


def test_lyra_calls_herself_a_corporate_dragon_and_never_claims_to_be_a_person():
    """Ray, 2026-10-26: Lyra says "corporate dragon", never "AI"; asked sincerely, she still says
    she is not a real person, with or without her character."""
    from utopia_homes_prime.meeting_assist.meeting import RESPOND_POLICY as POLICY

    RESPOND_POLICY = " ".join(POLICY.split())  # the prompt wraps lines anywhere
    assert "corporate dragon" in RESPOND_POLICY
    assert "Never call yourself an AI" in RESPOND_POLICY
    assert "say plainly that you are not" in RESPOND_POLICY
    # Never a false denial: asked if she is an AI, she says yes, in her own words.
    assert "never deny it: say yes, you're synthetic" in RESPOND_POLICY
    assert "say plainly that you are an AI" not in RESPOND_POLICY + CHARACTER_POLICY
    for persona in (None, BEACH):
        system, _ = build_respond_messages(_request(persona=persona), ())
        assert "synthetic, not a person" in " ".join(system.content.split())
