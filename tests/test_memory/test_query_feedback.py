"""Tests for memory query feedback metrics helpers."""

from datetime import datetime, timedelta, timezone

from personal_agent.memory.models import MemoryQuery
from personal_agent.memory.service import MemoryService


def test_detect_implicit_rephrase_true_for_recent_low_result_query() -> None:
    """Rephrase should be detected when a new query follows poor results quickly."""
    service = MemoryService()
    previous_state = {
        "signature": "text=python|entities=python|types=|conversations=|traces=|recency=None",
        "result_count": 0,
        "timestamp": datetime.now(timezone.utc) - timedelta(seconds=30),
    }

    detected = service._detect_implicit_rephrase(  # noqa: SLF001
        previous_state=previous_state,
        current_signature="text=python tutorial|entities=python,tutorial|types=|conversations=|traces=|recency=None",
    )

    assert detected is True


def test_detect_implicit_rephrase_false_for_stale_or_same_query() -> None:
    """Rephrase should not be detected when query is stale or unchanged."""
    service = MemoryService()
    stale_state = {
        "signature": "text=python|entities=python|types=|conversations=|traces=|recency=None",
        "result_count": 0,
        "timestamp": datetime.now(timezone.utc) - timedelta(minutes=30),
    }
    same_state = {
        "signature": "text=python|entities=python|types=|conversations=|traces=|recency=None",
        "result_count": 0,
        "timestamp": datetime.now(timezone.utc),
    }

    stale_detected = service._detect_implicit_rephrase(  # noqa: SLF001
        previous_state=stale_state,
        current_signature="text=python tutorial|entities=python,tutorial|types=|conversations=|traces=|recency=None",
    )
    same_detected = service._detect_implicit_rephrase(  # noqa: SLF001
        previous_state=same_state,
        current_signature="text=python|entities=python|types=|conversations=|traces=|recency=None",
    )

    assert stale_detected is False
    assert same_detected is False


def test_log_query_quality_metrics_updates_state_by_feedback_key() -> None:
    """Quality metrics logger should persist last query state for feedback."""
    service = MemoryService()
    query = MemoryQuery(entity_names=["Python"], limit=5)

    service._log_query_quality_metrics(  # noqa: SLF001
        query=query,
        relevance_scores={"conv-1": 0.7, "conv-2": 0.4},
        feedback_key="session-1",
        query_text="python",
    )

    assert "session-1" in service._query_feedback_by_key  # noqa: SLF001
    state = service._query_feedback_by_key["session-1"]  # noqa: SLF001
    assert state["result_count"] == 2
