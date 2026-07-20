"""Session storage repository using Postgres."""

import json
from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select, text, update
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
            # ADR-0121 T5 (FRE-920): vestigial — nothing reads this back; the
            # selection store is the source of truth. Column stays NOT NULL.
            execution_profile="local",
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
        # ADR-0098 D4/D6 (FRE-860) — any write reactivates a previously
        # soft-pruned session; clear the tombstone unconditionally (not just
        # for a messages write) since last_active_at is bumped unconditionally
        # too. Otherwise a non-messages field update (e.g. execution_profile)
        # on a purged session would look freshly active while permanently
        # excluded from future retention re-evaluation (purged_at IS NULL is
        # the scan/prune guard) with messages stuck at '[]'.
        update_data["purged_at"] = None

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
            .values(
                messages=messages,
                last_active_at=datetime.now(timezone.utc),
                # ADR-0098 D4/D6 (FRE-860) — writing messages reactivates a
                # previously soft-pruned session; clear the tombstone.
                purged_at=None,
            )
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

    async def prune_expired(self, retention_days: int) -> int:
        """Soft-prune sessions inactive past the retention window (FRE-860 / ADR-0098 D4/D6).

        Clears ``messages`` to ``[]`` and stamps ``purged_at`` for every
        not-yet-purged session whose ``last_active_at`` is older than
        ``retention_days``. A hard ``DELETE`` is not used: ``artifacts`` and
        ``session_events`` both FK to ``sessions(session_id)`` with no
        cascade. Resuming a pruned session (``append_message`` / ``update``)
        clears ``purged_at`` again.

        Args:
            retention_days: Number of days of inactivity before pruning.

        Returns:
            Number of session rows pruned by this sweep.
        """
        result = cast(
            CursorResult[Any],
            await self.db.execute(
                text(
                    "UPDATE sessions "
                    "SET messages = '[]'::jsonb, purged_at = NOW() "
                    "WHERE purged_at IS NULL "
                    "AND last_active_at < NOW() - make_interval(days => :days)"
                ),
                {"days": retention_days},
            ),
        )
        await self.db.commit()
        return int(result.rowcount)

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

    # ------------------------------------------------------------------
    # Pending-state persistence — cloud-attachment confirmation (FRE-749 /
    # ADR-0101 §8b) and document-page-budget continuation (ADR-0102 §4 /
    # FRE-685). Both are durable, cross-request records carried in the
    # ``sessions.metadata`` JSONB column, each under its own key so the two
    # pending states never collide or clobber each other. Key-level JSONB SQL
    # (``jsonb_set`` / key deletion / ``->``) is used rather than a
    # whole-dict read-modify-write so a concurrent writer to a *different*
    # metadata key (e.g. the PATCH ``/sessions/{id}`` settings path) is not
    # clobbered. TTL is a business concern applied by the caller — the
    # repository stores/returns the raw JSON. ``key`` below is always one of
    # the two literal constants below, never user input, so building it into
    # the SQL text (rather than a bind param, which ``jsonb_set``'s path
    # argument doesn't accept) carries no injection risk.
    # ------------------------------------------------------------------

    _PENDING_CLOUD_CONFIRMATION_KEY = "pending_cloud_confirmation"
    _PENDING_DOCUMENT_CONTINUATION_KEY = "pending_document_continuation"

    async def _save_pending(self, session_id: UUID, key: str, pending: dict[str, Any]) -> int:
        """Durably store a pending-state payload under ``key`` for a session.

        Args:
            session_id: UUID of the session to annotate.
            key: The ``sessions.metadata`` JSONB key to write under.
            pending: JSON-serializable pending-state payload.

        Returns:
            Number of rows updated (0 when the session row does not exist —
            the caller surfaces this as telemetry).
        """
        stmt = text(
            "UPDATE sessions "
            f"SET metadata = jsonb_set(COALESCE(metadata, '{{}}'::jsonb), "
            f"'{{{key}}}', CAST(:payload AS jsonb)), "
            "last_active_at = now() "
            "WHERE session_id = :sid"
        )
        result = cast(
            CursorResult[Any],
            await self.db.execute(stmt, {"payload": json.dumps(pending), "sid": str(session_id)}),
        )
        await self.db.commit()
        return int(result.rowcount)

    async def _load_pending(self, session_id: UUID, key: str) -> dict[str, Any] | None:
        """Load the raw pending-state payload under ``key`` for a session.

        TTL is NOT applied here — the caller decides expiry so the repository
        stays free of business policy.

        Args:
            session_id: UUID of the session to read.
            key: The ``sessions.metadata`` JSONB key to read.

        Returns:
            The stored payload dict, or None when absent / no such session.
        """
        stmt = text(f"SELECT metadata -> '{key}' AS pending FROM sessions WHERE session_id = :sid")
        row = (await self.db.execute(stmt, {"sid": str(session_id)})).first()
        if row is None:
            return None
        pending = row[0]
        if pending is None:
            return None
        # The JSONB `->` result may arrive already decoded (dict) or as a JSON
        # string depending on the driver's codec; normalize to a dict.
        if isinstance(pending, str):
            pending = json.loads(pending)
        return cast("dict[str, Any]", pending)

    async def _clear_pending(self, session_id: UUID, key: str) -> None:
        """Remove the pending-state key ``key`` for a session.

        A no-op when the session row or key is absent.

        Args:
            session_id: UUID of the session to clear.
            key: The ``sessions.metadata`` JSONB key to delete.
        """
        stmt = text(
            f"UPDATE sessions SET metadata = metadata - '{key}', last_active_at = now() "
            "WHERE session_id = :sid"
        )
        await self.db.execute(stmt, {"sid": str(session_id)})
        await self.db.commit()

    async def save_pending_confirmation(self, session_id: UUID, pending: dict[str, Any]) -> int:
        """Durably store pending cloud-attachment confirmation for a session."""
        return await self._save_pending(session_id, self._PENDING_CLOUD_CONFIRMATION_KEY, pending)

    async def load_pending_confirmation(self, session_id: UUID) -> dict[str, Any] | None:
        """Load the raw pending cloud-attachment confirmation for a session."""
        return await self._load_pending(session_id, self._PENDING_CLOUD_CONFIRMATION_KEY)

    async def clear_pending_confirmation(self, session_id: UUID) -> None:
        """Remove the pending cloud-attachment confirmation key for a session."""
        await self._clear_pending(session_id, self._PENDING_CLOUD_CONFIRMATION_KEY)

    async def save_pending_document_continuation(
        self, session_id: UUID, pending: dict[str, Any]
    ) -> int:
        """Durably store pending document-continuation offer(s) for a session."""
        return await self._save_pending(
            session_id, self._PENDING_DOCUMENT_CONTINUATION_KEY, pending
        )

    async def load_pending_document_continuation(self, session_id: UUID) -> dict[str, Any] | None:
        """Load the raw pending document-continuation offer(s) for a session."""
        return await self._load_pending(session_id, self._PENDING_DOCUMENT_CONTINUATION_KEY)

    async def clear_pending_document_continuation(self, session_id: UUID) -> None:
        """Remove the pending document-continuation key for a session."""
        await self._clear_pending(session_id, self._PENDING_DOCUMENT_CONTINUATION_KEY)
