"""collect_inputs against the test-stack Postgres (:5433), FRE-1702.

Seeds a user, a conversation with a stored model selection and effort, and a saved
choice. Proves the menu shows them, and that another user's conversation is refused.
"""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from personal_agent.config import load_model_config
from personal_agent.service.database import AsyncSessionLocal, engine
from personal_agent.service.repositories.constraint_preferences_repository import (
    ConstraintPreferencesRepository,
)
from personal_agent.service.repositories.session_model_selection_repository import (
    SessionModelSelectionRepository,
)
from personal_agent.service.settings_menu import SessionNotFoundError, build_menu, collect_inputs


def _postgres_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed(db, *, effort: str | None = None):
    user_id, session_id = uuid4(), uuid4()
    await db.execute(
        text("INSERT INTO users (user_id, email) VALUES (:uid, :email)"),
        {"uid": user_id, "email": f"fre1702-{user_id}@test.invalid"},
    )
    await db.execute(
        text(
            "INSERT INTO sessions (session_id, user_id, created_at, last_active_at, seshat_effort)"
            " VALUES (:sid, :uid, :now, :now, :effort)"
        ),
        {
            "sid": session_id,
            "uid": user_id,
            "now": datetime.now(timezone.utc),
            "effort": effort or "standard",
        },
    )
    await db.commit()
    return user_id, session_id


async def _cleanup(db, *user_ids) -> None:
    for uid in user_ids:
        await db.execute(text("DELETE FROM sessions WHERE user_id = :uid"), {"uid": uid})
        await db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": uid})
    await db.commit()


@pytest.mark.asyncio
async def test_menu_shows_the_stored_conversation_and_saved_choice():
    if not _postgres_available():
        pytest.skip("test Postgres :5433 unreachable — run make test-infra-up")
    models = load_model_config()
    default_primary = models.roles["primary"].deployment
    other = next(
        k for k, m in models.models.items() if k != default_primary and m.kind.value == "llm"
    )
    async with AsyncSessionLocal() as db:
        user_id, session_id = await _seed(db, effort="deep")
        try:
            await SessionModelSelectionRepository(db).upsert(
                session_id=session_id, role="primary", deployment_key=other
            )
            await ConstraintPreferencesRepository(db).upsert(
                user_id=user_id, constraint_name="spend_threshold", preferred_action="answer_now"
            )

            inputs = await collect_inputs(
                db, user_id=user_id, session_id=session_id, location_consent=None, trace_id="t"
            )
            menu = build_menu(inputs)

            by_name = {r.name: r for g in menu.groups for r in g.rows}
            assert by_name["session.primary_model"].value == other
            assert by_name["session.primary_model"].source == "session"
            assert by_name["session.effort_level"].value == "deep"
            saved = by_name["saved_choice.spend_threshold"]
            assert saved.value == "answer_now" and saved.clearable
            assert not by_name["saved_choice.tool_iteration_limit"].clearable
        finally:
            await _cleanup(db, user_id)


@pytest.mark.asyncio
async def test_another_users_conversation_is_refused():
    if not _postgres_available():
        pytest.skip("test Postgres :5433 unreachable — run make test-infra-up")
    async with AsyncSessionLocal() as db:
        owner, session_id = await _seed(db)
        intruder, _ = await _seed(db)
        try:
            with pytest.raises(SessionNotFoundError):
                await collect_inputs(
                    db, user_id=intruder, session_id=session_id, location_consent=None, trace_id="t"
                )
        finally:
            await _cleanup(db, owner, intruder)


@pytest.mark.asyncio
async def test_without_a_conversation_the_defaults_show():
    if not _postgres_available():
        pytest.skip("test Postgres :5433 unreachable — run make test-infra-up")
    async with AsyncSessionLocal() as db:
        user_id, _ = await _seed(db)
        try:
            inputs = await collect_inputs(
                db, user_id=user_id, session_id=None, location_consent=None, trace_id="t"
            )
            assert inputs.session is None and inputs.saved_choices == {}
            assert inputs.grounding is not None
        finally:
            await _cleanup(db, user_id)
