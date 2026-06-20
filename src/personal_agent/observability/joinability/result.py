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


class SubstrateResultDoc(BaseModel):
    """Flat per-``(run, substrate)`` projection of one :class:`ResultDoc`.

    Written to ``agent-monitors-joinability-substrate-YYYY.MM.DD`` (one doc per
    substrate per probe run) so legacy Kibana aggregation visualizations can
    break joinability detail down by substrate, status, and orphan severity.
    The run doc keeps ``orphans`` / ``substrate_checks`` as ``nested`` arrays,
    which legacy aggs cannot bucket — this flattening (FRE-550 / ADR-0074) is
    additive, not a replacement.

    Attributes:
        run_id: Parent :attr:`ResultDoc.run_id` — the join key back to the run.
        started_at: Copied from the parent run; the index-pattern time field.
        substrate: Substrate identifier, e.g. ``"postgres.api_costs"``.
        status: The substrate check status — ``"green"`` / ``"yellow"`` /
            ``"red"`` / ``"skipped"``.
        expected: ``"required"`` / ``"conditional"`` / ``"absent_ok"``.
        observed_count: Rows / docs / nodes the walk found for this substrate.
        duration_ms: Wall-clock time to walk this substrate.
        error: Error string when the substrate could not be reached.
        orphan_count: Total orphans attributed to this substrate.
        orphan_red_count: Hard-violation orphans on this substrate.
        orphan_yellow_count: Soft-violation orphans on this substrate.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    started_at: datetime
    substrate: str
    status: Literal["green", "yellow", "red", "skipped"]
    expected: Literal["required", "conditional", "absent_ok"]
    observed_count: int
    duration_ms: float
    error: str | None = None
    orphan_count: int = 0
    orphan_red_count: int = 0
    orphan_yellow_count: int = 0


def substrate_docs_from_result(result: ResultDoc) -> list[SubstrateResultDoc]:
    """Flatten a :class:`ResultDoc` into one doc per substrate check.

    Orphans are matched to their substrate by :attr:`Orphan.substrate`. Orphans
    whose substrate is not among ``result.substrate_checks`` are silently
    dropped (defensive — every orphan-emitting walk also appends a check for the
    same substrate, so this should not occur in practice).

    The walk appends exactly one :class:`SubstrateCheck` per substrate, so the
    sink can derive a unique ES doc id ``{run_id}::{substrate}``. To keep that
    contract honest, this factory **enforces** substrate uniqueness here — the
    single chokepoint — raising ``ValueError`` on a duplicate rather than
    letting the sink silently overwrite a sibling doc downstream. A future walk
    regression that emits a duplicate substrate then fails the probe run loudly
    instead of dropping dashboard rows.

    Args:
        result: Completed run-level result document.

    Returns:
        One :class:`SubstrateResultDoc` per check in ``result.substrate_checks``
        (order preserved).

    Raises:
        ValueError: If two substrate checks share a ``substrate`` value (would
            collide on the ``{run_id}::{substrate}`` ES doc id).
    """
    substrates = [c.substrate for c in result.substrate_checks]
    if len(substrates) != len(set(substrates)):
        dupes = sorted({s for s in substrates if substrates.count(s) > 1})
        raise ValueError(
            f"duplicate substrate(s) in result {result.run_id}: {dupes} — "
            f"would collide on the substrate-doc id"
        )

    orphans_by_substrate: dict[str, list[Orphan]] = {}
    for orphan in result.orphans:
        orphans_by_substrate.setdefault(orphan.substrate, []).append(orphan)

    docs: list[SubstrateResultDoc] = []
    for check in result.substrate_checks:
        sub_orphans = orphans_by_substrate.get(check.substrate, [])
        docs.append(
            SubstrateResultDoc(
                run_id=result.run_id,
                started_at=result.started_at,
                substrate=check.substrate,
                status=check.status,
                expected=check.expected,
                observed_count=check.observed_count,
                duration_ms=check.duration_ms,
                error=check.error,
                orphan_count=len(sub_orphans),
                orphan_red_count=sum(1 for o in sub_orphans if o.severity == "red"),
                orphan_yellow_count=sum(1 for o in sub_orphans if o.severity == "yellow"),
            )
        )
    return docs


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
