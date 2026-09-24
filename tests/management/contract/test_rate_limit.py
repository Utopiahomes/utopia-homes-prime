"""§13/§18 criterion 14: the provider permits at least 12 req/min and enforces its configured
ceiling over real HTTP without a retry storm — checked against a second, low-limit instance so
this doesn't interfere with the default fixture's request budget in other contract tests.
"""

from __future__ import annotations

import httpx
import pytest
from fixtures.management.tokens import make_token

pytestmark = pytest.mark.contract


def test_rate_limit_enforced_over_real_http(spawned_adapter_rate_limit_12) -> None:  # type: ignore[no-untyped-def]
    token = make_token(
        spawned_adapter_rate_limit_12.keypair.private_key, spawned_adapter_rate_limit_12.keypair.kid
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-Request-ID": "5a6a3c4e-1b2c-4d5e-89ab-1234567890ab",
    }
    with httpx.Client(base_url=spawned_adapter_rate_limit_12.base_url, timeout=5) as client:
        statuses = [
            client.get("/management/v1/identity", headers=headers).status_code for _ in range(13)
        ]
    assert statuses[:12] == [200] * 12
    assert statuses[12] == 429
