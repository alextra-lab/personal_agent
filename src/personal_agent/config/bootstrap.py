"""Bootstrap configuration helpers (pre-settings).

These helpers exist for "chicken-and-egg" situations where we need a small amount
of configuration before the full Pydantic settings singleton can be imported.

Constraints:
- Keep this module dependency-light (no telemetry imports) to avoid circular imports.
- Prefer validating values using existing config validators.
"""

from __future__ import annotations

import os

from personal_agent.config.env_loader import load_env_files
from personal_agent.config.validators import validate_log_level

_env_loaded_for_log_level = False


def get_bootstrap_log_level(default: str = "INFO") -> str:
    """Get logging level from environment without importing settings.

    Ensures .env files are loaded before reading APP_LOG_LEVEL so that
    APP_LOG_LEVEL=WARNING (and similar) in .env is respected even when
    logging is configured before the full settings singleton is used.

    Args:
        default: Default log level if not set or invalid.

    Returns:
        Uppercased, validated log level string.
    """
    global _env_loaded_for_log_level
    if not _env_loaded_for_log_level:
        load_env_files()
        _env_loaded_for_log_level = True
    value = os.getenv("APP_LOG_LEVEL", default)
    try:
        return validate_log_level(value)
    except ValueError:
        return validate_log_level(default)
