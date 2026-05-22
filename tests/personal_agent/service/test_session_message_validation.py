"""ADR-0074 / FRE-376 — assistant-message validation in SessionRepository.

Phase 1 enforces per-message model attribution at the service layer (the
``messages`` column is JSONB, so the DB cannot do this for us). These tests
pin the contract: assistant messages must carry ``model``, ``model_role``,
and ``model_config_path`` at the top level OR under ``metadata``; user /
tool / system messages are unaffected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from personal_agent.exceptions import InvalidMessageError
from personal_agent.service.models import SessionModel
from personal_agent.service.repositories.session_repository import SessionRepository


def _make_session(user_id: UUID) -> SessionModel:
    s = SessionModel()
    s.session_id = uuid4()
    s.user_id = user_id
    s.created_at = datetime.now(timezone.utc)
    s.last_active_at = datetime.now(timezone.utc)
    s.mode = "NORMAL"
    s.channel = "CHAT"
    s.metadata_ = {}
    s.messages = []
    return s


def _async_db_with_session(session: SessionModel) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.refresh = AsyncMock()
    # Both `get` calls inside append_message look up via `select(...)` →
    # `db.execute` → `result.scalar_one_or_none`. Return our stub session for
    # every execute call so the get-before-update and get-after-update paths
    # both resolve.
    result = MagicMock()
    result.scalar_one_or_none.return_value = session
    db.execute.return_value = result
    return db


@pytest.mark.asyncio
async def test_assistant_message_missing_all_attribution_raises() -> None:
    """No attribution anywhere → raise InvalidMessageError."""
    session = _make_session(uuid4())
    db = _async_db_with_session(session)
    repo = SessionRepository(db)

    with pytest.raises(InvalidMessageError) as exc_info:
        await repo.append_message(
            session.session_id,
            {
                "role": "assistant",
                "content": "hi",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": {"source": "service.app"},
            },
        )
    # Ensure the error names what was missing so operators can fix the caller.
    assert "model" in str(exc_info.value)


@pytest.mark.asyncio
async def test_assistant_message_with_metadata_attribution_passes() -> None:
    """All three fields under metadata → no raise."""
    session = _make_session(uuid4())
    db = _async_db_with_session(session)
    repo = SessionRepository(db)

    await repo.append_message(
        session.session_id,
        {
            "role": "assistant",
            "content": "hi",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "source": "service.app",
                "model": "anthropic/claude-sonnet-4.6",
                "model_role": "primary",
                "model_config_path": "/opt/seshat/config/models.cloud.yaml",
            },
        },
    )


@pytest.mark.asyncio
async def test_assistant_message_with_top_level_attribution_passes() -> None:
    """All three fields at the top level (no metadata wrapper) → no raise."""
    session = _make_session(uuid4())
    db = _async_db_with_session(session)
    repo = SessionRepository(db)

    await repo.append_message(
        session.session_id,
        {
            "role": "assistant",
            "content": "hi",
            "model": "anthropic/claude-sonnet-4.6",
            "model_role": "primary",
            "model_config_path": "/opt/seshat/config/models.cloud.yaml",
        },
    )


@pytest.mark.asyncio
async def test_assistant_message_missing_one_field_raises() -> None:
    """Partial attribution (model + role but no config_path) → raise."""
    session = _make_session(uuid4())
    db = _async_db_with_session(session)
    repo = SessionRepository(db)

    with pytest.raises(InvalidMessageError) as exc_info:
        await repo.append_message(
            session.session_id,
            {
                "role": "assistant",
                "content": "hi",
                "metadata": {
                    "source": "service.app",
                    "model": "anthropic/claude-sonnet-4.6",
                    "model_role": "primary",
                    # model_config_path missing
                },
            },
        )
    assert "model_config_path" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "tool", "system"])
async def test_non_assistant_messages_bypass_validation(role: str) -> None:
    """User/tool/system messages do not require attribution."""
    session = _make_session(uuid4())
    db = _async_db_with_session(session)
    repo = SessionRepository(db)

    await repo.append_message(
        session.session_id,
        {
            "role": role,
            "content": "hi",
            "metadata": {"source": "service.app"},
        },
    )
