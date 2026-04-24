"""Tests for build_error_pattern_captain_log_handler (ADR-0056 §step 6).

Tests verify:
1. handler calls manager.save_entry with a CONFIG_PROPOSAL CaptainLogEntry
2. scope is correctly derived for every prefix in the ADR §D5 table
3. CROSS_CUTTING is the fallback for unknown prefixes
4. Non-ErrorPatternDetectedEvent types are ignored
5. supporting_metrics and metrics_structured are populated
6. telemetry_refs are populated from sample_trace_ids
7. fingerprint is propagated to ProposedChange
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from personal_agent.captains_log.models import (
    CaptainLogEntryType,
    ChangeCategory,
    ChangeScope,
)
from personal_agent.events.models import (
    ConsolidationCompletedEvent,
    ErrorPatternDetectedEvent,
)
from personal_agent.events.pipeline_handlers import build_error_pattern_captain_log_handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    component: str = "tools.fetch_url",
    event_name: str = "fetch_url_timeout",
    error_type: str = "TimeoutError",
    occurrences: int = 12,
    fingerprint: str = "abc123def4560000",
    trace_ids: list[str] | None = None,
) -> ErrorPatternDetectedEvent:
    now = datetime.now(timezone.utc)
    return ErrorPatternDetectedEvent(
        source_component="telemetry.error_monitor",
        trace_id=None,
        fingerprint=fingerprint,
        component=component,
        event_name=event_name,
        error_type=error_type,
        level="ERROR",
        occurrences=occurrences,
        first_seen=now,
        last_seen=now,
        window_hours=24,
        sample_trace_ids=trace_ids or ["tid-1", "tid-2"],
        sample_messages=["Read timeout after 10s"],
    )


# ---------------------------------------------------------------------------
# Basic behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_calls_save_entry_with_config_proposal() -> None:
    """Handler emits a CONFIG_PROPOSAL CaptainLogEntry via manager.save_entry."""
    mock_manager = MagicMock()
    handler = build_error_pattern_captain_log_handler(manager=mock_manager)

    await handler(_make_event())

    mock_manager.save_entry.assert_called_once()
    entry = mock_manager.save_entry.call_args[0][0]
    assert entry.type == CaptainLogEntryType.CONFIG_PROPOSAL


@pytest.mark.asyncio
async def test_handler_ignores_non_error_pattern_events() -> None:
    """Non-ErrorPatternDetectedEvent events do not call save_entry."""
    mock_manager = MagicMock()
    handler = build_error_pattern_captain_log_handler(manager=mock_manager)
    other = ConsolidationCompletedEvent(
        source_component="brainstem.scheduler",
        captures_processed=3,
        entities_created=1,
        entities_promoted=0,
    )

    await handler(other)

    mock_manager.save_entry.assert_not_called()


@pytest.mark.asyncio
async def test_handler_sets_reliability_category() -> None:
    """proposed_change.category is ChangeCategory.RELIABILITY."""
    mock_manager = MagicMock()
    handler = build_error_pattern_captain_log_handler(manager=mock_manager)

    await handler(_make_event())

    entry = mock_manager.save_entry.call_args[0][0]
    assert entry.proposed_change is not None
    assert entry.proposed_change.category == ChangeCategory.RELIABILITY


@pytest.mark.asyncio
async def test_handler_propagates_fingerprint() -> None:
    """proposed_change.fingerprint matches the event fingerprint."""
    mock_manager = MagicMock()
    handler = build_error_pattern_captain_log_handler(manager=mock_manager)
    fp = "deadbeef12345678"

    await handler(_make_event(fingerprint=fp))

    entry = mock_manager.save_entry.call_args[0][0]
    assert entry.proposed_change is not None
    assert entry.proposed_change.fingerprint == fp


@pytest.mark.asyncio
async def test_handler_populates_supporting_metrics() -> None:
    """supporting_metrics list is non-empty and includes occurrences."""
    mock_manager = MagicMock()
    handler = build_error_pattern_captain_log_handler(manager=mock_manager)

    await handler(_make_event(occurrences=42))

    entry = mock_manager.save_entry.call_args[0][0]
    metrics_str = " ".join(entry.supporting_metrics)
    assert "42" in metrics_str


@pytest.mark.asyncio
async def test_handler_populates_metrics_structured() -> None:
    """metrics_structured contains occurrences and window_hours Metric objects."""
    mock_manager = MagicMock()
    handler = build_error_pattern_captain_log_handler(manager=mock_manager)

    await handler(_make_event())

    entry = mock_manager.save_entry.call_args[0][0]
    assert entry.metrics_structured is not None
    names = {m.name for m in entry.metrics_structured}
    assert "occurrences" in names
    assert "window_hours" in names


@pytest.mark.asyncio
async def test_handler_populates_telemetry_refs_from_trace_ids() -> None:
    """telemetry_refs has one ref per sample_trace_id."""
    mock_manager = MagicMock()
    handler = build_error_pattern_captain_log_handler(manager=mock_manager)

    await handler(_make_event(trace_ids=["tid-A", "tid-B"]))

    entry = mock_manager.save_entry.call_args[0][0]
    trace_ids_in_refs = [r.trace_id for r in entry.telemetry_refs]
    assert "tid-A" in trace_ids_in_refs
    assert "tid-B" in trace_ids_in_refs


# ---------------------------------------------------------------------------
# Scope derivation (ADR-0056 §D5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("component", "expected_scope"),
    [
        ("tools.fetch_url", ChangeScope.TOOLS),
        ("tools.elasticsearch", ChangeScope.TOOLS),
        ("mcp.gateway", ChangeScope.TOOLS),
        ("orchestrator.executor", ChangeScope.ORCHESTRATOR),
        ("request_gateway.pipeline", ChangeScope.ORCHESTRATOR),
        ("memory.service", ChangeScope.SECOND_BRAIN),
        ("second_brain.extractor", ChangeScope.SECOND_BRAIN),
        ("captains_log.manager", ChangeScope.CAPTAINS_LOG),
        ("brainstem.scheduler", ChangeScope.BRAINSTEM),
        ("telemetry.error_monitor", ChangeScope.TELEMETRY),
        ("governance.evaluator", ChangeScope.GOVERNANCE),
        ("insights.engine", ChangeScope.INSIGHTS),
        ("llm_client.main", ChangeScope.LLM_CLIENT),
        ("config.settings", ChangeScope.CROSS_CUTTING),
        ("service.app", ChangeScope.CROSS_CUTTING),
        ("totally_unknown.module", ChangeScope.CROSS_CUTTING),
    ],
)
async def test_scope_derivation(component: str, expected_scope: ChangeScope) -> None:
    """Each component prefix maps to the correct ChangeScope per ADR-0056 §D5."""
    mock_manager = MagicMock()
    handler = build_error_pattern_captain_log_handler(manager=mock_manager)

    await handler(_make_event(component=component))

    entry = mock_manager.save_entry.call_args[0][0]
    assert entry.proposed_change is not None
    assert entry.proposed_change.scope == expected_scope, (
        f"component={component!r} → expected {expected_scope}, "
        f"got {entry.proposed_change.scope}"
    )
