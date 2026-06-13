"""Execution-topology ES projection emitter (FRE-548 / ADR-0088).

Projects each completed turn's route-trace row — the turn-level row (``role=primary``) and
one segment row per sub-agent (``role=sub_agent``, FRE-517) — into the dedicated
``agent-topology-*`` Elasticsearch index, so Kibana can read the topology label, the
authoritative per-turn cost, and the per-``(trace_id, task_id)`` rows that otherwise live
only in the Postgres route-trace ledger (FRE-537 was blocked on exactly this).

The projection is the seam's **third sink** alongside the durable Postgres write and the
best-effort live bus publish: it is non-blocking (``schedule_es_index`` schedules an
``asyncio`` task) and best-effort (the whole body is guarded, so a telemetry failure can
never break the turn). It reuses the same in-hand :class:`RouteTraceRow` the seam already
assembled — one source of truth, never a re-derivation.

The document carries only an **explicit, known** field set (the dedicated index template is
``dynamic: false``); ``@timestamp`` is emitted manually because the direct ``index_document``
path adds no envelope. Money is a JSON ``float`` (``double``), join keys are strings
(``keyword``) and ``latency_total_ms`` a ``float`` — matching the template exactly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

from personal_agent.captains_log.es_indexer import schedule_es_index

if TYPE_CHECKING:
    from personal_agent.observability.route_trace.types import RouteTraceRow

log = structlog.get_logger(__name__)

TOPOLOGY_INDEX_PREFIX = "agent-topology"


def build_topology_doc(row: RouteTraceRow, *, topology: str) -> dict[str, Any]:
    """Build the explicit-typed ES document for one route-trace row.

    Pure (no I/O). Fields that are ``None`` (a turn-level row's ``task_id`` is not set; a
    segment row has no ``latency_total_ms`` / ``task_type`` / ``complexity``) are **omitted**
    rather than emitted as ``null``, so the dedicated index never carries an off-type null.

    Args:
        row: The assembled route-trace row (turn-level or per-sub-agent segment).
        topology: The resolved execution-topology label for the turn (``primary`` /
            ``hybrid_fanout`` / ``decompose`` / ``delegate``).

    Returns:
        A JSON-serialisable document matching the ``agent-topology-*`` template: join keys as
        strings (``keyword``), ``authoritative_cost_usd`` as ``float`` (``double``), tokens as
        ``int`` (``long``), ``latency_total_ms`` as ``float``, ``@timestamp`` as an ISO string.
    """
    ts = row.created_at or datetime.now(timezone.utc)
    doc: dict[str, Any] = {
        "@timestamp": ts.isoformat(),
        "trace_id": str(row.trace_id),
        "session_id": str(row.session_id) if row.session_id is not None else None,
        "topology": topology,
        # The (trace_id, task_id) discriminator: a segment row carries a task_id, the
        # turn-level row does not.
        "role": "sub_agent" if row.task_id is not None else "primary",
        "gateway_label": row.gateway_label,
        "result_type": row.orchestration_event,
        "authoritative_cost_usd": float(row.cost_authoritative_usd),
        "input_tokens": int(row.input_tokens),
        "output_tokens": int(row.output_tokens),
    }
    if row.task_id is not None:
        doc["task_id"] = str(row.task_id)
    if row.task_type is not None:
        doc["task_type"] = row.task_type
    if row.complexity is not None:
        doc["complexity"] = row.complexity
    if row.latency_total_ms is not None:
        doc["latency_total_ms"] = float(row.latency_total_ms)
    return doc


def project_route_trace_to_es(row: RouteTraceRow, *, topology: str) -> None:
    """Project one route-trace row to ``agent-topology-*`` (non-blocking, best-effort).

    Idempotent on ``doc_id = f"{trace_id}:{task_id or 'turn'}"`` (mirrors the Postgres
    ``(trace_id, task_id)`` key), so a re-run upserts the same document. No-op when ES / the
    event loop is unavailable. The **entire** body is guarded: ``schedule_es_index`` only
    protects the scheduled write, but the synchronous document build must not raise into the
    seam either.

    Args:
        row: The assembled route-trace row to project.
        topology: The resolved execution-topology label for the turn.
    """
    try:
        ts = row.created_at or datetime.now(timezone.utc)
        index_name = f"{TOPOLOGY_INDEX_PREFIX}-{ts.strftime('%Y-%m-%d')}"
        suffix = str(row.task_id) if row.task_id is not None else "turn"
        doc_id = f"{row.trace_id}:{suffix}"
        schedule_es_index(index_name, build_topology_doc(row, topology=topology), doc_id=doc_id)
    except Exception as e:
        log.warning(
            "topology_es_projection_failed",
            trace_id=str(getattr(row, "trace_id", "")),
            task_id=str(getattr(row, "task_id", "")),
            error=str(e),
        )
