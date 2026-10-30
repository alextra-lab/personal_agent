"""Tests for MCP governance discovery."""

import shutil
import tempfile
from pathlib import Path

from personal_agent.config.governance_loader import load_governance_config
from personal_agent.mcp.governance import MCPGovernanceManager

_MODES_YAML = Path(__file__).resolve().parents[2] / "config" / "governance" / "modes.yaml"


def test_governance_template_generation():
    """Test governance template generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create temp config file
        config_path = Path(tmpdir) / "tools.yaml"
        config_path.write_text('tools:\n  read_file:\n    allowed_in_modes: ["NORMAL"]\n')

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
            assert "category:" not in content
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


def test_discovered_tool_loads_through_the_governance_loader(tmp_path: Path, monkeypatch) -> None:
    """FRE-1719: a tools.yaml that discovery appended to still loads, with no category key."""
    shutil.copy(_MODES_YAML, tmp_path / "modes.yaml")
    (tmp_path / "tools.yaml").write_text('tools:\n  read_file:\n    allowed_in_modes: ["NORMAL"]\n')

    from personal_agent.config import settings

    monkeypatch.setattr(settings, "governance_config_path", tmp_path)
    MCPGovernanceManager().ensure_tool_configured(
        tool_name="mcp_github_search",
        tool_schema={"description": "Search GitHub repositories"},
        inferred_risk_level="medium",
    )

    config = load_governance_config(tmp_path)
    assert config.tools["mcp_github_search"].allowed_in_modes == ["NORMAL", "DEGRADED"]
    assert config.tools["read_file"].allowed_in_modes == ["NORMAL"]
