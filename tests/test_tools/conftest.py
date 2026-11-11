"""conftest for tests/test_tools/ — resets the module-level registry singleton.

``get_default_registry()`` caches the first-built registry for the process
lifetime.  Without a reset, tests that mutate settings (e.g. monkeypatch
primitive_tools_enabled) can leave a stale registry that breaks subsequent
tests in a different test file.  This autouse fixture ensures every test in
this package sees a freshly built registry that reflects the current settings.
"""

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_default_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    import personal_agent.tools as tools_module

    monkeypatch.setattr(tools_module, "_default_registry", None)


@pytest.fixture(autouse=True)
def _no_owner_alerts() -> Iterator[None]:
    """Reset the web_search alert state, and never let a test push to the owner (FRE-1768).

    A test that both-fails a search would otherwise reach ``push_to_owner``, which sends to
    the real ntfy topic wherever ``AGENT_OWNER_NTFY_URL`` is set.
    """
    from personal_agent.tools import web_search_alerts

    web_search_alerts.reset_alert_state()
    with patch(
        "personal_agent.tools.web_search_alerts.push_to_owner", AsyncMock(return_value=False)
    ):
        yield
    web_search_alerts.reset_alert_state()
