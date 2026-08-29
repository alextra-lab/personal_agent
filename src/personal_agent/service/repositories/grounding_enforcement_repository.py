"""Repository for D5's enforcement state (ADR-0138 D5 / FRE-1285).

The durable half of selection: :mod:`personal_agent.grounding.enforcement_selection`
decides what a rate *means*, and this decides what survives the turn that decided it.

**The write is guarded, not last-write-wins.** Turns run concurrently and each selects
independently, so two turns can hold the same stale standing and try to write different
transitions. The guard keeps the newer one: a demotion silently reset by a slower turn's
older reading would restart a cooldown, and nothing downstream could tell that it had.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.grounding.enforcement_selection import EnforcementLevel, EnforcementState
from personal_agent.service.models import GroundingEnforcementStateModel


class GroundingEnforcementRepository:
    """Read and upsert one model's standing enforcement state.

    Usage:
        async with AsyncSessionLocal() as db:
            repo = GroundingEnforcementRepository(db)
            state = await repo.get("gemma-3-27b")
            await repo.upsert("gemma-3-27b", selection.standing, updated_at=now)
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the repository with a database session.

        Args:
            db: Async SQLAlchemy session.
        """
        self.db = db

    async def get(self, model_key: str) -> EnforcementState | None:
        """Return one model's standing state.

        Args:
            model_key: Catalog deployment key.

        Returns:
            The stored state, or ``None`` when the model has never been selected for.
            ``None`` is deliberately not defaulted here: the caller decides what a model
            with no history means, and under D5 that is heavy with no cooldown owed —
            a policy that belongs with the policy, not with the storage.
        """
        statement = select(
            GroundingEnforcementStateModel.level,
            GroundingEnforcementStateModel.demoted_at,
        ).where(GroundingEnforcementStateModel.model_key == model_key)
        row = (await self.db.execute(statement)).one_or_none()
        if row is None:
            return None
        return EnforcementState(level=EnforcementLevel(row.level), demoted_at=row.demoted_at)

    async def upsert(
        self, model_key: str, state: EnforcementState, *, updated_at: datetime
    ) -> bool:
        """Persist a transition, unless a newer one already landed.

        Args:
            model_key: Catalog deployment key.
            state: The post-transition state to store.
            updated_at: The instant this transition was decided, timezone-aware. Passed
                rather than defaulted because it is the guard's comparison value: it must
                be the moment the *selection* was made, not the moment the write reached
                the database, or two turns would be ordered by their write latency.

        Returns:
            Whether the row now reflects this state. ``False`` means a newer transition
            was already stored and this one was correctly discarded.
        """
        insert = pg_insert(GroundingEnforcementStateModel).values(
            model_key=model_key,
            level=state.level.value,
            demoted_at=state.demoted_at,
            updated_at=updated_at,
        )
        statement = insert.on_conflict_do_update(
            index_elements=["model_key"],
            set_={
                "level": insert.excluded.level,
                "demoted_at": insert.excluded.demoted_at,
                "updated_at": insert.excluded.updated_at,
            },
            # The optimistic guard. Without it a turn that decided earlier but wrote later
            # would overwrite a newer transition — and on a demotion that means resetting
            # a cooldown to an older stamp, which is the one corruption that buys a model
            # a promotion it did not serve out.
            where=GroundingEnforcementStateModel.updated_at < insert.excluded.updated_at,
        ).returning(GroundingEnforcementStateModel.model_key)
        result = await self.db.execute(statement)
        applied = result.scalar_one_or_none() is not None
        await self.db.commit()
        return applied


__all__ = ["GroundingEnforcementRepository"]
