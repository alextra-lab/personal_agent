"""Root conftest — applies across the entire test suite.

Auto-skip logic for @pytest.mark.requires_llm_server:
  Reads models.yaml at collection time to find the primary model's endpoint.
  - Local model: skips if the endpoint is not reachable (e.g. no LM Studio on VPS).
  - Cloud model: skips if the API key env-var for that provider is not set.
"""

from __future__ import annotations

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
