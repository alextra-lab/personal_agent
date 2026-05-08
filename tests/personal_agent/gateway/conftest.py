"""conftest for gateway tests — disables auth so endpoint tests need no token.

``AGENT_GATEWAY_AUTH_ENABLED=true`` in the dev .env means every test that
calls a protected endpoint without a Bearer token gets 401.  All gateway
test modules in this package were written to test endpoint logic, not auth
logic (auth has its own test_auth.py).  This autouse fixture turns the gate
off for the duration of each test.
"""

import pytest


@pytest.fixture(autouse=True)
def _disable_gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from personal_agent.config import settings

    monkeypatch.setattr(settings, "gateway_auth_enabled", False)
