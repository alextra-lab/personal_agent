"""Mocked-driver unit tests for assert_stance / assert_claim Cypher (FRE-638).

Same pattern as test_neo4j_origination_properties.py — capture the emitted Cypher
and params against a mock driver so the shape (owner sentinel, HAS_STANCE / HAS_FACT,
bitemporal props, supersession) is locked without a live Neo4j. Behavioural proof of
the ACs is in the live-Neo4j test_claims_stance_storage.py (integration).
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.memory.models import Claim, Stance
from personal_agent.memory.service import MemoryService

_NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


class _AsyncRows:
    """Async-iterable stand-in for a Neo4j result cursor."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def __aiter__(self):
        async def gen():
            for row in self._rows:
                yield row

        return gen()


def _service_capturing(
    *, current_rows: list[dict] | None = None
) -> tuple[MemoryService, list[tuple[str, dict]]]:
    """MemoryService whose mock driver captures every run() call.

    The first run() (the assert_claim candidate fetch) returns ``current_rows``
    as an async cursor; every other run() returns a single record.
    """
    service = MemoryService.__new__(MemoryService)
    service.connected = True

    captured: list[tuple[str, dict]] = []
    calls = {"n": 0}

    async def capture_run(cypher: str, **kwargs: object):
        captured.append((cypher, dict(kwargs)))
        calls["n"] += 1
        if calls["n"] == 1 and current_rows is not None:
            return _AsyncRows(current_rows)
        result = AsyncMock()
        result.single = AsyncMock(return_value={"superseded": 0, "invalidated": 0, "claim_id": "x"})
        return result

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.run = capture_run

    mock_driver = AsyncMock()
    mock_driver.session = lambda: mock_session
    service.driver = mock_driver
    return service, captured


@pytest.mark.asyncio
async def test_assert_stance_emits_owner_sentinel_and_native_edge() -> None:
    service, captured = _service_capturing()
    stance = Stance(
        target="Toyota RAV4 Hybrid",
        affect="loves the hybrid powertrain",
        mastery=None,
        observed_at=_NOW,
        extracted_at=_NOW,
        trace_id="trace-1",
        session_id="sess-1",
    )

    ok = await service.assert_stance(stance, trace_id="trace-1")

    assert ok is True
    cypher, params = captured[0]
    # Owner sentinel resolves to the is_owner Person node (ADR-0052).
    assert "Person {is_owner: true}" in cypher
    # Native HAS_STANCE edge to the World :Entity (Core-unified, AC-5).
    assert "HAS_STANCE" in cypher
    assert "MATCH (c:Entity {name: $target})" in cypher
    # Prior current stance to the same concept is superseded, not deleted.
    assert "cur.valid_to IS NULL AND cur.invalid_at IS NULL" in cypher
    assert params["target"] == "Toyota RAV4 Hybrid"
    assert params["valid_from"] == _NOW.isoformat()


@pytest.mark.asyncio
async def test_assert_claim_fresh_creates_current_claim() -> None:
    service, captured = _service_capturing(current_rows=[])  # no current claims
    claim = Claim(
        content="The user's lease ends in March.",
        confidence=0.8,
        observed_at=_NOW,
        facet="lease_end_date",
        update_kind="correction",
    )

    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(return_value=[1.0, 0.0]),
    ):
        claim_id = await service.assert_claim(claim, trace_id="trace-1")

    assert claim_id  # non-empty id returned
    # First run() is the candidate fetch; second is the write.
    fetch_cypher, _ = captured[0]
    write_cypher, write_params = captured[1]
    assert "HAS_FACT" in fetch_cypher and "cl.valid_to IS NULL" in fetch_cypher
    assert "cl.facet AS facet" in fetch_cypher  # FRE-712: facet fetched for matching
    assert "CREATE (o)-[:HAS_FACT]->(cl:Claim" in write_cypher
    # Fresh claim is current: both temporal bounds null, nothing superseded.
    assert write_params["new_valid_to"] is None
    assert write_params["new_invalid_at"] is None
    assert write_params["supersede_ids"] == []
    assert write_params["valid_from"] == _NOW.isoformat()
    # FRE-712: facet + update_kind stored on the new Claim.
    assert write_params["facet"] == "lease_end_date"
    assert write_params["update_kind"] == "correction"


@pytest.mark.asyncio
async def test_assert_claim_skips_when_owner_absent() -> None:
    # Write returns no record → owner :Person {is_owner:true} does not exist.
    service, captured = _service_capturing(current_rows=[])

    async def capture_run(cypher: str, **kwargs: object):
        captured.append((cypher, dict(kwargs)))
        if len(captured) == 1:
            return _AsyncRows([])
        result = AsyncMock()
        result.single = AsyncMock(return_value=None)
        return result

    service.driver.session().run = capture_run
    claim = Claim(content="x", observed_at=_NOW)
    with patch(
        "personal_agent.memory.service.generate_embedding",
        new=AsyncMock(return_value=[1.0, 0.0]),
    ):
        claim_id = await service.assert_claim(claim)
    assert claim_id == ""
