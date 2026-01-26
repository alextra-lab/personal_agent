"""Bootstrap configuration helpers (pre-settings).

These helpers exist for "chicken-and-egg" situations where we need a small amount
of configuration before the full Pydantic settings singleton can be imported.

Constraints:
- Keep this module dependency-light (no telemetry imports) to avoid circular imports.
- Prefer validating values using existing config validators.
"""

from __future__ import annotations

import os

from personal_agent.config.validators import validate_log_level


def get_bootstrap_log_level(default: str = "INFO") -> str:
    """Get logging level from environment without importing settings.

    Args:
        default: Default log level if not set or invalid.

    Returns:
        Uppercased, validated log level string.
    """
    value = os.getenv("APP_LOG_LEVEL", default)
    try:
        return validate_log_level(value)
    except ValueError:
        return validate_log_level(default)
