r"""FRE-778 — ADR-0104 multipath recall A/B driver (the FRE-724 proof instrument).

Drives the FRE-489 (lexical) and FRE-670 (semantic) gate sets through the recall
paths, multipath OFF vs ON, on a **co-resident haystack** in the isolated test
substrate: each gate set's full case list is seeded once with no wipe between
cases (adapted from ``ab_relevance_bounded.py``'s ``calibrate()`` mode), so every
query ranks against a real haystack of the other cases rather than an empty
graph. This is a **test-substrate proof instrument for FRE-724 to consume** —
not a substitute for FRE-724's own master-owned, deploy-gated live/prod
graduation proof.

Reports, per gate set: recall@k off vs on (and the lift), the AC-3 "recovered"
tail-win count (denied off, surfaced on — FRE-670's vocabulary-divergent cases
are exactly the out-of-vocabulary probe AC-3 wants), broad-path hit counts, p50
wall-clock latency (ON state) against the FRE-724 AC-6b 17s ceiling, and a
**dense-arm-only** floor invariant (the lowest true-positive dense-vector cosine
must clear the configured 0.60 floor — this does not cover the lexical arm,
which has no cosine, or the fused/reranked output, which has no single
comparable threshold by RRF's design). Also runs one FRE-658 window check
(explicit hard recency window excludes an older-than-window turn; an omitted
window does not) directly against ``MemoryService.query_memory`` with multipath
on, since ``MemoryServiceAdapter``/``MemoryRecallQuery`` carry no
``hard_recency_days`` field.

Distractor strategy is co-resident cases only (owner-approved scope) — no
production Neo4j read. ``harness.fetch_live_distractors`` (a separate, prod-read
mechanism) is not used here.

**Multi-query arm environment note:** the paraphrase-generation call
(``multi_query_recall_arm``) uses the ``sub_agent`` model role, whose endpoint
in ``config/models.yaml`` (the localhost-resolving file this driver pins) is
``http://localhost:8000/v1`` — a local SLM server that does not run on this VPS
(the SLM runs on a separate Mac host, reached in production via the
``slm.example.com`` Cloudflare Access tunnel through ``config/models.cloud.yaml``,
which this driver deliberately does NOT use — that file's embedder endpoint is
a Docker-internal hostname unreachable from a host process). In that
environment the multi-query arm's call fails, is caught, and the arm
contributes nothing — a graceful degrade, not a crash. Dense + lexical arms are
unaffected and are what this driver's off-vs-on measurement actually rests on
in this environment.

Run (test substrate up + local embedder reachable — ``docker start
cloud-sim-embeddings`` first if it's stopped; ``docker stop`` it again after,
since the live default profile is the managed OVH embedder):

    PYTHONPATH=. uv run python scripts/eval/fre435_memory_recall/ab_multipath.py \\
        --run-id fre778-$(date +%Y%m%d) --gate-set both
"""

from __future__ import annotations

import os

# Pin the TEST substrate before importing personal_agent (settings is a cached
# import-time singleton). Hard assignment, NOT setdefault — an ambient .env
# value (e.g. AGENT_SUBSTRATE_PROFILE=managed_embedder) must not win (FRE-778
# codex review finding #1).
_TEST_SUBSTRATE_ENV = {
    "APP_ENV": "test",
    "AGENT_SUBSTRATE_PROFILE": "test",
    "AGENT_NEO4J_URI": "bolt://localhost:7688",
    "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
    "AGENT_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    # This driver does no admin DDL or sysgraph writes, but AppConfig's FRE-375
    # guard checks all five substrate URIs for a prod fingerprint under
    # APP_ENV=test regardless of which fields a given script actually uses —
    # so these two must be pinned to the test stack too, or construction raises.
    "AGENT_DATABASE_ADMIN_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_SYSGRAPH_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_ELASTICSEARCH_INDEX_PREFIX": "agent-logs-test",
    "AGENT_CAPTAINS_LOG_INDEX_PREFIX": "agent-captains-test",
}
for _key, _value in _TEST_SUBSTRATE_ENV.items():
    os.environ[_key] = _value  # hard pin -- NOT setdefault

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import statistics  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from dataclasses import asdict, dataclass, field  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import structlog  # noqa: E402
from scripts.eval.fre435_memory_recall.harness import (  # noqa: E402
    detect_embedding_backend,
    seed_replay,
    store_turn,
    wipe_substrate,
)
from scripts.eval.fre435_memory_recall.metrics import recall_at_k  # noqa: E402
from scripts.eval.fre435_memory_recall.probes import ProbeCase, load_probe_set  # noqa: E402
from scripts.eval.fre435_memory_recall.scoring import flatten_recall  # noqa: E402

from personal_agent.config import settings  # noqa: E402
from personal_agent.events import AccessContext  # noqa: E402
from personal_agent.memory.embeddings import generate_embedding  # noqa: E402
from personal_agent.memory.models import MemoryQuery  # noqa: E402
from personal_agent.memory.protocol import MemoryRecallQuery  # noqa: E402
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter  # noqa: E402
from personal_agent.memory.service import MemoryService  # noqa: E402
from personal_agent.request_gateway.context import _capitalized_entity_hints  # noqa: E402

log = structlog.get_logger(__name__)

GATE_SETS = {
    "lexical": "scripts/eval/fre435_memory_recall/bespoke_probe.yaml",
    "semantic": "scripts/eval/fre435_memory_recall/semantic_probe.yaml",
}
DEFAULT_OUT = "telemetry/evaluation/fre778-multipath-ab"
#: FRE-724 AC-6b: measured p50 must not exceed the reranker-dominated baseline ceiling.
LATENCY_CEILING_S = 17.0
#: FRE-706 owner-confirmed noise-guard floor (deploy config, not the code default).
MULTIPATH_FLOOR = 0.60
#: FRE-658 window-check fixture: a marker entity + turn old enough to fall outside
#: WINDOW_PROBE_WINDOW_DAYS but still recallable when the window is omitted.
WINDOW_PROBE_ENTITY = "FRE778 Window Probe"
WINDOW_PROBE_TURN_AGE_DAYS = 40
WINDOW_PROBE_WINDOW_DAYS = 7


@dataclass(frozen=True)
class LatencySummary:
    """Median wall-clock recall latency vs the FRE-724 AC-6b ceiling.

    Attributes:
        median_s: Median latency in seconds, or ``None`` if no durations were
            captured (excluded from pass/fail, not a false pass).
        ceiling_s: The latency ceiling being checked against.
        within_ceiling: Whether ``median_s <= ceiling_s``, or ``None`` when
            ``median_s`` is ``None``.
    """

    median_s: float | None
    ceiling_s: float
    within_ceiling: bool | None


def latency_summary(durations: Sequence[float], ceiling_s: float) -> LatencySummary:
    """Compute the median latency and whether it clears *ceiling_s*.

    Args:
        durations: Per-query wall-clock durations in seconds.
        ceiling_s: The latency ceiling (FRE-724 AC-6b: 17s).

    Returns:
        The median and pass/fail against the ceiling, or an undefined
        (``None``) result when *durations* is empty.
    """
    if not durations:
        return LatencySummary(median_s=None, ceiling_s=ceiling_s, within_ceiling=None)
    median = statistics.median(durations)
    return LatencySummary(median_s=median, ceiling_s=ceiling_s, within_ceiling=median <= ceiling_s)


@dataclass(frozen=True)
class FloorInvariantResult:
    """The dense-arm-only floor invariant: the lowest true-positive cosine vs the floor.

    Does NOT cover the lexical arm (no cosine score) or the fused/reranked output
    (no single comparable threshold — RRF fuses by rank, not score, by design).

    Attributes:
        min_positive: The lowest captured expected-entity cosine, or ``None`` if
            none were captured (unproven, not vacuously passing).
        floor: The configured similarity floor being checked against.
        holds: Whether ``min_positive >= floor``, or ``None`` when unproven.
    """

    min_positive: float | None
    floor: float
    holds: bool | None


def dense_floor_invariant(positive_cosines: Sequence[float], floor: float) -> FloorInvariantResult:
    """Check whether the lowest true-positive dense-arm cosine clears *floor*.

    Args:
        positive_cosines: Best expected-entity cosine per case (dense arm only).
        floor: The configured ``recall_similarity_floor`` (0.60, FRE-706).

    Returns:
        The minimum observed positive and whether it clears the floor, or an
        unproven (``None``) result when no positives were captured.
    """
    if not positive_cosines:
        return FloorInvariantResult(min_positive=None, floor=floor, holds=None)
    min_positive = min(positive_cosines)
    return FloorInvariantResult(min_positive=min_positive, floor=floor, holds=min_positive >= floor)


def _set_multipath(settings_obj: Any, *, enabled: bool) -> None:
    """Toggle the ADR-0104 arm flags together; pin the floor + relevance-bounded flag.

    The floor stays at :data:`MULTIPATH_FLOOR` for BOTH states (a constant
    condition per the ticket, not something toggled — ``dense_recall_arm`` reads
    it independently of ``multipath_recall_enabled``). ``relevance_bounded_recall_enabled``
    is pinned ``False`` throughout so the ADR-0100 flag can't confound this
    ADR-0104 A/B (FRE-778 codex review finding #6).

    Args:
        settings_obj: The settings object to mutate (the live singleton at
            runtime; a fake in unit tests).
        enabled: Whether multipath recall (+ its lexical/multi-query arms) is on.
    """
    settings_obj.multipath_recall_enabled = enabled
    settings_obj.lexical_arm_enabled = enabled
    settings_obj.multiquery_arm_enabled = enabled
    settings_obj.recall_similarity_floor = MULTIPATH_FLOOR
    settings_obj.relevance_bounded_recall_enabled = False


@dataclass
class CaseResult:
    """Per-case multipath A/B outcome within one gate set."""

    case_id: str
    tags: tuple[str, ...]
    entity_recall_off: float | None
    entity_recall_on: float | None
    broad_hit_off: bool
    broad_hit_on: bool
    recovered: bool  # AC-3: denied off, surfaced on
    latency_on_s: float
    positive_cosine: float | None


@dataclass
class GateSetReport:
    """The A/B outcome for one gate set (FRE-489 or FRE-670) on the co-resident haystack."""

    gate_set: str
    probe_set: str
    n_cases: int
    recall_off_mean: float
    recall_on_mean: float
    recall_lift: float
    recovered_count: int
    broad_off: int
    broad_on: int
    latency: LatencySummary
    floor_invariant: FloorInvariantResult
    cases: list[CaseResult] = field(default_factory=list)


@dataclass
class WindowCheckResult:
    """FRE-658 explicit hard-window behavior under multipath ON."""

    window_days: int
    turn_age_days: int
    in_window_hit: bool
    omitted_window_hit: bool
    passed: bool


@dataclass
class MultipathABReport:
    """The full FRE-778 run."""

    run_id: str
    timestamp: str
    floor: float
    relevance_bounded_recall_enabled: bool
    gate_sets: dict[str, GateSetReport]
    window_check: WindowCheckResult


async def _capture_positive_cosine(
    service: MemoryService, case: ProbeCase, top_k: int
) -> float | None:
    """Best expected-entity cosine among the dense arm's candidates (floor-invariant input)."""
    embedding = await generate_embedding(case.query, mode="query")
    if not any(x != 0.0 for x in embedding):
        return None
    async with service.driver.session() as session:  # type: ignore[union-attr]
        rows = await service._query_entity_vector_candidates(session, embedding, top_k)
    expected = {e.name.strip().lower() for e in case.seed_entities} | {
        n.strip().lower() for n in case.expected.entity_names
    }
    pos = [float(r["score"]) for r in rows if str(r.get("name", "")).strip().lower() in expected]
    return max(pos) if pos else None


async def _entity_recall_timed(
    adapter: MemoryServiceAdapter, case: ProbeCase, k: int, trace_id: str
) -> tuple[float | None, float]:
    """Entity-path recall@k plus wall-clock latency (prod-faithful: hints + query_text)."""
    hints = _capitalized_entity_hints(case.query)
    query = MemoryRecallQuery(entity_names=hints[:5], query_text=case.query, limit=k)
    start = time.perf_counter()
    result = await adapter.recall(query, trace_id=trace_id)
    elapsed = time.perf_counter() - start
    retrieved = flatten_recall(result.episodes, result.entities, result.relevance_scores)
    relevant = set(case.relevant_ids)
    if not relevant:
        return None, elapsed
    return recall_at_k(retrieved, relevant, k), elapsed


async def _broad_hit_timed(
    adapter: MemoryServiceAdapter, case: ProbeCase, trace_id: str
) -> tuple[bool, float]:
    """Broad-path AC-1b-style check plus wall-clock latency."""
    start = time.perf_counter()
    broad = await adapter.recall_broad(
        entity_types=None,
        recency_days=90,
        limit=20,
        trace_id=trace_id,
        query_text=case.query,
    )
    elapsed = time.perf_counter() - start
    names = {
        str(e.get("name")).strip().lower()
        for group in broad.entities_by_type.values()
        for e in group
        if e.get("name")
    }
    expected = {n.strip().lower() for n in case.expected.entity_names}
    return bool(expected & names), elapsed


async def _run_gate_set(
    service: MemoryService,
    adapter: MemoryServiceAdapter,
    gate_set: str,
    probe_set_path: str,
    prod_k: int,
) -> GateSetReport:
    """Co-seed *probe_set_path*'s full case list (no wipe between cases) and drive the A/B."""
    cases = load_probe_set(Path(probe_set_path))
    await wipe_substrate(service, str(uuid.uuid4()))
    for case in cases:
        trace_id = str(uuid.uuid4())
        await seed_replay(service, case, trace_id, f"fre778-{gate_set}-{case.case_id}")

    top_k = settings.proactive_memory_vector_top_k
    results: list[CaseResult] = []
    on_latencies: list[float] = []
    positives: list[float] = []
    try:
        for case in cases:
            trace_id = str(uuid.uuid4())
            positive = await _capture_positive_cosine(service, case, top_k)
            if positive is not None:
                positives.append(positive)

            _set_multipath(settings, enabled=False)
            e_off, _ = await _entity_recall_timed(adapter, case, prod_k, trace_id)
            b_off, _ = await _broad_hit_timed(adapter, case, trace_id)

            _set_multipath(settings, enabled=True)
            e_on, lat_entity = await _entity_recall_timed(adapter, case, prod_k, trace_id)
            b_on, lat_broad = await _broad_hit_timed(adapter, case, trace_id)
            on_latencies.extend([lat_entity, lat_broad])

            recovered = bool(e_off is not None and e_off == 0.0 and e_on and e_on > 0.0)
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    tags=case.tags,
                    entity_recall_off=e_off,
                    entity_recall_on=e_on,
                    broad_hit_off=b_off,
                    broad_hit_on=b_on,
                    recovered=recovered,
                    latency_on_s=lat_entity,
                    positive_cosine=positive,
                )
            )
            log.info(
                "multipath_ab_case",
                gate_set=gate_set,
                case=case.case_id,
                e_off=e_off,
                e_on=e_on,
                recovered=recovered,
            )
    finally:
        _set_multipath(settings, enabled=False)
        settings.recall_similarity_floor = 0.0

    e_off_vals = [c.entity_recall_off for c in results if c.entity_recall_off is not None]
    e_on_vals = [c.entity_recall_on for c in results if c.entity_recall_on is not None]
    recall_off_mean = round(sum(e_off_vals) / len(e_off_vals), 4) if e_off_vals else 0.0
    recall_on_mean = round(sum(e_on_vals) / len(e_on_vals), 4) if e_on_vals else 0.0

    return GateSetReport(
        gate_set=gate_set,
        probe_set=probe_set_path,
        n_cases=len(cases),
        recall_off_mean=recall_off_mean,
        recall_on_mean=recall_on_mean,
        recall_lift=round(recall_on_mean - recall_off_mean, 4),
        recovered_count=sum(1 for c in results if c.recovered),
        broad_off=sum(1 for c in results if c.broad_hit_off),
        broad_on=sum(1 for c in results if c.broad_hit_on),
        latency=latency_summary(on_latencies, LATENCY_CEILING_S),
        floor_invariant=dense_floor_invariant(positives, MULTIPATH_FLOOR),
        cases=results,
    )


def _has_marker(result: Any) -> bool:
    """Whether the FRE-658 marker turn is present in a ``MemoryQueryResult``."""
    return any(WINDOW_PROBE_ENTITY in (turn.key_entities or []) for turn in result.conversations)


async def _run_window_check(service: MemoryService) -> WindowCheckResult:
    """FRE-658 window check under multipath ON.

    An explicit hard window excludes an older-than-window turn; an omitted
    window returns it (de-gated), with multipath ON throughout. Bypasses
    ``MemoryServiceAdapter`` (whose ``MemoryRecallQuery`` carries no
    ``hard_recency_days`` field) and calls ``MemoryService.query_memory``
    directly on the raw ``MemoryQuery`` (FRE-778 codex review finding #2/#5).
    """
    trace_id = str(uuid.uuid4())
    session_id = f"fre778-window-{trace_id}"
    old_time = datetime.now(timezone.utc) - timedelta(days=WINDOW_PROBE_TURN_AGE_DAYS)
    await store_turn(
        service,
        turn_id=f"{session_id}:0",
        session_id=session_id,
        trace_id=trace_id,
        sequence=0,
        user_message=f"Discussing {WINDOW_PROBE_ENTITY}.",
        assistant_response=None,
        key_entities=[WINDOW_PROBE_ENTITY],
        timestamp=old_time,
    )

    _set_multipath(settings, enabled=True)
    try:
        in_window = await service.query_memory(
            MemoryQuery(hard_recency_days=WINDOW_PROBE_WINDOW_DAYS, limit=10),
            query_text=WINDOW_PROBE_ENTITY,
            access_context=AccessContext.SEARCH,
            trace_id=trace_id,
        )
        omitted_window = await service.query_memory(
            MemoryQuery(hard_recency_days=None, limit=10),
            query_text=WINDOW_PROBE_ENTITY,
            access_context=AccessContext.SEARCH,
            trace_id=trace_id,
        )
    finally:
        _set_multipath(settings, enabled=False)
        settings.recall_similarity_floor = 0.0

    in_window_hit = _has_marker(in_window)
    omitted_window_hit = _has_marker(omitted_window)
    return WindowCheckResult(
        window_days=WINDOW_PROBE_WINDOW_DAYS,
        turn_age_days=WINDOW_PROBE_TURN_AGE_DAYS,
        in_window_hit=in_window_hit,
        omitted_window_hit=omitted_window_hit,
        passed=(not in_window_hit) and omitted_window_hit,
    )


def _print_summary(report: MultipathABReport) -> None:
    """Emit a human-readable A/B summary to stdout."""
    print("\n=== FRE-778 multipath A/B (ADR-0104) ===")
    print(
        f"floor={report.floor}  "
        f"relevance_bounded_recall_enabled={report.relevance_bounded_recall_enabled}"
    )
    for name, gs in report.gate_sets.items():
        print(f"\n[{name}] n={gs.n_cases}  probe_set={gs.probe_set}")
        print(f"  recall@k off={gs.recall_off_mean}  on={gs.recall_on_mean}  lift={gs.recall_lift}")
        print(f"  recovered (AC-3 tail-win): {gs.recovered_count}/{gs.n_cases}")
        print(f"  broad hit off={gs.broad_off}/{gs.n_cases}  on={gs.broad_on}/{gs.n_cases}")
        lat = gs.latency
        print(
            f"  p50 latency (on): {lat.median_s}s  ceiling={lat.ceiling_s}s  "
            f"within_ceiling={lat.within_ceiling}"
        )
        fi = gs.floor_invariant
        print(
            f"  dense-arm floor invariant: min_positive={fi.min_positive}  "
            f"floor={fi.floor}  holds={fi.holds}"
        )
    wc = report.window_check
    print(
        f"\n[window check, FRE-658] window={wc.window_days}d turn_age={wc.turn_age_days}d  "
        f"in_window_hit={wc.in_window_hit}  omitted_window_hit={wc.omitted_window_hit}  "
        f"passed={wc.passed}"
    )


async def run(args: argparse.Namespace) -> int:
    """Drive the A/B across the requested gate set(s) plus the window check."""
    embedding_backend = await detect_embedding_backend()
    if embedding_backend != "real":
        log.error(
            "embedder_unreachable",
            hint=(
                "the local embedder is unreachable (zero-vector probe) -- "
                "`docker start cloud-sim-embeddings` then retry; "
                "`docker stop cloud-sim-embeddings` when done"
            ),
        )
        return 2

    service = MemoryService()  # fre-375-allow: test stack pinned module-top (:7688)
    if not await service.connect():
        log.error("memory_service_unreachable", uri=os.environ.get("AGENT_NEO4J_URI"))
        return 3
    if not await service.ensure_vector_index():
        log.error("vector_index_unavailable")
        await service.disconnect()
        return 4
    if not await service.ensure_fulltext_index():
        log.error("fulltext_index_unavailable")
        await service.disconnect()
        return 5
    adapter = MemoryServiceAdapter(service)

    gate_sets_to_run = (
        GATE_SETS if args.gate_set == "both" else {args.gate_set: GATE_SETS[args.gate_set]}
    )
    gate_set_reports: dict[str, GateSetReport] = {}
    try:
        for name, path in gate_sets_to_run.items():
            log.info("gate_set_start", gate_set=name)
            gate_set_reports[name] = await _run_gate_set(service, adapter, name, path, args.prod_k)
            log.info("gate_set_done", gate_set=name)
        window_check = await _run_window_check(service)
    finally:
        await service.disconnect()

    report = MultipathABReport(
        run_id=args.run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        floor=MULTIPATH_FLOOR,
        relevance_bounded_recall_enabled=False,
        gate_sets=gate_set_reports,
        window_check=window_check,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"ab-{args.run_id}.json"
    out_path.write_text(json.dumps(asdict(report), indent=2, default=str))
    _print_summary(report)
    log.info("multipath_ab_done", out=str(out_path))
    return 0


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="FRE-778 ADR-0104 multipath recall A/B")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--gate-set", choices=("lexical", "semantic", "both"), default="both")
    parser.add_argument("--prod-k", type=int, default=5)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
