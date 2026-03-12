"""Security utilities for preventing information disclosure."""

import re

from personal_agent.config import settings
from personal_agent.config.env_loader import Environment


def _user_message_with_debug_hint(base: str, error_type: str, error_str: str) -> str:
    """Append a safe debug hint in development/debug so the real error is visible."""
    if not settings.debug and settings.environment != Environment.DEVELOPMENT:
        return base
    snippet = (error_str or "").strip()[:200]
    if snippet:
        return f"{base} (Debug: {error_type}: {snippet})"
    return f"{base} (Debug: {error_type})"


def sanitize_error_message(error: Exception) -> str:
    """Create a user-friendly error message without exposing sensitive details.

    This function filters out sensitive information like file paths, stack traces,
    memory addresses, and other internal details that could leak system information.
    In development or when debug is True, appends a safe hint with error type and
    a redacted snippet so the underlying cause can be diagnosed.

    Args:
        error: The exception that occurred

    Returns:
        A sanitized, user-friendly error message
    """
    error_type = type(error).__name__
    error_str = str(error)

    # Filter out sensitive patterns (file paths, stack traces, etc.)
    # Remove absolute paths
    error_str = re.sub(r"/[^\s]+", "[path]", error_str)
    # Remove common sensitive patterns
    error_str = re.sub(r"0x[0-9a-fA-F]+", "[address]", error_str)
    error_str = re.sub(r"line \d+", "[line]", error_str)

    # Categorize errors and provide helpful messages
    if "Connection" in error_type or "connection" in error_str.lower():
        return _user_message_with_debug_hint(
            "Unable to connect to the language model service. Please try again in a moment.",
            error_type,
            error_str,
        )
    elif "Timeout" in error_type or "timeout" in error_str.lower():
        return _user_message_with_debug_hint(
            "The request took too long to process. Please try again with a simpler request.",
            error_type,
            error_str,
        )
    elif "Permission" in error_type or "permission" in error_str.lower():
        return "Permission denied. Please check your configuration."
    elif "Validation" in error_type or "validation" in error_str.lower():
        return _user_message_with_debug_hint(
            "Invalid request format. Please check your input and try again.",
            error_type,
            error_str,
        )
    elif "NotFound" in error_type or "not found" in error_str.lower():
        return "The requested resource was not found."
    elif "RateLimit" in error_type or "rate limit" in error_str.lower():
        return "Too many requests. Please wait a moment and try again."
    elif "Configuration" in error_type or "config" in error_str.lower():
        return "Service configuration error. Please contact support."
    else:
        return _user_message_with_debug_hint(
            "An error occurred while processing your request. Please try again.",
            error_type,
            error_str,
        )
