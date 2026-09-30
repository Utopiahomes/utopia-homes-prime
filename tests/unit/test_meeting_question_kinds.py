"""Meeting Lucy answers three kinds of question: Utopia ones strictly from Utopia sources, general
ones from the model's own knowledge, and current-events ones through a web-search call that sees
only a standalone question."""

from __future__ import annotations

import pytest
from fixtures.fake_openrouter import make_direct
from fixtures.meeting import build_meeting_env, reply, respond_body

from utopia_homes_prime.config import Config, ConfigError
from utopia_homes_prime.inference.backend import InferenceFailure
from utopia_homes_prime.meeting_assist.meeting import (
    FALLBACK_ANSWER,
    SEARCH_FAILED_ANSWER,
    SEARCH_OFF_ANSWER,
    MeetingEngine,
    OperationSettings,
    RespondRequest,
    parse_request,
    spoken_text,
)


class ScriptedBackend:
    name = "scripted"

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def infer(self, call, *, deadline):
        self.calls.append(call)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _engine(backend, search_backend=None):
    return MeetingEngine(
        respond=OperationSettings("respond", 6_000, 1_200, 20_000, 10_000),
        draft=OperationSettings("draft", 15_000, 2_500, 20_000, 20_000),
        transit_allowance_ms=100,
        reserve_ms=500,
        backend=backend,
        search=OperationSettings("search", 7_000, 600, 55_000, 9_000),
        search_backend=search_backend,
    )


def _request(message: str) -> RespondRequest:
    return parse_request(respond_body(message, materials=[]), RespondRequest)


async def test_general_questions_are_answered_from_general_knowledge():
    backend = ScriptedBackend(
        reply("Paris has been France's capital since 987.", kind="general", display=None)
    )
    result = await _engine(backend).respond(_request("How long has Paris been the capital?"), ())
    assert result["answer"] == "Paris has been France's capital since 987."
    assert len(backend.calls) == 1


async def test_a_general_answer_that_speaks_for_utopia_keeps_the_utopia_rules():
    backend = ScriptedBackend(
        reply("Our management fee is 25 percent.", kind="general", display=None),
        reply("Our management fee is 30 percent.", kind="general", display=None),
    )
    result = await _engine(backend).respond(_request("What's the fee?"), ())
    assert result["answer"] == FALLBACK_ANSWER
    assert len(backend.calls) == 2


async def test_current_questions_search_with_only_the_standalone_question():
    first = ScriptedBackend(
        reply("", kind="current", display=None, search_query="Who is the US president today?")
    )
    search = ScriptedBackend(
        {"answer": "According to [Reuters](https://reuters.com/x), it is Jane Doe [1]."}
    )
    result = await _engine(first, search).respond(_request("Who is president? Sample Owner"), ())
    assert result["outcome"] == "answered"
    assert result["answer"] == "According to Reuters, it is Jane Doe."
    sent = " ".join(message.content for message in search.calls[0].messages)
    assert "Who is the US president today?" in sent
    assert "Sample Owner" not in sent and "Welcome" not in sent  # nothing from the meeting
    assert search.calls[0].max_cost_microusd == 55_000


@pytest.mark.parametrize(
    "search_result",
    [InferenceFailure("deadline", code="timeout"), {"answer": "Utopia Homes charges 25%."}],
)
async def test_a_failed_lookup_is_said_not_silent(search_result):
    first = ScriptedBackend(reply("", kind="current", display=None, search_query="Weather today?"))
    result = await _engine(first, ScriptedBackend(search_result)).respond(
        _request("What's the weather?"), ()
    )
    assert (result["outcome"], result["answer"]) == ("answered", SEARCH_FAILED_ANSWER)


async def test_without_a_search_route_current_questions_get_an_honest_line():
    first = ScriptedBackend(reply("", kind="current", display=None, search_query="News today?"))
    result = await _engine(first).respond(_request("Any news?"), ())
    assert result["answer"] == SEARCH_OFF_ANSWER


def test_spoken_text_drops_links_and_citation_marks():
    assert spoken_text("See www.example.com for more [2].") == "See for more."


def _direct_meeting_env(tmp_path, **search):
    prime, _ = build_meeting_env(tmp_path)
    make_direct(prime.env)
    prefix = "GUEST_ANSWER_PROVIDER_MEETING_SEARCH_"
    prime.env.update(
        {
            f"{prefix}MODEL": "google/gemini-3.8-flash",
            f"{prefix}MAX_COST_MICROUSD": "55000",
            f"{prefix}MAX_PROMPT_USD_PER_MILLION": "1.0",
            f"{prefix}MAX_COMPLETION_USD_PER_MILLION": "4.0",
        }
    )
    prime.env.update({prefix + name: value for name, value in search.items()})
    return prime.env


def test_search_config_reads_and_caps_the_cost(tmp_path):
    search = Config.from_environment(_direct_meeting_env(tmp_path)).meeting.search
    assert (search.model, search.engine, search.profile.max_cost_microusd) == (
        "google/gemini-3.8-flash",
        "native",
        55_000,
    )
    with pytest.raises(ConfigError, match="at most 60000"):
        Config.from_environment(_direct_meeting_env(tmp_path, MAX_COST_MICROUSD="60001"))
