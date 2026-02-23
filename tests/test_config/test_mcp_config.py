"""Tests for MCP Gateway configuration."""

import pytest

from personal_agent.config.settings import AppConfig


def test_mcp_gateway_defaults(monkeypatch: pytest.MonkeyPatch):
    """Test default MCP configuration (isolated from .env overrides)."""
    monkeypatch.delenv("AGENT_MCP_GATEWAY_ENABLED", raising=False)
    monkeypatch.delenv("MCP_GATEWAY_ENABLED", raising=False)
    config = AppConfig()
    assert config.mcp_gateway_enabled is False
    assert config.mcp_gateway_command == ["docker", "mcp", "gateway", "run"]
    assert (
        config.mcp_gateway_timeout_seconds == 60
    )  # Increased for external API tools like Perplexity


def test_mcp_gateway_env_override(monkeypatch: pytest.MonkeyPatch):
    """Test environment variable override."""
    monkeypatch.setenv("AGENT_MCP_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("AGENT_MCP_GATEWAY_TIMEOUT_SECONDS", "120")

    config = AppConfig()
    assert config.mcp_gateway_enabled is True
    assert config.mcp_gateway_timeout_seconds == 120


def test_mcp_gateway_command_json_parsing(monkeypatch: pytest.MonkeyPatch):
    """Test gateway command parses from JSON."""
    monkeypatch.setenv(
        "AGENT_MCP_GATEWAY_COMMAND", '["docker", "mcp", "gateway", "run", "--verbose"]'
    )

    config = AppConfig()
    assert config.mcp_gateway_command == ["docker", "mcp", "gateway", "run", "--verbose"]


def test_mcp_gateway_command_space_separated():
    """Test gateway command parses from space-separated string.

    Note: This test is skipped because pydantic-settings tries JSON parsing first,
    which fails for space-separated strings. Users should use JSON array format instead.
    """
    pytest.skip("Space-separated strings not supported by pydantic-settings without custom source")
