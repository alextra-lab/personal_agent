"""Tests for CompactionRecord and compaction logging (ADR-0047 D3)."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from personal_agent.telemetry.compaction import (
    CompactionRecord,
    clear_dropped_entities,
    get_dropped_entities,
    log_compaction,
)


def test_compaction_record_creation() -> None:
    """CompactionRecord fields are correctly stored and accessible."""
    record = CompactionRecord(
        trace_id="test-trace",
        session_id="test-session",
        timestamp=datetime.now(timezone.utc),
        trigger="budget_exceeded",
        tier_affected="near",
        tokens_before=4000,
        tokens_after=2000,
        tokens_removed=2000,
        strategy="truncate",
        content_summary="Truncated 10 messages",
        entities_preserved=("entity-1", "entity-2"),
        entities_dropped=("entity-3",),
    )
    assert record.tokens_removed == record.tokens_before - record.tokens_after
    assert "entity-1" in record.entities_preserved
    assert "entity-3" in record.entities_dropped


def test_compaction_record_is_frozen() -> None:
    """CompactionRecord is immutable (frozen dataclass)."""
    record = CompactionRecord(
        trace_id="test",
        session_id="test",
        timestamp=datetime.now(timezone.utc),
        trigger="manual",
        tier_affected="episodic",
        tokens_before=100,
        tokens_after=50,
        tokens_removed=50,
        strategy="drop_oldest",
        content_summary="test",
        entities_preserved=(),
        entities_dropped=(),
    )
    with pytest.raises(Exception):  # frozen dataclass raises FrozenInstanceError
        record.trace_id = "changed"  # type: ignore[misc]


def test_log_compaction_updates_dropped_cache() -> None:
    """log_compaction populates the dropped-entity cache for quality feedback."""
    session_id = "session-cache-test"
    clear_dropped_entities(session_id)  # ensure clean state

    record = CompactionRecord(
        trace_id="trace-001",
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        trigger="budget_exceeded",
        tier_affected="episodic",
        tokens_before=3000,
        tokens_after=1500,
        tokens_removed=1500,
        strategy="drop_oldest",
        content_summary="Dropped memory context",
        entities_preserved=("entity-kept",),
        entities_dropped=("entity-gone", "entity-also-gone"),
    )
    log_compaction(record)

    dropped = get_dropped_entities(session_id)
    assert "entity-gone" in dropped
    assert "entity-also-gone" in dropped
    assert "entity-kept" not in dropped

    # Clean up
    clear_dropped_entities(session_id)


def test_get_dropped_entities_returns_empty_for_unknown_session() -> None:
    """get_dropped_entities returns empty set for sessions with no compaction."""
    result = get_dropped_entities("no-such-session-xyz")
    assert result == set()


def test_clear_dropped_entities_removes_cache() -> None:
    """clear_dropped_entities removes the session entry from the cache."""
    session_id = "session-to-clear"
    record = CompactionRecord(
        trace_id="t",
        session_id=session_id,
        timestamp=datetime.now(timezone.utc),
        trigger="manual",
        tier_affected="near",
        tokens_before=200,
        tokens_after=100,
        tokens_removed=100,
        strategy="truncate",
        content_summary="Manual clear",
        entities_preserved=(),
        entities_dropped=("e1",),
    )
    log_compaction(record)
    assert "e1" in get_dropped_entities(session_id)

    clear_dropped_entities(session_id)
    assert get_dropped_entities(session_id) == set()
