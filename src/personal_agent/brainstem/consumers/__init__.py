"""Brainstem event-bus consumers.

Consumers in this package subscribe to Redis Streams via the global EventBus
and drive homeostasis control loops:

- ``ModeControllerConsumer`` — cg:mode-controller (ADR-0055).  Aggregates
  MetricsSampledEvents into a rolling 60 s window, calls
  ``ModeManager.evaluate_transitions`` every 30 s, and tracks
  ModeTransitionEvent cadence to emit Captain's Log calibration proposals on
  anomalous edge frequency.
"""

from personal_agent.brainstem.consumers.mode_controller import ModeControllerConsumer

__all__ = ["ModeControllerConsumer"]
