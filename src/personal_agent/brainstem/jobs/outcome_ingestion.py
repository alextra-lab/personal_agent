"""Ticket-outcome ingestion + realized-value signal (ADR-0105 D7 / FRE-717).

Sweeps promoted tickets awaiting an outcome, classifies each from its current
Linear workflow state + labels, records the outcome in the sysgraph store, and
updates the realized-value signal the promotion pipeline reads back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from personal_agent.captains_log.linear_client import LinearClient
from personal_agent.config.settings import get_settings
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.telemetry.es_handler import ElasticsearchHandler

log = get_logger(__name__)

_OutcomeResult = Literal["shipped", "owner-rejected", "canceled-as-noise"]


def _classify_outcome(issue: dict[str, Any]) -> _OutcomeResult | None:
    """Classify a Linear issue's terminal outcome from its state name + labels.

    Pure function, no I/O. ``owner-rejected`` vs ``canceled-as-noise`` is
    distinguished by the ``Rejected`` label (the ADR-0040 label channel) — the
    same label ``handle_rejected`` applies at proposal-review time, which also
    moves the issue to ``Canceled``, so a plain state check alone cannot tell
    the two apart.

    Args:
        issue: A ``LinearClient.get_issue()``-shaped dict.

    Returns:
        The classified outcome, or ``None`` if the ticket is still open (not
        yet decided) — never synthesizes ``deferred``.
    """
    state_name = (issue.get("state") or {}).get("name")
    if state_name == "Done":
        return "shipped"
    if state_name in ("Canceled", "Duplicate"):
        labels = LinearClient.labels_from_issue(issue)
        return "owner-rejected" if "Rejected" in labels else "canceled-as-noise"
    return None


async def _stamp_ticket_outcome(
    es_handler: "ElasticsearchHandler | None", linear_issue_id: str, result: _OutcomeResult
) -> None:
    """Stamp the classified outcome onto the source ES doc (ADR-0105 D6, FRE-719), best-effort.

    Without this, the promoted proposal document (``agent-captains-reflections-*``)
    never carries a queryable shipped/canceled signal for the funnel dashboard.
    Requires ``linear_issue_id`` to already be present on the doc (see
    ``PromotionPipeline._stamp_reflection_linkage``).

    Args:
        es_handler: Elasticsearch handler, or ``None``/disconnected to skip.
        linear_issue_id: The ticket identifier to match on.
        result: The classified outcome to stamp.
    """
    handler = es_handler
    if handler is None:
        from personal_agent.captains_log.manager import CaptainLogManager

        handler = CaptainLogManager._default_es_handler
    if handler is None or not getattr(handler, "_connected", False):
        return
    try:
        await handler.es_logger.update_by_query(
            "agent-captains-reflections-*",
            {"term": {"linear_issue_id": linear_issue_id}},
            "ctx._source.ticket_outcome = params.ticket_outcome",
            {"ticket_outcome": result},
        )
    except Exception as exc:
        log.warning(
            "outcome_ingestion_es_stamp_failed",
            linear_issue_id=linear_issue_id,
            result=result,
            error=str(exc),
        )


async def run_outcome_ingestion(
    linear_client: LinearClient,
    trace_id: str,
    es_handler: "ElasticsearchHandler | None" = None,
) -> None:
    """Execute one outcome-ingestion pass (ADR-0105 D7 / AC-6).

    Args:
        linear_client: Configured Linear client for reading ticket state.
        trace_id: Correlation id for structured logs (ADR-0074 §I3).
        es_handler: Optional Elasticsearch handler override for stamping
            ``ticket_outcome`` onto the source document (ADR-0105 D6, FRE-719);
            falls back to ``CaptainLogManager._default_es_handler`` when omitted.
    """
    cfg = get_settings()
    if not cfg.outcome_ingestion_enabled:
        log.debug("outcome_ingestion_skipped_disabled", trace_id=trace_id)
        return

    from personal_agent.sysgraph import SysgraphRepository

    repo = SysgraphRepository(cfg.sysgraph_database_url)
    try:
        await repo.connect()
    except Exception as exc:
        log.warning("outcome_ingestion_sysgraph_connect_failed", error=str(exc), trace_id=trace_id)
        return

    try:
        pending = await repo.tickets_awaiting_outcome()
        ingested = 0
        for linear_issue_id in pending:
            try:
                issue = await linear_client.get_issue(linear_issue_id)
                result = _classify_outcome(issue)
                if result is None:
                    continue

                kind = await repo.ticket_source_kind(linear_issue_id)
                before = await repo.get_signal(*kind) if kind else None
                recorded = await repo.record_outcome(linear_issue_id, result)
                if recorded:
                    await _stamp_ticket_outcome(es_handler, linear_issue_id, result)
                if recorded and kind is not None:
                    after = await repo.compute_and_apply_signal(*kind)
                    log.info(
                        "sysgraph_outcome_ingested",
                        linear_issue_id=linear_issue_id,
                        result=result,
                        source=kind[0],
                        category=kind[1],
                        value_before=before.value if before else None,
                        value_after=after.value,
                        suppressed=after.suppressed,
                        trace_id=trace_id,
                    )
                    ingested += 1
            except Exception as exc:
                log.warning(
                    "outcome_ingestion_ticket_failed",
                    linear_issue_id=linear_issue_id,
                    error=str(exc),
                    trace_id=trace_id,
                )
        log.info(
            "outcome_ingestion_completed",
            scanned=len(pending),
            ingested=ingested,
            trace_id=trace_id,
        )
    finally:
        await repo.disconnect()
