"""Tests for the execution-topology ES projection emitter (FRE-548).

``build_topology_doc`` is a pure ``RouteTraceRow`` -> explicit-typed ES doc adapter;
``project_route_trace_to_es`` fires that doc non-blocking + best-effort to the dedicated
``agent-topology-*`` index. These tests pin the doc schema (the FRE-537 contract) — the
field types most likely to mis-map (money ``double``, ms ``float``, join keys ``keyword``)
and the omit-when-None cases — plus the idempotent doc-id and the never-raise guarantee.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import personal_agent.observability.topology.es_projection as proj

from personal_agent.observability.route_trace.types import RouteTraceRow


def _turn_level_row(**overrides: object) -> RouteTraceRow:
    base: dict[str, object] = dict(
        trace_id=uuid4(),
        session_id=uuid4(),
        task_id=None,
        created_at=datetime(2026, 6, 13, 8, 30, tzinfo=timezone.utc),
        task_type="memory_recall",
        complexity="simple",
        gateway_label="memory_recall/single",
        orchestration_event="primary_handled",
        cost_authoritative_usd=0.42,
        input_tokens=100,
        output_tokens=50,
        latency_total_ms=12.5,
    )
    base.update(overrides)
    return RouteTraceRow(**base)  # type: ignore[arg-type]


def _segment_row(**overrides: object) -> RouteTraceRow:
    base: dict[str, object] = dict(
        trace_id=uuid4(),
        session_id=uuid4(),
        task_id=uuid4(),
        created_at=datetime(2026, 6, 13, 8, 30, tzinfo=timezone.utc),
        model_role="sub_agent",
        cost_authoritative_usd=0.02,
        input_tokens=0,
        output_tokens=0,
        latency_total_ms=None,
        task_type=None,
        complexity=None,
    )
    base.update(overrides)
    return RouteTraceRow(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# build_topology_doc — schema discipline
# ---------------------------------------------------------------------------


def test_turn_level_doc_shape() -> None:
    row = _turn_level_row()
    doc = proj.build_topology_doc(row, topology="primary")

    assert doc["role"] == "primary"
    assert "task_id" not in doc  # turn-level row carries no task_id
    assert doc["trace_id"] == str(row.trace_id)
    assert doc["session_id"] == str(row.session_id)
    assert doc["topology"] == "primary"
    assert doc["gateway_label"] == "memory_recall/single"
    assert doc["result_type"] == "primary_handled"  # sourced from orchestration_event
    assert doc["task_type"] == "memory_recall"
    assert doc["complexity"] == "simple"
    # @timestamp is an ISO string with the T separator (ES strict_date_optional_time).
    assert "T" in doc["@timestamp"]
    assert doc["@timestamp"] == row.created_at.isoformat()  # type: ignore[union-attr]
    # Money is a JSON float (double), tokens are ints (long), latency is a float.
    assert isinstance(doc["authoritative_cost_usd"], float)
    assert doc["authoritative_cost_usd"] == 0.42
    assert isinstance(doc["input_tokens"], int) and doc["input_tokens"] == 100
    assert isinstance(doc["output_tokens"], int) and doc["output_tokens"] == 50
    assert isinstance(doc["latency_total_ms"], float) and doc["latency_total_ms"] == 12.5


def test_segment_doc_shape() -> None:
    row = _segment_row()
    doc = proj.build_topology_doc(row, topology="hybrid_fanout")

    assert doc["role"] == "sub_agent"
    assert doc["task_id"] == str(row.task_id)  # segment carries its task_id (keyword)
    assert doc["topology"] == "hybrid_fanout"
    # None-valued fields are omitted, not emitted as null.
    assert "latency_total_ms" not in doc
    assert "task_type" not in doc
    assert "complexity" not in doc
    assert isinstance(doc["authoritative_cost_usd"], float)


def test_doc_timestamp_falls_back_when_created_at_none() -> None:
    row = _turn_level_row(created_at=None)
    doc = proj.build_topology_doc(row, topology="primary")
    # A fallback timestamp is present and ES-parseable (ISO with T).
    assert "T" in doc["@timestamp"]


# ---------------------------------------------------------------------------
# project_route_trace_to_es — index/doc_id + never-raise
# ---------------------------------------------------------------------------


def test_project_turn_level_index_and_doc_id(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, dict[str, object], str | None]] = []

    def _fake(index_name, document, *, doc_id=None):  # type: ignore[no-untyped-def]
        calls.append((index_name, document, doc_id))

    monkeypatch.setattr(proj, "schedule_es_index", _fake)
    row = _turn_level_row()
    proj.project_route_trace_to_es(row, topology="primary")

    assert len(calls) == 1
    index_name, document, doc_id = calls[0]
    assert index_name == "agent-topology-2026-06-13"
    assert doc_id == f"{row.trace_id}:turn"  # turn-level uses the ':turn' suffix
    assert document["role"] == "primary"


def test_project_segment_doc_id_uses_task_id(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, dict[str, object], str | None]] = []
    monkeypatch.setattr(
        proj,
        "schedule_es_index",
        lambda index_name, document, *, doc_id=None: calls.append((index_name, document, doc_id)),
    )
    row = _segment_row()
    proj.project_route_trace_to_es(row, topology="decompose")

    assert calls[0][2] == f"{row.trace_id}:{row.task_id}"


def test_project_never_raises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("es down")

    monkeypatch.setattr(proj, "schedule_es_index", _boom)
    # Must swallow — a telemetry failure can never break the turn.
    proj.project_route_trace_to_es(_turn_level_row(), topology="primary")
