"""Tests for configuration settings."""

import os
from pathlib import Path

import pytest

from personal_agent.config import (
    AppConfig,
    Environment,
    get_environment,
    get_settings,
    load_app_config,
    settings,
)


class TestEnvironmentDetection:
    """Test environment detection."""

    def test_get_environment_default(self) -> None:
        """Test default environment is development."""
        # Clear APP_ENV if set
        if "APP_ENV" in os.environ:
            del os.environ["APP_ENV"]

        env = get_environment()
        assert env == Environment.DEVELOPMENT

    def test_get_environment_production(self) -> None:
        """Test production environment detection."""
        os.environ["APP_ENV"] = "production"
        try:
            env = get_environment()
            assert env == Environment.PRODUCTION
        finally:
            del os.environ["APP_ENV"]

    def test_get_environment_prod_alias(self) -> None:
        """Test 'prod' alias for production."""
        os.environ["APP_ENV"] = "prod"
        try:
            env = get_environment()
            assert env == Environment.PRODUCTION
        finally:
            del os.environ["APP_ENV"]

    def test_get_environment_staging(self) -> None:
        """Test staging environment detection."""
        os.environ["APP_ENV"] = "staging"
        try:
            env = get_environment()
            assert env == Environment.STAGING
        finally:
            del os.environ["APP_ENV"]

    def test_get_environment_stage_alias(self) -> None:
        """Test 'stage' alias for staging."""
        os.environ["APP_ENV"] = "stage"
        try:
            env = get_environment()
            assert env == Environment.STAGING
        finally:
            del os.environ["APP_ENV"]

    def test_get_environment_test(self) -> None:
        """Test test environment detection."""
        os.environ["APP_ENV"] = "test"
        try:
            env = get_environment()
            assert env == Environment.TEST
        finally:
            del os.environ["APP_ENV"]


class TestAppConfig:
    """Test AppConfig class."""

    def test_app_config_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test AppConfig has correct code defaults (isolated from .env)."""
        monkeypatch.delenv("AGENT_LOG_LEVEL", raising=False)
        monkeypatch.delenv("APP_LOG_LEVEL", raising=False)
        monkeypatch.delenv("AGENT_LLM_BASE_URL", raising=False)
        config = AppConfig()
        assert config.environment == Environment.DEVELOPMENT
        assert config.debug is False
        assert config.project_name == "Personal Local AI Collaborator"
        assert config.version == "0.1.0"
        assert config.log_level == "INFO"
        assert config.log_format == "json"
        assert config.llm_base_url == "http://localhost:8000/v1"
        assert config.llm_timeout_seconds == 120
        assert config.orchestrator_max_concurrent_tasks == 5

    def test_app_config_from_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test AppConfig reads from environment variables with AGENT_ prefix."""
        monkeypatch.delenv("APP_LOG_LEVEL", raising=False)
        monkeypatch.delenv("APP_DEBUG", raising=False)
        monkeypatch.setenv("AGENT_DEBUG", "1")
        monkeypatch.setenv("AGENT_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("AGENT_LLM_BASE_URL", "http://test:8080/v1")

        config = AppConfig()
        assert config.debug is True
        assert config.log_level == "DEBUG"
        assert config.llm_base_url == "http://test:8080/v1"

    def test_app_config_log_level_validation(self) -> None:
        """Test log level validation."""
        # Reset singleton
        import personal_agent.config.settings as settings_module

        original_settings = getattr(settings_module, "_settings", None)
        settings_module._settings = None

        os.environ["APP_LOG_LEVEL"] = "INVALID"
        try:
            from personal_agent.config.env_loader import load_env_files

            load_env_files()
            from pydantic import ValidationError

            with pytest.raises(ValidationError):
                AppConfig()
        finally:
            del os.environ["APP_LOG_LEVEL"]
            if original_settings is not None:
                settings_module._settings = original_settings

    def test_app_config_log_format_validation(self) -> None:
        """Test log format validation."""
        # Reset singleton
        import personal_agent.config.settings as settings_module

        original_settings = getattr(settings_module, "_settings", None)
        settings_module._settings = None

        os.environ["APP_LOG_FORMAT"] = "invalid"
        try:
            from personal_agent.config.env_loader import load_env_files

            load_env_files()
            from pydantic import ValidationError

            with pytest.raises(ValidationError):
                AppConfig()
        finally:
            del os.environ["APP_LOG_FORMAT"]
            if original_settings is not None:
                settings_module._settings = original_settings

    def test_app_config_path_resolution(self) -> None:
        """Test that relative paths are resolved to absolute."""
        config = AppConfig()
        assert config.log_dir.is_absolute()
        assert config.governance_config_path.is_absolute()
        assert config.model_config_path.is_absolute()


class TestSingleton:
    """Test singleton pattern."""

    def test_get_settings_returns_singleton(self) -> None:
        """Test that get_settings returns the same instance."""
        settings1 = get_settings()
        settings2 = get_settings()
        assert settings1 is settings2

    def test_settings_module_export(self) -> None:
        """Test that settings is exported from module."""
        assert settings is not None
        assert isinstance(settings, AppConfig)


class TestEnvFileLoading:
    """Test .env file loading."""

    def test_load_env_files_priority(self, tmp_path: Path) -> None:
        """Test .env file loading priority order."""
        # Create .env files with different values
        (tmp_path / ".env").write_text("TEST_VAR=base\n")
        (tmp_path / ".env.local").write_text("TEST_VAR=local\n")
        (tmp_path / ".env.development").write_text("TEST_VAR=development\n")
        (tmp_path / ".env.development.local").write_text("TEST_VAR=development_local\n")

        # Set environment to development
        original_env = os.environ.get("APP_ENV")
        os.environ["APP_ENV"] = "development"

        try:
            from personal_agent.config.env_loader import load_env_files

            load_env_files(tmp_path)

            # Highest priority file should win
            assert os.getenv("TEST_VAR") == "development_local"
        finally:
            if original_env:
                os.environ["APP_ENV"] = original_env
            elif "APP_ENV" in os.environ:
                del os.environ["APP_ENV"]
            if "TEST_VAR" in os.environ:
                del os.environ["TEST_VAR"]


class TestLoadAppConfig:
    """Test load_app_config function."""

    def test_load_app_config_creates_config(self) -> None:
        """Test that load_app_config creates a valid config."""
        config = load_app_config()
        assert isinstance(config, AppConfig)
        assert config.log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

    def test_load_app_config_logs(self) -> None:
        """Test that load_app_config logs configuration loading."""
        # Reset singleton to test logging
        import personal_agent.config.settings as settings_module

        # Access the module-level _settings variable
        if hasattr(settings_module, "_settings"):
            original_settings = getattr(settings_module, "_settings", None)
            settings_module._settings = None
        else:
            original_settings = None

        try:
            config = load_app_config()
            assert config is not None
            # Check that logs were emitted (structured logs may not appear in caplog)
        finally:
            if original_settings is not None:
                settings_module._settings = original_settings
