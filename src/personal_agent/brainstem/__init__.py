"""Brainstem service for mode management and autonomic control.

This module provides the brainstem service that manages operational modes
and monitors system health through sensor polling.

The brainstem is the always-on regulatory core of the agent, maintaining
system stability and enforcing operational modes.
"""

from personal_agent.brainstem.mode_manager import ModeManager, ModeManagerError
from personal_agent.brainstem.optimizer import ThresholdOptimizer
from personal_agent.brainstem.sensors import get_system_metrics_snapshot, poll_system_metrics
from personal_agent.governance.models import Mode

# Global mode manager instance (singleton pattern)
_mode_manager: ModeManager | None = None


def get_mode_manager() -> ModeManager:
    """Get or create the global mode manager instance.

    Returns:
        Global ModeManager instance.

    Raises:
        ModeManagerError: If mode manager cannot be initialized.
    """
    global _mode_manager
    if _mode_manager is None:
        _mode_manager = ModeManager()
    return _mode_manager


def get_current_mode() -> Mode:
    """Get current operational mode.

    This is the main public API for querying the current mode.
    Other components should use this function rather than accessing
    the mode manager directly.

    Returns:
        Current operational mode.

    Raises:
        ModeManagerError: If mode manager cannot be initialized.
    """
    return get_mode_manager().get_current_mode()


__all__ = [
    "ModeManager",
    "ModeManagerError",
    "ThresholdOptimizer",
    "Mode",
    "get_mode_manager",
    "get_current_mode",
    "poll_system_metrics",
    "get_system_metrics_snapshot",
]
