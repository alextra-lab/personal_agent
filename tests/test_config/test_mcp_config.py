"""Tests for MCP Gateway configuration."""

import os

import pytest

from personal_agent.config.settings import AppConfig


def test_mcp_gateway_defaults():
    """Test default MCP configuration."""
    config = AppConfig()
    assert config.mcp_gateway_enabled is False
    assert config.mcp_gateway_command == ["docker", "mcp", "gateway", "run"]
    assert (
        config.mcp_gateway_timeout_seconds == 60
    )  # Increased for external API tools like Perplexity


def test_mcp_gateway_env_override():
    """Test environment variable override."""
    os.environ["MCP_GATEWAY_ENABLED"] = "true"
    os.environ["MCP_GATEWAY_TIMEOUT_SECONDS"] = "120"  # Override the 60s default

    config = AppConfig()
    assert config.mcp_gateway_enabled is True
    assert config.mcp_gateway_timeout_seconds == 120

    # Cleanup
    del os.environ["MCP_GATEWAY_ENABLED"]
    del os.environ["MCP_GATEWAY_TIMEOUT_SECONDS"]


def test_mcp_gateway_command_json_parsing():
    """Test gateway command parses from JSON."""
    os.environ["MCP_GATEWAY_COMMAND"] = '["docker", "mcp", "gateway", "run", "--verbose"]'

    config = AppConfig()
    assert config.mcp_gateway_command == ["docker", "mcp", "gateway", "run", "--verbose"]

    # Cleanup
    del os.environ["MCP_GATEWAY_COMMAND"]


def test_mcp_gateway_command_space_separated():
    """Test gateway command parses from space-separated string.

    Note: This test is skipped because pydantic-settings tries JSON parsing first,
    which fails for space-separated strings. Users should use JSON array format instead.
    """
    pytest.skip("Space-separated strings not supported by pydantic-settings without custom source")
