"""Session storage repository using Postgres."""

from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.exceptions import InvalidMessageError
from personal_agent.service.models import SessionCreate, SessionModel, SessionUpdate

# ADR-0074 (FRE-376) — every persisted assistant message must carry these
# fields so per-message model attribution survives in Postgres. They live in
# the top-level message dict OR under ``message["metadata"]``; either is fine.
_REQUIRED_ASSISTANT_FIELDS: tuple[str, ...] = ("model", "model_role", "model_config_path")


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

    async def create(
        self,
        data: SessionCreate,
        user_id: UUID,
        *,
        primary_model_at_creation: str | None = None,
        model_config_path: str | None = None,
    ) -> SessionModel:
        """Create a new session owned by user_id.

        Args:
            data: Session creation parameters.
            user_id: UUID of the authenticated user who owns this session.
            primary_model_at_creation: Identifier of the primary model active
                when the session opened (ADR-0074 / FRE-376). Optional only so
                callers in tests can omit it; production paths should always
                pass the resolved value.
            model_config_path: Resolved YAML config path active when the
                session opened. Same provenance contract.

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
            primary_model_at_creation=primary_model_at_creation,
            model_config_path=model_config_path,
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

    async def append_message(
        self, session_id: UUID, message: dict[str, Any]
    ) -> SessionModel | None:
        """Append message to session (internal — no ownership check).

        Args:
            session_id: UUID of session.
            message: Message dict with role, content, etc. Assistant messages
                must additionally carry ``model``, ``model_role`` and
                ``model_config_path`` — either at the top level or under
                ``message["metadata"]`` — per ADR-0074 (FRE-376).

        Returns:
            Updated session model or None if not found.

        Raises:
            InvalidMessageError: If an assistant message is missing any of the
                required per-message attribution fields.
        """
        if message.get("role") == "assistant":
            metadata = message.get("metadata") or {}
            missing = [
                f
                for f in _REQUIRED_ASSISTANT_FIELDS
                if message.get(f) is None and metadata.get(f) is None
            ]
            if missing:
                raise InvalidMessageError(
                    f"assistant message missing required attribution fields: {missing}"
                )

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
        result = cast(
            CursorResult[Any],
            await self.db.execute(
                delete(SessionModel).where(SessionModel.session_id == session_id)
            ),
        )
        await self.db.commit()
        return bool(result.rowcount > 0)

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
