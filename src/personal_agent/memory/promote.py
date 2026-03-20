"""Promotion pipeline — episodic to semantic memory.

See: docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md Section 5.3
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from personal_agent.memory.fact import PromotionCandidate, PromotionResult
from personal_agent.memory.service import MemoryService

logger = structlog.get_logger(__name__)


async def run_promotion_pipeline(
    service: MemoryService,
    candidates: Sequence[PromotionCandidate],
    trace_id: str,
) -> PromotionResult:
    """Run the promotion pipeline on a set of candidates.

    Args:
        service: MemoryService for Neo4j operations.
        candidates: Pre-filtered candidates to promote.
        trace_id: Request trace identifier.

    Returns:
        PromotionResult with counts and errors.
    """
    promoted = 0
    skipped = 0
    facts_created: list[str] = []
    errors: list[str] = []

    for candidate in candidates:
        confidence = candidate.stability_score()

        try:
            success = await service.promote_entity(
                entity_name=candidate.entity_name,
                confidence=confidence,
                source_turn_ids=candidate.source_turn_ids,
                trace_id=trace_id,
            )
            if success:
                promoted += 1
                facts_created.append(f"fact-{candidate.entity_name}")
            else:
                skipped += 1
                errors.append(f"{candidate.entity_name}: not found in Neo4j")
        except Exception as exc:
            logger.warning(
                "promotion_entity_failed",
                entity_name=candidate.entity_name,
                trace_id=trace_id,
                exc_info=True,
            )
            skipped += 1
            errors.append(f"{candidate.entity_name}: {exc}")

    logger.info(
        "promotion_pipeline_complete",
        promoted=promoted,
        skipped=skipped,
        errors=len(errors),
        trace_id=trace_id,
    )

    return PromotionResult(
        promoted_count=promoted,
        skipped_count=skipped,
        facts_created=facts_created,
        errors=errors,
    )
