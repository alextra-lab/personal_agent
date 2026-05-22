"""Recovery Wave 1.1 — survey existing telemetry before any canary work.

Reuses the production ``ConsolidationQualityMonitor`` and ``TelemetryQueries``
rather than reinventing them. Surfaces:

  A. Pipeline-flow counts from Elasticsearch (capture, extraction, scheduler,
     cost-gate denials, service-startup events).
  B. Quality reports from ``ConsolidationQualityMonitor`` (entity ratios,
     duplicate rate, extraction failure rate, graph health).
  C. Model identity audit from ``config/models.yaml`` (which model fills
     each role; flag if multiple roles share one model — single point of
     failure).
  D. Embedding health probe (live ``/v1/embeddings`` call + ``zero_embedding``
     ES count + Neo4j sample of stored Entity embeddings).

Output: a markdown report under
``telemetry/evaluation/EVAL-agent-self-diagnosis/<survey-id>/report.md``.

Hard rule: fail loudly on missing ES indices, unreachable Neo4j, or
unreachable embedding endpoint. Silent zeros are worse than crashes for a
diagnostic survey.

Usage:

    uv run python scripts/eval/recovery_survey.py --days 7
    uv run python scripts/eval/recovery_survey.py --days 7 --out custom/path
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from personal_agent.config import get_settings, load_model_config
from personal_agent.memory.embeddings import (
    _get_embedding_config,  # noqa: PLC2701 — same package; intentional reuse
    cosine_similarity,
    generate_embedding,
)
from personal_agent.memory.service import MemoryService
from personal_agent.second_brain.quality_monitor import ConsolidationQualityMonitor
from personal_agent.telemetry import TelemetryQueries

log = structlog.get_logger(__name__)


# Event types we know are emitted (verified by grep against src/).
PIPELINE_EVENT_TYPES: tuple[str, ...] = (
    "request.captured",
    "entity_extraction_started",
    "entity_extraction_completed",
    "entity_extraction_failed",
    "entity_extraction_timeout",
    "entity_extraction_json_parse_failed",
)

STARTUP_EVENT_TYPES: tuple[str, ...] = (
    "elasticsearch_logging_enabled",
    "memory_service_initialized",
    "event_bus_ready",
)

# Roles that share a single model are a single-point-of-failure smell.
MODEL_ROLES: tuple[str, ...] = (
    "entity_extraction_role",
    "captains_log_role",
    "insights_role",
)

EMBEDDING_PROBE_STRINGS: tuple[str, ...] = (
    "ultramarine pigment color",
    "diagnostic recovery plan for the agent",
    "the unrelated banana tree in the courtyard",
)


# ---------------------------------------------------------------------------
# Section A — pipeline-flow counts
# ---------------------------------------------------------------------------


async def survey_pipeline_counts(queries: TelemetryQueries, days: int) -> dict[str, Any]:
    """Count key pipeline events over the given window."""
    counts: dict[str, int] = {}
    for event_type in PIPELINE_EVENT_TYPES + STARTUP_EVENT_TYPES:
        counts[event_type] = await queries.get_event_count(event_type, days)

    captured = counts.get("request.captured", 0)
    started = counts.get("entity_extraction_started", 0)
    completed = counts.get("entity_extraction_completed", 0)
    failed = counts.get("entity_extraction_failed", 0)

    capture_to_extraction_gap = max(captured - started, 0)
    extraction_success_ratio = (completed / started) if started > 0 else None
    extraction_failure_ratio = (failed / started) if started > 0 else None

    log.info(
        "survey_pipeline_counts_done",
        captured=captured,
        started=started,
        completed=completed,
        failed=failed,
        gap=capture_to_extraction_gap,
    )
    return {
        "counts": counts,
        "capture_to_extraction_gap": capture_to_extraction_gap,
        "extraction_success_ratio": extraction_success_ratio,
        "extraction_failure_ratio": extraction_failure_ratio,
    }


# ---------------------------------------------------------------------------
# Section B — quality reports via existing monitor
# ---------------------------------------------------------------------------


async def survey_quality_reports(monitor: ConsolidationQualityMonitor, days: int) -> dict[str, Any]:
    """Call the existing quality monitor and return report dataclasses as dicts."""
    extraction_quality = await monitor.check_entity_extraction_quality(days=days)
    graph_health = await monitor.check_graph_health()
    log.info(
        "survey_quality_reports_done",
        entities=extraction_quality.entities,
        ratio=extraction_quality.entities_per_conversation_ratio,
        duplicate_rate=extraction_quality.duplicate_rate,
        failure_rate=extraction_quality.extraction_failure_rate,
    )
    return {
        "extraction": extraction_quality,
        "graph": graph_health,
    }


# ---------------------------------------------------------------------------
# Section C — model identity audit
# ---------------------------------------------------------------------------


def survey_model_identity() -> dict[str, Any]:
    """Read models.yaml and surface which model fills each load-bearing role."""
    config = load_model_config()
    role_assignments = {role: getattr(config, role, None) for role in MODEL_ROLES}
    distinct_models = {role: role_assignments[role] for role in MODEL_ROLES}
    shared_model_warning = (
        len({m for m in distinct_models.values() if m is not None}) == 1
        and distinct_models[MODEL_ROLES[0]] is not None
    )
    embedding_def = config.models.get("embedding")
    embedding_summary = (
        {
            "id": embedding_def.id,
            "endpoint": embedding_def.endpoint,
            "context_length": embedding_def.context_length,
        }
        if embedding_def is not None
        else None
    )
    log.info(
        "survey_model_identity_done",
        roles=role_assignments,
        shared_model_warning=shared_model_warning,
    )
    return {
        "role_assignments": role_assignments,
        "shared_model_warning": shared_model_warning,
        "embedding": embedding_summary,
    }


# ---------------------------------------------------------------------------
# Section D — embedding health probe
# ---------------------------------------------------------------------------


async def survey_embedding_health(
    queries: TelemetryQueries, days: int, memory_service: MemoryService
) -> dict[str, Any]:
    """Live probe + ES + Neo4j sample of embedding health.

    Three checks:
      1. Live call to ``/v1/embeddings`` with three test strings; verify
         dimensionality and non-zero, with sensible pairwise variance.
      2. ES count of ``zero_embedding`` short-circuits in the last ``days``.
      3. Sample 50 Entity nodes' stored embeddings; report fraction with
         null/all-zero and the mean pairwise cosine similarity (collapse
         signal).
    """
    settings = get_settings()
    expected_dim = settings.embedding_dimensions
    model_id, endpoint = _get_embedding_config()

    # 1. Live probe
    vectors: list[list[float]] = []
    for text in EMBEDDING_PROBE_STRINGS:
        vec = await generate_embedding(text, mode="document")
        vectors.append(vec)
    live_dimensions = [len(v) for v in vectors]
    live_all_zero = all(all(x == 0.0 for x in v) for v in vectors)
    pairwise_similarities: list[float] = []
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            pairwise_similarities.append(cosine_similarity(vectors[i], vectors[j]))
    live_mean_pairwise = statistics.mean(pairwise_similarities) if pairwise_similarities else None
    live_collapsed = live_mean_pairwise is not None and live_mean_pairwise >= 0.95

    # 2. ES count — proactive_memory_suggest_empty events. Note: the
    # zero_embedding case is one of several `reason` values on this event
    # (see protocol_adapter.py:260). TelemetryQueries.get_event_count only
    # filters on event_type, so this count is a *superset* of true
    # zero-embedding incidents. Inspect logs by reason to disambiguate.
    proactive_empty_events = await queries.get_event_count("proactive_memory_suggest_empty", days)

    # 3. Neo4j sample
    sample_size = 0
    null_or_zero = 0
    sample_pairwise: list[float] = []
    if memory_service.connected and memory_service.driver is not None:
        async with memory_service.driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) WHERE e.embedding IS NOT NULL "
                "RETURN e.embedding AS embedding LIMIT 50"
            )
            sample_vectors: list[list[float]] = []
            async for record in result:
                emb = record.get("embedding")
                if emb is None:
                    null_or_zero += 1
                    continue
                emb_list = [float(x) for x in emb]
                if all(x == 0.0 for x in emb_list):
                    null_or_zero += 1
                    continue
                sample_vectors.append(emb_list)
            sample_size = len(sample_vectors) + null_or_zero
            for i in range(len(sample_vectors)):
                for j in range(i + 1, min(i + 10, len(sample_vectors))):
                    sample_pairwise.append(cosine_similarity(sample_vectors[i], sample_vectors[j]))

    sample_mean_pairwise = statistics.mean(sample_pairwise) if sample_pairwise else None
    sample_collapsed = sample_mean_pairwise is not None and sample_mean_pairwise >= 0.95

    log.info(
        "survey_embedding_health_done",
        live_dim=live_dimensions[0] if live_dimensions else None,
        live_collapsed=live_collapsed,
        sample_size=sample_size,
        sample_collapsed=sample_collapsed,
        null_or_zero=null_or_zero,
    )
    return {
        "model_id": model_id,
        "endpoint": endpoint,
        "expected_dimensions": expected_dim,
        "live": {
            "dimensions": live_dimensions,
            "all_zero": live_all_zero,
            "pairwise_similarities": pairwise_similarities,
            "mean_pairwise_similarity": live_mean_pairwise,
            "collapsed": live_collapsed,
        },
        "es": {
            "proactive_memory_suggest_empty_events": proactive_empty_events,
        },
        "neo4j": {
            "sample_size": sample_size,
            "null_or_zero": null_or_zero,
            "mean_pairwise_similarity": sample_mean_pairwise,
            "collapsed": sample_collapsed,
        },
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_report(
    *,
    survey_id: str,
    days: int,
    pipeline: dict[str, Any],
    quality: dict[str, Any],
    model_identity: dict[str, Any],
    embedding_health: dict[str, Any],
) -> str:
    """Render the survey results as a markdown report."""
    lines: list[str] = []
    lines.append(f"# Recovery Survey — {survey_id}")
    lines.append("")
    lines.append(f"**Generated**: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"**Window**: last {days} day(s)")
    lines.append("")
    lines.append(
        "Source plan: `docs/plans/2026-05-05-agent-self-diagnosis-recovery-execution-waves-0-2.md` "
        "(Wave 1.1)."
    )
    lines.append("")

    # ── Section A ──
    lines.append("## A. Pipeline-flow counts")
    lines.append("")
    lines.append("| event_type | count |")
    lines.append("|---|---:|")
    for event_type, count in pipeline["counts"].items():
        lines.append(f"| `{event_type}` | {count} |")
    lines.append("")
    lines.append(
        f"- **capture → extraction gap**: {pipeline['capture_to_extraction_gap']} "
        "(non-trivial → scheduler is dropping work)"
    )
    success = pipeline.get("extraction_success_ratio")
    failure = pipeline.get("extraction_failure_ratio")
    lines.append(
        f"- **extraction success ratio**: {success:.2%}"
        if success is not None
        else "- **extraction success ratio**: n/a (no starts)"
    )
    lines.append(
        f"- **extraction failure ratio**: {failure:.2%}"
        if failure is not None
        else "- **extraction failure ratio**: n/a"
    )
    lines.append("")

    # ── Section B ──
    lines.append("## B. Quality reports (existing ConsolidationQualityMonitor)")
    lines.append("")
    e = quality["extraction"]
    g = quality["graph"]
    lines.append("### Entity extraction quality")
    lines.append("")
    lines.append(f"- conversations: {e.conversations}")
    lines.append(f"- entities: {e.entities}")
    lines.append(f"- entities/conversation ratio: {e.entities_per_conversation_ratio:.3f}")
    lines.append(f"- duplicate entity count: {e.duplicate_entity_count}")
    lines.append(f"- duplicate rate: {e.duplicate_rate:.3%}")
    lines.append(f"- extraction started (window): {e.extraction_started}")
    lines.append(f"- extraction failed  (window): {e.extraction_failed}")
    lines.append(f"- extraction failure rate: {e.extraction_failure_rate:.3%}")
    lines.append(f"- name length distribution: {e.entity_name_length_distribution}")
    lines.append("")
    lines.append("### Graph health")
    lines.append("")
    lines.append(f"- total nodes: {g.total_nodes}")
    lines.append(f"- entity nodes: {g.entity_nodes}")
    lines.append(f"- conversation nodes: {g.conversation_nodes}")
    lines.append(f"- relationship count: {g.relationship_count}")
    lines.append(f"- relationship density: {g.relationship_density:.3f}")
    lines.append(f"- orphaned entities: {g.orphaned_entities}")
    lines.append(f"- orphaned entity rate: {g.orphaned_entity_rate:.3%}")
    lines.append(f"- clustered entity rate: {g.clustered_entity_rate:.3%}")
    lines.append(f"- max temporal gap (h): {g.max_temporal_gap_hours:.2f}")
    lines.append("")
    lines.append("### Threshold flags")
    lines.append("")
    flags: list[str] = []
    if e.entities_per_conversation_ratio < 1.0:
        flags.append("⚠ entities/conversation < 1.0 — under-extraction.")
    if e.duplicate_rate > 0.10:
        flags.append("⚠ duplicate rate > 10% — dedup or embedding issue.")
    if e.extraction_failure_rate > 0.20:
        flags.append("⚠ extraction failure rate > 20% — model crashing.")
    if not flags:
        flags.append("✓ all thresholds within target.")
    for line in flags:
        lines.append(f"- {line}")
    lines.append("")

    # ── Section C ──
    lines.append("## C. Model identity audit")
    lines.append("")
    lines.append("| role | model |")
    lines.append("|---|---|")
    for role, model in model_identity["role_assignments"].items():
        lines.append(f"| `{role}` | `{model}` |")
    lines.append("")
    if model_identity["shared_model_warning"]:
        lines.append(
            "⚠ **All roles share one model** — single point of failure. "
            "If that model degrades, three pipelines fail simultaneously. "
            "See FRE-319 follow-up for the broader audit."
        )
    else:
        lines.append("✓ Roles are decoupled across distinct models.")
    lines.append("")
    if model_identity["embedding"]:
        emb = model_identity["embedding"]
        lines.append("### Embedding model")
        lines.append("")
        lines.append(f"- id: `{emb['id']}`")
        lines.append(f"- endpoint: `{emb['endpoint']}`")
        lines.append(f"- context_length: {emb['context_length']}")
    else:
        lines.append("⚠ No `embedding` entry in models.yaml.")
    lines.append("")

    # ── Section D ──
    lines.append("## D. Embedding health probe")
    lines.append("")
    eh = embedding_health
    lines.append(f"- model: `{eh['model_id']}`")
    lines.append(f"- endpoint: `{eh['endpoint']}`")
    lines.append(f"- expected dimensions: {eh['expected_dimensions']}")
    lines.append("")
    lines.append("### Live probe (3 test strings)")
    lines.append("")
    lines.append(f"- returned dimensions: {eh['live']['dimensions']}")
    lines.append(f"- all-zero: {eh['live']['all_zero']}")
    lines.append(
        f"- pairwise similarities: {[round(x, 4) for x in eh['live']['pairwise_similarities']]}"
    )
    if eh["live"]["mean_pairwise_similarity"] is not None:
        lines.append(f"- mean pairwise similarity: {eh['live']['mean_pairwise_similarity']:.4f}")
    if eh["live"]["collapsed"]:
        lines.append("⚠ **Live embeddings collapsed** (mean pairwise ≥ 0.95).")
    elif eh["live"]["all_zero"]:
        lines.append("⚠ **Live embeddings all zero** — endpoint likely degraded.")
    else:
        lines.append("✓ Live embeddings non-degenerate.")
    lines.append("")
    lines.append("### Elasticsearch")
    lines.append("")
    empty_events = eh["es"]["proactive_memory_suggest_empty_events"]
    lines.append(
        f"- `proactive_memory_suggest_empty` events (last {days}d): {empty_events} "
        "(superset; filter by `reason=zero_embedding` in Kibana to isolate the "
        "embedding-degradation slice)"
    )
    if empty_events > 0:
        lines.append(
            "⚠ Non-zero empty-suggest events. Drill into logs by `reason` to see "
            "whether `zero_embedding` (model degraded) or `no_raw_rows` "
            "(no semantic neighbours) dominates."
        )
    lines.append("")
    lines.append("### Neo4j sample (≤50 Entity embeddings)")
    lines.append("")
    lines.append(f"- sampled: {eh['neo4j']['sample_size']}")
    lines.append(f"- null or all-zero: {eh['neo4j']['null_or_zero']}")
    if eh["neo4j"]["mean_pairwise_similarity"] is not None:
        lines.append(f"- mean pairwise similarity: {eh['neo4j']['mean_pairwise_similarity']:.4f}")
    if eh["neo4j"]["collapsed"]:
        lines.append(
            "⚠ **Stored embeddings collapsed** — historical writes are degenerate. "
            "Consider rebuilding the entity_embedding index."
        )
    elif eh["neo4j"]["sample_size"] == 0:
        lines.append("⚠ No Entity nodes with embeddings sampled.")
    else:
        lines.append("✓ Stored embeddings non-degenerate.")
    lines.append("")

    # ── Likely localized failures ──
    lines.append("## Likely localized failures")
    lines.append("")
    likely: list[str] = []
    if pipeline["capture_to_extraction_gap"] > 0:
        likely.append(f"Scheduler dropping captures: gap={pipeline['capture_to_extraction_gap']}.")
    if (
        pipeline.get("extraction_success_ratio") is not None
        and pipeline["extraction_success_ratio"] < 0.80
    ):
        likely.append(
            f"Extraction failing: success_ratio={pipeline['extraction_success_ratio']:.2%}."
        )
    if eh["live"]["all_zero"] or eh["live"]["collapsed"]:
        likely.append("Embedding model is degraded (live probe).")
    if eh["neo4j"]["collapsed"]:
        likely.append(
            "Historical Entity embeddings are collapsed — stored vectors are not "
            "meaningfully discriminative."
        )
    if model_identity["shared_model_warning"]:
        likely.append(
            "All extraction-side roles share one model — failure of that model "
            "cascades to entity_extraction, captains_log, and insights."
        )
    if not likely:
        likely.append("No high-signal failures localized; proceed to canaries.")
    for item in likely:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def run_survey(*, days: int, out_dir: Path) -> Path:
    """Run all four sections and write the markdown report. Return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.md"

    queries = TelemetryQueries()
    memory_service = MemoryService()  # fre-375-allow: read-only MemoryService, post-FRE-375 settings-driven
    monitor = ConsolidationQualityMonitor(memory_service=memory_service, telemetry_queries=queries)

    try:
        await memory_service.connect()
        if not memory_service.connected:
            raise RuntimeError(
                "Neo4j unreachable — survey requires live graph access. "
                "Confirm bolt://localhost:7687 (or AGENT_NEO4J_URI) is up."
            )

        log.info("survey_running_section_a")
        pipeline = await survey_pipeline_counts(queries, days)

        log.info("survey_running_section_b")
        quality = await survey_quality_reports(monitor, days)

        log.info("survey_running_section_c")
        model_identity = survey_model_identity()

        log.info("survey_running_section_d")
        embedding_health = await survey_embedding_health(queries, days, memory_service)

        report_md = render_report(
            survey_id=out_dir.name,
            days=days,
            pipeline=pipeline,
            quality=quality,
            model_identity=model_identity,
            embedding_health=embedding_health,
        )
        report_path.write_text(report_md)
        log.info("survey_report_written", path=str(report_path))
        return report_path
    finally:
        await queries.disconnect()
        await memory_service.disconnect()


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Window for ES counts and quality reports (default: 7).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Output directory. Defaults to "
            "telemetry/evaluation/EVAL-agent-self-diagnosis/survey-<YYYY-MM-DD>."
        ),
    )
    return parser.parse_args()


def main() -> int:
    """CLI entry point. Returns exit code."""
    args = parse_args()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = args.out or Path(f"telemetry/evaluation/EVAL-agent-self-diagnosis/survey-{today}")
    try:
        report_path = asyncio.run(run_survey(days=args.days, out_dir=out_dir))
    except Exception as exc:  # noqa: BLE001 — CLI top-level
        log.error("survey_failed", error=str(exc), error_type=type(exc).__name__)
        return 1
    log.info("survey_done", report=str(report_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
