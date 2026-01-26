"""Tests for governance config loading and validation."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import yaml

from personal_agent.config import GovernanceConfigError, load_governance_config
from personal_agent.governance.models import GovernanceConfig


def test_load_governance_config_success() -> None:
    """Test loading valid governance configuration."""
    # Use the actual config directory
    project_root = Path(__file__).parent.parent.parent
    config_dir = project_root / "config" / "governance"

    config = load_governance_config(config_dir)

    assert isinstance(config, GovernanceConfig)
    assert len(config.modes) == 5
    assert "NORMAL" in config.modes
    assert "ALERT" in config.modes
    assert "DEGRADED" in config.modes
    assert "LOCKDOWN" in config.modes
    assert "RECOVERY" in config.modes

    # Verify mode structure
    normal_mode = config.modes["NORMAL"]
    assert normal_mode.description == "Default healthy operation"
    assert normal_mode.max_concurrent_tasks == 5
    assert normal_mode.background_monitoring_enabled is True

    # Verify tools
    assert len(config.tools) > 0
    assert "read_file" in config.tools
    assert "write_file" in config.tools
    assert "system_metrics_snapshot" in config.tools

    # Verify model constraints
    assert len(config.mode_constraints) == 5
    assert "NORMAL" in config.mode_constraints
    normal_constraints = config.mode_constraints["NORMAL"]
    assert "router" in normal_constraints.allowed_roles
    assert "reasoning" in normal_constraints.allowed_roles

    # Verify safety config
    assert config.safety.content_filtering.enabled is True
    assert len(config.safety.rate_limits) > 0


def test_load_governance_config_default_path() -> None:
    """Test loading with default config directory path."""
    config = load_governance_config()

    assert isinstance(config, GovernanceConfig)
    assert len(config.modes) == 5


def test_load_governance_config_missing_directory() -> None:
    """Test error when config directory doesn't exist."""
    with pytest.raises(GovernanceConfigError, match="does not exist"):
        load_governance_config(Path("/nonexistent/path"))


def test_load_governance_config_not_directory() -> None:
    """Test error when config path is not a directory."""
    with TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "not_a_dir"
        file_path.touch()

        with pytest.raises(GovernanceConfigError, match="not a directory"):
            load_governance_config(file_path)


def test_load_governance_config_missing_file() -> None:
    """Test error when required config file is missing."""
    with TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)

        # Create empty directory (no YAML files)
        with pytest.raises(GovernanceConfigError, match="not found"):
            load_governance_config(config_dir)


def test_load_governance_config_invalid_yaml() -> None:
    """Test error when YAML file is invalid."""
    with TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)

        # Create invalid YAML file
        modes_file = config_dir / "modes.yaml"
        modes_file.write_text("invalid: yaml: content: [unclosed")

        with pytest.raises(GovernanceConfigError, match="Failed to parse YAML"):
            load_governance_config(config_dir)


def test_load_governance_config_validation_error() -> None:
    """Test error when configuration fails validation."""
    with TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)

        # Create config files with invalid data
        modes_file = config_dir / "modes.yaml"
        modes_file.write_text(
            yaml.dump(
                {
                    "modes": {
                        "NORMAL": {
                            "description": "Test",
                            "max_concurrent_tasks": -1,  # Invalid: negative
                            "background_monitoring_enabled": True,
                        }
                    }
                }
            )
        )

        tools_file = config_dir / "tools.yaml"
        tools_file.write_text(yaml.dump({"tools": {}}))

        models_file = config_dir / "models.yaml"
        models_file.write_text(yaml.dump({"mode_constraints": {}}))

        safety_file = config_dir / "safety.yaml"
        safety_file.write_text(yaml.dump({}))

        with pytest.raises(GovernanceConfigError, match="validation failed"):
            load_governance_config(config_dir)


def test_load_governance_config_missing_required_fields() -> None:
    """Test error when required fields are missing."""
    with TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)

        # Create config files with missing required fields in mode definition
        modes_file = config_dir / "modes.yaml"
        modes_file.write_text(
            yaml.dump(
                {
                    "modes": {
                        "NORMAL": {
                            # Missing required fields: description, max_concurrent_tasks, etc.
                        }
                    }
                }
            )
        )

        tools_file = config_dir / "tools.yaml"
        tools_file.write_text(yaml.dump({"tools": {}}))

        models_file = config_dir / "models.yaml"
        models_file.write_text(yaml.dump({"mode_constraints": {}}))

        safety_file = config_dir / "safety.yaml"
        safety_file.write_text(yaml.dump({}))

        with pytest.raises(GovernanceConfigError, match="validation failed"):
            load_governance_config(config_dir)


def test_governance_config_mode_structure() -> None:
    """Test that mode definitions have correct structure."""
    project_root = Path(__file__).parent.parent.parent
    config_dir = project_root / "config" / "governance"

    config = load_governance_config(config_dir)

    # Check all modes have required fields
    for _mode_name, mode_def in config.modes.items():
        assert mode_def.description
        assert mode_def.max_concurrent_tasks >= 0
        assert isinstance(mode_def.background_monitoring_enabled, bool)
        assert isinstance(mode_def.allowed_tool_categories, list)
        assert isinstance(mode_def.require_approval_for, list)
        assert isinstance(mode_def.thresholds, type(config.modes["NORMAL"].thresholds))


def test_governance_config_tool_structure() -> None:
    """Test that tool policies have correct structure."""
    project_root = Path(__file__).parent.parent.parent
    config_dir = project_root / "config" / "governance"

    config = load_governance_config(config_dir)

    # Check read_file tool
    read_file_tool = config.tools["read_file"]
    assert read_file_tool.category == "read_only"
    assert len(read_file_tool.allowed_in_modes) > 0
    assert "NORMAL" in read_file_tool.allowed_in_modes

    # Check write_file tool
    write_file_tool = config.tools["write_file"]
    assert write_file_tool.category == "system_write"
    assert "LOCKDOWN" in write_file_tool.forbidden_in_modes


def test_governance_config_model_constraints() -> None:
    """Test that model constraints are correctly structured."""
    project_root = Path(__file__).parent.parent.parent
    config_dir = project_root / "config" / "governance"

    config = load_governance_config(config_dir)

    # Check NORMAL mode constraints
    normal_constraints = config.mode_constraints["NORMAL"]
    assert len(normal_constraints.allowed_roles) > 0
    assert "router" in normal_constraints.allowed_roles

    # Check LOCKDOWN mode constraints (should be minimal)
    lockdown_constraints = config.mode_constraints["LOCKDOWN"]
    assert len(lockdown_constraints.allowed_roles) == 0


def test_governance_config_safety_policies() -> None:
    """Test that safety policies are correctly structured."""
    project_root = Path(__file__).parent.parent.parent
    config_dir = project_root / "config" / "governance"

    config = load_governance_config(config_dir)

    # Check content filtering
    assert config.safety.content_filtering.enabled is True
    assert len(config.safety.content_filtering.secret_patterns) > 0

    # Check rate limits exist for all modes
    assert "NORMAL" in config.safety.rate_limits
    assert "ALERT" in config.safety.rate_limits
    assert "DEGRADED" in config.safety.rate_limits
    assert "LOCKDOWN" in config.safety.rate_limits
    assert "RECOVERY" in config.safety.rate_limits

    # Check rate limit structure
    normal_limits = config.safety.rate_limits["NORMAL"]
    assert normal_limits.tool_calls_per_minute >= 0
    assert normal_limits.llm_calls_per_minute >= 0
    assert normal_limits.outbound_requests_per_hour >= 0
