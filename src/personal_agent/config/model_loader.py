"""Load and validate model configuration from YAML file.

This module provides the main entry point for loading model configuration:
- Loads config/models.yaml
- Validates against Pydantic schema
- Returns typed ModelConfig object

All configuration loaders live in the config/ module per ADR-0007.
"""

from pathlib import Path

import structlog
from pydantic import ValidationError

from personal_agent.config.loader import ConfigLoadError, load_yaml_file
from personal_agent.llm_client.models import ModelConfig

log = structlog.get_logger(__name__)


class ModelConfigError(ConfigLoadError):
    """Raised when model configuration cannot be loaded or is invalid."""

    pass


def load_model_config(config_path: Path | None = None) -> ModelConfig:
    """Load and validate model configuration from YAML file.

    Args:
        config_path: Path to models.yaml file. If None, uses settings.model_config_path
            from unified config.

    Returns:
        Validated ModelConfig object (not a raw dict).

    Raises:
        ModelConfigError: If configuration cannot be loaded, parsed, or validated.

    Example:
        >>> from personal_agent.config import load_model_config
        >>> config = load_model_config()
        >>> print(config.models["router"].id)
        qwen/qwen3-4b-thinking-2507
    """
    if config_path is None:
        # Use unified config system (ADR-0007)
        from personal_agent.config import settings  # noqa: PLC0415

        config_path = settings.model_config_path
        # Resolve relative paths to absolute (relative to project root)
        if not config_path.is_absolute():
            project_root = Path(__file__).parent.parent.parent.parent
            config_path = (project_root / config_path).resolve()
        else:
            config_path = config_path.resolve()
        log.debug("using_model_config_path_from_settings", path=str(config_path))
    elif isinstance(config_path, str):
        config_path = Path(config_path)

    # Ensure config_path is a Path at this point
    config_path = Path(config_path)

    if not config_path.exists():
        raise ModelConfigError(f"Model config file not found: {config_path}")

    if not config_path.is_file():
        raise ModelConfigError(f"Model config path is not a file: {config_path}")

    log.info("loading_model_config", config_path=str(config_path))

    # Load YAML file using shared loader
    try:
        content = load_yaml_file(config_path, error_class=ModelConfigError)
        if not content:
            log.warning("model_config_empty", config_path=str(config_path))
            # Return empty config with empty models dict
            return ModelConfig(models={})
    except ConfigLoadError as e:
        raise ModelConfigError(f"Failed to load model config file: {e}") from None

    # Validate against Pydantic schema
    try:
        config = ModelConfig.model_validate(content)
        log.info(
            "model_config_loaded",
            models_count=len(config.models),
            model_ids=[model.id for model in config.models.values()],
        )
        return config
    except ValidationError as e:
        # Format validation errors for better debugging
        error_messages = []
        for error in e.errors():
            field_path = " -> ".join(str(loc) for loc in error["loc"])
            error_msg = f"{field_path}: {error['msg']}"
            error_messages.append(error_msg)

        error_summary = "\n".join(error_messages)
        raise ModelConfigError(f"Model configuration validation failed:\n{error_summary}") from None
    except Exception as e:
        raise ModelConfigError(f"Unexpected error validating model config: {e}") from None
