"""Load and validate model configuration from YAML file.

This module provides the main entry point for loading model configuration:
- Loads config/models.yaml
- Validates against Pydantic schema
- Returns typed ModelConfig object

All configuration loaders live in the config/ module per ADR-0007.
"""

import functools
from pathlib import Path

import structlog
from pydantic import ValidationError

from personal_agent.config.loader import ConfigLoadError, load_yaml_file
from personal_agent.llm_client.models import ModelConfig

log = structlog.get_logger(__name__)


class ModelConfigError(ConfigLoadError):
    """Raised when model configuration cannot be loaded or is invalid."""

    pass


@functools.lru_cache(maxsize=8)
def _load_model_config_at_path(config_path_str: str) -> ModelConfig:
    """Load and validate model configuration for a resolved path."""
    config_path = Path(config_path_str)

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


def load_model_config(config_path: Path | str | None = None) -> ModelConfig:
    """Load and validate model configuration from YAML file.

    Args:
        config_path: Path (or path string) to models.yaml file. If None, uses
            settings.model_config_path from unified config.

    Returns:
        Validated ModelConfig object (not a raw dict).

    Raises:
        ModelConfigError: If configuration cannot be loaded, parsed, or validated.

    Example:
        >>> from personal_agent.config import load_model_config
        >>> config = load_model_config()
        >>> print(config.models["primary"].id)
        qwen/qwen3-35b-a22b
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

    # Ensure config_path is a Path at this point and use a canonical key for caching.
    config_path = Path(config_path).resolve()

    if not config_path.exists():
        raise ModelConfigError(f"Model config file not found: {config_path}")

    if not config_path.is_file():
        raise ModelConfigError(f"Model config path is not a file: {config_path}")

    return _load_model_config_at_path(str(config_path))


def resolve_active_attribution(
    *,
    trace_id: str | None = None,
) -> tuple[str | None, str]:
    """Resolve the active primary model id and its config path string.

    ADR-0074 (FRE-376) requires every new session and every assistant
    message to carry the model attribution that was in effect when the row
    was written. This helper centralises the lookup so the service layer
    and the Redis event consumer both produce identical attribution.

    Args:
        trace_id: Originating chat trace, threaded through for log
            correlation on the warning path (ADR-0074 §I3). Optional because
            this helper is also called from startup smoke checks where no
            trace exists.

    Returns:
        ``(primary_model_id, model_config_path_str)`` — ``primary_model_id``
        is ``None`` only if the config has no ``primary`` role assignment
        (degenerate startup config); ``model_config_path_str`` is always
        the resolved path string from settings.
    """
    from personal_agent.config import settings  # noqa: PLC0415

    config_path_str = str(settings.model_config_path)
    try:
        cfg = load_model_config()
        primary = cfg.models.get("primary")
        primary_id = primary.id if primary is not None else None
    except Exception as exc:  # noqa: BLE001 — keep the chat-turn path live
        log.warning(
            "model_attribution_resolve_failed",
            error=str(exc),
            config_path=config_path_str,
            trace_id=trace_id,
        )
        primary_id = None
    return primary_id, config_path_str
