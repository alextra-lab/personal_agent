"""Unit tests for the FRE-757 default-rating backfill selection logic.

Covers the pure `select_rating_docs` core (no Elasticsearch): callsite
preference, skip-already-rated, original-timestamp fidelity, and field copying.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scripts.migrate_fre757_backfill_default_rating import select_rating_docs


def _event(
    trace_id: str,
    *,
    callsite: str | None = "orchestrator.primary",
    ts: str = "2026-06-01T12:00:00Z",
    session_id: str = "sess-1",
    static_hash: str | None = "static-h",
    dyn_hash: str | None = "dyn-h",
    component_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "trace_id": trace_id,
        "session_id": session_id,
        "prompt_callsite": callsite,
        "prompt_static_prefix_hash": static_hash,
        "prompt_dynamic_hash": dyn_hash,
        "prompt_component_ids": component_ids if component_ids is not None else ["c1"],
        "@timestamp": ts,
    }


def test_rating_is_always_two_and_fields_copied() -> None:
    """A backfilled doc is rating=2 with denorms + session copied from the event."""
    docs = select_rating_docs([_event("t1")], existing_trace_ids=set())
    assert len(docs) == 1
    d = docs[0]
    assert d.trace_id == "t1"
    assert d.rating == 2
    assert d.session_id == "sess-1"
    assert d.prompt_callsite == "orchestrator.primary"
    assert d.prompt_static_prefix_hash == "static-h"
    assert d.prompt_dynamic_hash == "dyn-h"
    assert d.prompt_component_ids == ("c1",)


def test_rated_at_is_the_original_timestamp_not_now() -> None:
    """rated_at must equal the event's original @timestamp (windowing hazard)."""
    docs = select_rating_docs([_event("t1", ts="2026-03-14T09:30:00Z")], existing_trace_ids=set())
    assert docs[0].rated_at == datetime(2026, 3, 14, 9, 30, tzinfo=timezone.utc)


def test_already_rated_trace_is_skipped() -> None:
    """A trace that already has a rating doc is not backfilled."""
    docs = select_rating_docs([_event("t1")], existing_trace_ids={"t1"})
    assert docs == []


def test_callsite_preference_orchestrator_over_role() -> None:
    """When a trace has multiple callsites, the preferred one is chosen."""
    events = [
        _event("t1", callsite="role.primary", ts="2026-06-01T12:00:05Z"),
        _event("t1", callsite="orchestrator.primary", ts="2026-06-01T12:00:00Z"),
    ]
    docs = select_rating_docs(events, existing_trace_ids=set())
    assert len(docs) == 1
    assert docs[0].prompt_callsite == "orchestrator.primary"
    # rated_at comes from the CHOSEN (orchestrator.primary) event.
    assert docs[0].rated_at == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_non_preferred_callsites_pick_most_recent() -> None:
    """Between two non-preferred callsites, the more recent event wins."""
    events = [
        _event("t1", callsite="sub.agent", ts="2026-06-01T12:00:00Z"),
        _event("t1", callsite="tool.exec", ts="2026-06-01T12:00:09Z"),
    ]
    docs = select_rating_docs(events, existing_trace_ids=set())
    assert len(docs) == 1
    assert docs[0].prompt_callsite == "tool.exec"


def test_missing_timestamp_is_skipped() -> None:
    """A trace whose event has no usable @timestamp is skipped (not stamped now)."""
    ev = _event("t1")
    ev["@timestamp"] = ""
    docs = select_rating_docs([ev], existing_trace_ids=set())
    assert docs == []


def test_multiple_traces_deterministic_order() -> None:
    """Distinct traces each yield one doc, ordered by trace_id."""
    docs = select_rating_docs([_event("t2"), _event("t1")], existing_trace_ids=set())
    assert [d.trace_id for d in docs] == ["t1", "t2"]
