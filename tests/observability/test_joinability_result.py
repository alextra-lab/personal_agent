"""Unit tests for :mod:`personal_agent.observability.joinability.result`."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from personal_agent.observability.joinability.result import (
    Orphan,
    ResultDoc,
    SubstrateCheck,
    aggregate_outcome,
)

NOW = datetime(2026, 5, 23, 14, 0, 0, tzinfo=timezone.utc)


def _check(
    substrate: str = "postgres.api_costs",
    status: str = "green",
    expected: str = "required",
    observed_count: int = 1,
) -> SubstrateCheck:
    return SubstrateCheck(
        substrate=substrate,
        expected=expected,  # type: ignore[arg-type]
        observed_count=observed_count,
        status=status,  # type: ignore[arg-type]
        duration_ms=1.0,
    )


def _orphan(severity: str = "red") -> Orphan:
    return Orphan(
        substrate="postgres.api_costs",
        kind="missing_identity",
        detail={"row_id": 42},
        severity=severity,  # type: ignore[arg-type]
    )


# -- aggregate_outcome -------------------------------------------------------


def test_aggregate_skipped_when_no_session() -> None:
    assert aggregate_outcome([_check()], [], sampled_session_id=None) == "skipped"


def test_aggregate_green_when_all_green() -> None:
    checks = [_check(status="green") for _ in range(5)]
    assert aggregate_outcome(checks, [], sampled_session_id="s1") == "green"


def test_aggregate_red_when_any_orphan() -> None:
    checks = [_check(status="green")]
    assert aggregate_outcome(checks, [_orphan("red")], sampled_session_id="s1") == "red"


def test_aggregate_red_overrides_yellow() -> None:
    checks = [_check(status="yellow"), _check(status="red")]
    assert aggregate_outcome(checks, [], sampled_session_id="s1") == "red"


def test_aggregate_yellow_when_any_yellow_no_red() -> None:
    checks = [_check(status="green"), _check(status="yellow")]
    assert aggregate_outcome(checks, [], sampled_session_id="s1") == "yellow"


def test_aggregate_yellow_orphan_does_not_red() -> None:
    checks = [_check(status="green")]
    assert aggregate_outcome(checks, [_orphan("yellow")], sampled_session_id="s1") == "green"


def test_aggregate_skipped_check_is_neutral() -> None:
    checks = [_check(status="green"), _check(status="skipped")]
    assert aggregate_outcome(checks, [], sampled_session_id="s1") == "green"


# -- model integrity ---------------------------------------------------------


def test_result_doc_round_trips_json() -> None:
    doc = ResultDoc(
        run_id="r1",
        started_at=NOW,
        duration_ms=482.3,
        source="scheduler",
        window_hours=24,
        random_seed=1_748_016_000,
        sampled_session_id="00000000-0000-0000-0000-000000000001",
        sampled_trace_ids=["00000000-0000-0000-0000-000000000002"],
        substrate_checks=[_check()],
        orphans=[],
        outcome="green",
        trace_id="00000000-0000-0000-0000-000000000003",
    )
    blob = doc.model_dump_json()
    reloaded = ResultDoc.model_validate_json(blob)
    assert reloaded == doc


def test_result_doc_kind_default() -> None:
    doc = ResultDoc(
        run_id="r1",
        started_at=NOW,
        duration_ms=1.0,
        source="cli",
        window_hours=24,
        random_seed=0,
        sampled_session_id=None,
        outcome="skipped",
        trace_id="t1",
    )
    assert doc.kind == "system:joinability_probe"


def test_models_are_frozen() -> None:
    o = _orphan()
    with pytest.raises(Exception):  # noqa: PT011 — Pydantic raises ValidationError
        o.substrate = "x"  # type: ignore[misc]

    c = _check()
    with pytest.raises(Exception):  # noqa: PT011
        c.observed_count = 99  # type: ignore[misc]
