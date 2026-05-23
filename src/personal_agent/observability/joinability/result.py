"""Result document for one joinability-probe run (ADR-0074 Phase 5).

A single :class:`ResultDoc` is written to Elasticsearch index
``agent-monitors-joinability-YYYY.MM.DD`` per probe invocation. The doc is
the audit trail: the 7-day green gate (:mod:`status`) aggregates over these
docs to decide when ADR-0074 may flip Proposed → Accepted.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Outcomes are ordered so that ``max(outcomes, key=SEVERITY.__getitem__)`` returns
# the worst — used by :func:`aggregate_outcome`.
SEVERITY: dict[str, int] = {
    "green": 0,
    "skipped": 0,  # neutral: doesn't drag a run worse, but isn't itself green
    "yellow": 1,
    "red": 2,
}


class Orphan(BaseModel):
    """One identity violation discovered by the walk.

    Attributes:
        substrate: Substrate identifier, e.g. ``"postgres.api_costs"``.
        kind: Coarse failure class — ``"missing_identity"``,
            ``"dangling_fk"``, ``"es_pg_mismatch"``, ``"three_way_mismatch"``,
            or ``"unjoinable_payload"``.
        detail: Row-shaped evidence; bounded to identifying keys only, never
            user-facing PII.
        severity: ``"red"`` for hard violations, ``"yellow"`` for soft
            (e.g. conditional substrate where the trigger fired).
    """

    model_config = ConfigDict(frozen=True)

    substrate: str
    kind: str
    detail: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["red", "yellow"]


class SubstrateCheck(BaseModel):
    """Outcome of walking one substrate during a probe run.

    Attributes:
        substrate: Substrate identifier.
        expected: Whether a row was ``"required"``, ``"conditional"`` (allowed
            to be absent), or ``"absent_ok"``.
        observed_count: Number of rows / docs / nodes the walk found.
        status: ``"green"`` / ``"yellow"`` / ``"red"`` / ``"skipped"``.
        duration_ms: Wall-clock time to walk this substrate.
        error: Connection / query error string when the substrate could not
            be reached. Distinct from an orphan: a network blip yellows the
            check; an orphan reds it.
    """

    model_config = ConfigDict(frozen=True)

    substrate: str
    expected: Literal["required", "conditional", "absent_ok"]
    observed_count: int
    status: Literal["green", "yellow", "red", "skipped"]
    duration_ms: float
    error: str | None = None


class ResultDoc(BaseModel):
    """Top-level document persisted to ES for one probe run.

    Attributes:
        run_id: Per-run UUID, distinct from the trace id of the probe itself.
        started_at: Wall-clock start time of the run.
        duration_ms: Total run wall-clock duration.
        source: Caller — ``"scheduler"``, ``"cli"``, ``"ci"``, ``"manual"``.
        window_hours: How far back the sampling looked for eligible sessions.
        random_seed: Seed used by :func:`sampling.pick_session`; reproducer
            command in logs is derived from this.
        sampled_session_id: The session walked, or ``None`` when the outcome
            is ``"skipped"`` (e.g. no eligible session in window).
        sampled_trace_ids: All trace_ids derived from the anchor session
            during the walk (typically the api_costs trace_ids).
        substrate_checks: Per-substrate verdicts; the order matches walk order.
        orphans: All identity violations found across substrates.
        outcome: Aggregated outcome computed by :func:`aggregate_outcome`.
        trace_id: The probe's own ``SystemTraceContext`` trace id — the probe
            is itself joinable. Filtered out of future sampling pools.
        kind: ``"system:joinability_probe"`` (the ``SystemTraceContext.kind``).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    started_at: datetime
    duration_ms: float
    source: Literal["scheduler", "cli", "ci", "manual"]
    window_hours: int
    random_seed: int
    sampled_session_id: str | None
    sampled_trace_ids: list[str] = Field(default_factory=list)
    substrate_checks: list[SubstrateCheck] = Field(default_factory=list)
    orphans: list[Orphan] = Field(default_factory=list)
    outcome: Literal["green", "yellow", "red", "skipped"]
    trace_id: str
    kind: str = "system:joinability_probe"


def aggregate_outcome(
    checks: Sequence[SubstrateCheck],
    orphans: Iterable[Orphan],
    *,
    sampled_session_id: str | None,
) -> Literal["green", "yellow", "red", "skipped"]:
    """Reduce per-substrate verdicts and orphans to one outcome.

    Rules (in order):
        1. No anchor session sampled → ``"skipped"``.
        2. Any ``red`` orphan or check → ``"red"``.
        3. Any ``yellow`` check → ``"yellow"``.
        4. Otherwise → ``"green"``.

    ``skipped`` checks are treated as neutral: they neither drag a run worse
    nor block it from being green.

    Args:
        checks: Per-substrate verdicts.
        orphans: Identity violations.
        sampled_session_id: ``None`` iff no session was sampled (drains the
            ``"skipped"`` short-circuit).

    Returns:
        The aggregated outcome literal.
    """
    if sampled_session_id is None:
        return "skipped"
    if any(o.severity == "red" for o in orphans):
        return "red"
    if any(c.status == "red" for c in checks):
        return "red"
    if any(c.status == "yellow" for c in checks):
        return "yellow"
    return "green"
