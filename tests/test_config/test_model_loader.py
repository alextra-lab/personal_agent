"""Tests for model configuration loader."""

import re
from pathlib import Path

import pytest
import structlog

from personal_agent.config import ModelConfigError, load_model_config, settings
from personal_agent.config import model_loader as model_loader_module
from personal_agent.config.model_loader import check_vision_capabilities
from personal_agent.config.settings import AppConfig
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

    def test_supports_pdf_document_defaults_false(self, tmp_path: Path) -> None:
        """ModelDefinition.supports_pdf_document defaults to False when omitted (ADR-0102 §3)."""
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

        assert config.models["router"].supports_pdf_document is False

    def test_supports_pdf_document_explicit_true(self, tmp_path: Path) -> None:
        """ModelDefinition.supports_pdf_document is set from config when declared true."""
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  document_model:
    id: "test-document"
    context_length: 8192
    quantization: "8bit"
    max_concurrency: 4
    default_timeout: 5
    supports_pdf_document: true
"""
        )

        config = load_model_config(config_file)

        assert config.models["document_model"].supports_pdf_document is True

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


class TestSlmTunnelOverride:
    """FRE-895: config/models*.yaml ship a placeholder SLM tunnel host; the real host

    is only ever supplied at runtime via ``settings.slm_tunnel_base_url``, never
    hardcoded in tracked source.
    """

    @staticmethod
    def _write_config(tmp_path: Path) -> Path:
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  reranker:
    id: "test-reranker"
    endpoint: "https://slm.example.com/v1"
    context_length: 8192
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 30
  cloud:
    id: "test-cloud"
    context_length: 8192
    quantization: "8bit"
    max_concurrency: 2
    default_timeout: 30
"""
        )
        return config_file

    def test_placeholder_untouched_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "slm_tunnel_base_url", None)
        config_file = self._write_config(tmp_path)

        config = load_model_config(config_file)

        assert config.models["reranker"].endpoint == "https://slm.example.com/v1"

    def test_rewritten_when_set_path_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "slm_tunnel_base_url", "https://slm.real-tunnel.test")
        config_file = self._write_config(tmp_path)

        config = load_model_config(config_file)

        assert config.models["reranker"].endpoint == "https://slm.real-tunnel.test/v1"

    def test_non_placeholder_endpoint_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A local/dev endpoint that isn't the placeholder host is never rewritten."""
        monkeypatch.setattr(settings, "slm_tunnel_base_url", "https://slm.real-tunnel.test")
        config_file = tmp_path / "models.yaml"
        config_file.write_text(
            """
models:
  router:
    id: "test-router"
    endpoint: "http://localhost:8000/v1"
    context_length: 8192
    quantization: "8bit"
    max_concurrency: 4
    default_timeout: 5
"""
        )

        config = load_model_config(config_file)

        assert config.models["router"].endpoint == "http://localhost:8000/v1"

    def test_no_endpoint_untouched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A role with no endpoint override (cloud models) is left as None."""
        monkeypatch.setattr(settings, "slm_tunnel_base_url", "https://slm.real-tunnel.test")
        config_file = self._write_config(tmp_path)

        config = load_model_config(config_file)

        assert config.models["cloud"].endpoint is None

    def test_explicit_settings_param_wins_over_live_singleton(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression (codex/security-review catch, FRE-895): a caller resolving

        against an explicit ``AppConfig`` (ADR-0112 D3/AC-2's "same interface, no
        code edit" seam, e.g. ``resolve_substrate(profile, settings=custom)``)
        must get *that* config's slm_tunnel_base_url, never the live process-wide
        singleton's — even when they differ.
        """
        monkeypatch.setattr(settings, "slm_tunnel_base_url", "https://slm.live-singleton.test")
        config_file = self._write_config(tmp_path)

        explicit_settings = AppConfig(slm_tunnel_base_url="https://slm.explicit-config.test")
        config = load_model_config(config_file, settings=explicit_settings)

        assert config.models["reranker"].endpoint == "https://slm.explicit-config.test/v1"

    def test_no_settings_param_falls_back_to_live_singleton(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "slm_tunnel_base_url", "https://slm.live-singleton.test")
        config_file = self._write_config(tmp_path)

        config = load_model_config(config_file)

        assert config.models["reranker"].endpoint == "https://slm.live-singleton.test/v1"


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

    _VISION_ROLES = (
        "qwen3.6-35b-thinking",
        "qwen3.6-35b-instruct",
        "claude_sonnet",
        "claude_haiku",
    )

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


class TestSupportsPdfDocumentDeployedConfig:
    """ADR-0102 §3: the deployed cloud Claude models declare supports_pdf_document.

    FRE-682: mirrors the FRE-734 vision-parity guard (``TestSupportsVisionDeployedConfig``
    above) so the document capability flag can't independently drift between
    ``config/models.yaml`` and ``config/models.cloud.yaml`` the way ``supports_vision``
    once did. ``primary``/``sub_agent`` are vision-capable but NOT document-capable
    (they have no native-PDF-block equivalent) — asserted False here to lock in that
    composition per ADR-0102's Implementation Notes.
    """

    _PDF_CAPABLE_ROLES = ("claude_sonnet", "claude_haiku")
    _PDF_INCAPABLE_ROLES = ("qwen3.6-35b-thinking", "qwen3.6-35b-instruct")

    @pytest.mark.parametrize(
        "config_path",
        [
            Path("config/models.yaml"),
            Path("config/models.cloud.yaml"),
        ],
    )
    def test_deployed_pdf_document_capable_models_flagged(self, config_path: Path) -> None:
        """claude_sonnet and claude_haiku declare supports_pdf_document=True."""
        config = load_model_config(config_path)

        for key in self._PDF_CAPABLE_ROLES:
            assert config.models[key].supports_pdf_document is True, (
                f"{key} must support the native PDF document block in {config_path}"
            )

    @pytest.mark.parametrize(
        "config_path",
        [
            Path("config/models.yaml"),
            Path("config/models.cloud.yaml"),
        ],
    )
    def test_deployed_pdf_document_incapable_models_unflagged(self, config_path: Path) -> None:
        """Primary and sub_agent declare supports_pdf_document=False."""
        config = load_model_config(config_path)

        for key in self._PDF_INCAPABLE_ROLES:
            assert config.models[key].supports_pdf_document is False, (
                f"{key} must not claim the native PDF document block in {config_path}"
            )


class TestEntityExtractionTemperatureDeployedConfig:
    """FRE-758: the deployed entity extractor runs at a pinned near-0 temperature.

    Asserts through the real YAML + role indirection (``entity_extraction_role``),
    not a hand-supplied ``model_def`` — a mocked call-site test cannot catch the
    role pointing at a *different, unpinned* model entry (caught in codex
    plan-review: ``config/models.yaml``'s ``entity_extraction_role`` is
    ``gpt-5.4-nano``, not ``gpt-5.4-mini``).
    """

    @pytest.mark.parametrize(
        "config_path",
        [
            Path("config/models.yaml"),
            Path("config/models.cloud.yaml"),
        ],
    )
    def test_entity_extraction_role_has_pinned_temperature(self, config_path: Path) -> None:
        """The resolved entity_extraction role model must have temperature == 0.0."""
        from personal_agent.config.model_loader import resolve_role_model_key

        config = load_model_config(config_path)
        resolved_key = resolve_role_model_key("entity_extraction", config_path=config_path)

        role_model = config.models[resolved_key]
        assert role_model.temperature == 0.0, (
            f"{resolved_key} in {config_path} must be pinned to "
            f"temperature=0.0, got {role_model.temperature!r}"
        )


class TestCloudGptPricingDeployedConfig:
    """FRE-742 / ADR-0101 §8b: the gpt-5.4 cloud models carry config-owned pricing.

    FRE-691 moved cloud cost into the model definition for the Claude entries only;
    gpt-5.4-nano / gpt-5.4-mini still deferred to litellm's shipped registry, which
    silently meters $0 if a future litellm upgrade renames or drops those ids (cf.
    FRE-734). Guards BOTH deployed config files (``config/models.yaml`` and
    ``config/models.cloud.yaml``) so a value present in one but not the other — the
    exact drift that broke vision in FRE-734 — fails CI. Rates per the YAML comments:
    nano $0.20/$1.25 per MTok, mini $0.75/$4.50 per MTok.
    """

    # (entry key, expected input_cost_per_token, expected output_cost_per_token)
    _EXPECTED = (
        ("gpt-5.4-nano", 0.0000002, 0.00000125),
        ("gpt-5.4-mini", 0.00000075, 0.0000045),
    )

    @pytest.mark.parametrize(
        "config_path",
        [
            Path("config/models.yaml"),
            Path("config/models.cloud.yaml"),
        ],
    )
    def test_gpt_cloud_models_carry_config_pricing(self, config_path: Path) -> None:
        """gpt-5.4-nano and gpt-5.4-mini declare the documented per-token pricing."""
        config = load_model_config(config_path)

        for key, expected_input, expected_output in self._EXPECTED:
            model = config.models[key]
            assert model.input_cost_per_token == pytest.approx(expected_input), (
                f"{key} in {config_path} must carry input_cost_per_token="
                f"{expected_input}, got {model.input_cost_per_token!r}"
            )
            assert model.output_cost_per_token == pytest.approx(expected_output), (
                f"{key} in {config_path} must carry output_cost_per_token="
                f"{expected_output}, got {model.output_cost_per_token!r}"
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
