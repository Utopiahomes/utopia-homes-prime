"""Utopia Lucy's Hermes plugin registers its four business tools and talks to the business core
exactly as the core expects (path, token, attribution). The HTTP layer is faked; no network."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "lucy/profile/plugins/utopia_business/__init__.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("utopia_business", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {}

    def register_tool(self, **kwargs: Any) -> None:
        self.tools[kwargs["name"]] = kwargs


def test_it_registers_the_four_business_tools():
    ctx = FakeContext()
    load_plugin().register(ctx)
    assert set(ctx.tools) >= {
        "utopia_list_properties",
        "utopia_get_property",
        "utopia_update_property",
        "utopia_property_history",
    }
    assert all(t["toolset"] == "utopia_business" for t in ctx.tools.values())
    assert all(t["schema"]["name"] == name for name, t in ctx.tools.items())


def test_an_update_is_attributed_to_the_operator(monkeypatch: pytest.MonkeyPatch):
    plugin = load_plugin()
    monkeypatch.setenv("UTOPIA_BUSINESS_API_URL", "http://core.test/")
    monkeypatch.setenv("UTOPIA_BUSINESS_LUCY_TOKEN", "secret-token")
    monkeypatch.setenv("UTOPIA_LUCY_OPERATOR_NAME", "Ray")
    seen: dict[str, Any] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        seen.update(
            url=request.full_url,
            method=request.get_method(),
            auth=request.get_header("Authorization"),
            body=json.loads(request.data),
        )
        return Response()

    monkeypatch.setattr(plugin.urllib.request, "urlopen", fake_urlopen)
    plugin._update(
        {
            "slug": "the-shamrock",
            "changes": {"parking": "Parking for 5 cars."},
            "reason": "Ray: garage",
        }
    )
    assert seen == {
        "url": "http://core.test/internal/v1/properties/the-shamrock",
        "method": "PATCH",
        "auth": "Bearer secret-token",
        "body": {
            "changes": {"parking": "Parking for 5 cars."},
            "changed_by": "Ray via Utopia Lucy",
            "reason": "Ray: garage",
        },
    }


def fake_core(monkeypatch: pytest.MonkeyPatch, plugin: Any, reply: dict[str, Any]) -> list[Any]:
    monkeypatch.setenv("UTOPIA_BUSINESS_API_URL", "http://core.test")
    monkeypatch.setenv("UTOPIA_BUSINESS_LUCY_TOKEN", "secret-token")
    monkeypatch.setenv("UTOPIA_LUCY_OPERATOR_NAME", "Ray")
    calls: list[Any] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(reply).encode()

    def fake_urlopen(request, timeout):
        calls.append((request.get_method(), request.full_url,
                      json.loads(request.data) if request.data else None))  # fmt: skip
        return Response()

    monkeypatch.setattr(plugin.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_requests_for_work_become_work_items(monkeypatch: pytest.MonkeyPatch):
    plugin = load_plugin()
    ctx = FakeContext()
    plugin.register(ctx)
    assert {"utopia_open_work", "utopia_list_work", "utopia_update_work"} <= set(ctx.tools)
    stored = {"id": "wi-000000000001", "type": "maintenance", "title": "Pool heater",
              "status": "open", "purpose": "long text", "notes": [{"text": "called"}],
              "input_refs": [], "created_at": "x"}  # fmt: skip
    calls = fake_core(monkeypatch, plugin, {"work": stored})
    out = json.loads(
        plugin._open_work({"type": "maintenance", "title": "Pool heater",
                           "purpose": "Guests say it's cold", "slug": "buttercup-beauty"})
    )  # fmt: skip
    method, url, body = calls[0]
    assert (method, url) == ("POST", "http://core.test/internal/v1/work")
    assert body["requested_by"] == "Ray via Utopia Lucy" and body["item"]["status"] == "open"
    assert body["item"]["property_slug"] == "buttercup-beauty"
    assert out["work"]["last_note"] == "called" and "purpose" not in out["work"]


def test_a_property_read_can_ask_for_just_some_fields(monkeypatch: pytest.MonkeyPatch):
    plugin = load_plugin()
    fake_core(monkeypatch, plugin, {
        "property": {"slug": "the-shamrock", "parking": "Parking for 8 cars.",
                     "full_description": "long" * 200},
        "unconfirmed_fields": ["parking", "full_description"],
    })  # fmt: skip
    out = json.loads(plugin._get({"slug": "the-shamrock", "fields": ["parking"]}))
    assert out == {
        "property": {"slug": "the-shamrock", "parking": "Parking for 8 cars."},
        "unconfirmed_fields": ["parking"],
    }
