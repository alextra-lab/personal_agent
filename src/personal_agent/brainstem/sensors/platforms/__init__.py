"""Platform-specific sensor implementations.

This package contains platform-specific sensor polling implementations.
Each platform module should export a `poll_platform_metrics()` function
that returns a dictionary of platform-specific metrics.
"""

from personal_agent.brainstem.sensors.platforms.apple import poll_apple_metrics
from personal_agent.brainstem.sensors.platforms.base import poll_base_metrics

__all__ = ["poll_apple_metrics", "poll_base_metrics"]
