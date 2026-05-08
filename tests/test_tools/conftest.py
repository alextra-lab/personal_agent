"""conftest for tests/test_tools/ — resets the module-level registry singleton.

``get_default_registry()`` caches the first-built registry for the process
lifetime.  Without a reset, tests that mutate settings (e.g. monkeypatch
primitive_tools_enabled) can leave a stale registry that breaks subsequent
tests in a different test file.  This autouse fixture ensures every test in
this package sees a freshly built registry that reflects the current settings.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_default_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    import personal_agent.tools as tools_module

    monkeypatch.setattr(tools_module, "_default_registry", None)
