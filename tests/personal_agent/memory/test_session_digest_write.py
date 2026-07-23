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

import orjson
import pytest

from personal_agent.memory.models import SessionNode
from personal_agent.memory.service import MemoryService
from personal_agent.memory.session_digest import (
    DigestItem,
    SessionDigest,
    SummaryFailureReason,
)

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


# --------------------------------------------------------------------------
# AC-6 — the atomic conditional write
# --------------------------------------------------------------------------


def _digest() -> SessionDigest:
    return SessionDigest(
        decisions=[DigestItem(text="Deferred the reindex.", basis="user_statement")]
    )


@pytest.mark.asyncio
async def test_write_predicates_the_mutation_on_the_captured_ended_at() -> None:
    """The comparison and the mutation must be ONE statement.

    A re-read followed by an unconditional write leaves a TOCTOU window in which a
    new turn lands and a digest built from already-stale captures gets published.
    """
    service, captured = _make_service_with_mock(single_returns={"session_id": "sess-1"})

    await service.write_session_digest(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        generated_at=_ENDED_AT,
        turn_count=3,
        label="A label",
        digest=_digest(),
    )

    assert len(captured) == 1, "the check and the write must not be two statements"
    cypher, params = captured[0]
    assert "WHERE s.ended_at = $expected_ended_at" in cypher
    assert params["expected_ended_at"] == _ENDED_AT.isoformat()
    # And the mutation is in that same statement.
    assert "SET s.session_label" in cypher


@pytest.mark.asyncio
async def test_stale_writer_is_refused() -> None:
    """AC-6: the loser's write is REFUSED, not merely overwritten.

    This is what discriminates the implementation. A read-then-write would return
    True here — the MATCH would find the session and set it — so asserting on the
    return value distinguishes atomic refusal from a lucky ordering, which merely
    observing a surviving property value cannot do.
    """
    # single() returns None => the MATCH matched nothing => the predicate refused.
    service, _ = _make_service_with_mock(single_returns=None)

    accepted = await service.write_session_digest(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        generated_at=_ENDED_AT,
        turn_count=3,
        label="A label",
        digest=_digest(),
    )

    assert accepted is False


@pytest.mark.asyncio
async def test_accepted_write_reports_true() -> None:
    service, _ = _make_service_with_mock(single_returns={"session_id": "sess-1"})

    accepted = await service.write_session_digest(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        generated_at=_ENDED_AT,
        turn_count=3,
        label="A label",
        digest=_digest(),
    )

    assert accepted is True


@pytest.mark.asyncio
async def test_digest_is_stored_as_a_json_string() -> None:
    """Neo4j node properties cannot hold nested maps."""
    service, captured = _make_service_with_mock(single_returns={"session_id": "sess-1"})

    await service.write_session_digest(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        generated_at=_ENDED_AT,
        turn_count=3,
        label="A label",
        digest=_digest(),
    )

    stored = captured[0][1]["digest"]
    assert isinstance(stored, str)
    assert orjson.loads(stored)["decisions"][0]["text"] == "Deferred the reindex."


@pytest.mark.asyncio
async def test_floor_skip_advances_freshness_through_the_same_predicate() -> None:
    """D-b: a below-floor skip is a completed projection with an empty result.

    It must advance freshness — otherwise a single-turn session is permanently dirty
    and AC-2 can never pass — but through the SAME conditional write, or a turn
    landing mid-skip would be marked clean.
    """
    service, captured = _make_service_with_mock(single_returns={"session_id": "sess-1"})

    accepted = await service.mark_session_projection_clean(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        generated_at=_ENDED_AT,
    )

    assert accepted is True
    cypher, params = captured[0]
    assert "WHERE s.ended_at = $expected_ended_at" in cypher
    assert params["generated_at"] == _ENDED_AT.isoformat()


@pytest.mark.asyncio
async def test_floor_skip_write_is_refused_when_ended_at_moved() -> None:
    """The race codex flagged: a second turn landing mid-skip must refuse the skip."""
    service, _ = _make_service_with_mock(single_returns=None)

    accepted = await service.mark_session_projection_clean(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        generated_at=_ENDED_AT,
    )

    assert accepted is False


@pytest.mark.asyncio
async def test_marking_clean_never_touches_the_stored_digest() -> None:
    """The regression the pre-PR review caught: this is the clobber bug in a new field.

    A session digested weeks ago, resumed today after retention purged its old
    captures, reads below the floor. Writing label/digest=None here would erase a
    perfectly good digest — exactly what ADR-0124 exists to stop `session_summary`
    doing, reintroduced via `session_label`/`session_digest`.
    """
    service, captured = _make_service_with_mock(single_returns={"session_id": "sess-1"})

    await service.mark_session_projection_clean(
        "sess-1", expected_ended_at=_ENDED_AT, generated_at=_ENDED_AT
    )

    cypher, params = captured[0]
    for untouched in ("session_label", "session_digest"):
        assert f"s.{untouched} =" not in cypher
        assert untouched not in params


@pytest.mark.asyncio
async def test_unregenerable_session_keeps_its_turn_count() -> None:
    """A read that found nothing is not evidence the session had no turns.

    Measured on the live graph: all 59 multi-turn sessions have zero captures on
    disk. Writing `turn_count=0` for them would destroy the correct value for every
    one — and AC-7 could not catch it, because AC-7 compares `turn_count` against a
    recount and this write would have corrupted both sides.
    """
    service, captured = _make_service_with_mock(single_returns={"session_id": "sess-1"})

    await service.mark_session_projection_clean(
        "sess-1", expected_ended_at=_ENDED_AT, generated_at=_ENDED_AT
    )

    cypher, params = captured[0]
    assert "s.turn_count" not in cypher
    assert "turn_count" not in params
    # Freshness still advances, or the session is re-swept forever.
    assert "s.summary_generated_at = $generated_at" in cypher


@pytest.mark.asyncio
async def test_marking_clean_still_clears_prior_failure_state() -> None:
    """A session that failed, then became unregenerable, must not look terminally failed."""
    service, captured = _make_service_with_mock(single_returns={"session_id": "sess-1"})

    await service.mark_session_projection_clean(
        "sess-1", expected_ended_at=_ENDED_AT, generated_at=_ENDED_AT
    )

    cypher = captured[0][0]
    assert "s.summary_failure_reason = null" in cypher
    assert "s.summary_attempt_count = 0" in cypher


# --------------------------------------------------------------------------
# AC-4 — a failure is inert and loud
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_is_inert_and_loud() -> None:
    """AC-4's four-way assertion, at the write layer.

    Stored digest and label unchanged; freshness does not advance; a failure event
    is emitted; the session stays eligible for retry.
    """
    service, captured = _make_service_with_mock(single_returns={"attempts": 1})

    recorded = await service.record_session_summary_failure(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        failure_reason=SummaryFailureReason.BUDGET_DENIED.value,
    )

    assert recorded is True
    cypher, params = captured[0]
    # Inert: the artifacts and the freshness stamp are untouched.
    for untouched in ("session_label", "session_digest", "summary_generated_at"):
        assert f"s.{untouched} =" not in cypher
    # Loud + retryable: the reason is stored and the attempt counter advances.
    assert "s.summary_failure_reason = $failure_reason" in cypher
    assert "s.summary_attempt_count = coalesce(s.summary_attempt_count, 0) + 1" in cypher
    assert params["failure_reason"] == "budget_denied"


@pytest.mark.asyncio
async def test_failure_record_is_also_predicated_on_ended_at() -> None:
    """A failure record must not clobber a concurrent successful write."""
    service, captured = _make_service_with_mock(single_returns={"attempts": 1})

    await service.record_session_summary_failure(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        failure_reason=SummaryFailureReason.MODEL_ERROR.value,
    )

    assert "WHERE s.ended_at = $expected_ended_at" in captured[0][0]


@pytest.mark.asyncio
async def test_a_successful_write_clears_prior_failure_state() -> None:
    """Otherwise a session that recovers still looks terminally failed."""
    service, captured = _make_service_with_mock(single_returns={"session_id": "sess-1"})

    await service.write_session_digest(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        generated_at=_ENDED_AT,
        turn_count=3,
        label="A label",
        digest=_digest(),
    )

    cypher = captured[0][0]
    assert "s.summary_failure_reason = null" in cypher
    assert "s.summary_attempt_count = 0" in cypher


# --------------------------------------------------------------------------
# AC-7 — turn_count is written from a recount
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_count_is_written_from_the_recount() -> None:
    """AC-7: turn_count on a swept session equals a recount from its captures."""
    service, captured = _make_service_with_mock(single_returns={"session_id": "sess-1"})

    await service.write_session_digest(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        generated_at=_ENDED_AT,
        turn_count=7,
        label="A label",
        digest=_digest(),
    )

    assert captured[0][1]["turn_count"] == 7
    assert "s.turn_count = $turn_count" in captured[0][0]


# --------------------------------------------------------------------------
# AC-2 — the dirty-and-idle scan
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dirty_scan_includes_the_is_null_disjunct() -> None:
    """AC-2 names this explicitly.

    In Cypher a comparison against NULL yields NULL and the row is silently dropped,
    so a never-summarised session escapes a bare `<` scan — the exact sessions the
    check exists to find.
    """
    service, captured = _make_service_with_mock()
    service.driver.session.return_value.__aenter__.return_value.run.return_value.data = AsyncMock(
        return_value=[]
    )

    await service.find_dirty_idle_sessions(idle_threshold_seconds=900.0, max_attempts=2)

    cypher = captured[0][0]
    assert "s.summary_generated_at IS NULL" in cypher
    assert "s.summary_generated_at < s.ended_at" in cypher


@pytest.mark.asyncio
async def test_dirty_scan_excludes_only_terminal_failures() -> None:
    """Transient reasons must keep coming back; deterministic ones may go terminal."""
    service, captured = _make_service_with_mock()
    service.driver.session.return_value.__aenter__.return_value.run.return_value.data = AsyncMock(
        return_value=[]
    )

    await service.find_dirty_idle_sessions(idle_threshold_seconds=900.0, max_attempts=2)

    cypher, params = captured[0]
    assert "NOT (s.summary_failure_reason IN $terminal_reasons" in cypher
    assert "coalesce(s.summary_attempt_count, 0) >= $max_attempts" in cypher
    assert "budget_denied" not in params["terminal_reasons"], (
        "a budget denial is transient by nature and must never be terminal"
    )
    assert "oversized_input" in params["terminal_reasons"]
