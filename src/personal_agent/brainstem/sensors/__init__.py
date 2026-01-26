"""Sensor polling package.

This package provides platform-aware sensor polling. The main module
detects the platform and delegates to platform-specific implementations.

Structure:
- sensors.py: Main API that detects platform and combines metrics
- platforms/base.py: Cross-platform metrics (psutil)
- platforms/apple.py: Apple Silicon-specific metrics (GPU via powermetrics)
"""

from personal_agent.brainstem.sensors.sensors import (
    get_system_metrics_snapshot,
    poll_system_metrics,
)

__all__ = ["poll_system_metrics", "get_system_metrics_snapshot"]
