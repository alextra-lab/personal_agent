"""AC-3: the pre-bootstrap logger population is enumerated, not absorbed into a tolerance.

ADR-0129 D3 / FRE-1069. ``PRE_BOOTSTRAP_LOGGERS`` (telemetry/otel_bootstrap.py) names every
logger that can emit before ``configure_tracing()`` runs. These tests exercise each real
emission path and assert the observed logger name is on that list.
"""

from __future__ import annotations

import sys

from personal_agent.telemetry.otel_bootstrap import PRE_BOOTSTRAP_LOGGERS
from tests._helpers.log_capture import capture_log_records


def test_settings_reload_logger_is_enumerated(monkeypatch) -> None:
    """``get_settings()`` re-triggering ``load_app_config()`` stays within the allowlist."""
    # personal_agent/config/__init__.py does `settings = get_settings()` at module level,
    # which shadows the `personal_agent.config.settings` *submodule* attribute on the
    # `personal_agent.config` package with the resulting AppConfig instance — so
    # `import personal_agent.config.settings as x` (attribute-based resolution) binds `x`
    # to that instance, not the module. sys.modules holds the real module regardless.
    import personal_agent.config.settings  # noqa: F401 — ensures it is registered

    settings_mod = sys.modules["personal_agent.config.settings"]

    with capture_log_records() as records:
        # capture_log_records() calls configure_logging() on entry, and
        # configure_logging() -> _get_log_dir() itself calls get_settings()
        # internally — resetting the singleton BEFORE that point would just get
        # silently refilled before this test's own call ever runs. Reset inside
        # the block, after that refill has already happened.
        monkeypatch.setattr(settings_mod, "_settings", None)
        settings_mod.get_settings()

    assert records, "get_settings() with a cleared singleton must log at least once"
    observed = {record["logger"] for record in records}
    assert observed <= PRE_BOOTSTRAP_LOGGERS
    assert "personal_agent.config.settings" in observed


def test_uvicorn_error_logger_name_matches_the_allowlist() -> None:
    """Verify the ``uvicorn.error`` citation dynamically, not just by comment.

    Production boots via Uvicorn (``docker-compose.cloud.yml``), and the
    installed Uvicorn server logs through ``logging.getLogger("uvicorn.error")``
    (``uvicorn/server.py``) before the ASGI app's ``lifespan()`` — and therefore
    ``configure_tracing()`` — ever runs. A full subprocess boot of a real
    ``uvicorn.Server`` would prove this end-to-end, but is heavyweight and
    network-dependent for a unit-test suite; asserting against the installed
    package's own logger object is deterministic, non-flaky, and still catches
    drift if a future Uvicorn upgrade renames it — which is the failure mode
    this test exists to catch.
    """
    import uvicorn.server

    assert uvicorn.server.logger.name == "uvicorn.error"
    assert "uvicorn.error" in PRE_BOOTSTRAP_LOGGERS
