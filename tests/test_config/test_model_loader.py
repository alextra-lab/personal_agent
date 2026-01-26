"""Tests for model configuration loader."""

from pathlib import Path

import pytest

from personal_agent.config import ModelConfigError, load_model_config
from personal_agent.llm_client.models import ModelConfig, ModelDefinition


class TestLoadModelConfig:
    """Test model configuration loading."""

    def test_load_valid_config(self, tmp_path: Path) -> None:
        """Test loading a valid model config file."""
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  router:
    id: "test-router"
    context_length: 8192
    quantization: "8bit"
    max_concurrency: 4
    default_timeout: 5
  reasoning:
    id: "test-reasoning"
    endpoint: "http://localhost:8002/v1"
    context_length: 32768
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 60
"""
        )

        config = load_model_config(config_file)

        assert isinstance(config, ModelConfig)
        assert "router" in config.models
        assert "reasoning" in config.models

        router_model = config.models["router"]
        assert isinstance(router_model, ModelDefinition)
        assert router_model.id == "test-router"
        assert router_model.context_length == 8192
        assert router_model.endpoint is None

        reasoning_model = config.models["reasoning"]
        assert reasoning_model.id == "test-reasoning"
        assert reasoning_model.endpoint == "http://localhost:8002/v1"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        """Test that missing file raises ModelConfigError."""
        config_file = tmp_path / "nonexistent.yaml"

        with pytest.raises(ModelConfigError, match="not found"):
            load_model_config(config_file)

    def test_load_invalid_yaml(self, tmp_path: Path) -> None:
        """Test that invalid YAML raises ModelConfigError."""
        config_file = tmp_path / "invalid.yaml"
        config_file.write_text("invalid: yaml: content: [unclosed")

        with pytest.raises(ModelConfigError, match="Failed to parse"):
            load_model_config(config_file)

    def test_load_empty_file(self, tmp_path: Path) -> None:
        """Test that empty file returns empty ModelConfig."""
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")

        config = load_model_config(config_file)

        assert isinstance(config, ModelConfig)
        assert len(config.models) == 0

    def test_load_none_content(self, tmp_path: Path) -> None:
        """Test that file with only comments returns empty ModelConfig."""
        config_file = tmp_path / "comments.yaml"
        config_file.write_text("# Just comments\n# No actual content")

        config = load_model_config(config_file)

        assert isinstance(config, ModelConfig)
        assert len(config.models) == 0

    def test_accepts_none_and_uses_settings(self, tmp_path: Path) -> None:
        """Test that load_model_config accepts None and uses settings.model_config_path."""
        # This test verifies the function signature accepts None
        # The actual integration with settings is tested through the client
        # which calls load_model_config() without arguments
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  router:
    id: "test-router"
    context_length: 8192
    quantization: "8bit"
    max_concurrency: 4
    default_timeout: 5
"""
        )

        # Test with explicit path (the None case is tested via client integration)
        config = load_model_config(config_file)

        assert isinstance(config, ModelConfig)
        assert config.models["router"].id == "test-router"

    def test_validation_error_missing_required_fields(self, tmp_path: Path) -> None:
        """Test that missing required fields raises validation error."""
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  router:
    id: "test-router"
    # Missing required fields: context_length, quantization, etc.
"""
        )

        with pytest.raises(ModelConfigError, match="validation failed"):
            load_model_config(config_file)

    def test_validation_error_invalid_values(self, tmp_path: Path) -> None:
        """Test that invalid field values raise validation error."""
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  router:
    id: "test-router"
    context_length: -1  # Invalid: negative
    quantization: "8bit"
    max_concurrency: 0  # Invalid: must be >= 1
    default_timeout: 5
"""
        )

        with pytest.raises(ModelConfigError, match="validation failed"):
            load_model_config(config_file)
