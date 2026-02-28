"""Load and validate governance configuration from YAML files.

This module provides the main entry point for loading governance configuration:
- Loads all YAML files from config/governance/
- Validates against Pydantic schemas
- Raises actionable errors on validation failure

All configuration loaders live in the config/ module per ADR-0007.
"""

from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError

from personal_agent.config.loader import ConfigLoadError, load_yaml_file
from personal_agent.governance.models import GovernanceConfig

log = structlog.get_logger(__name__)


class GovernanceConfigError(ConfigLoadError):
    """Raised when governance configuration cannot be loaded or validated."""

    pass


def load_governance_config(config_dir: Path | str | None = None) -> GovernanceConfig:
    """Load and validate governance configuration from YAML files.

    Loads configuration from the following files in config/governance/:
    - modes.yaml: Mode definitions and transition rules
    - tools.yaml: Tool permissions and categories
    - models.yaml: Model constraints per mode
    - safety.yaml: Safety policies and rate limits

    Args:
        config_dir: Path to governance config directory. If None, uses
            `settings.governance_config_path` from unified config.

    Returns:
        Validated GovernanceConfig object.

    Raises:
        GovernanceConfigError: If configuration cannot be loaded or validated.
            Error messages include actionable details about what failed.

    Example:
        >>> from personal_agent.config import load_governance_config
        >>> config = load_governance_config()
        >>> print(config.modes["NORMAL"].description)
        Default healthy operation
    """
    if config_dir is None:
        # Use unified config system (ADR-0007)
        from personal_agent.config import settings  # noqa: PLC0415

        config_path = settings.governance_config_path
        # Resolve relative paths to absolute (relative to project root)
        if not config_path.is_absolute():
            project_root = Path(__file__).parent.parent.parent.parent
            config_dir = (project_root / config_path).resolve()
        else:
            config_dir = config_path.resolve()
        log.debug("using_governance_path_from_settings", path=str(config_dir))
    elif isinstance(config_dir, str):
        config_dir = Path(config_dir)

    # Ensure config_dir is a Path at this point
    config_dir = Path(config_dir)

    if not config_dir.exists():
        raise GovernanceConfigError(f"Governance config directory does not exist: {config_dir}")

    if not config_dir.is_dir():
        raise GovernanceConfigError(f"Governance config path is not a directory: {config_dir}")

    log.info(
        "loading_governance_config",
        config_dir=str(config_dir),
    )

    # Load all YAML files using shared loader
    modes_file = config_dir / "modes.yaml"
    tools_file = config_dir / "tools.yaml"
    models_file = config_dir / "models.yaml"
    safety_file = config_dir / "safety.yaml"

    try:
        modes_data = load_yaml_file(modes_file, error_class=GovernanceConfigError)
        tools_data = load_yaml_file(tools_file, error_class=GovernanceConfigError)
        models_data = load_yaml_file(models_file, error_class=GovernanceConfigError)
        safety_data = load_yaml_file(safety_file, error_class=GovernanceConfigError)
    except ConfigLoadError as e:
        raise GovernanceConfigError(f"Failed to load governance config files: {e}") from None

    # Merge all data into a single structure
    merged_data: dict[str, Any] = {
        "modes": modes_data.get("modes", {}),
        "transition_rules": modes_data.get("transition_rules", {}),
        "tool_categories": tools_data.get("tool_categories", {}),
        "tools": tools_data.get("tools", {}),
        "mode_constraints": models_data.get("mode_constraints", {}),
        "safety": safety_data,
    }

    # Validate against Pydantic schema
    try:
        config = GovernanceConfig.model_validate(merged_data)
        log.info(
            "governance_config_loaded",
            modes_count=len(config.modes),
            tools_count=len(config.tools),
            transition_rules_count=len(config.transition_rules),
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
        raise GovernanceConfigError(
            f"Governance configuration validation failed:\n{error_summary}"
        ) from None
    except Exception as e:
        raise GovernanceConfigError(f"Unexpected error validating governance config: {e}") from None
