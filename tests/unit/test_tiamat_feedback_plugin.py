from __future__ import annotations

import importlib.util
import json
import urllib.error
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "lucy/profile/plugins/tiamat_feedback"
TOKEN = "dedicated-tiamat-feedback-token-for-tests"


def load():
    spec = importlib.util.spec_from_file_location("tiamat_feedback", PLUGIN / "__init__.py")
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    return plugin


def test_registration_uses_only_dedicated_feedback_credentials():
    tools: dict[str, Any] = {}

    class Context:
        def register_tool(self, **kwargs):
            tools[kwargs["name"]] = kwargs

    plugin = load()
    plugin.register(Context())
    assert set(tools) == {"utopia_record_feedback"}
    tool = tools["utopia_record_feedback"]
    assert tool["requires_env"] == ["UTOPIA_TIAMAT_FEEDBACK_ORIGIN", "UTOPIA_TIAMAT_FEEDBACK_TOKEN"]
    assert tool["toolset"] == "utopia_business"
    assert tool["schema"]["parameters"]["additionalProperties"] is False
    manifest = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
    assert "  - utopia_record_feedback" in manifest


def configured(monkeypatch):
    plugin = load()
    monkeypatch.setenv("UTOPIA_TIAMAT_FEEDBACK_ORIGIN", "https://feedback.example")
    monkeypatch.setenv("UTOPIA_TIAMAT_FEEDBACK_TOKEN", TOKEN)
    return plugin


def test_explicit_feedback_defaults_private_and_does_not_use_business_auth(monkeypatch):
    plugin = configured(monkeypatch)
    calls = []

    class Opener:
        def open(self, request, timeout):
            body = json.loads(request.data)
            calls.append((request, timeout, body))

            class Response:
                status = 201

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return None

                def read(self, limit):
                    return json.dumps(
                        {"accepted": True, "report_id": body["report_id"], "duplicate": False}
                    ).encode()

            return Response()

    handlers = []

    def opener(*args):
        handlers.extend(args)
        return Opener()

    monkeypatch.setattr(plugin.urllib.request, "build_opener", opener)
    args = {"target": "homes_dragon", "topic": "Answer quality", "text": "Ask fewer questions"}
    result = json.loads(plugin._record(args))
    assert result["outcome"] == "recorded" and result["product_owner_delivery"] == "not_sent"
    request, timeout, body = calls[0]
    assert request.full_url == "https://feedback.example/api/field/intake/homes" and timeout == 10
    assert request.get_header("Authorization") == "Bearer " + TOKEN
    assert body["target"] == "dragon" and body["sharing"] == "private"
    assert body["evidence_kind"] == "operator_observation" and body["synthetic"] is False
    assert str(UUID(result["report_id"])) == result["report_id"]
    assert any(isinstance(h, plugin._NoRedirect) for h in handlers)
    assert any(
        isinstance(h, plugin.urllib.request.ProxyHandler) and h.proxies == {} for h in handlers
    )


def test_missing_permission_and_extra_chat_are_rejected_before_network(monkeypatch):
    plugin = configured(monkeypatch)
    args = {"target": "stoin", "topic": "Feedback", "text": "Feedback wording"}
    monkeypatch.setattr(
        plugin.urllib.request, "build_opener", lambda *args: pytest.fail("Must not contact intake")
    )
    for body in (
        {**args, "sharing": "summary"},
        {**args, "history": ["private chat"]},
        {**args, "synthetic": "false"},
    ):
        assert json.loads(plugin._record(body))["outcome"] == "not_recorded"
    monkeypatch.setenv("UTOPIA_TIAMAT_FEEDBACK_ORIGIN", "http://external.example")
    assert json.loads(plugin._record(args))["outcome"] == "not_recorded"
    monkeypatch.delenv("UTOPIA_TIAMAT_FEEDBACK_TOKEN")
    assert json.loads(plugin._record(args))["error"] == "feedback_not_configured"


def test_unknown_outcome_retains_report_id_and_never_retries(monkeypatch):
    plugin = configured(monkeypatch)
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(json.loads(request.data))
            raise urllib.error.URLError("private credential diagnostic " + TOKEN)

    monkeypatch.setattr(plugin.urllib.request, "build_opener", lambda *args: Opener())
    args = {
        "target": "stoin",
        "topic": "Joining",
        "text": "PRIVATE feedback",
        "sharing": "summary",
        "approved_summary": "Approved summary",
        "synthetic": True,
    }
    first = json.loads(plugin._record(args))
    assert first["outcome"] == "unknown" and len(calls) == 1
    second = json.loads(plugin._record({**args, "report_id": first["report_id"]}))
    assert second["report_id"] == first["report_id"] and len(calls) == 2
    assert calls[0] == calls[1]
    assert "PRIVATE" not in json.dumps(first) and TOKEN not in json.dumps(first)
