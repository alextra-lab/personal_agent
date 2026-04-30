"""Weekly knowledge-graph freshness review (FRE-166 / ADR-0042 Step 6).

Aggregates staleness tiers, persists week-over-week snapshots, emits telemetry,
and writes Captain's Log proposals when dormant counts exceed thresholds.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from personal_agent.captains_log.dedup import compute_proposal_fingerprint
from personal_agent.captains_log.manager import CaptainLogManager
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    ChangeCategory,
    ChangeScope,
    Metric,
    ProposedChange,
    TelemetryRef,
)
from personal_agent.config.settings import AppConfig, get_settings
from personal_agent.memory.freshness_aggregate import (
    GraphStalenessSummary,
    StalenessTierCounts,
    freshness_tier_snapshot_path,
    tier_counts_delta,
)
from personal_agent.memory.service import MemoryService
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_DEFAULT_CRON_MINUTE = 0
_DEFAULT_CRON_HOUR = 3
_DEFAULT_PYTHON_WEEKDAY = 6  # Sunday (matches ``0 3 * * 0``)


def parse_freshness_review_schedule(cron: str) -> tuple[int, int, int]:
    """Parse a 5-field crontab line into minute, hour, and Python weekday.

    Crontab day-of-week: ``0`` or ``7`` = Sunday, ``1`` = Monday, … ``6`` = Saturday
    (Vixie-style). Maps to :meth:`datetime.weekday` (Monday ``0`` … Sunday ``6``).

    Args:
        cron: Five whitespace-separated fields (minute hour dom month dow).

    Returns:
        ``(minute, hour, python_weekday)``. On parse failure, returns
        ``(0, 3, 6)`` — Sunday 03:00 UTC.
    """
    parts = cron.strip().split()
    if len(parts) != 5:
        return (_DEFAULT_CRON_MINUTE, _DEFAULT_CRON_HOUR, _DEFAULT_PYTHON_WEEKDAY)
    minute_s, hour_s, _dom, _month, dow_s = parts
    try:
        minute = int(minute_s)
        hour = int(hour_s)
    except ValueError:
        return (_DEFAULT_CRON_MINUTE, _DEFAULT_CRON_HOUR, _DEFAULT_PYTHON_WEEKDAY)
    if dow_s == "*":
        py_weekday = _DEFAULT_PYTHON_WEEKDAY
    else:
        first_dow = dow_s.split(",")[0].strip()
        try:
            cron_dow = int(first_dow)
        except ValueError:
            py_weekday = _DEFAULT_PYTHON_WEEKDAY
        else:
            if cron_dow in (0, 7):
                py_weekday = 6
            elif 1 <= cron_dow <= 6:
                py_weekday = cron_dow - 1
            else:
                py_weekday = _DEFAULT_PYTHON_WEEKDAY
    return minute, hour, py_weekday


def _load_previous_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("freshness_snapshot_read_failed", path=str(path))
        return None


def _write_snapshot(path: Path, iso_week: str, summary: GraphStalenessSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "iso_week": iso_week,
        "entities": summary.entities.to_dict(),
        "relationships": summary.relationships.to_dict(),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _dormant_entity_what_text() -> str:
    return "Review dormant knowledge graph entities for archival or re-validation per ADR-0042"


def _dormant_relationship_what_text() -> str:
    return "Review dormant knowledge graph relationships for archival or re-validation per ADR-0042"


def _build_entity_dormant_proposal(
    summary: GraphStalenessSummary,
    trace_id: str,
    cfg: AppConfig,
) -> CaptainLogEntry | None:
    if summary.entities.dormant < cfg.freshness_dormant_entity_proposal_threshold:
        return None
    what = _dormant_entity_what_text()
    fp = compute_proposal_fingerprint(
        ChangeCategory.KNOWLEDGE_QUALITY,
        ChangeScope.SECOND_BRAIN,
        what,
    )
    lines = [
        f"{summary.entities.dormant} entity/entities in the dormant tier "
        f"(threshold {cfg.freshness_dormant_entity_proposal_threshold}).",
        "Sample (oldest staleness first, up to 5):",
    ]
    for name, last_acc, cnt, first_seen in summary.dormant_entity_samples:
        la = last_acc.isoformat() if last_acc else "never"
        fs = first_seen.isoformat() if first_seen else "unknown"
        lines.append(f"  — {name}: last_accessed_at={la}, access_count={cnt}, first_seen={fs}")
    summary_text = "\n".join(lines)
    metrics_evidence: dict[str, float | int | str] = {
        "dormant_entities": summary.entities.dormant,
        "warm_entities": summary.entities.warm,
        "cooling_entities": summary.entities.cooling,
        "cold_entities": summary.entities.cold,
        "never_accessed_old_entities": summary.never_accessed_old_entity_count,
    }
    supporting = [f"{k}: {v}" for k, v in metrics_evidence.items()]
    metrics_structured = [
        Metric(name=str(k), value=_metric_val(v), unit=None) for k, v in metrics_evidence.items()
    ]
    return CaptainLogEntry(
        entry_id="",
        type=CaptainLogEntryType.CONFIG_PROPOSAL,
        title=(
            f"Dormant entities: {summary.entities.dormant} — review for archival or re-validation"
        ),
        rationale=(
            "Weekly freshness review (ADR-0042) identified dormant entities. "
            "These may be stale or low-value; human review is required before archival."
        ),
        proposed_change=ProposedChange(
            what=what,
            why=summary_text,
            how=(
                "Inspect listed entities in Neo4j, validate relevance, then archive or "
                "re-validate as appropriate. Use Linear feedback (ADR-0040) for tracking."
            ),
            category=ChangeCategory.KNOWLEDGE_QUALITY,
            scope=ChangeScope.SECOND_BRAIN,
            fingerprint=fp,
        ),
        supporting_metrics=supporting,
        metrics_structured=metrics_structured,
        impact_assessment="Reduces graph noise and surfaces outdated knowledge safely.",
        telemetry_refs=[
            TelemetryRef(trace_id=trace_id, metric_name="freshness_review", value=None)
        ],
    )


def _metric_val(v: float | int | str) -> float | int | str:
    if isinstance(v, (int, float, str)):
        return v
    return str(v)


def _build_relationship_dormant_proposal(
    summary: GraphStalenessSummary,
    trace_id: str,
    cfg: AppConfig,
) -> CaptainLogEntry | None:
    if summary.relationships.dormant < cfg.freshness_dormant_relationship_proposal_threshold:
        return None
    what = _dormant_relationship_what_text()
    fp = compute_proposal_fingerprint(
        ChangeCategory.KNOWLEDGE_QUALITY,
        ChangeScope.SECOND_BRAIN,
        what,
    )
    by_type = summary.dormant_relationships_by_type
    top_types = sorted(by_type.items(), key=lambda x: -x[1])[:8]
    type_lines = "\n".join(f"  — {t}: {n} dormant" for t, n in top_types) or "  (none typed)"
    summary_text = (
        f"{summary.relationships.dormant} relationship(s) in the dormant tier "
        f"(threshold {cfg.freshness_dormant_relationship_proposal_threshold}).\n"
        f"Dormant counts by relationship type:\n{type_lines}"
    )
    metrics_evidence: dict[str, float | int | str] = {
        "dormant_relationships": summary.relationships.dormant,
        "warm_relationships": summary.relationships.warm,
        "cooling_relationships": summary.relationships.cooling,
        "cold_relationships": summary.relationships.cold,
    }
    supporting = [f"{k}: {v}" for k, v in metrics_evidence.items()]
    metrics_structured = [
        Metric(name=str(k), value=_metric_val(v), unit=None) for k, v in metrics_evidence.items()
    ]
    return CaptainLogEntry(
        entry_id="",
        type=CaptainLogEntryType.CONFIG_PROPOSAL,
        title=(f"Dormant relationships: {summary.relationships.dormant} — review for accuracy"),
        rationale=(
            "Weekly freshness review (ADR-0042) flagged relationships not accessed within "
            "the cold threshold; some may no longer reflect reality."
        ),
        proposed_change=ProposedChange(
            what=what,
            why=summary_text,
            how=(
                "Review relationship types with high dormant counts; validate or remove "
                "edges that are obsolete after human confirmation."
            ),
            category=ChangeCategory.KNOWLEDGE_QUALITY,
            scope=ChangeScope.SECOND_BRAIN,
            fingerprint=fp,
        ),
        supporting_metrics=supporting,
        metrics_structured=metrics_structured,
        impact_assessment="Improves temporal truthfulness of the knowledge graph.",
        telemetry_refs=[
            TelemetryRef(trace_id=trace_id, metric_name="freshness_review", value=None)
        ],
    )


async def run_freshness_review(memory_service: MemoryService | None, trace_id: str) -> None:
    """Execute one freshness review pass: aggregate, log, snapshot, optional CL proposals.

    Args:
        memory_service: Connected memory service, or ``None`` to skip.
        trace_id: Correlation id for structured logs (e.g. ``freshness-review-2026-W14``).
    """
    cfg = get_settings()
    if not cfg.freshness_enabled:
        log.debug("freshness_review_skipped_disabled", trace_id=trace_id)
        return
    if memory_service is None or not memory_service.connected or memory_service.driver is None:
        log.warning("freshness_review_skipped_no_memory", trace_id=trace_id)
        return

    summary = await memory_service.aggregate_graph_staleness()
    if summary is None:
        log.warning("freshness_review_skipped_no_summary", trace_id=trace_id)
        return

    now = datetime.now(timezone.utc)
    iso = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    snap_path = freshness_tier_snapshot_path(cfg)
    prev = _load_previous_snapshot(snap_path)

    prev_e = (
        StalenessTierCounts.from_dict(prev["entities"])
        if prev and isinstance(prev.get("entities"), dict)
        else None
    )
    prev_r = (
        StalenessTierCounts.from_dict(prev["relationships"])
        if prev and isinstance(prev.get("relationships"), dict)
        else None
    )
    delta_e = tier_counts_delta(prev_e, summary.entities)
    delta_r = tier_counts_delta(prev_r, summary.relationships)

    log.info(
        "freshness_review_completed",
        trace_id=trace_id,
        iso_week=iso,
        entities_warm=summary.entities.warm,
        entities_cooling=summary.entities.cooling,
        entities_cold=summary.entities.cold,
        entities_dormant=summary.entities.dormant,
        relationships_warm=summary.relationships.warm,
        relationships_cooling=summary.relationships.cooling,
        relationships_cold=summary.relationships.cold,
        relationships_dormant=summary.relationships.dormant,
        never_accessed_old_entities=summary.never_accessed_old_entity_count,
        top_accessed_entities=summary.top_accessed_entities,
        dormant_relationships_by_type=summary.dormant_relationships_by_type,
    )
    log.info(
        "freshness_review_tier_migration",
        trace_id=trace_id,
        iso_week=iso,
        entity_tier_delta=delta_e,
        relationship_tier_delta=delta_r,
        previous_iso_week=prev.get("iso_week") if prev else None,
    )

    _write_snapshot(snap_path, iso, summary)

    manager = CaptainLogManager()
    for builder in (_build_entity_dormant_proposal, _build_relationship_dormant_proposal):
        entry = builder(summary, trace_id, cfg)
        if entry is not None:
            try:
                path = manager.save_entry(entry)
                log.info(
                    "freshness_review_captains_log_proposal_saved",
                    trace_id=trace_id,
                    title=entry.title,
                    path=str(path) if path else None,
                )
            except Exception as exc:
                log.warning(
                    "freshness_review_captains_log_failed",
                    trace_id=trace_id,
                    error=str(exc),
                    exc_info=True,
                )

    # ADR-0060 §D8 Stream 6: new bus path (additive — direct CL writes above are retained)
    if cfg.graph_quality_stream_enabled:
        await _emit_staleness_reviewed_event(summary, iso, trace_id)


async def _emit_staleness_reviewed_event(
    summary: GraphStalenessSummary,
    iso_week: str,
    trace_id: str,
) -> None:
    """Dual-write staleness review summary to JSONL and publish bus event (ADR-0060 §D8).

    Follows ADR-0054 D4 ordering: durable append first, bus publish second.
    Bus failures are logged and swallowed (ADR-0054 D6).

    Args:
        summary: Aggregated staleness tier counts from Neo4j.
        iso_week: ISO week string, e.g. ``"2026-W18"``.
        trace_id: Correlation ID for structured logs.
    """
    import dataclasses
    import json
    from pathlib import Path

    from personal_agent.events.bus import get_event_bus
    from personal_agent.events.models import (
        STREAM_MEMORY_STALENESS_REVIEWED,
        MemoryStalenessReviewedEvent,
    )
    from personal_agent.insights.fingerprints import cost_fingerprint
    from personal_agent.second_brain.quality_monitor import (
        GraphStalenessReviewSummary,
        _dominant_tier,
    )

    tier = _dominant_tier(
        entities_dormant=summary.entities.dormant,
        entities_cold=summary.entities.cold,
        entities_cooling=summary.entities.cooling,
    )
    fp = cost_fingerprint(f"staleness_review_{tier}", iso_week)

    gsr = GraphStalenessReviewSummary(
        fingerprint=fp,
        trace_id=trace_id,
        iso_week=iso_week,
        entities_warm=summary.entities.warm,
        entities_cooling=summary.entities.cooling,
        entities_cold=summary.entities.cold,
        entities_dormant=summary.entities.dormant,
        relationships_dormant=summary.relationships.dormant,
        never_accessed_old_entity_count=summary.never_accessed_old_entity_count,
        dominant_tier=tier,
    )

    # Durable write first (ADR-0054 D4)
    output_dir = Path("telemetry/freshness_review")
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / f"FR-{iso_week}.jsonl"
    try:
        line = json.dumps(dataclasses.asdict(gsr)) + "\n"
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        log.warning(
            "staleness_review_jsonl_failed",
            iso_week=iso_week,
            fingerprint=fp,
            error=str(exc),
        )
        return  # Skip bus publish if durable write failed

    # Bus publish second (ADR-0054 D4)
    try:
        event = MemoryStalenessReviewedEvent(
            fingerprint=fp,
            iso_week=iso_week,
            entities_warm=gsr.entities_warm,
            entities_cooling=gsr.entities_cooling,
            entities_cold=gsr.entities_cold,
            entities_dormant=gsr.entities_dormant,
            relationships_dormant=gsr.relationships_dormant,
            never_accessed_old_entity_count=gsr.never_accessed_old_entity_count,
            dominant_tier=tier,
            source_component="brainstem.jobs.freshness_review",
        )
        await get_event_bus().publish(STREAM_MEMORY_STALENESS_REVIEWED, event)
        log.debug(
            "staleness_review_event_published",
            iso_week=iso_week,
            fingerprint=fp,
            dominant_tier=tier,
        )
    except Exception as exc:
        log.warning(
            "staleness_review_bus_failed",
            iso_week=iso_week,
            fingerprint=fp,
            error=str(exc),
        )
