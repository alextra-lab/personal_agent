"""Brainstem service for mode management and autonomic control.

This module provides the brainstem service that manages operational modes
and monitors system health through sensor polling.

The brainstem is the always-on regulatory core of the agent, maintaining
system stability and enforcing operational modes.
"""

from personal_agent.brainstem.mode_manager import ModeManager, ModeManagerError
from personal_agent.brainstem.optimizer import ThresholdOptimizer
from personal_agent.brainstem.sensors import (
    get_global_metrics_daemon,
    get_system_metrics_snapshot,
    poll_system_metrics,
    set_global_metrics_daemon,
)
from personal_agent.brainstem.sensors.metrics_daemon import MetricsDaemon
from personal_agent.events.bus import EventBus
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


def get_or_create_metrics_daemon(
    event_bus: EventBus | None = None,
) -> MetricsDaemon:
    """Get the global MetricsDaemon singleton, creating it if needed.

    Args:
        event_bus: Optional event bus to inject when creating the daemon for
            the first time.  If the singleton already exists the ``event_bus``
            argument is silently ignored — the running instance keeps the bus
            it was originally constructed with.

    Returns:
        The global MetricsDaemon singleton.
    """
    daemon = get_global_metrics_daemon()
    if daemon is None:
        from personal_agent.config import settings

        daemon = MetricsDaemon(
            poll_interval_seconds=settings.metrics_daemon_poll_interval_seconds,
            es_emit_interval_seconds=settings.metrics_daemon_es_emit_interval_seconds,
            buffer_size=settings.metrics_daemon_buffer_size,
            event_bus=event_bus,
        )
        set_global_metrics_daemon(daemon)
    return daemon


__all__ = [
    "ModeManager",
    "ModeManagerError",
    "ThresholdOptimizer",
    "Mode",
    "get_mode_manager",
    "get_current_mode",
    "get_or_create_metrics_daemon",
    "poll_system_metrics",
    "get_system_metrics_snapshot",
    "get_global_metrics_daemon",
    "set_global_metrics_daemon",
]
