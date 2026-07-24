"""Live-Neo4j behavioural proof of the ADR-0124 Phase 1 read path (FRE-948).

Marked ``integration`` (out of ``make test``); runs against the isolated test Neo4j
(:7688). Writes a real digest through the actual Phase-0 write path
(``write_session_digest``, not a hand-crafted node), then reads it back through
``get_session_digest_views`` — the one test in this ticket that proves the full
real shape round-trips, not just that each layer's mock agrees with itself. Also
proves ``ensure_session_id_index()`` succeeds against the live substrate (mirrors
``test_entity_class_persistence_live.py::test_ensure_entity_class_index_succeeds``).
"""

from __future__ import annotations

# ruff: noqa: D103
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from personal_agent.memory.models import SessionNode
from personal_agent.memory.service import MemoryService
from personal_agent.memory.session_digest import DigestItem, SessionDigest, render_digest

pytestmark = pytest.mark.integration

_STARTED_AT = datetime(2026, 7, 24, 9, 0, 0, tzinfo=timezone.utc)
_ENDED_AT = datetime(2026, 7, 24, 9, 15, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def svc():
    service = MemoryService()  # fre-375-allow: integration test, skips when Neo4j unavailable
    if not await service.connect():
        pytest.skip("Neo4j not available (make test-infra-up)")
    yield service
    await service.disconnect()


async def _create_and_publish(
    service: MemoryService, session_id: str, *, label: str, digest: SessionDigest
) -> None:
    assert service.driver is not None
    await service.create_session(
        SessionNode(
            session_id=session_id,
            started_at=_STARTED_AT,
            ended_at=_ENDED_AT,
            turn_count=3,
        )
    )
    accepted = await service.write_session_digest(
        session_id,
        expected_ended_at=_ENDED_AT,
        generated_at=datetime.now(timezone.utc),
        turn_count=3,
        label=label,
        digest=digest,
    )
    assert accepted is True


async def _delete_session(service: MemoryService, session_id: str) -> None:
    assert service.driver is not None
    async with service.driver.session() as s:
        await s.run("MATCH (s:Session {session_id: $sid}) DETACH DELETE s", sid=session_id)


@pytest.mark.asyncio
async def test_round_trips_a_real_written_digest(svc: MemoryService) -> None:
    """The full real shape — write via the Phase-0 path, read via this ticket's method."""
    session_id = f"FRE948_{uuid4()}"
    digest = SessionDigest(
        established=[
            DigestItem(text="Neo4j backs the knowledge graph.", basis="assistant_reasoning")
        ],
    )

    try:
        await _create_and_publish(svc, session_id, label="A real generated label", digest=digest)

        views = await svc.get_session_digest_views([session_id])

        assert views[session_id].label == "A real generated label"
        assert views[session_id].digest_text == render_digest(digest)
    finally:
        await _delete_session(svc, session_id)


@pytest.mark.asyncio
async def test_session_with_no_digest_written_is_absent(svc: MemoryService) -> None:
    session_id = f"FRE948_{uuid4()}"

    try:
        await svc.create_session(
            SessionNode(
                session_id=session_id, started_at=_STARTED_AT, ended_at=_ENDED_AT, turn_count=1
            )
        )

        views = await svc.get_session_digest_views([session_id])

        assert session_id not in views
    finally:
        await _delete_session(svc, session_id)


@pytest.mark.asyncio
async def test_ensure_session_id_index_succeeds(svc: MemoryService) -> None:
    assert await svc.ensure_session_id_index() is True
