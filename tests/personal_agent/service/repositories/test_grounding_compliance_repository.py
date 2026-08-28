"""GroundingComplianceRepository tests (ADR-0138 D5 / FRE-1284).

Real-DB tests against the test-stack Postgres (:5433 — FRE-375 isolation), mirroring
``test_session_model_selection_repository.py``. Skips cleanly when the test stack is
unreachable (``make test-infra-up``).

Proves the three properties the metric depends on and cannot verify for itself: the store
is append-only and **idempotent on trace_id** (a replayed turn must not inflate the
numerator), reads come back **newest-first with a total order** (an index does not define
SQL result order, and a window assembled from the wrong end is a reading nobody could tell
was wrong), and the window is **bounded by limit** rather than by whatever the table holds.
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from personal_agent.service.database import AsyncSessionLocal, engine
from personal_agent.service.models import GroundingComplianceObservationModel
from personal_agent.service.repositories.grounding_compliance_repository import (
    GroundingComplianceRepository,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _postgres_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5433), timeout=2):
            return True
    except OSError:
        return False


def test_observed_at_has_server_default() -> None:
    """The column carries a DDL server_default (no DB needed).

    Guards the deploy-ordering bug: if ``Base.metadata.create_all`` builds this table
    before migration 0029 runs, ``observed_at`` must still get ``DEFAULT NOW()`` rather
    than a NOT NULL column with no default.
    """
    column = GroundingComplianceObservationModel.__table__.c.observed_at
    assert column.server_default is not None
    assert not column.nullable


def test_trace_id_is_unique() -> None:
    """One observation per turn, enforced by the schema rather than by convention."""
    column = GroundingComplianceObservationModel.__table__.c.trace_id
    assert column.unique
    assert not column.nullable


pytestmark = pytest.mark.skipif(
    not _postgres_available(), reason="test-stack Postgres (:5433) unreachable"
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    """Dispose pooled connections around each test.

    Mirrors ``test_session_model_selection_repository.py``: the module-level engine's pool
    outlives a test's event loop, and a connection carried across loops fails with
    "another operation is in progress" rather than anything that names the real cause.
    """
    await engine.dispose()
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def repo():
    """Yield a repository over the test stack, cleaning up its own rows.

    The table is **not** created here. DDL belongs to migration 0029, run as the `agent`
    superuser (FRE-808: the app role cannot run DDL), so a fixture that created it would
    be testing a table the deployed schema might not have — the exact drift a schema
    migration exists to prevent. If this fixture fails on a missing relation, the test
    stack has not had `docker/postgres/migrations/0029_*.sql` applied.
    """
    async with AsyncSessionLocal() as db:
        yield GroundingComplianceRepository(db)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM grounding_compliance_observations WHERE model_key LIKE 'test-%'")
        )
        await db.commit()


@pytest.mark.asyncio
async def test_record_then_read_back(repo: GroundingComplianceRepository) -> None:
    model = f"test-{uuid4().hex[:8]}"
    assert await repo.record(model_key=model, compliant=True, trace_id=uuid4().hex, observed_at=NOW)

    rows = await repo.recent(model, limit=10)
    assert len(rows) == 1
    assert rows[0].model_key == model
    assert rows[0].compliant is True


@pytest.mark.asyncio
async def test_replayed_trace_does_not_inflate(repo: GroundingComplianceRepository) -> None:
    """The idempotency key earns its keep: a second write for a trace is a no-op.

    Written as an inflation test rather than a uniqueness test because inflation is the
    consequence that matters — a duplicated compliant turn buys a promotion twice.
    """
    model = f"test-{uuid4().hex[:8]}"
    trace = uuid4().hex

    assert await repo.record(model_key=model, compliant=True, trace_id=trace, observed_at=NOW)
    assert not await repo.record(model_key=model, compliant=True, trace_id=trace, observed_at=NOW)

    assert len(await repo.recent(model, limit=10)) == 1


@pytest.mark.asyncio
async def test_recent_returns_newest_first(repo: GroundingComplianceRepository) -> None:
    model = f"test-{uuid4().hex[:8]}"
    for index in range(5):
        await repo.record(
            model_key=model,
            compliant=index % 2 == 0,
            trace_id=uuid4().hex,
            observed_at=NOW - timedelta(minutes=index),
        )

    rows = await repo.recent(model, limit=10)
    assert [row.observed_at for row in rows] == sorted(
        (row.observed_at for row in rows), reverse=True
    )
    assert rows[0].observed_at == NOW


@pytest.mark.asyncio
async def test_limit_bounds_the_window(repo: GroundingComplianceRepository) -> None:
    model = f"test-{uuid4().hex[:8]}"
    for index in range(7):
        await repo.record(
            model_key=model,
            compliant=True,
            trace_id=uuid4().hex,
            observed_at=NOW - timedelta(minutes=index),
        )

    rows = await repo.recent(model, limit=3)
    assert len(rows) == 3
    assert rows[0].observed_at == NOW, "the limit must take the newest, not the oldest"


@pytest.mark.asyncio
async def test_reads_are_scoped_to_one_model(repo: GroundingComplianceRepository) -> None:
    """A per-model metric that read another model's turns would be worse than useless."""
    mine = f"test-{uuid4().hex[:8]}"
    theirs = f"test-{uuid4().hex[:8]}"
    await repo.record(model_key=mine, compliant=True, trace_id=uuid4().hex, observed_at=NOW)
    await repo.record(model_key=theirs, compliant=False, trace_id=uuid4().hex, observed_at=NOW)

    rows = await repo.recent(mine, limit=10)
    assert [row.model_key for row in rows] == [mine]
