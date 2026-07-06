"""Root conftest — applies across the entire test suite.

Auto-skip logic for @pytest.mark.requires_llm_server:
  Reads models.yaml at collection time to find the primary model's endpoint.
  - Local model: skips if the endpoint is not reachable (e.g. no LM Studio on VPS).
  - Cloud model: skips if the API key env-var for that provider is not set.
"""

from __future__ import annotations

# FRE-375: Set test-stack URIs before any module import triggers get_settings().
# This ensures all module-level "settings = get_settings()" calls (e.g. service.py:46)
# resolve to the test configuration rather than prod defaults.
# setdefault is used so individual tests or CI pipelines can override by setting
# env vars before running pytest.
import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("AGENT_NEO4J_URI", "bolt://localhost:7688")
os.environ.setdefault("AGENT_ELASTICSEARCH_URL", "http://localhost:9201")
# FRE-808: the app-under-test connects as the restricted, non-superuser
# `seshat_app` role; admin/DDL fixtures (migration tests, full-init.sql parity)
# use AGENT_DATABASE_ADMIN_URL (the `agent` superuser). Both point at the test
# stack (:5433).
os.environ.setdefault(
    "AGENT_DATABASE_URL",
    "postgresql+asyncpg://seshat_app:seshat_app_dev_password@localhost:5433/personal_agent",
)
os.environ.setdefault(
    "AGENT_DATABASE_ADMIN_URL",
    "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent",
)
os.environ.setdefault(
    "AGENT_SYSGRAPH_DATABASE_URL",
    "postgresql+asyncpg://sysgraph_role:sysgraph_dev_password@localhost:5433/personal_agent",
)
os.environ.setdefault("AGENT_ELASTICSEARCH_INDEX_PREFIX", "agent-logs-test")
os.environ.setdefault("AGENT_CAPTAINS_LOG_INDEX_PREFIX", "agent-captains-test")

import httpx
import pytest


def _llm_server_reachable() -> tuple[bool, str]:
    """Check whether the configured primary model's inference endpoint is available.

    Returns:
        (is_available, reason_if_not)
    """
    try:
        from personal_agent.config import load_model_config, settings  # noqa: PLC0415
        from personal_agent.config.model_loader import ModelConfigError  # noqa: PLC0415
    except Exception as e:
        return False, f"Could not import config: {e}"

    try:
        model_config = load_model_config()
    except ModelConfigError as e:
        return False, f"models.yaml not loadable: {e}"

    primary = model_config.models.get("primary")
    if primary is None:
        return False, "No 'primary' model in models.yaml"

    if primary.provider is not None:
        # Cloud model — check for API key
        if primary.provider == "anthropic":
            key = settings.anthropic_api_key
            if key:
                return True, ""
            return False, "AGENT_ANTHROPIC_API_KEY not set (cloud primary model)"
        if primary.provider == "openai":
            key = settings.openai_api_key
            if key:
                return True, ""
            return False, "AGENT_OPENAI_API_KEY not set (cloud primary model)"
        return False, f"Unknown cloud provider '{primary.provider}'"

    # Local model — probe the endpoint
    endpoint = primary.endpoint or settings.llm_base_url
    # Normalise: strip trailing /v1 if present, then re-add /v1/models
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    probe_url = f"{base}/v1/models"

    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(probe_url)
            if resp.status_code < 500:
                return True, ""
            return False, f"LLM server at {probe_url} returned HTTP {resp.status_code}"
    except Exception as exc:
        return False, f"LLM server at {probe_url} not reachable: {exc}"


# Cache result so the probe runs exactly once per collection phase.
_LLM_SERVER_RESULT: tuple[bool, str] | None = None


def _cached_llm_server_reachable() -> tuple[bool, str]:
    global _LLM_SERVER_RESULT
    if _LLM_SERVER_RESULT is None:
        _LLM_SERVER_RESULT = _llm_server_reachable()
    return _LLM_SERVER_RESULT


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip requires_llm_server tests when the configured endpoint is not reachable."""
    needs_check = any(item.get_closest_marker("requires_llm_server") for item in items)
    if not needs_check:
        return

    available, reason = _cached_llm_server_reachable()
    if available:
        return

    skip_mark = pytest.mark.skip(reason=f"LLM server unavailable — {reason}")
    for item in items:
        if item.get_closest_marker("requires_llm_server"):
            item.add_marker(skip_mark)


@pytest.fixture(scope="session", autouse=True)
def _substrate_test_env_guard() -> None:
    """Log which substrate URIs the test session will use.

    Substrate-touching tests skip individually when they can't connect.
    This fixture logs the configured test URIs at session start for diagnostics.
    """
    import structlog  # noqa: PLC0415

    log = structlog.get_logger(__name__)
    database_url = os.environ.get("AGENT_DATABASE_URL", "")
    log.info(
        "test_session_substrate_config",
        app_env=os.environ.get("APP_ENV"),
        neo4j_uri=os.environ.get("AGENT_NEO4J_URI"),
        elasticsearch_url=os.environ.get("AGENT_ELASTICSEARCH_URL"),
        database_url=database_url[:50] + "..." if len(database_url) > 50 else database_url,
    )
