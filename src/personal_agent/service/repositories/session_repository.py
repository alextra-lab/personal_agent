"""Session storage repository using Postgres."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.service.models import SessionCreate, SessionModel, SessionUpdate


class SessionRepository:
    """Repository for session CRUD operations.

    All user-facing methods accept an optional user_id parameter. When
    provided the query is scoped to that user and returns None / empty
    on ownership mismatch — the endpoint layer converts this to a 404.
    Internal callers (orchestrator, brainstem) may omit user_id to
    perform unscoped reads.

    Usage:
        async with get_db_session() as db:
            repo = SessionRepository(db)
            session = await repo.create(SessionCreate(channel="CHAT"), user_id=uid)
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize repository with database session."""
        self.db = db

    async def create(self, data: SessionCreate, user_id: UUID) -> SessionModel:
        """Create a new session owned by user_id.

        Args:
            data: Session creation parameters.
            user_id: UUID of the authenticated user who owns this session.

        Returns:
            Created session model.
        """
        now = datetime.now(timezone.utc)
        session = SessionModel(
            user_id=user_id,
            created_at=now,
            last_active_at=now,
            mode=data.mode,
            channel=data.channel,
            metadata_=data.metadata,
            messages=[],
        )
        self.db.add(session)
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def get(self, session_id: UUID, user_id: UUID | None = None) -> SessionModel | None:
        """Get session by ID, optionally scoped to user_id.

        Args:
            session_id: UUID of session.
            user_id: When provided, returns None if the session is owned
                by a different user (endpoint sees a 404).

        Returns:
            Session model or None if not found / ownership mismatch.
        """
        stmt = select(SessionModel).where(SessionModel.session_id == session_id)
        if user_id is not None:
            stmt = stmt.where(SessionModel.user_id == user_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def update(
        self,
        session_id: UUID,
        data: SessionUpdate,
        user_id: UUID | None = None,
    ) -> SessionModel | None:
        """Update session, optionally scoped to user_id.

        Args:
            session_id: UUID of session.
            data: Fields to update (None values are skipped).
            user_id: When provided, the UPDATE is scoped to this owner
                so a wrong owner causes 0 rows touched → returns None.

        Returns:
            Updated session model or None if not found / ownership mismatch.
        """
        update_data = {k: v for k, v in data.model_dump().items() if v is not None}
        if "metadata" in update_data:
            update_data["metadata_"] = update_data.pop("metadata")
        update_data["last_active_at"] = datetime.now(timezone.utc)

        stmt = (
            update(SessionModel).where(SessionModel.session_id == session_id).values(**update_data)
        )
        if user_id is not None:
            stmt = stmt.where(SessionModel.user_id == user_id)

        await self.db.execute(stmt)
        await self.db.commit()
        return await self.get(session_id, user_id=user_id)

    async def append_message(self, session_id: UUID, message: dict) -> SessionModel | None:
        """Append message to session (internal — no ownership check).

        Args:
            session_id: UUID of session.
            message: Message dict with role, content, etc.

        Returns:
            Updated session model or None if not found.
        """
        session = await self.get(session_id)
        if not session:
            return None

        messages = list(session.messages or [])
        messages.append(message)

        await self.db.execute(
            update(SessionModel)
            .where(SessionModel.session_id == session_id)
            .values(messages=messages, last_active_at=datetime.now(timezone.utc))
        )
        await self.db.commit()
        return await self.get(session_id)

    async def delete(self, session_id: UUID) -> bool:
        """Delete session (internal — no ownership check).

        Args:
            session_id: UUID of session.

        Returns:
            True if deleted, False if not found.
        """
        result = await self.db.execute(
            delete(SessionModel).where(SessionModel.session_id == session_id)
        )
        await self.db.commit()
        return result.rowcount > 0

    async def list_recent(self, limit: int = 50, user_id: UUID | None = None) -> list[SessionModel]:
        """List recent sessions, optionally scoped to user_id.

        Args:
            limit: Maximum number to return.
            user_id: When provided, returns only sessions owned by this user.

        Returns:
            List of sessions ordered by last_active_at DESC.
        """
        stmt = select(SessionModel).order_by(SessionModel.last_active_at.desc()).limit(limit)
        if user_id is not None:
            stmt = stmt.where(SessionModel.user_id == user_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
