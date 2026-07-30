"""Shared test helpers for personal_agent.events tests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4


def _capturing_log() -> tuple[MagicMock, list[tuple[str, dict[str, object]]]]:
    """Return a mock module logger and the list its calls land in (FRE-847 pattern).

    Patches the module-level ``log`` object directly rather than using
    ``structlog.testing.capture_logs()``, which is unreliable under this
    suite's ``cache_logger_on_first_use`` config for an already-materialized
    logger (see ``tests/test_tools/test_perplexity.py``).
    """
    calls: list[tuple[str, dict[str, object]]] = []
    mock_log = MagicMock()
    mock_log.debug = MagicMock(side_effect=lambda event, **kw: calls.append((event, kw)))
    mock_log.warning = MagicMock(side_effect=lambda event, **kw: calls.append((event, kw)))
    return mock_log, calls


def _consolidation_event_payload() -> dict[str, object]:
    """Build a minimal ``consolidation.completed`` event payload accepted by parse_stream_event."""
    return {
        "event_type": "consolidation.completed",
        "event_id": str(uuid4()),
        "trace_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_component": "test",
        "captures_processed": 0,
        "entities_created": 0,
        "entities_promoted": 0,
    }
