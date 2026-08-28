"""GroundingEnforcementRepository tests (ADR-0138 D5 / FRE-1285).

Real-DB tests against the test-stack Postgres (:5433 — FRE-375 isolation), mirroring
``test_grounding_compliance_repository.py``. Skips cleanly when the test stack is
unreachable (``make test-infra-up``).

The property worth the round trip is the **optimistic guard**. Turns run concurrently and
select independently, so two can hold the same stale standing and write different
transitions. Last-write-wins would let a slower turn's older reading reset a cooldown —
and a reset cooldown buys a model a promotion it never served out, with nothing downstream
able to tell that it happened.
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from personal_agent.grounding.enforcement_selection import EnforcementLevel, EnforcementState
from personal_agent.service.database import AsyncSessionLocal, engine
from personal_agent.service.models import GroundingEnforcementStateModel
from personal_agent.service.repositories.grounding_enforcement_repository import (
    GroundingEnforcementRepository,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _postgres_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


def test_model_key_is_the_primary_key() -> None:
    """Exactly one standing state per model (no DB needed).

    A table that could hold two rows for one model is a table where a reader has to guess
    which one is in force.
    """
    assert GroundingEnforcementStateModel.__table__.c.model_key.primary_key


def test_demoted_at_is_nullable() -> None:
    """NULL means *never demoted*, which is not the same as demoted long ago.

    D5 gives the cooldown to a demoted model, so a model that has never been light must be
    able to say so rather than carry a sentinel timestamp someone later reads as a real
    demotion.
    """
    assert GroundingEnforcementStateModel.__table__.c.demoted_at.nullable


def test_updated_at_has_server_default() -> None:
    """Guards the deploy-ordering bug, per migration 0029's precedent."""
    column = GroundingEnforcementStateModel.__table__.c.updated_at
    assert column.server_default is not None
    assert not column.nullable


pytestmark = pytest.mark.skipif(
    not _postgres_available(), reason="test-stack Postgres (:5433) unreachable"
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    """Dispose pooled connections around each test.

    Mirrors ``test_grounding_compliance_repository.py``: the module-level engine's pool
    outlives a test's event loop, and a connection carried across loops fails with
    "another operation is in progress" rather than anything that names the real cause.
    """
    await engine.dispose()
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def repo():
    """Yield a repository over the test stack, cleaning up its own rows.

    The table is **not** created here. DDL belongs to migration 0030, run as the `agent`
    superuser (FRE-808: the app role cannot run DDL). If this fixture fails on a missing
    relation, the test stack has not had
    ``docker/postgres/migrations/0030_grounding_enforcement_state.sql`` applied.
    """
    async with AsyncSessionLocal() as db:
        yield GroundingEnforcementRepository(db)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM grounding_enforcement_state WHERE model_key LIKE 'test-%'")
        )
        await db.commit()


@pytest.mark.asyncio
async def test_unknown_model_reads_back_none(repo: GroundingEnforcementRepository) -> None:
    """Storage reports absence; the caller decides what absence means.

    Under D5 it means heavy with no cooldown owed — a policy that belongs with the policy
    rather than defaulted here, where nobody reviewing the selection rules would see it.
    """
    assert await repo.get(f"test-{uuid4().hex[:8]}") is None


@pytest.mark.asyncio
async def test_upsert_then_read_back(repo: GroundingEnforcementRepository) -> None:
    """The base case: a stamped demotion survives the turn that decided it."""
    model = f"test-{uuid4().hex[:8]}"
    state = EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=NOW)

    assert await repo.upsert(model, state, updated_at=NOW)

    stored = await repo.get(model)
    assert stored is not None
    assert stored.level is EnforcementLevel.HEAVY
    assert stored.demoted_at == NOW


@pytest.mark.asyncio
async def test_promotion_clears_the_cooldown_stamp(repo: GroundingEnforcementRepository) -> None:
    """A promoted model owes no cooldown until it is demoted again."""
    model = f"test-{uuid4().hex[:8]}"
    await repo.upsert(
        model, EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=NOW), updated_at=NOW
    )

    promoted = EnforcementState(level=EnforcementLevel.LIGHT, demoted_at=None)
    assert await repo.upsert(model, promoted, updated_at=NOW + timedelta(hours=25))

    stored = await repo.get(model)
    assert stored is not None
    assert stored.level is EnforcementLevel.LIGHT
    assert stored.demoted_at is None


@pytest.mark.asyncio
async def test_stale_write_does_not_clobber_a_newer_transition(
    repo: GroundingEnforcementRepository,
) -> None:
    """The guard, stated as the corruption it prevents.

    A turn that decided at T0 but wrote after a turn that decided at T1 must not win.
    On a demotion that would reset the cooldown to an older stamp, which is exactly how a
    model gets a promotion it never served out.
    """
    model = f"test-{uuid4().hex[:8]}"
    newer = NOW + timedelta(minutes=5)

    demoted_recently = EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=newer)
    assert await repo.upsert(model, demoted_recently, updated_at=newer)

    demoted_earlier = EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=NOW)
    assert not await repo.upsert(model, demoted_earlier, updated_at=NOW)

    stored = await repo.get(model)
    assert stored is not None
    assert stored.demoted_at == newer, "the older reading overwrote a newer transition"


@pytest.mark.asyncio
async def test_a_write_at_the_same_instant_is_not_applied(
    repo: GroundingEnforcementRepository,
) -> None:
    """Strictly newer, not newer-or-equal.

    Two turns selecting at the identical instant have nothing to order them by, so the
    first write stands rather than the arrival order silently deciding.
    """
    model = f"test-{uuid4().hex[:8]}"
    await repo.upsert(
        model, EnforcementState(level=EnforcementLevel.HEAVY, demoted_at=NOW), updated_at=NOW
    )

    assert not await repo.upsert(
        model, EnforcementState(level=EnforcementLevel.LIGHT, demoted_at=None), updated_at=NOW
    )

    stored = await repo.get(model)
    assert stored is not None
    assert stored.level is EnforcementLevel.HEAVY
