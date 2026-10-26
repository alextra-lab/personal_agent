"""AC-3 of FRE-1702: Clear restores the default for the next resolution.

Real-DB test against the test-stack Postgres (:5433 — FRE-375 isolation). It seeds a
saved ``artifact_builder`` choice, proves the turn's pause path applies it silently,
calls the ``DELETE`` handler, and proves the same pause path no longer applies it and the
effective builder deployment is the configured default.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from personal_agent.service.auth import RequestUser
from personal_agent.service.database import AsyncSessionLocal, engine


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


@pytest.mark.asyncio
async def test_clear_restores_default_resolution():
    """After DELETE the turn's pause path ignores the row and the builder is the default."""
    if not _postgres_available():
        pytest.skip("test Postgres :5433 unreachable — run make test-infra-up")

    from personal_agent.config.model_loader import is_selectable_binding, load_model_config
    from personal_agent.orchestrator.constraint_options import (
        artifact_builder_default_key,
        resolve_effective_artifact_builder_deployment,
    )
    from personal_agent.orchestrator.executor import _maybe_pause_for_constraint
    from personal_agent.service.app import clear_constraint_preference
    from personal_agent.service.repositories.constraint_preferences_repository import (
        ConstraintPreferencesRepository,
    )

    catalog = load_model_config()
    default_key = artifact_builder_default_key(catalog)

    # Provider credentials differ per environment; availability is not what this test proves.
    def available(_provider: str) -> bool:
        return True

    saved_key = next(
        key
        for key in catalog.models
        if key != default_key and is_selectable_binding("artifact_builder", key, catalog)
    )

    user_id = uuid4()
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("INSERT INTO users (user_id, email) VALUES (:uid, :email)"),
            {"uid": user_id, "email": f"fre1702-{user_id}@test.invalid"},
        )
        await db.commit()
        try:
            await ConstraintPreferencesRepository(db).upsert(
                user_id=user_id, constraint_name="artifact_builder", preferred_action=saved_key
            )

            async def _pause():
                return await _maybe_pause_for_constraint(
                    session_id=str(uuid4()),
                    trace_id="trace-fre1702",
                    user_id=user_id,
                    constraint="artifact_builder",
                    context="Choose the model to build this artifact.",
                    timeout_seconds=0.05,
                )

            # Before Clear: the saved choice is applied silently and wins.
            before = await _pause()
            assert before.resolution == "preference_applied"
            assert str(before) == saved_key
            assert (
                resolve_effective_artifact_builder_deployment(
                    before, catalog, is_provider_available=available
                )
                == saved_key
            )

            result = await clear_constraint_preference(
                constraint_name="artifact_builder",
                request_user=RequestUser(user_id=user_id, email="x@test.invalid"),
                db=db,
            )
            assert result == {"constraint_name": "artifact_builder", "cleared": True}

            # After Clear: no preference applies. With no client the pause takes its
            # timeout branch (waiter patched to that outcome), and the build uses the default.
            async def _timeout(**kwargs):
                return {"decision": kwargs["event"].default_option, "resolution": "timeout_default"}

            with (
                patch(
                    "personal_agent.transport.agui.transport.register_and_push_constraint",
                    side_effect=_timeout,
                ),
                patch(
                    "personal_agent.transport.agui.transport.emit_constraint_resolved",
                    AsyncMock(return_value=None),
                ),
            ):
                after = await _pause()
            assert after.resolution == "timeout_default"
            assert (
                resolve_effective_artifact_builder_deployment(
                    after, catalog, is_provider_available=available
                )
                == default_key
            )
            assert saved_key != default_key
        finally:
            await db.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
            await db.commit()
