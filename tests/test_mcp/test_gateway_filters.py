"""Tests for MCP gateway server allowlist matching."""

from __future__ import annotations

from typing import Any

import pytest

from personal_agent.mcp_server_allowlist import mcp_tool_matches_enabled_server


@pytest.mark.parametrize(
    ("tool", "token", "expected"),
    [
        (
            {"name": "perplexity_ask"},
            "perplexity",
            True,
        ),
        (
            {"name": "save_issue", "meta": {"microsoft/allowedDomains": ["linear.app<https://linear.app/>"]}},
            "linear",
            True,
        ),
        (
            {"name": "save_issue"},
            "linear",
            False,
        ),
        (
            {"name": "esql"},
            "elasticsearch",
            True,
        ),
        (
            {"name": "search"},
            "duckduckgo",
            True,
        ),
        (
            {"name": "resolve-library-id"},
            "context7",
            True,
        ),
        (
            {"name": "esql"},
            "linear",
            False,
        ),
    ],
)
def test_mcp_tool_matches_enabled_server(
    tool: dict[str, Any],
    token: str,
    expected: bool,
) -> None:
    """Server allowlist matches substrings, Linear meta, and known aliases."""
    assert mcp_tool_matches_enabled_server(tool, token) is expected
