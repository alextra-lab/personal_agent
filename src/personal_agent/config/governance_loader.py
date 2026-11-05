"""Load and validate governance configuration from YAML files.

This module provides the main entry point for loading governance configuration:
- Loads all YAML files from config/governance/
- Validates against Pydantic schemas
- Raises actionable errors on validation failure

All configuration loaders live in the config/ module per ADR-0007.
"""

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, JsonValue, ValidationError

from personal_agent.config.loader import ConfigLoadError, load_yaml_file
from personal_agent.config.setting_overrides import (
    MODE_TRANSITION_RULES_PARAMETER,
    SUB_AGENT_TOOLS_PARAMETER,
    TOOL_PARAMETER_PREFIX,
    current_overrides,
)
from personal_agent.governance.models import (
    GovernanceConfig,
    SubAgentToolDecision,
    ToolPolicy,
    TransitionRule,
)

log = structlog.get_logger(__name__)

#: The fields of a tool policy that hold paths. Their values stay hidden and file-only.
TOOL_PATH_FIELDS: tuple[str, ...] = ("allowed_paths", "forbidden_paths", "unattended_paths")


class GovernanceConfigError(ConfigLoadError):
    """Raised when governance configuration cannot be loaded or validated."""

    pass


def load_governance_config(config_dir: Path | str | None = None) -> GovernanceConfig:
    """Load governance configuration, with the stored overrides on the default files (FRE-1724).

    Args:
        config_dir: A governance config directory, which holds no override. If None, this uses
            ``settings.governance_config_path`` and applies the stored overrides.

    Returns:
        Validated GovernanceConfig object.

    Raises:
        GovernanceConfigError: If configuration cannot be loaded or validated.
    """
    config = file_governance_config(config_dir)
    return (
        config
        if config_dir is not None
        else apply_governance_overrides(config, current_overrides())
    )


def file_governance_config(config_dir: Path | str | None = None) -> GovernanceConfig:
    """Load and validate governance configuration from YAML files.

    Loads configuration from the following files in config/governance/:
    - modes.yaml: Mode definitions and transition rules
    - tools.yaml: Tool permissions and the sub-agent tool decisions

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

    try:
        modes_data = load_yaml_file(modes_file, error_class=GovernanceConfigError)
        tools_data = load_yaml_file(tools_file, error_class=GovernanceConfigError)
    except ConfigLoadError as e:
        raise GovernanceConfigError(f"Failed to load governance config files: {e}") from None

    # Merge all data into a single structure
    merged_data: dict[str, Any] = {
        "modes": modes_data.get("modes", {}),
        "transition_rules": modes_data.get("transition_rules", {}),
        "tools": tools_data.get("tools", {}),
        "sub_agent_tools": tools_data.get("sub_agent_tools", {}),
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


def _strict[T: BaseModel](model: type[T], value: JsonValue) -> T:
    """Check a stored value with the model that loads the file. A string does not coerce."""
    if not isinstance(value, dict):
        raise GovernanceConfigError(f"{model.__name__} takes an object")
    unknown = sorted(set(value) - set(model.model_fields))
    if unknown:
        raise GovernanceConfigError(f"unknown fields: {', '.join(unknown)}")
    try:
        return model.model_validate_json(json.dumps(value), strict=True)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise GovernanceConfigError(reasons) from None


def _tool_with_value(policy: ToolPolicy, value: JsonValue) -> ToolPolicy:
    if not isinstance(value, dict):
        raise GovernanceConfigError("a tool policy takes an object")
    paths = sorted(set(value) & set(TOOL_PATH_FIELDS))
    if paths:
        raise GovernanceConfigError(f"the path lists stay in the file: {', '.join(paths)}")
    return _strict(ToolPolicy, {**policy.model_dump(mode="json"), **value})


def _map_with_value[T: BaseModel](model: type[T], value: JsonValue) -> dict[str, T]:
    if not isinstance(value, dict) or not value:
        raise GovernanceConfigError(f"this takes a non-empty map of {model.__name__}")
    return {name: _strict(model, entry) for name, entry in value.items()}


def apply_governance_overrides(
    config: GovernanceConfig, overrides: Mapping[str, JsonValue]
) -> GovernanceConfig:
    """Return the config with the stored overrides applied (FRE-1724).

    A stored row that fails is skipped with a warning, and the file value applies. Every tool call
    reads this, so a stored row must never make it raise.

    Args:
        config: The config as the files state it.
        overrides: Every stored override, keyed by parameter name.

    Returns:
        The same object when no stored row applies, else a copy.
    """
    update: dict[str, object] = {}
    tools = dict(config.tools)
    for parameter, value in overrides.items():
        try:
            if parameter.startswith(TOOL_PARAMETER_PREFIX):
                name = parameter.removeprefix(TOOL_PARAMETER_PREFIX)
                if name in tools:
                    tools[name] = _tool_with_value(tools[name], value)
                    update["tools"] = tools
            elif parameter == SUB_AGENT_TOOLS_PARAMETER:
                update["sub_agent_tools"] = _map_with_value(SubAgentToolDecision, value)
            elif parameter == MODE_TRANSITION_RULES_PARAMETER:
                update["transition_rules"] = _map_with_value(TransitionRule, value)
        except GovernanceConfigError as exc:
            log.warning("governance_override_skipped", parameter=parameter, error=str(exc))
    return config.model_copy(update=update) if update else config


def validate_governance_override(parameter: str, value: JsonValue) -> JsonValue:
    """Check one proposed governance edit against the file config (FRE-1724).

    Args:
        parameter: The settings menu row name.
        value: The proposed value.

    Returns:
        The value to store. For a tool, the fields that differ from the file, so a later file change
        to any other field takes effect (FRE-1733). The result is empty when no field differs.

    Raises:
        GovernanceConfigError: With the reason, when the value is refused.
    """
    config = file_governance_config()
    if parameter.startswith(TOOL_PARAMETER_PREFIX):
        name = parameter.removeprefix(TOOL_PARAMETER_PREFIX)
        if name not in config.tools:
            raise GovernanceConfigError(f"the tool {name} is not in the governance file")
        if value == {}:
            raise GovernanceConfigError(f"{parameter} takes a non-empty object")
        edited = _tool_with_value(config.tools[name], value)
        policy = edited.model_dump(mode="json")
        assert isinstance(value, dict)
        return {
            key: policy[key]
            for key in sorted(value)
            if getattr(edited, key) != getattr(config.tools[name], key)
        }
    model = SubAgentToolDecision if parameter == SUB_AGENT_TOOLS_PARAMETER else TransitionRule
    entries = _map_with_value(model, value)
    return {name: entry.model_dump(mode="json") for name, entry in entries.items()}


class LiveGovernanceConfig:
    """A held governance config that reloads when the stored overrides change (FRE-1724).

    A reader that keeps the config for the life of the process uses this, so that an edit reaches
    its next read. A reload that fails keeps the last config, because every tool call reads it.
    """

    def __init__(self, loader: Callable[[], GovernanceConfig]) -> None:
        """Load the config once.

        Args:
            loader: The function that loads the config.
        """
        self._loader = loader
        self._loaded_for = current_overrides()
        self._config = loader()

    def get(self) -> GovernanceConfig:
        """Return the config for the stored overrides of this moment."""
        overrides = current_overrides()
        if overrides is not self._loaded_for:
            try:
                self._config = self._loader()
            except ConfigLoadError as exc:
                log.warning("governance_reload_failed", error=str(exc))
            self._loaded_for = overrides
        return self._config
