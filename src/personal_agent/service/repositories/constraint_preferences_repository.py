"""Repository for user constraint governance preferences (ADR-0076 / FRE-389)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.service.models import UserConstraintPreferenceModel


class ConstraintPreferencesRepository:
    """CRUD for ``user_constraint_preferences``.

    A missing row for a (user, constraint) pair means ``always_pause``; the
    read helper returns ``None`` in that case so callers apply their default.

    Usage:
        async with get_db_session() as db:
            repo = ConstraintPreferencesRepository(db)
            action = await repo.get_preferred_action(user_id, "tool_iteration_limit")
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the repository with a database session.

        Args:
            db: Async SQLAlchemy session.
        """
        self.db = db

    async def get_preferred_action(self, user_id: UUID, constraint_name: str) -> str | None:
        """Return the stored ``preferred_action`` for a (user, constraint) pair.

        Args:
            user_id: Owning user.
            constraint_name: Constraint name (e.g. ``tool_iteration_limit``).

        Returns:
            The stored ``action_id`` / ``always_pause`` string, or ``None`` when
            no preference row exists.
        """
        stmt = select(UserConstraintPreferenceModel.preferred_action).where(
            UserConstraintPreferenceModel.user_id == user_id,
            UserConstraintPreferenceModel.constraint_name == constraint_name,
        )
        result = await self.db.execute(stmt)
        row = result.scalar_one_or_none()
        return str(row) if row is not None else None

    async def list_for_user(self, user_id: UUID) -> list[UserConstraintPreferenceModel]:
        """Return every stored preference row for a user, ordered by constraint name.

        Args:
            user_id: Owning user.

        Returns:
            The user's rows, or an empty list when the user has saved no choice.
        """
        stmt = (
            select(UserConstraintPreferenceModel)
            .where(UserConstraintPreferenceModel.user_id == user_id)
            .order_by(UserConstraintPreferenceModel.constraint_name)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def delete(self, user_id: UUID, constraint_name: str) -> bool:
        """Delete one (user, constraint) preference row.

        Args:
            user_id: Owning user.
            constraint_name: Constraint whose saved choice is cleared.

        Returns:
            ``True`` when a row existed and was deleted, ``False`` when none existed.
        """
        stmt = (
            sa_delete(UserConstraintPreferenceModel)
            .where(
                UserConstraintPreferenceModel.user_id == user_id,
                UserConstraintPreferenceModel.constraint_name == constraint_name,
            )
            .returning(UserConstraintPreferenceModel.constraint_name)
        )
        result = await self.db.execute(stmt)
        deleted = result.first() is not None
        await self.db.commit()
        return deleted

    async def upsert(
        self,
        *,
        user_id: UUID,
        constraint_name: str,
        preferred_action: str,
        source_session_id: UUID | None = None,
    ) -> None:
        """Insert or update a standing constraint preference.

        Args:
            user_id: Owning user.
            constraint_name: Constraint name the preference applies to.
            preferred_action: Stable ``action_id`` or the literal ``always_pause``.
            source_session_id: Session where the preference was set (audit trail).
        """
        now = datetime.now(UTC)
        stmt = (
            pg_insert(UserConstraintPreferenceModel)
            .values(
                user_id=user_id,
                constraint_name=constraint_name,
                preferred_action=preferred_action,
                created_at=now,
                updated_at=now,
                source_session_id=source_session_id,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "constraint_name"],
                set_={
                    "preferred_action": preferred_action,
                    "updated_at": now,
                    "source_session_id": source_session_id,
                },
            )
        )
        await self.db.execute(stmt)
        await self.db.commit()
