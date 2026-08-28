"""Repository for the D5 compliance observation store (ADR-0138 D5 / FRE-1284).

The durable half of the metric: :mod:`personal_agent.grounding.compliance` decides what a
window of observations *means*, and this decides where the window comes from.

**Reads pin their own order.** An index does not define SQL result order, so ``recent``
sorts explicitly — a window silently assembled from the wrong end of the table would be a
reading nobody could tell was wrong, which for a metric that gates model promotion is the
worst available failure.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.grounding.compliance import ComplianceObservation
from personal_agent.service.models import GroundingComplianceObservationModel


class GroundingComplianceRepository:
    """Append-and-window access to ``grounding_compliance_observations``.

    Usage:
        async with AsyncSessionLocal() as db:
            repo = GroundingComplianceRepository(db)
            await repo.record(
                model_key="gemma-3-27b",
                compliant=True,
                trace_id=trace_id,
                observed_at=verified_at,
            )
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the repository with a database session.

        Args:
            db: Async SQLAlchemy session.
        """
        self.db = db

    async def record(
        self,
        *,
        model_key: str,
        compliant: bool,
        trace_id: str,
        observed_at: datetime,
    ) -> bool:
        """Append one unconfounded observation.

        Idempotent on ``trace_id``: a turn yields at most one observation, so a second
        write for the same trace is a replay rather than a second measurement, and
        letting it through would inflate whichever way the first row went.

        Args:
            model_key: Catalog deployment key of the model that answered.
            compliant: Whether every non-exempt span passed on first generation.
            trace_id: The turn's trace identifier — the idempotency key.
            observed_at: When the turn was **verified**, timezone-aware. Passed rather
                than defaulted because the write is backgrounded and the staleness rule
                reads this column.

        Returns:
            Whether a row was inserted. ``False`` means the trace was already recorded.
        """
        statement = (
            pg_insert(GroundingComplianceObservationModel)
            .values(
                model_key=model_key,
                compliant=compliant,
                trace_id=trace_id,
                observed_at=observed_at,
            )
            .on_conflict_do_nothing(index_elements=["trace_id"])
            .returning(GroundingComplianceObservationModel.id)
        )
        result = await self.db.execute(statement)
        inserted = result.scalar_one_or_none() is not None
        await self.db.commit()
        return inserted

    async def recent(self, model_key: str, *, limit: int) -> list[ComplianceObservation]:
        """Return one model's most recent observations, newest first.

        Args:
            model_key: The model to read.
            limit: How many observations to take — the configured window size.

        Returns:
            The observations, newest first. Ordered by ``observed_at`` then ``id`` so two
            observations sharing an instant still have a total order, rather than a window
            boundary that moves between reads.
        """
        statement = (
            select(
                GroundingComplianceObservationModel.model_key,
                GroundingComplianceObservationModel.observed_at,
                GroundingComplianceObservationModel.compliant,
            )
            .where(GroundingComplianceObservationModel.model_key == model_key)
            .order_by(
                GroundingComplianceObservationModel.observed_at.desc(),
                GroundingComplianceObservationModel.id.desc(),
            )
            .limit(limit)
        )
        rows = await self.db.execute(statement)
        return [
            ComplianceObservation(
                model_key=row.model_key, observed_at=row.observed_at, compliant=row.compliant
            )
            for row in rows
        ]


__all__ = ["GroundingComplianceRepository"]
