# ruff: noqa: D101, D102
"""The attach snapshot query on the real ``dev_jobs`` table (ADR-0159 D10.6; FRE-1688).

It runs on the test stack through ``AsyncSessionLocal``, the session that the attach replay uses,
so a passing test also proves the gateway role's grants on ``dev_jobs`` and ``dev_job_spend``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from personal_agent.dev_agent.job_card import CardContext, JobCardStore
from personal_agent.service.database import AsyncSessionLocal, engine

from .conftest import MakeJob

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CONTEXT = CardContext(cap_usd=Decimal("2.36"), repo_base_url="https://github.com/example-org")


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine_connections():  # type: ignore[no-untyped-def]
    """Each test has its own event loop, so drop the shared engine's pooled connections after it."""
    yield
    await engine.dispose()


class TestLatest:
    async def test_an_ended_job_gives_its_end_card_with_the_spend_of_its_row(
        self, admin_pool: asyncpg.Pool, make_job: MakeJob
    ) -> None:
        job = await make_job()
        artifact = uuid4()
        check = {"passed": False, "steps": [{"name": "load", "passed": False}]}
        await admin_pool.execute(
            """
            UPDATE dev_jobs SET state = 'failed_check', failure_reason = 'check_failed',
                   spend_usd = 0.250000, ended_at = now(), repo = 'page-abc',
                   result_artifact_id = $2, main_moved = false, check_result = $3::jsonb
             WHERE id = $1
            """,
            job.id,
            artifact,
            json.dumps(check),
        )
        row_spend = await admin_pool.fetchval(
            "SELECT spend_usd::text FROM dev_jobs WHERE id = $1", job.id
        )

        async with AsyncSessionLocal() as db:
            card = await JobCardStore(db, CONTEXT).latest(job.session_id)

        assert card is not None
        assert (card["event"], card["state"], card["ended"]) == ("end", "failed_check", True)
        assert card["spend_usd"] == row_spend == "0.250000"
        assert card["cap_usd"] == "2.36"
        assert card["failure_reason"] == "check_failed"
        assert card["check_result"] == check
        assert card["repo_url"] == "https://github.com/example-org/page-abc"
        assert card["result_artifact_id"] == str(artifact)
        assert card["main_moved"] is False
        assert card["step"] is None

    async def test_a_running_job_gives_the_committed_spend_so_far(
        self, admin_pool: asyncpg.Pool, make_job: MakeJob
    ) -> None:
        job = await make_job()
        await admin_pool.execute("UPDATE dev_jobs SET state = 'running' WHERE id = $1", job.id)
        await admin_pool.execute(
            "INSERT INTO dev_job_spend (job_id, committed_usd) VALUES ($1, 0.100000)", job.id
        )
        async with AsyncSessionLocal() as db:
            card = await JobCardStore(db, CONTEXT).latest(job.session_id)
        assert card is not None
        assert (card["event"], card["state"], card["ended"]) == ("state", "running", False)
        assert card["spend_usd"] == "0.100000"

    async def test_the_newest_job_of_the_session_wins(
        self, admin_pool: asyncpg.Pool, make_job: MakeJob
    ) -> None:
        session_id = uuid4()
        first = await make_job(session_id=session_id)
        await admin_pool.execute(
            "UPDATE dev_jobs SET state = 'succeeded', ended_at = now() WHERE id = $1", first.id
        )
        second = await make_job(session_id=session_id)
        async with AsyncSessionLocal() as db:
            card = await JobCardStore(db, CONTEXT).latest(session_id)
        assert card is not None
        assert card["job_id"] == str(second.id)

    async def test_a_session_with_no_job_gives_none(self, admin_pool: asyncpg.Pool) -> None:
        async with AsyncSessionLocal() as db:
            assert await JobCardStore(db, CONTEXT).latest(uuid4()) is None
