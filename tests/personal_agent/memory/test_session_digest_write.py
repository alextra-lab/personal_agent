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

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import orjson
import pytest

from personal_agent.memory.models import SessionNode
from personal_agent.memory.service import MemoryService
from personal_agent.memory.session_digest import (
    DigestItem,
    SessionDigest,
    SessionWriteResult,
    SummaryFailureReason,
)

_ENDED_AT = datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc)


def _flat(cypher: str) -> str:
    """Collapse the statement's wrapping so shape assertions survive reformatting."""
    return " ".join(cypher.split())


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

    assert accepted is SessionWriteResult.REFUSED


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

    assert accepted is SessionWriteResult.ACCEPTED


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

    assert accepted is SessionWriteResult.ACCEPTED
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

    assert accepted is SessionWriteResult.REFUSED


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
    service, captured = _make_service_with_mock(
        single_returns={"attempts": 1, "evidence_attempts": 0}
    )

    recorded = await service.record_session_summary_failure(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        failure_reason=SummaryFailureReason.MODEL_ERROR.value,
    )

    assert recorded is SessionWriteResult.ACCEPTED
    cypher, params = captured[0]
    # Inert: the artifacts and the freshness stamp are untouched.
    for untouched in ("session_label", "session_digest", "summary_generated_at"):
        assert f"s.{untouched} =" not in cypher
    # Loud + retryable: the reason is stored and the attempt counter advances. Asserted
    # on a reason that reached the model — FRE-987 stopped budget denials spending the
    # shared counter, since nothing was ever sent for them to be evidence about.
    assert "s.summary_failure_reason = $failure_reason" in cypher
    assert _flat(cypher).count("coalesce(s.summary_attempt_count, 0) + (CASE WHEN") == 1
    assert params["spend_attempt"] is True
    assert params["failure_reason"] == "model_error"


@pytest.mark.asyncio
async def test_failure_record_is_also_predicated_on_ended_at() -> None:
    """A failure record must not clobber a concurrent successful write."""
    service, captured = _make_service_with_mock(
        single_returns={"attempts": 1, "evidence_attempts": 0}
    )

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


# --------------------------------------------------------------------------
# FRE-992 — unreadable evidence is bounded on its own counter
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_unavailable_is_not_a_shared_counter_terminal_reason() -> None:
    """It must be excluded on its OWN counter, not the shared attempt count.

    ``summary_attempt_count`` is spent by every failure reason while terminality tests
    only the *current* one. Listing this reason among the terminal-eligible ones would
    let two unrelated model errors retire a session on its first unreadable sweep —
    permanently writing it off over a transient outage, which is the FRE-992 defect.
    """
    service, captured = _make_service_with_mock()
    service.driver.session.return_value.__aenter__.return_value.run.return_value.data = AsyncMock(
        return_value=[]
    )

    await service.find_dirty_idle_sessions(idle_threshold_seconds=900.0, max_attempts=2)

    cypher, params = captured[0]
    assert SummaryFailureReason.EVIDENCE_UNAVAILABLE.value not in params["terminal_reasons"]
    assert "coalesce(s.summary_evidence_failure_count, 0) < $max_attempts" in cypher


@pytest.mark.asyncio
async def test_dirty_scan_reports_a_turn_node_count_not_the_turn_count_property() -> None:
    """``s.turn_count`` is overwritten with one consolidation batch's count.

    ``Turn`` nodes are MERGE-d one per capture and accumulate, so counting them yields
    a lower bound on the turns that genuinely existed — the only signal that can prove
    evidence is missing without ever falsely accusing a complete read.
    """
    service, captured = _make_service_with_mock()
    service.driver.session.return_value.__aenter__.return_value.run.return_value.data = AsyncMock(
        return_value=[]
    )

    await service.find_dirty_idle_sessions(idle_threshold_seconds=900.0, max_attempts=2)

    cypher = captured[0][0]
    assert "OPTIONAL MATCH (t:Turn {session_id: s.session_id})" in cypher
    assert "count(t) AS graph_turn_count" in cypher
    assert "s.turn_count AS" not in cypher, "the batch-local property is not the oracle"
    # The page is cut BEFORE the per-session Turn count, or the count runs once per
    # candidate rather than once per returned row — O(dirty backlog x total turns)
    # every sweep interval.
    assert cypher.index("LIMIT $limit") < cypher.index("OPTIONAL MATCH"), (
        "LIMIT must precede the aggregation, not follow it"
    )


@pytest.mark.asyncio
async def test_only_an_evidence_failure_bumps_the_evidence_counter() -> None:
    service, captured = _make_service_with_mock(
        single_returns={"attempts": 1, "evidence_attempts": 1}
    )

    await service.record_session_summary_failure(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        failure_reason=SummaryFailureReason.EVIDENCE_UNAVAILABLE.value,
        evidence_failure=True,
    )
    await service.record_session_summary_failure(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        failure_reason=SummaryFailureReason.MODEL_ERROR.value,
    )

    assert captured[0][1]["evidence_failure"] is True
    assert captured[1][1]["evidence_failure"] is False
    # One statement, branching on the flag — so the shared counter always advances
    # while the evidence counter advances only for its own reason.
    assert "CASE WHEN $evidence_failure THEN 1 ELSE 0 END" in captured[0][0]


@pytest.mark.asyncio
async def test_both_success_paths_clear_the_evidence_counter() -> None:
    """A session that becomes readable again must be fully rehabilitated."""
    service, captured = _make_service_with_mock(single_returns={"session_id": "sess-1"})

    await service.write_session_digest(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        generated_at=_ENDED_AT,
        turn_count=3,
        label="A label",
        digest=_digest(),
    )
    await service.mark_session_projection_clean(
        "sess-1", expected_ended_at=_ENDED_AT, generated_at=_ENDED_AT
    )

    for cypher, _ in captured:
        assert "s.summary_evidence_failure_count = 0" in cypher


@pytest.mark.asyncio
async def test_new_turns_restore_a_written_off_sessions_retry_budget() -> None:
    """Otherwise the exclusion is self-reinforcing and permanent.

    An excluded session is never selected again, and the only other reset points are a
    successful read or clean-mark — the very things the exclusion prevents. New turns
    mean new captures, so the evidence situation has materially changed and the session
    earns a fresh bound. Without this, a brief store outage retires a session that goes
    on to receive many more real turns.
    """
    service, captured = _make_service_with_mock()

    await service.create_session(_session_node(), trace_id="t-1")

    cypher = captured[0][0]
    assert "s.summary_evidence_failure_count = 0" in cypher
    # Still no encroachment on the sweep's own fields (ADR-0124 D1).
    assert "summary_generated_at" not in cypher
    assert "summary_failure_reason" not in cypher


# --------------------------------------------------------------------------
# FRE-987 — retry pacing at the write layer
# --------------------------------------------------------------------------


def _make_failing_service() -> MemoryService:
    """A connected service whose driver raises on every statement.

    Models the case the accept/refuse bool could not express: the graph is reachable
    enough to have been marked connected, but the write itself blows up.
    """
    service = MemoryService.__new__(MemoryService)
    service.connected = True

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(side_effect=RuntimeError("neo4j is having a day"))
    service.driver = MagicMock()
    service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return service


@pytest.mark.asyncio
async def test_failure_record_stores_the_retry_stamp() -> None:
    """A failure records WHEN it may next be attempted, not only that it failed."""
    service, captured = _make_service_with_mock(
        single_returns={"attempts": 1, "evidence_attempts": 0}
    )
    retry_after = datetime(2026, 7, 24, 0, 0, 0, tzinfo=timezone.utc)

    await service.record_session_summary_failure(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        failure_reason=SummaryFailureReason.BUDGET_DENIED.value,
        retry_after=retry_after,
    )

    cypher, params = captured[0]
    assert "s.summary_retry_after = coalesce($retry_after, s.summary_retry_after)" in _flat(cypher)
    assert params["retry_after"] == retry_after.isoformat()


@pytest.mark.asyncio
async def test_the_retry_stamp_is_normalised_to_utc() -> None:
    """Eligibility compares ISO strings lexicographically, as `ended_at` already does.

    That ordering is only sound if every stored instant carries the same offset, so the
    normalisation is done at the boundary rather than assumed of every caller: a stamp
    written as +02:00 would sort as if it were two hours later than it is, and the
    session would be released early.
    """
    service, captured = _make_service_with_mock(
        single_returns={"attempts": 1, "evidence_attempts": 0}
    )
    cest = timezone(timedelta(hours=2))

    await service.record_session_summary_failure(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        failure_reason=SummaryFailureReason.MODEL_ERROR.value,
        retry_after=datetime(2026, 7, 24, 2, 0, 0, tzinfo=cest),
    )

    assert captured[0][1]["retry_after"] == "2026-07-24T00:00:00+00:00"


@pytest.mark.asyncio
async def test_a_budget_denial_does_not_spend_the_shared_retry_budget() -> None:
    """FRE-987: a denial never reached the model, so it is not evidence about the session.

    `summary_attempt_count` is shared across every reason while terminality tests only
    the current one. A week of denials would otherwise leave a session that its FIRST
    genuine deterministic failure retires — the shared-counter hazard FRE-992 removed
    for evidence failures, arriving through the other transient door.
    """
    service, captured = _make_service_with_mock(
        single_returns={"attempts": 0, "evidence_attempts": 0}
    )

    await service.record_session_summary_failure(
        "sess-1",
        expected_ended_at=_ENDED_AT,
        failure_reason=SummaryFailureReason.BUDGET_DENIED.value,
        spend_attempt=False,
    )

    cypher, params = captured[0]
    assert params["spend_attempt"] is False
    # One statement branching on the flag, so the reason and the stamp are still stored.
    assert "CASE WHEN $spend_attempt THEN 1 ELSE 0 END" in _flat(cypher)
    assert "s.summary_failure_reason = $failure_reason" in cypher


@pytest.mark.asyncio
async def test_the_dirty_scan_holds_a_stamped_session_until_its_instant_passes() -> None:
    """The predicate that turns 288 attempts a day into a bounded few.

    Carries the explicit IS NULL disjunct for the same reason AC-2 demands one on
    freshness: a NULL comparison yields NULL, and every session that has never failed
    would silently drop out of the scan.
    """
    service, captured = _make_service_with_mock()
    service.driver.session.return_value.__aenter__.return_value.run.return_value.data = AsyncMock(
        return_value=[]
    )

    await service.find_dirty_idle_sessions(idle_threshold_seconds=900.0, max_attempts=2)

    cypher, params = captured[0]
    assert "s.summary_retry_after IS NULL OR s.summary_retry_after <= $now" in _flat(cypher)
    assert params["now"].endswith("+00:00")


@pytest.mark.asyncio
async def test_the_dirty_scan_returns_the_attempt_count() -> None:
    """The sweep sizes the next backoff from it, so it must come back with the row."""
    service, captured = _make_service_with_mock()
    service.driver.session.return_value.__aenter__.return_value.run.return_value.data = AsyncMock(
        return_value=[]
    )

    await service.find_dirty_idle_sessions(idle_threshold_seconds=900.0, max_attempts=2)

    assert "AS summary_attempt_count" in captured[0][0]


@pytest.mark.asyncio
async def test_every_rehabilitating_write_clears_the_retry_stamp() -> None:
    """A recovered session must not sit out a cooldown earned by a failure it survived.

    Includes `create_session`: new turns are new input, so the condition that failed may
    simply not exist any more — and holding the session for up to six hours would delay
    the digest the user is waiting on.
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
    await service.mark_session_projection_clean(
        "sess-1", expected_ended_at=_ENDED_AT, generated_at=_ENDED_AT
    )
    await service.create_session(_session_node(), trace_id="t-1")

    for cypher, _ in captured:
        assert "s.summary_retry_after = null" in cypher


@pytest.mark.asyncio
async def test_a_broken_graph_is_distinguishable_from_a_refused_write() -> None:
    """The Critical finding from the FRE-987 plan review.

    Both used to return False, so the sweep could not tell "a turn landed, come back
    next quiet period" from "nothing was persisted at all". The second is the expensive
    one: the model call was already paid for, nothing recorded it, and the session is
    still dirty on the next 300-second tick.
    """
    service = _make_failing_service()

    assert (
        await service.write_session_digest(
            "sess-1",
            expected_ended_at=_ENDED_AT,
            generated_at=_ENDED_AT,
            turn_count=3,
            label="A label",
            digest=_digest(),
        )
        is SessionWriteResult.UNAVAILABLE
    )
    assert (
        await service.mark_session_projection_clean(
            "sess-1", expected_ended_at=_ENDED_AT, generated_at=_ENDED_AT
        )
        is SessionWriteResult.UNAVAILABLE
    )
    assert (
        await service.record_session_summary_failure(
            "sess-1",
            expected_ended_at=_ENDED_AT,
            failure_reason=SummaryFailureReason.MODEL_ERROR.value,
        )
        is SessionWriteResult.UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_a_disconnected_graph_reports_unavailable_not_refused() -> None:
    """Same distinction on the other no-write path."""
    service = MemoryService.__new__(MemoryService)
    service.connected = False
    service.driver = None

    assert (
        await service.mark_session_projection_clean(
            "sess-1", expected_ended_at=_ENDED_AT, generated_at=_ENDED_AT
        )
        is SessionWriteResult.UNAVAILABLE
    )
