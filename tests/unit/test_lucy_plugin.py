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
    assert set(ctx.tools) == {
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
