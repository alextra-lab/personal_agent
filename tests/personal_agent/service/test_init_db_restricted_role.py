"""FRE-808 AC-3: startup ``init_db()`` succeeds under the restricted app role.

The service and gateway both call ``init_db()`` at startup, which runs
``Base.metadata.create_all`` (``checkfirst=True``). Because the SQLAlchemy models
are a strict subset of the tables ``init.sql``/migrations already create, this
emits **zero DDL** and therefore needs no CREATE privilege — so it works for the
non-superuser ``seshat_app`` role the app now connects as.

This test is also the drift tripwire: if a model is ever added without a matching
``init.sql`` table, ``create_all`` would attempt ``CREATE TABLE`` and fail here
under the DML-only role — exactly the signal we want.
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_init_db_succeeds_as_app_role() -> None:
    """``init_db()`` (create_all as seshat_app) raises nothing against the test DB."""
    from personal_agent.service.database import init_db

    await init_db()
