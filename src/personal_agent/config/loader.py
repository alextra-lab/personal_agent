"""Shared YAML loading utilities for configuration files.

This module provides common YAML loading functionality used by all
domain-specific config loaders (governance, models, etc.).
"""

from pathlib import Path
from typing import Any

import structlog
import yaml

log = structlog.get_logger(__name__)


class ConfigLoadError(Exception):
    """Base exception for configuration loading errors."""

    pass


def load_yaml_file(
    file_path: Path, error_class: type[Exception] = ConfigLoadError
) -> dict[str, Any]:
    """Load and parse a YAML file.

    Shared utility for loading YAML configuration files with consistent
    error handling and logging.

    Args:
        file_path: Path to the YAML file.
        error_class: Exception class to raise on errors. Defaults to ConfigLoadError.

    Returns:
        Parsed YAML content as a dictionary. Returns empty dict if file is empty or None.

    Raises:
        error_class: If file cannot be read or parsed. The error message includes
            the file path and specific error details.

    Example:
        >>> from pathlib import Path
        >>> data = load_yaml_file(Path("config/models.yaml"))
        >>> print(data.get("models", {}))
    """
    try:
        with file_path.open("r", encoding="utf-8") as f:
            content: dict[str, Any] | None = yaml.safe_load(f)
            if content is None:
                log.debug("yaml_file_empty", file_path=str(file_path))
                return {}
            return content
    except FileNotFoundError:
        raise error_class(f"Configuration file not found: {file_path}") from None
    except yaml.YAMLError as e:
        raise error_class(f"Failed to parse YAML file {file_path}: {e}") from None
    except Exception as e:
        raise error_class(f"Unexpected error reading {file_path}: {e}") from None
