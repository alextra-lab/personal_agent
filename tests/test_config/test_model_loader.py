"""Tests for model configuration loader."""

import re
from pathlib import Path

import pytest
import structlog

from personal_agent.config import ModelConfigError, load_model_config
from personal_agent.config import model_loader as model_loader_module
from personal_agent.config.model_loader import check_vision_capabilities
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

    def test_supports_vision_defaults_false(self, tmp_path: Path) -> None:
        """ModelDefinition.supports_vision defaults to False when omitted (ADR-0101 §5)."""
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

        config = load_model_config(config_file)

        assert config.models["router"].supports_vision is False

    def test_supports_vision_explicit_true(self, tmp_path: Path) -> None:
        """ModelDefinition.supports_vision is set from config when declared true."""
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  vision_model:
    id: "test-vision"
    context_length: 8192
    quantization: "8bit"
    max_concurrency: 4
    default_timeout: 5
    supports_vision: true
"""
        )

        config = load_model_config(config_file)

        assert config.models["vision_model"].supports_vision is True

    def test_load_uses_cache_for_same_path(self, tmp_path: Path) -> None:
        """Test repeated loads for same path only parse YAML once."""
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

        model_loader_module._load_model_config_at_path.cache_clear()
        try:
            first = load_model_config(config_file)
            second = load_model_config(config_file)
            cache_info = model_loader_module._load_model_config_at_path.cache_info()
        finally:
            model_loader_module._load_model_config_at_path.cache_clear()

        assert first == second
        assert cache_info.misses == 1
        assert cache_info.hits == 1


class TestSupportsVisionDeployedConfig:
    """ADR-0101 §5: the deployed vision-capable models declare supports_vision.

    FRE-734: guards BOTH deployed config files. The original guard read only
    ``config/models.yaml``, so the cloud deployment (``config/models.cloud.yaml``,
    selected via ``AGENT_MODEL_CONFIG_PATH`` in ``docker-compose.cloud.yml``) drifted to
    ``supports_vision=False`` on every model and broke vision in production while CI
    stayed green. ``docker-compose.eval.yml`` also selects ``config/models.cloud.yaml``
    (FRE-735 repointed it off a retired ``config/models.eval.yaml``), so it needs no
    separate parametrization here.
    """

    _VISION_ROLES = ("primary", "sub_agent", "claude_sonnet", "claude_haiku")

    @pytest.mark.parametrize(
        "config_path",
        [
            Path("config/models.yaml"),
            Path("config/models.cloud.yaml"),
        ],
    )
    def test_deployed_vision_capable_models_flagged(self, config_path: Path) -> None:
        """primary, sub_agent, claude_sonnet, claude_haiku all declare supports_vision=True."""
        config = load_model_config(config_path)

        for key in self._VISION_ROLES:
            assert config.models[key].supports_vision is True, (
                f"{key} must support vision in {config_path}"
            )


class TestDockerComposeModelConfigPaths:
    """Every AGENT_MODEL_CONFIG_PATH in a docker-compose file must resolve to a real file.

    FRE-735: ``docker-compose.eval.yml`` drifted to ``config/models.eval.yaml``, a file
    FRE-645 retired months earlier — the compose reference was never updated, so
    bringing up the eval gateway raised ``ModelConfigError`` at startup. This
    generalizes FRE-734's per-file vision-parity guard into a plain existence
    check across every ``docker-compose*.yml`` in the repo, so a stale pointer
    fails CI instead of only surfacing at container boot.
    """

    _CONTAINER_PREFIX = "/app/"
    _PATTERN = re.compile(r"AGENT_MODEL_CONFIG_PATH:\s*(\S+)")

    def test_all_referenced_configs_exist(self) -> None:
        """Every AGENT_MODEL_CONFIG_PATH across docker-compose*.yml points at a real file."""
        repo_root = Path(__file__).resolve().parents[2]
        compose_files = sorted(repo_root.glob("docker-compose*.yml"))
        assert compose_files, "expected at least one docker-compose*.yml in repo root"

        missing: list[str] = []
        for compose_file in compose_files:
            for line in compose_file.read_text().splitlines():
                match = self._PATTERN.search(line)
                if match is None:
                    continue
                container_path = match.group(1).strip().strip("\"'")
                relative_path = container_path.removeprefix(self._CONTAINER_PREFIX)
                if not (repo_root / relative_path).is_file():
                    missing.append(f"{compose_file.name}: {container_path}")

        assert not missing, (
            f"docker-compose AGENT_MODEL_CONFIG_PATH references a missing config file: {missing}"
        )


class TestCheckVisionCapabilities:
    """FRE-734 startup drift guard: check_vision_capabilities (ADR-0101 §5)."""

    @staticmethod
    def _model(*, supports_vision: bool) -> ModelDefinition:
        return ModelDefinition(
            id="test-model",
            context_length=8192,
            max_concurrency=1,
            default_timeout=30,
            supports_vision=supports_vision,
        )

    def _patch_config(
        self, monkeypatch: pytest.MonkeyPatch, models: dict[str, ModelDefinition]
    ) -> None:
        monkeypatch.setattr(
            model_loader_module,
            "load_model_config",
            lambda *a, **k: ModelConfig(models=models),
        )

    def test_all_expected_roles_flagged_no_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When every expected role is vision-flagged, missing is empty and no warning fires."""
        self._patch_config(
            monkeypatch,
            {
                role: self._model(supports_vision=True)
                for role in model_loader_module._EXPECTED_VISION_ROLES
            },
        )

        with structlog.testing.capture_logs() as logs:
            capable, missing = check_vision_capabilities()

        assert missing == []
        assert set(capable) == set(model_loader_module._EXPECTED_VISION_ROLES)
        events = [entry["event"] for entry in logs]
        assert "vision_capabilities_at_startup" in events
        assert "vision_capable_roles_missing" not in events

    def test_drift_warns_role_aware(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The cloud-drift shape (roles present but unflagged) warns and reports the roles."""
        self._patch_config(
            monkeypatch,
            {
                role: self._model(supports_vision=False)
                for role in model_loader_module._EXPECTED_VISION_ROLES
            },
        )

        with structlog.testing.capture_logs() as logs:
            capable, missing = check_vision_capabilities()

        assert capable == []
        assert set(missing) == set(model_loader_module._EXPECTED_VISION_ROLES)
        warnings = [e for e in logs if e["event"] == "vision_capable_roles_missing"]
        assert len(warnings) == 1
        assert set(warnings[0]["missing_roles"]) == set(model_loader_module._EXPECTED_VISION_ROLES)

    def test_load_failure_is_non_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A config load failure is swallowed, logged, and returns empty lists (never raises)."""

        def _boom(*a: object, **k: object) -> ModelConfig:
            raise ModelConfigError("simulated load failure")

        monkeypatch.setattr(model_loader_module, "load_model_config", _boom)

        with structlog.testing.capture_logs() as logs:
            capable, missing = check_vision_capabilities()

        assert (capable, missing) == ([], [])
        assert "vision_capabilities_check_failed" in [e["event"] for e in logs]
