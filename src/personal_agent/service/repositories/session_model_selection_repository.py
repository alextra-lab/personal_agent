"""Repository for session-scoped model selections (ADR-0121 §4 / FRE-917).

The server-authoritative store that carries a session's chosen model per role,
replacing the execution-profile "Path" as the source of truth (ADR-0079's
invariants inherited). A missing row for a ``(session_id, role)`` pair means
"resolve through the role's configured binding default" — the fail-closed
fallback the guardrail (ADR-0121 §6) relies on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.service.models import SessionModelSelectionModel


class SessionModelSelectionRepository:
    """CRUD for ``session_model_selections``.

    A missing row for a ``(session_id, role)`` pair means the role resolves
    through its configured binding default; the read helpers return ``None`` /
    omit the role in that case so callers apply the fail-closed default.

    Ownership is enforced by the caller (the write API scopes the session read
    to the CF-Access user before upserting) — this repository is session-scoped
    and carries no user column of its own.

    Usage:
        async with AsyncSessionLocal() as db:
            repo = SessionModelSelectionRepository(db)
            key = await repo.get(session_id, "primary")
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the repository with a database session.

        Args:
            db: Async SQLAlchemy session.
        """
        self.db = db

    async def get(self, session_id: UUID, role: str) -> str | None:
        """Return the stored deployment key for a ``(session_id, role)`` pair.

        Args:
            session_id: The session the selection belongs to.
            role: The role name (e.g. ``"primary"``).

        Returns:
            The stored deployment key, or ``None`` when no row exists (the
            caller then applies the role's configured default).
        """
        stmt = select(SessionModelSelectionModel.deployment_key).where(
            SessionModelSelectionModel.session_id == session_id,
            SessionModelSelectionModel.role == role,
        )
        result = await self.db.execute(stmt)
        row = result.scalar_one_or_none()
        return str(row) if row is not None else None

    async def get_all(self, session_id: UUID) -> dict[str, str]:
        """Return all stored selections for a session as ``{role: deployment_key}``.

        Args:
            session_id: The session whose selections to load.

        Returns:
            A mapping of role → deployment key; empty when the session has no
            stored selections (every role then resolves through its default).
        """
        stmt = select(
            SessionModelSelectionModel.role,
            SessionModelSelectionModel.deployment_key,
        ).where(SessionModelSelectionModel.session_id == session_id)
        result = await self.db.execute(stmt)
        return {str(role): str(key) for role, key in result.all()}

    async def upsert(self, *, session_id: UUID, role: str, deployment_key: str) -> None:
        """Insert or update a session's model selection for a role.

        Atomic upsert on the composite primary key ``(session_id, role)``.

        Args:
            session_id: The session the selection belongs to.
            role: The role name (e.g. ``"primary"``).
            deployment_key: The catalog deployment key to store. Callers are
                responsible for validating the key against the guardrail
                (ADR-0121 §6) before persisting — this repository stores what it
                is given.
        """
        now = datetime.now(UTC)
        stmt = (
            pg_insert(SessionModelSelectionModel)
            .values(
                session_id=session_id,
                role=role,
                deployment_key=deployment_key,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["session_id", "role"],
                set_={"deployment_key": deployment_key, "updated_at": now},
            )
        )
        await self.db.execute(stmt)
        await self.db.commit()
