"""UI module for the personal agent.

This module provides user interface components including:
- CLI interface (Typer-based)
- Approval workflows (future)

The CLI can be run directly:
    python -m personal_agent.ui.cli "Your message here"

Note: We don't export CLI components from __init__.py to avoid
module loading issues when running as a script.
"""

__all__ = []  # CLI is run directly, no exports needed
