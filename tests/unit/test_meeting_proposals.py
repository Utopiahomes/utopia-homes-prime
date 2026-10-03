from typing import Any

from utopia_homes_prime.meeting_assist.meeting import (
    PROPOSALS_POLICY,
    RESPOND_POLICY,
    RespondRequest,
    build_respond_messages,
    check_reply,
    respond_schema,
)


def _request(message: str = "Can someone send Linda the onboarding checklist?") -> RespondRequest:
    return RespondRequest.model_validate(
        {
            "contract_version": "1.0",
            "meeting_id": "meeting-1",
            "turn_id": "6f1c1c7e-2f0a-4c55-9a39-0f1e8f2b9b11",
            "requester": {"display_name": "Ray", "is_host": True},
            "message": message,
            "context": [],
            "materials": [],
            "locale": "en-US",
        }
    )


def _content(**extra: Any) -> dict[str, Any]:
    return {
        "kind": "utopia",
        "outcome": "answered",
        "answer": "I've proposed that as an action item for someone to assign.",
        "search_query": "",
        "display_material": None,
        "show_file": None,
        "show_page": None,
        "decline_reason": None,
        **extra,
    }


def test_proposals_off_keeps_the_schema_and_prompt_exactly_as_before():
    schema = respond_schema(())
    assert "action_item" not in schema["properties"]
    assert "action_item" not in schema["required"]
    system, _ = build_respond_messages(_request(), ())
    assert system.content.startswith(RESPOND_POLICY)
    assert PROPOSALS_POLICY not in system.content


def test_proposals_on_adds_the_fields_and_the_instruction():
    schema = respond_schema((), proposals=True)
    assert schema["properties"]["action_item"] == {"type": "string", "maxLength": 300}
    assert {"action_item", "action_context"} <= set(schema["required"])
    system, _ = build_respond_messages(_request(), (), proposals=True)
    assert PROPOSALS_POLICY in system.content


def test_a_proposed_action_rides_on_the_answer():
    reply = check_reply(
        _content(action_item="Send Linda   the checklist", action_context="She asked for it."),
        _request(),
        (),
    )
    assert reply["propose_action"] == {
        "description": "Send Linda the checklist",
        "context": "She asked for it.",
    }
    assert "propose_action" not in check_reply(_content(action_item=""), _request(), ())
    assert "propose_action" not in check_reply(_content(), _request(), ())
