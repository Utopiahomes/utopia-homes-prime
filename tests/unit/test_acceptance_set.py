"""The Homes acceptance set is well formed: every request it produces is a valid RC2 request, and
every source it expects exists in the knowledge it is written against. No network."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from utopia_homes_prime.guest_answer import schema_validation

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = json.loads(
    (ROOT / "knowledge/evaluation/homes-guest-answer-acceptance.v1.json").read_text(
        encoding="utf-8"
    )
)
R1 = json.loads((ROOT / "knowledge/r1/utopia-public-knowledge.r1.json").read_text(encoding="utf-8"))
SOURCES = {entry["source"]["id"] for entry in R1["entries"]}
OUTCOMES = {"answered", "partial", "clarification_needed", "out_of_scope", "refused"}
TURN_KEYS = {
    "message",
    "page_context",
    "allowed_outcomes",
    "required_term_groups",
    "forbidden_terms",
    "required_source_ids",
    "forbidden_source_ids",
    "max_characters",
}


def test_the_set_is_large_enough_and_ids_are_unique():
    ids = [c["id"] for c in DOCUMENT["conversations"]]
    assert len(ids) == len(set(ids))
    assert 40 <= len(ids) <= 80
    assert (
        sum(bool(len(c["turns"]) > 1 or c["initial_history"]) for c in DOCUMENT["conversations"])
        >= 8
    )


@pytest.mark.parametrize("conversation", DOCUMENT["conversations"], ids=lambda c: c["id"])
def test_every_turn_is_a_valid_request_with_known_sources(conversation):
    history = [{"turn_id": str(uuid.uuid4()), **t} for t in conversation["initial_history"]]
    for turn in conversation["turns"]:
        assert set(turn) == TURN_KEYS
        assert set(turn["allowed_outcomes"]) <= OUTCOMES and turn["allowed_outcomes"]
        assert set(turn["required_source_ids"]) | set(turn["forbidden_source_ids"]) <= SOURCES
        assert all(group for group in turn["required_term_groups"])
        request = {
            "contract_version": "1.0",
            "session_id": "27b20c7b-777e-4af3-8db1-5fe1fc2f3b7e",
            "message": {
                "turn_id": "056d5f10-ed13-44fa-b8bc-1af90d43dceb",
                "content": turn["message"],
            },
            "locale": "en-US",
        }
        if history:
            request["history"] = history
        if turn["page_context"] is not None:
            request["page_context"] = turn["page_context"]
        schema_validation.validate_request(request)
        history = [
            *history,
            {"turn_id": str(uuid.uuid4()), "role": "user", "content": turn["message"]},
            {"turn_id": str(uuid.uuid4()), "role": "assistant", "content": "A prior answer."},
        ]
