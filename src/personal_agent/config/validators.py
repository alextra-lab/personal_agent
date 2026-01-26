"""Custom Pydantic validators for configuration.

This module provides validators for cross-field validation and
custom type conversions.
"""

from pathlib import Path


def validate_log_level(value: str) -> str:
    """Validate log level is one of the standard levels.

    Args:
        value: Log level string.

    Returns:
        Validated log level.

    Raises:
        ValueError: If log level is not valid.
    """
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if value.upper() not in valid_levels:
        raise ValueError(f"log_level must be one of {valid_levels}, got {value}")
    return value.upper()


def validate_log_format(value: str) -> str:
    """Validate log format is 'json' or 'console'.

    Args:
        value: Log format string.

    Returns:
        Validated log format.

    Raises:
        ValueError: If log format is not valid.
    """
    valid_formats = {"json", "console"}
    if value.lower() not in valid_formats:
        raise ValueError(f"log_format must be one of {valid_formats}, got {value}")
    return value.lower()


def resolve_path(value: Path | str) -> Path:
    """Resolve relative paths to absolute paths.

    Args:
        value: Path value (can be string or Path).

    Returns:
        Resolved Path object.
    """
    if isinstance(value, str):
        path = Path(value)
    else:
        path = value

    # If relative, resolve relative to project root
    if not path.is_absolute():
        # Assume we're in src/personal_agent/config, go up to project root
        project_root = Path(__file__).parent.parent.parent.parent
        path = (project_root / path).resolve()
    else:
        path = path.resolve()

    return path
