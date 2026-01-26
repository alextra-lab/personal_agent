"""Tests for shared YAML loader utilities."""

from pathlib import Path

import pytest

from personal_agent.config.loader import ConfigLoadError, load_yaml_file


class TestLoadYamlFile:
    """Test shared YAML loading utility."""

    def test_load_valid_yaml(self, tmp_path: Path) -> None:
        """Test loading a valid YAML file."""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(
            """
key1: value1
key2:
  nested: value2
list:
  - item1
  - item2
"""
        )

        result = load_yaml_file(yaml_file)

        assert result == {
            "key1": "value1",
            "key2": {"nested": "value2"},
            "list": ["item1", "item2"],
        }

    def test_load_empty_file(self, tmp_path: Path) -> None:
        """Test that empty file returns empty dict."""
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")

        result = load_yaml_file(yaml_file)

        assert result == {}

    def test_load_none_content(self, tmp_path: Path) -> None:
        """Test that file with only comments returns empty dict."""
        yaml_file = tmp_path / "comments.yaml"
        yaml_file.write_text("# Just comments\n# No actual content")

        result = load_yaml_file(yaml_file)

        assert result == {}

    def test_load_missing_file(self, tmp_path: Path) -> None:
        """Test that missing file raises ConfigLoadError."""
        yaml_file = tmp_path / "nonexistent.yaml"

        with pytest.raises(ConfigLoadError, match="not found"):
            load_yaml_file(yaml_file)

    def test_load_invalid_yaml(self, tmp_path: Path) -> None:
        """Test that invalid YAML raises ConfigLoadError."""
        yaml_file = tmp_path / "invalid.yaml"
        yaml_file.write_text("invalid: yaml: content: [unclosed")

        with pytest.raises(ConfigLoadError, match="Failed to parse YAML"):
            load_yaml_file(yaml_file)

    def test_custom_error_class(self, tmp_path: Path) -> None:
        """Test that custom error class can be used."""

        class CustomError(ConfigLoadError):
            pass

        yaml_file = tmp_path / "nonexistent.yaml"

        with pytest.raises(CustomError, match="not found"):
            load_yaml_file(yaml_file, error_class=CustomError)
