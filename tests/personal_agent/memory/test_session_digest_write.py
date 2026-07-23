"""Mocked-driver unit tests for the ADR-0124 Phase 0 session write path (FRE-947).

Covers:

* **The clobber fix (S1, hard prerequisite).** ``create_session`` must stop owning
  ``session_summary``. Today it sets the field unconditionally on every session
  MERGE, and the generator returns ``None`` on budget denial / timeout / model
  error — so a transient failure erases the previously good summary. Until this
  lands, D2's "fail loudly on oversized input" policy means *fail by deleting*.
* **AC-6** — the atomic conditional write: the comparison against the captured
  ``ended_at`` and the mutation must be the same Cypher statement, and the loser
  must be *refused* (not merely overwritten).
* **AC-4** — a generation failure is inert and loud.
* **AC-7** — ``turn_count`` is written from a recount of the session's captures.

These lock the emitted Cypher shape and the accept/refuse contract without a live
Neo4j; the genuine two-writer concurrency proof is the integration test in
``test_session_digest_write_live.py``.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.memory.models import SessionNode
from personal_agent.memory.service import MemoryService

_ENDED_AT = datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc)
_STARTED_AT = datetime(2026, 7, 23, 9, 0, 0, tzinfo=timezone.utc)


def _make_service_with_mock(
    *, single_returns: object = None
) -> tuple[MemoryService, list[tuple[str, dict]]]:
    """Build a MemoryService whose driver captures every Cypher statement.

    Args:
        single_returns: What ``result.single()`` resolves to. ``None`` models a
            statement whose MATCH matched nothing — i.e. a refused conditional write.

    Returns:
        The service and the list of ``(cypher, params)`` pairs it ran.
    """
    service = MemoryService.__new__(MemoryService)
    service.connected = True

    captured: list[tuple[str, dict]] = []
    result = AsyncMock()
    result.single = AsyncMock(return_value=single_returns)

    async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
        captured.append((cypher, dict(kwargs)))
        return result

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(side_effect=capture_run)
    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return service, captured


def _session_node() -> SessionNode:
    return SessionNode(
        session_id="sess-1",
        started_at=_STARTED_AT,
        ended_at=_ENDED_AT,
        turn_count=3,
        dominant_entities=["neo4j"],
    )


# --------------------------------------------------------------------------
# S1 — the clobber fix (prerequisite)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_does_not_write_session_summary() -> None:
    """create_session must not own session_summary (ADR-0124 D1, clobber fix).

    The per-turn session MERGE setting the field unconditionally is what turns a
    transient generation failure into data loss.
    """
    service, captured = _make_service_with_mock()

    await service.create_session(_session_node(), trace_id="t-1")

    assert captured, "expected a MERGE statement"
    cypher, params = captured[0]
    assert "session_summary" not in cypher
    assert "session_summary" not in params
    # The properties it legitimately owns are still written.
    assert "s.turn_count = $turn_count" in cypher
    assert "s.ended_at = $ended_at" in cypher


@pytest.mark.asyncio
async def test_create_session_does_not_write_digest_fields() -> None:
    """The digest, label and freshness stamp belong to the sweep, not the turn path.

    If the per-turn MERGE touched them, the next turn after a sweep would NULL the
    fresh digest — the same clobber in a new field.
    """
    service, captured = _make_service_with_mock()

    await service.create_session(_session_node(), trace_id="t-1")

    cypher, params = captured[0]
    for owned_by_the_sweep in (
        "session_label",
        "session_digest",
        "summary_generated_at",
        "summary_failure_reason",
        "summary_attempt_count",
    ):
        assert owned_by_the_sweep not in cypher
        assert owned_by_the_sweep not in params
