"""Session storage repository using Postgres."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.service.models import SessionCreate, SessionModel, SessionUpdate


class SessionRepository:
    """Repository for session CRUD operations.

    Usage:
        async with get_db_session() as db:
            repo = SessionRepository(db)
            session = await repo.create(SessionCreate(channel="CHAT"))
    """

    def __init__(self, db: AsyncSession):  # noqa: D107
        """Initialize repository with database session."""
        self.db = db

    async def create(self, data: SessionCreate) -> SessionModel:
        """Create a new session.

        Args:
            data: Session creation parameters

        Returns:
            Created session model
        """
        now = datetime.utcnow()
        session = SessionModel(
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

    async def get(self, session_id: UUID) -> Optional[SessionModel]:
        """Get session by ID.

        Args:
            session_id: UUID of session

        Returns:
            Session model or None if not found
        """
        result = await self.db.execute(
            select(SessionModel).where(SessionModel.session_id == session_id)
        )
        return result.scalar_one_or_none()

    async def update(self, session_id: UUID, data: SessionUpdate) -> Optional[SessionModel]:
        """Update session.

        Args:
            session_id: UUID of session
            data: Fields to update (None values are skipped)

        Returns:
            Updated session model or None if not found
        """
        update_data = {k: v for k, v in data.model_dump().items() if v is not None}
        update_data["last_active_at"] = datetime.utcnow()

        await self.db.execute(
            update(SessionModel).where(SessionModel.session_id == session_id).values(**update_data)
        )
        await self.db.commit()
        return await self.get(session_id)

    async def append_message(self, session_id: UUID, message: dict) -> Optional[SessionModel]:
        """Append message to session.

        Args:
            session_id: UUID of session
            message: Message dict with role, content, etc.

        Returns:
            Updated session model or None if not found
        """
        session = await self.get(session_id)
        if not session:
            return None

        messages = list(session.messages or [])
        messages.append(message)

        await self.db.execute(
            update(SessionModel)
            .where(SessionModel.session_id == session_id)
            .values(messages=messages, last_active_at=datetime.utcnow())
        )
        await self.db.commit()
        return await self.get(session_id)

    async def delete(self, session_id: UUID) -> bool:
        """Delete session.

        Args:
            session_id: UUID of session

        Returns:
            True if deleted, False if not found
        """
        result = await self.db.execute(
            delete(SessionModel).where(SessionModel.session_id == session_id)
        )
        await self.db.commit()
        return result.rowcount > 0

    async def list_recent(self, limit: int = 50) -> list[SessionModel]:
        """List recent sessions.

        Args:
            limit: Maximum number to return

        Returns:
            List of sessions ordered by last_active_at DESC
        """
        result = await self.db.execute(
            select(SessionModel).order_by(SessionModel.last_active_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
