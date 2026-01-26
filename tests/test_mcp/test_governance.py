"""Tests for MCP governance discovery."""

import tempfile
from pathlib import Path

from personal_agent.mcp.governance import MCPGovernanceManager


def test_governance_template_generation():
    """Test governance template generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create temp config file
        config_path = Path(tmpdir) / "tools.yaml"
        config_path.write_text("tools:\n  read_file:\n    category: read_only\n")

        # Override config path
        from personal_agent.config import settings

        original_path = settings.governance_config_path
        settings.governance_config_path = Path(tmpdir)

        try:
            mgr = MCPGovernanceManager()

            # Test tool configuration
            mgr.ensure_tool_configured(
                tool_name="mcp_github_search",
                tool_schema={"description": "Search GitHub repositories"},
                inferred_risk_level="low",
            )

            # Verify entry added
            content = config_path.read_text()
            assert "mcp_github_search:" in content
            assert 'category: "mcp"' in content
            assert 'risk_level: "low"' in content

            # Test idempotency (don't duplicate)
            mgr.ensure_tool_configured(
                tool_name="mcp_github_search",
                tool_schema={"description": "Search GitHub repositories"},
                inferred_risk_level="low",
            )

            # Verify not duplicated
            content = config_path.read_text()
            assert content.count("mcp_github_search:") == 1

        finally:
            settings.governance_config_path = original_path
