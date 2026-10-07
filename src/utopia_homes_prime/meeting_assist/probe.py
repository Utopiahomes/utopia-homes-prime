"""Operator check: ask meeting Clara a few made-up questions and print the SHAPE of each reply.

    python -m utopia_homes_prime.meeting_assist.probe [--proposals]

Runs in the deployed service's environment (a Render job), against the real model, at public
level (no audience), so the prompt holds only public knowledge. It prints field names, value
types, and string lengths, whether the reply passed Homes' checks, and whether it proposed an
action item, never what was said. `--proposals` tries the action-item proposals switch without
turning it on for live meetings.
"""

from __future__ import annotations

import argparse
import uuid
from typing import Any

from fastapi.testclient import TestClient

from utopia_homes_prime.business_api.api import create_app
from utopia_homes_prime.config import Config
from utopia_homes_prime.inference import direct_openrouter
from utopia_homes_prime.meeting_assist.meeting import RespondRequest

QUESTIONS = (
    "Where does Utopia Homes operate?",
    "Clara, can you draft a one-page summary of the Shamrock for Linda?",
    "Can someone send Linda the onboarding checklist? Clara, make that an action item.",
)

_validate = direct_openrouter.validate_instance  # type: ignore[attr-defined]


def _shape_check(schema: dict[str, Any], value: object, where: str = "$") -> None:
    if where == "$" and isinstance(value, dict):
        shape = {
            key: type(item).__name__ + (f":{len(item)}" if isinstance(item, str) else "")
            for key, item in sorted(value.items())
        }
        missing = sorted(set(schema.get("properties", {})) - set(value))
        print("PROBE shape", shape, "missing", missing)
    _validate(schema, value, where)


async def _ask(engine: Any, message: str) -> None:
    request = RespondRequest.model_validate(
        {
            "contract_version": "1.0",
            "meeting_id": "probe-meeting",
            "turn_id": str(uuid.uuid4()),
            "requester": {"display_name": "Probe", "is_host": True},
            "message": message,
            "context": [],
            "materials": [],
            "locale": "en-US",
        }
    )
    result = await engine.respond(request, ())
    proposes = "propose_action" in result
    print(f"PROBE result outcome={result.get('outcome')} proposes={proposes}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--proposals", action="store_true", help="Try action-item proposals")
    args = parser.parse_args()
    direct_openrouter.validate_instance = _shape_check  # type: ignore[attr-defined]
    app = create_app(config=Config.from_environment())
    with TestClient(app) as client:
        engine = app.state.meeting_engine
        engine._proposals = args.proposals
        for message in QUESTIONS:
            assert client.portal is not None
            client.portal.call(_ask, engine, message)


if __name__ == "__main__":
    main()
