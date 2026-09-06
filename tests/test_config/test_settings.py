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
        monkeypatch.setenv("AGENT_SLM_BASE_URL", "http://localhost:9099")
        config = AppConfig()
        assert config.environment == Environment.DEVELOPMENT
        assert config.debug is False
        assert config.project_name == "Personal Local AI Collaborator"
        assert config.version == "0.1.0"
        assert config.log_level == "INFO"
        assert config.log_format == "json"
        assert config.llm_timeout_seconds == 120
        assert config.orchestrator_max_concurrent_tasks == 5
        # FRE-1413: must cover a thinking-capable PRIMARY planner's full think,
        # not the retired thinking-disabled SUB_AGENT call this was sized for.
        assert config.planner_timeout_seconds == 600.0

    def test_app_config_from_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test AppConfig reads from environment variables.

        Fields with explicit alias (e.g. APP_DEBUG, APP_LOG_LEVEL) use that alias;
        others use AGENT_ prefix per env_prefix.
        """
        monkeypatch.delenv("APP_LOG_LEVEL", raising=False)
        monkeypatch.delenv("APP_DEBUG", raising=False)
        monkeypatch.setenv("AGENT_SLM_BASE_URL", "http://localhost:9099")
        monkeypatch.setenv("APP_DEBUG", "1")
        monkeypatch.setenv("APP_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("AGENT_SLM_BASE_URL", "http://test:8080/v1")

        config = AppConfig()
        assert config.debug is True
        assert config.log_level == "DEBUG"
        assert config.slm_base_url == "http://test:8080/v1"

    def test_build_fingerprint_defaults_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FRE-1341: outside a container build, no fingerprint was baked in."""
        monkeypatch.delenv("AGENT_BUILD_FINGERPRINT", raising=False)
        monkeypatch.setenv("AGENT_SLM_BASE_URL", "http://localhost:9099")
        config = AppConfig()
        assert config.build_fingerprint is None

    def test_build_fingerprint_reads_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FRE-1341: Dockerfile.gateway bakes this in via AGENT_BUILD_FINGERPRINT."""
        monkeypatch.setenv("AGENT_SLM_BASE_URL", "http://localhost:9099")
        monkeypatch.setenv("AGENT_BUILD_FINGERPRINT", "deadbeef123")
        config = AppConfig()
        assert config.build_fingerprint == "deadbeef123"

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


class TestCompressionGeometry:
    """Recovery plan 2026-05-05 Wave 0.2 — guard against pathological geometry."""

    def test_defaults_pass_validator(self) -> None:
        """Default compression geometry must reserve ≥1024 tokens for head+middle."""
        config = AppConfig()
        assert config.context_window_max_tokens == 48000
        assert config.within_session_min_tail_ratio == 0.25
        absolute_tail = int(config.context_window_max_tokens * config.within_session_min_tail_ratio)
        head_middle_budget = config.context_window_max_tokens - absolute_tail
        assert head_middle_budget >= 1024

    def test_validator_rejects_pathological_ratio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 0.99 tail ratio against a 2048 window must fail validation."""
        from pydantic import ValidationError

        monkeypatch.setenv("AGENT_CONTEXT_WINDOW_MAX_TOKENS", "2048")
        monkeypatch.setenv("AGENT_WITHIN_SESSION_MIN_TAIL_RATIO", "0.99")
        with pytest.raises(ValidationError) as exc_info:
            AppConfig()
        assert "head+middle" in str(exc_info.value)

    def test_validator_rejects_old_default_geometry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pre-recovery defaults (2048 window, ~2000 tail) must not be re-introduced."""
        from pydantic import ValidationError

        monkeypatch.setenv("AGENT_CONTEXT_WINDOW_MAX_TOKENS", "2048")
        monkeypatch.setenv("AGENT_WITHIN_SESSION_MIN_TAIL_RATIO", "0.9766")
        with pytest.raises(ValidationError):
            AppConfig()

    def test_validator_accepts_small_window_with_low_ratio(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """4096 window + 0.25 ratio → 3072 head+middle, comfortably above the 1024 floor."""
        monkeypatch.setenv("AGENT_CONTEXT_WINDOW_MAX_TOKENS", "4096")
        monkeypatch.setenv("AGENT_WITHIN_SESSION_MIN_TAIL_RATIO", "0.25")
        config = AppConfig()
        assert config.context_window_max_tokens == 4096
        assert config.within_session_min_tail_ratio == 0.25

    def test_ratio_field_constraints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ratio must be in [0.0, 1.0)."""
        from pydantic import ValidationError

        monkeypatch.setenv("AGENT_WITHIN_SESSION_MIN_TAIL_RATIO", "1.0")
        with pytest.raises(ValidationError):
            AppConfig()

        monkeypatch.setenv("AGENT_WITHIN_SESSION_MIN_TAIL_RATIO", "-0.1")
        with pytest.raises(ValidationError):
            AppConfig()


class TestAttachmentGuardrailCaps:
    """FRE-666 / ADR-0101 §6 — raster attachment resolution guardrail defaults."""

    def test_defaults(self) -> None:
        config = AppConfig()
        assert config.attachment_image_max_pixels == 1568
        assert config.attachment_image_max_bytes == 5_242_880
        assert config.attachment_max_images_per_turn == 4
        assert config.attachment_max_total_payload_bytes == 15_728_640


class TestDocumentGuardrailCaps:
    """FRE-683 / ADR-0102 §1, §4, §5 — document resolution guardrail defaults."""

    def test_defaults(self) -> None:
        config = AppConfig()
        assert config.document_text_density_floor_per_page == 100
        assert config.document_max_pages_per_turn == 40
        assert config.document_page_max_pixels == 1568
        assert config.document_page_max_bytes == 5_242_880
        assert config.document_max_total_payload_bytes == 15_728_640
        assert config.document_max_extracted_text_chars == 200_000


class TestEntityExtractionFewshotFlag:
    """FRE-759 — the flag gating the few-shot exemplar block (default OFF)."""

    def test_flag_exists_and_defaults_false(self) -> None:
        """The prompt-exemplar flag exists and defaults False (ships flag-dark)."""
        config = AppConfig()
        assert config.entity_extraction_fewshot_exemplars_enabled is False

    def test_flag_reads_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The flag is togglable via its AGENT_-prefixed env var."""
        monkeypatch.setenv("AGENT_ENTITY_EXTRACTION_FEWSHOT_EXEMPLARS_ENABLED", "true")
        config = AppConfig()
        assert config.entity_extraction_fewshot_exemplars_enabled is True


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

    def test_env_files_skipped_by_default_in_test_environment(self, tmp_path: Path) -> None:
        """FRE-1318: a unit test run must not load a developer's real .env.

        CI never has a .env file, so a local run that loads one diverges from CI by
        construction. ``APP_ENV=test`` (conftest's default for the whole suite) must
        skip file loading with no marker required on the individual test.
        """
        (tmp_path / ".env").write_text("FRE_1318_TEST_VAR=from_dotenv\n")
        original_app_env = os.environ.get("APP_ENV")
        original_integration = os.environ.get("PERSONAL_AGENT_INTEGRATION")
        os.environ["APP_ENV"] = "test"
        os.environ.pop("PERSONAL_AGENT_INTEGRATION", None)
        try:
            from personal_agent.config.env_loader import load_env_files

            load_env_files(tmp_path)
            assert os.getenv("FRE_1318_TEST_VAR") is None
        finally:
            if original_app_env is not None:
                os.environ["APP_ENV"] = original_app_env
            else:
                os.environ.pop("APP_ENV", None)
            if original_integration is not None:
                os.environ["PERSONAL_AGENT_INTEGRATION"] = original_integration
            os.environ.pop("FRE_1318_TEST_VAR", None)

    def test_env_files_loaded_when_integration_opt_in_set(self, tmp_path: Path) -> None:
        """FRE-1318: PERSONAL_AGENT_INTEGRATION=1 is the escape hatch back to real .env.

        Mirrors FRE-375's AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE posture, applied
        to configuration instead of substrate — reuses the flag every integration
        test already sets rather than adding a second one.
        """
        (tmp_path / ".env").write_text("FRE_1318_TEST_VAR=from_dotenv\n")
        original_app_env = os.environ.get("APP_ENV")
        original_integration = os.environ.get("PERSONAL_AGENT_INTEGRATION")
        os.environ["APP_ENV"] = "test"
        os.environ["PERSONAL_AGENT_INTEGRATION"] = "1"
        try:
            from personal_agent.config.env_loader import load_env_files

            load_env_files(tmp_path)
            assert os.getenv("FRE_1318_TEST_VAR") == "from_dotenv"
        finally:
            if original_app_env is not None:
                os.environ["APP_ENV"] = original_app_env
            else:
                os.environ.pop("APP_ENV", None)
            if original_integration is not None:
                os.environ["PERSONAL_AGENT_INTEGRATION"] = original_integration
            else:
                os.environ.pop("PERSONAL_AGENT_INTEGRATION", None)
            os.environ.pop("FRE_1318_TEST_VAR", None)


class TestLoadAppConfig:
    """Test load_app_config function."""

    def test_load_app_config_creates_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that load_app_config creates a valid config."""
        # A leaked AGENT_DEPLOYMENT_PROFILE would put this "default config" test
        # on the cloud profile, whose required-secret check (FRE-649) fails with
        # no API keys set. Isolate the default-profile intent explicitly.
        monkeypatch.delenv("AGENT_DEPLOYMENT_PROFILE", raising=False)
        config = load_app_config()
        assert isinstance(config, AppConfig)
        assert config.log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

    def test_load_app_config_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that load_app_config logs configuration loading."""
        monkeypatch.delenv("AGENT_DEPLOYMENT_PROFILE", raising=False)  # see test above

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


class TestCFAccessSettings:
    """Test CF Access credential fields on AppConfig."""

    def test_outbound_cf_fields_deleted_by_adr_0132(self) -> None:
        """The outbound CF service-token pair no longer exists (ADR-0132 D1).

        Caddy holds the credential now; the inbound JWT-verification fields
        (cf_access_team_domain / cf_access_aud) are deliberately retained.
        """
        config = AppConfig()
        assert not hasattr(config, "cf_access_client_id")
        assert not hasattr(config, "cf_access_client_secret")
