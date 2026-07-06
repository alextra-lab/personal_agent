"""Generation-time read-before-emit dedup (ADR-0105 D9/D10, FRE-721/T7).

D10's separation probe (FRE-720, ``scripts/eval/fre720_insights_separation/probe_result.json``)
found no clean similarity floor on the insights corpus, so the decided branch is
**fallback (category+scope grouping over sysgraph edges), never semantic clustering**
(AC-8). This module performs no similarity-scoring or re-ranking of any kind — by
construction, not merely by configuration — which is what satisfies AC-10 (no
reranker, no laptop/Mac-GPU dependency on this path).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import asyncpg  # type: ignore[import-untyped]

from personal_agent.sysgraph.repository import ProposalRecord, SysgraphRepository
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class ReadBeforeEmitDecision(str, Enum):
    """Outcome of a generation-time read-before-emit check (ADR-0105 D9)."""

    DECIDED_SKIP = "decided_skip"
    REINFORCED = "reinforced"
    GENERATE_NEW = "generate_new"
    DEGRADED_GENERATE_NEW = "degraded_generate_new"


@dataclass(frozen=True)
class ReadBeforeEmitResult:
    """Decision plus the affected proposal id (when any)."""

    decision: ReadBeforeEmitDecision
    proposal_id: object | None = None


async def check_before_emit(
    repo: SysgraphRepository | None,
    *,
    source: str,
    category: str,
    scope: str | None,
    proposal: ProposalRecord,
    trace_id: str | None = None,
) -> ReadBeforeEmitResult:
    """Read sysgraph before a producer would otherwise record a new proposal.

    Fails **open**: any sysgraph unavailability (no repo wired, no connection,
    or a query/connectivity error) degrades to ``DEGRADED_GENERATE_NEW`` and
    never blocks generation (ADR-0105 D9's explicit fail-open requirement).
    A programming error unrelated to sysgraph availability (e.g. a malformed
    ``ProposalRecord``) is not caught here and propagates — silently folding
    a caller bug into "sysgraph unreachable" would hide real defects.

    Args:
        repo: A connected repository, or ``None`` when this call site has no
            sysgraph wiring (unchanged behavior: always ``GENERATE_NEW``).
        source: Proposal source discriminator (ADR-0105 D1).
        category: Proposal category.
        scope: Proposal scope — D9's cheap fallback facet.
        proposal: Fields for a new proposal row (used only on the
            generate-new branch).
        trace_id: Originating request trace_id for log correlation (ADR-0074 §I3).

    Returns:
        The branch taken, per :class:`ReadBeforeEmitDecision`.
    """
    if repo is None or repo.pool is None:
        return ReadBeforeEmitResult(decision=ReadBeforeEmitDecision.GENERATE_NEW)

    try:
        result = await repo.read_before_emit(source, category, scope, proposal)
    except (OSError, asyncpg.PostgresError) as exc:
        log.warning(
            "sysgraph_read_before_emit_degraded",
            source=source,
            category=category,
            scope=scope,
            error=str(exc),
            trace_id=trace_id,
        )
        return ReadBeforeEmitResult(decision=ReadBeforeEmitDecision.DEGRADED_GENERATE_NEW)

    decision = ReadBeforeEmitDecision(result.decision)
    log.info(
        "sysgraph_read_before_emit_decided",
        decision=decision.value,
        source=source,
        category=category,
        scope=scope,
        proposal_id=str(result.proposal_id) if result.proposal_id else None,
        trace_id=trace_id,
    )
    return ReadBeforeEmitResult(decision=decision, proposal_id=result.proposal_id)
