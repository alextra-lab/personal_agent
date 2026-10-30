"""Tests for governance config loading and validation."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import yaml

from personal_agent.config import GovernanceConfigError, load_governance_config
from personal_agent.governance.models import GovernanceConfig, ModeDefinition


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

    # Verify tools
    assert len(config.tools) > 0
    assert "write_file" in config.tools
    assert "bash" in config.tools
    assert "read" in config.tools


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
                            "description": ["not", "a", "string"],  # Invalid: not a str
                        }
                    }
                }
            )
        )

        tools_file = config_dir / "tools.yaml"
        tools_file.write_text(yaml.dump({"tools": {}}))

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
                            # Missing required field: description
                        }
                    }
                }
            )
        )

        tools_file = config_dir / "tools.yaml"
        tools_file.write_text(yaml.dump({"tools": {}}))

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


def test_governance_config_tool_structure() -> None:
    """Test that tool policies have correct structure."""
    project_root = Path(__file__).parent.parent.parent
    config_dir = project_root / "config" / "governance"

    config = load_governance_config(config_dir)

    # Check read primitive (replaced legacy read_file in FRE-265)
    read_tool = config.tools["read"]
    assert read_tool.category == "read_only"
    assert len(read_tool.allowed_in_modes) > 0
    assert "NORMAL" in read_tool.allowed_in_modes

    # Check write_file tool
    write_file_tool = config.tools["write_file"]
    assert write_file_tool.category == "system_write"
    assert "LOCKDOWN" in write_file_tool.forbidden_in_modes


def test_governance_config_sub_agent_tools_is_a_distinct_grant_set() -> None:
    """FRE-1388 AC-1 — the sub-agent principal's grant set loads from the real config

    and is independent of the primary's per-tool ``allowed_in_modes``.

    FRE-1463 turned the flat list into a per-tool decision record, so the
    independence claim is now asserted against the granted subset. Membership of
    the mapping proves only that a decision was recorded.
    """
    project_root = Path(__file__).parent.parent.parent
    config_dir = project_root / "config" / "governance"

    config = load_governance_config(config_dir)

    granted = config.granted_sub_agent_tool_names()
    # Decisions of 2026-09-04 and 2026-09-08 (FRE-1388, FRE-1463, FRE-1467) and of
    # 2026-10-10 (FRE-1564: option B, the telemetry tool, and fetch_url; ADR-0156 D9.4,
    # FRE-1580: read, bounded to the files a general task names; FRE-1678: artifact_read,
    # the artifact a general task names by id).
    assert set(granted) == {
        "run_python",
        "web_search",
        "search_memory",
        "recall_personal_history",
        "fetch_url",
        "get_library_docs",
        "read_skill",
        "notes_search",
        "search_logs",
        "measure_model_latency",
        "read",
        "artifact_read",
    }
    # write_file is a tool of the primary in NORMAL that no sub-agent holds — proves the
    # two policies are genuinely independent (fetch_url used to be the proof, until
    # FRE-1564 granted it).
    assert "write_file" in config.tools
    assert "write_file" not in granted
    # Every granted tool carries a recorded reason (FRE-1463 AC-1).
    assert all(config.sub_agent_tools[name].reason.strip() for name in granted)


def test_loader_needs_only_modes_and_tools_files() -> None:
    """FRE-1701: the loader reads modes.yaml and tools.yaml only; nothing reads the deleted parts.

    governance/models.yaml (mode_constraints) and safety.yaml were deleted, and the schema no
    longer carries the sections that no code read.
    """
    with TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        (config_dir / "modes.yaml").write_text(
            yaml.dump({"modes": {"NORMAL": {"description": "Test"}}})
        )
        (config_dir / "tools.yaml").write_text(yaml.dump({"tools": {}}))

        config = load_governance_config(config_dir)

    assert config.modes["NORMAL"].description == "Test"
    assert {"mode_constraints", "safety", "tool_categories"}.isdisjoint(
        GovernanceConfig.model_fields
    )
    assert set(ModeDefinition.model_fields) == {"description"}
