"""Unit tests for the flat per-substrate projection (FRE-550 / ADR-0074).

Covers :class:`SubstrateResultDoc` and the
:func:`substrate_docs_from_result` factory that flattens a run-level
:class:`ResultDoc` into one doc per ``(run_id, substrate)`` so legacy Kibana
aggregation visualizations can break joinability detail down by substrate
(``nested`` ``orphans`` / ``substrate_checks`` can't be aggregated by legacy
vizzes).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from personal_agent.observability.joinability.result import (
    Orphan,
    ResultDoc,
    SubstrateCheck,
    SubstrateResultDoc,
    substrate_docs_from_result,
)

NOW = datetime(2026, 6, 20, 14, 0, 0, tzinfo=timezone.utc)


def _check(
    substrate: str,
    status: str = "green",
    expected: str = "conditional",
    observed_count: int = 1,
    duration_ms: float = 1.5,
    error: str | None = None,
) -> SubstrateCheck:
    return SubstrateCheck(
        substrate=substrate,
        expected=expected,  # type: ignore[arg-type]
        observed_count=observed_count,
        status=status,  # type: ignore[arg-type]
        duration_ms=duration_ms,
        error=error,
    )


def _orphan(substrate: str, severity: str = "red") -> Orphan:
    return Orphan(
        substrate=substrate,
        kind="missing_identity",
        detail={"row_id": 42},
        severity=severity,  # type: ignore[arg-type]
    )


def _result(
    *,
    checks: list[SubstrateCheck],
    orphans: list[Orphan] | None = None,
    outcome: str = "green",
    run_id: str = "run-1",
    sampled_session_id: str | None = "00000000-0000-0000-0000-000000000001",
) -> ResultDoc:
    return ResultDoc(
        run_id=run_id,
        started_at=NOW,
        duration_ms=10.0,
        source="scheduler",
        window_hours=24,
        random_seed=0,
        sampled_session_id=sampled_session_id,
        substrate_checks=checks,
        orphans=orphans or [],
        outcome=outcome,  # type: ignore[arg-type]
        trace_id="trace-1",
    )


# -- factory ----------------------------------------------------------------


def test_substrate_docs_from_result_empty() -> None:
    """A ResultDoc with no substrate checks flattens to no docs."""
    assert substrate_docs_from_result(_result(checks=[])) == []


def test_substrate_docs_one_per_check() -> None:
    """N checks -> N docs, order preserved, run_id / started_at copied."""
    checks = [
        _check("postgres.sessions", status="green"),
        _check("elasticsearch.agent_logs", status="red"),
        _check("neo4j.turn", status="yellow"),
    ]
    docs = substrate_docs_from_result(_result(checks=checks))
    assert [d.substrate for d in docs] == [
        "postgres.sessions",
        "elasticsearch.agent_logs",
        "neo4j.turn",
    ]
    assert all(d.run_id == "run-1" for d in docs)
    assert all(d.started_at == NOW for d in docs)
    assert [d.status for d in docs] == ["green", "red", "yellow"]


def test_substrate_docs_match_orphans_by_substrate() -> None:
    """Orphan red/yellow counts tally per substrate from orphan severity."""
    checks = [_check("postgres.api_costs", status="red")]
    orphans = [
        _orphan("postgres.api_costs", "red"),
        _orphan("postgres.api_costs", "red"),
        _orphan("postgres.api_costs", "yellow"),
    ]
    (doc,) = substrate_docs_from_result(_result(checks=checks, orphans=orphans, outcome="red"))
    assert doc.orphan_count == 3
    assert doc.orphan_red_count == 2
    assert doc.orphan_yellow_count == 1


def test_substrate_docs_check_with_zero_orphans() -> None:
    """A checked substrate with no orphans still emits a doc, counts zero."""
    (doc,) = substrate_docs_from_result(_result(checks=[_check("postgres.sessions")]))
    assert doc.orphan_count == 0
    assert doc.orphan_red_count == 0
    assert doc.orphan_yellow_count == 0


def test_substrate_docs_mixed_orphan_and_clean() -> None:
    """One substrate has orphans, another is clean; both rows correct."""
    checks = [
        _check("postgres.api_costs", status="red"),
        _check("postgres.sessions", status="green"),
    ]
    orphans = [_orphan("postgres.api_costs", "red")]
    docs = substrate_docs_from_result(_result(checks=checks, orphans=orphans, outcome="red"))
    by_sub = {d.substrate: d for d in docs}
    assert by_sub["postgres.api_costs"].orphan_red_count == 1
    assert by_sub["postgres.sessions"].orphan_count == 0


def test_substrate_docs_orphan_substrate_not_in_checks_ignored() -> None:
    """An orphan whose substrate has no matching check produces no phantom doc."""
    checks = [_check("postgres.sessions", status="green")]
    orphans = [_orphan("neo4j.turn", "red")]  # no check for neo4j.turn
    docs = substrate_docs_from_result(_result(checks=checks, orphans=orphans))
    assert [d.substrate for d in docs] == ["postgres.sessions"]
    assert docs[0].orphan_count == 0


def test_substrate_docs_skipped_run() -> None:
    """A skipped run (no session, no checks) flattens to no docs."""
    result = _result(checks=[], outcome="skipped", sampled_session_id=None)
    assert substrate_docs_from_result(result) == []


def test_substrate_docs_substrate_uniqueness_holds_for_real_walk() -> None:
    """Representative multi-substrate run yields unique (run_id, substrate) ids."""
    checks = [
        _check("postgres.sessions"),
        _check("postgres.api_costs"),
        _check("postgres.metrics"),
        _check("elasticsearch.agent_logs"),
        _check("elasticsearch.captains_captures"),
        _check("neo4j.turn"),
        _check("neo4j.entity"),
        _check("redis.streams"),
    ]
    docs = substrate_docs_from_result(_result(checks=checks))
    ids = [f"{d.run_id}::{d.substrate}" for d in docs]
    assert len(ids) == len(set(ids))


def test_substrate_docs_duplicate_substrate_raises() -> None:
    """Two checks sharing a substrate raise ValueError (doc-id collision guard)."""
    checks = [
        _check("postgres.sessions", status="green"),
        _check("postgres.sessions", status="red"),
    ]
    with pytest.raises(ValueError, match="duplicate substrate"):
        substrate_docs_from_result(_result(checks=checks))


# -- model integrity --------------------------------------------------------


def test_substrate_result_doc_round_trips_json() -> None:
    doc = SubstrateResultDoc(
        run_id="run-1",
        started_at=NOW,
        substrate="postgres.api_costs",
        status="red",
        expected="conditional",
        observed_count=3,
        duration_ms=2.25,
        error=None,
        orphan_count=2,
        orphan_red_count=2,
        orphan_yellow_count=0,
    )
    reloaded = SubstrateResultDoc.model_validate_json(doc.model_dump_json())
    assert reloaded == doc


def test_substrate_result_doc_is_frozen() -> None:
    doc = SubstrateResultDoc(
        run_id="run-1",
        started_at=NOW,
        substrate="postgres.sessions",
        status="green",
        expected="required",
        observed_count=1,
        duration_ms=1.0,
    )
    with pytest.raises(Exception):  # noqa: PT011 — Pydantic raises ValidationError
        doc.status = "red"  # type: ignore[misc]
