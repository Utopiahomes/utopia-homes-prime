"""Meeting access levels: Homes maps the verified people present to a level for each turn, and only
an admin turn carries Utopia's internal knowledge."""

from __future__ import annotations

import pytest
from fixtures.meeting import respond_body

from utopia_homes_prime.business_core.knowledge_items import KnowledgeItem
from utopia_homes_prime.meeting_assist.meeting import (
    INTERNAL_KNOWLEDGE_CHARS_MAX,
    RESPOND_POLICY,
    Audience,
    RespondRequest,
    build_respond_messages,
    parse_request,
    resolve_access_level,
    select_internal,
)

LEVELS = {"ray@utopiahomes.com": "admin", "meg@example.com": "public"}
RAY = ("Ray@UtopiaHomes.com ", True)
GUEST = ("guest@example.com", False)


def _audience(mode: str, *people: tuple[str, bool]) -> Audience:
    return Audience.model_validate(
        {"mode": mode, "people": [{"email": e, "is_host": h} for e, h in people]}
    )


@pytest.mark.parametrize(
    ("mode", "people", "level"),
    [
        ("host", [RAY, GUEST], "admin"),
        ("host", [("ray@utopiahomes.com", False), ("guest@example.com", True)], "public"),
        ("host", [("ray@utopiahomes.com", False)], "public"),  # no host present
        ("lowest", [RAY], "admin"),
        ("lowest", [RAY, GUEST], "public"),
        ("lowest", [RAY, ("meg@example.com", False)], "public"),
        ("highest", [GUEST, ("ray@utopiahomes.com", False)], "admin"),
        ("highest", [GUEST, ("stranger@example.org", True)], "public"),
        ("host", [], "public"),
        ("lowest", [], "public"),
        ("highest", [], "public"),
    ],
)
def test_levels_combine_by_mode(mode, people, level):
    assert resolve_access_level(_audience(mode, *people), LEVELS) == level


def test_no_audience_and_no_mapping_are_public():
    assert resolve_access_level(None, LEVELS) == "public"
    assert resolve_access_level(_audience("highest", RAY), {}) == "public"


def _item(n: int, text: str, topic: str = "ops") -> KnowledgeItem:
    return KnowledgeItem(
        id=f"ki-{n:012d}",
        audience="internal",
        topic=topic,
        title=f"Item {n}",
        text=text,
        status="active",
        created_by="test",
    )


def test_internal_items_all_fit_or_the_relevant_ones_are_kept():
    small = [_item(1, "Cleaners come on Tuesdays."), _item(2, "Payouts go out on the 7th.")]
    assert [i["title"] for i in select_internal(small, ["Anything?"])] == ["Item 1", "Item 2"]

    filler = [_item(n, "x" * 1_900, topic=f"filler{n}") for n in range(10, 40)]
    many = [*filler, _item(5, "Owner payouts go out on the 7th of each month.", "payouts")]
    chosen = select_internal(many, ["Welcome.", "When do owner payouts go out?"])
    assert chosen[0]["title"] == "Item 5"
    assert all(i["title"] != "Item 10" for i in chosen)  # unrelated filler is left out
    assert sum(len(str(i)) for i in chosen) <= INTERNAL_KNOWLEDGE_CHARS_MAX


def test_the_public_prompt_is_unchanged_and_admin_adds_internal_knowledge():
    request = parse_request(respond_body(), RespondRequest)
    public_system = build_respond_messages(request, ())[0].content
    assert public_system.startswith(RESPOND_POLICY + "\n\nAPPROVED_MATERIALS=")
    assert "INTERNAL_KNOWLEDGE" not in public_system and "ACCESS:" not in public_system
    internal = [{"title": "Payout day", "text": "The 7th.", "property": None}]
    admin_system = build_respond_messages(request, (), internal=internal)[0].content
    assert "ACCESS: The requester's verified access level" in admin_system
    assert admin_system.endswith(
        'INTERNAL_KNOWLEDGE=[{"title":"Payout day","text":"The 7th.","property":null}]'
    )
