r"""ADR-0114 D5 τ_merge sweep driver + AC-3(a,d,e,f) computed checks (FRE-842).

Freezes the categorizer's proposal ledgers by seed (FRE-839's
`MembershipAssertion.seed`), then replays each seed's ledger — in
chronological conversation order and in ≥2 pre-registered permutations
(ADR-0114 AC-3) — through `scripts.study.consolidator`'s two-stage
canonicalizer at every τ_merge in a grid, entirely **in memory**: no Neo4j
write, and no per-config round trip, so many (seed × ordering × τ_merge)
configs can run against the SAME frozen ledgers without one config's state
leaking into another's (a shared-graph write-back per config would corrupt
exactly this isolation — see `consolidator.apply_canonicalization_to_graph`'s
docstring for why that function is never called from here).

**Scope (mirrors FRE-840's precedent on AC-4).** This module computes the
**objectively-computable** AC-3 sub-parts — (a) plateau, (d) distinctness,
(e) non-collapse floor, (f) stochastic stability — from real data, and
produces the top-20/tail category **tables** ready for a rating pass. It
does NOT deliver AC-3(b)/(c) legibility (needs 2 independent human/LLM
judges per the ADR) or an AC-3 pass/fail verdict — FRE-843 (the v0-synthesis
seam) selects τ_merge* and owns that judgment call.

Run (study infra up; read-only, no writes):

    uv run python -m scripts.study.sweep
    uv run python -m scripts.study.sweep --seeds 0,1 --tau-merge-grid 0.3,0.5,0.7
"""

from __future__ import annotations

from scripts.study.config import STUDY_NEO4J_BOLT_PORT, StudySettings

_STUDY_NEO4J_URI = f"bolt://localhost:{STUDY_NEO4J_BOLT_PORT}"


class StudyTargetMismatchError(RuntimeError):
    """Raised when the resolved study settings do not point at the study sandbox.

    Fail loud rather than silently sweeping against the wrong Neo4j —
    mirrors `baseline_harness.StudyTargetMismatchError`'s preflight posture,
    adapted for this module's direct `StudySettings`-based connection (no
    `personal_agent.config` singleton involved, so there is no cached-import
    race to guard against — just a plain resolved-value check).
    """


def _assert_study_target(settings: StudySettings) -> None:
    """Refuse to proceed unless `settings.neo4j_uri` resolves to the study sandbox."""
    if settings.neo4j_uri != _STUDY_NEO4J_URI:
        raise StudyTargetMismatchError(
            f"expected study sandbox {_STUDY_NEO4J_URI!r}, "
            f"StudySettings().neo4j_uri resolved to {settings.neo4j_uri!r} -- "
            "refusing to sweep against a non-study Neo4j target"
        )


import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
import statistics  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from dataclasses import asdict, dataclass  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import structlog  # noqa: E402

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.study.consolidator import (  # noqa: E402
    CandidatePair,
    CanonicalizationResult,
    CategoryMembers,
    canonicalize,
    generate_candidates_pairwise,
)
from scripts.study.neo4j_types import Neo4jDriver  # noqa: E402

log = structlog.get_logger(__name__)

_DEFAULT_TAU_MERGE_GRID: tuple[float, ...] = (0.3, 0.5, 0.7)
_DEFAULT_CHECKPOINT_EVERY = 5
_DEFAULT_N_PERMUTATIONS = 2
_DEFAULT_TOP_K = 100
_DEFAULT_MIN_JACCARD = 0.05
_DEFAULT_NON_COLLAPSE_FLOOR = 1
_DEFAULT_OVERLAP_CEILING = 0.5
_DEFAULT_STABILITY_VARIANCE_BOUND = 4.0
_DEFAULT_TAIL_SAMPLE_SIZE = 20


@dataclass(frozen=True)
class AssertionRecord:
    """One `MembershipAssertion` read off the frozen evidence layer."""

    concept_id: str
    category_normalized_name: str
    category_display_name: str
    proposed_confidence: float
    episode_id: str
    when: datetime
    seed: int


@dataclass(frozen=True)
class CurvePoint:
    """One checkpoint on the category-count-vs-conversations curve.

    `raw_category_count` (no consolidation) is the free "no-consolidator"
    control curve — computed from the same snapshot at zero extra cost.
    """

    conversations_processed: int
    raw_category_count: int
    canonical_category_count: int


@dataclass(frozen=True)
class PlateauResult:
    """AC-3(a): does the canonical-category curve plateau, not grow linearly?"""

    first_tertile_rate: float
    final_tertile_rate: float
    rate_ceiling: float
    passes: bool


@dataclass(frozen=True)
class DistinctnessResult:
    """AC-3(d): are canonical categories distinct, not overlapping duplicates?"""

    pct_exceeding_ceiling: float
    overlap_histogram: list[float]
    passes: bool


@dataclass(frozen=True)
class StabilityResult:
    """AC-3(f): is the final canonical count stable across seeds?

    `passes` is `None` (not `True`) when fewer than 2 seeds are available —
    an insufficient-seeds caveat reported honestly, never silently treated
    as a pass.
    """

    n_seeds: int
    variance: float | None
    passes: bool | None


async def fetch_seeded_ledger(driver: Neo4jDriver, *, seed: int) -> list[AssertionRecord]:
    """Read every `MembershipAssertion` written under one categorizer run (seed).

    Args:
        driver: Connected async Neo4j driver pointed at the study sandbox.
        seed: The `AssertionProvenance.seed` to filter on.

    Returns:
        The full assertion ledger for that seed.
    """
    async with driver.session() as session:
        result = await session.run(
            "MATCH (a:MembershipAssertion {seed: $seed})-[:ABOUT]->(c:Concept) "
            "MATCH (a)-[:PROPOSES]->(cat:Category) "
            "MATCH (a)<-[:PRODUCED]-(:Mention)<-[:HAS_MENTION]-(ep:Episode) "
            "RETURN c.id AS concept_id, cat.normalized_name AS category_normalized_name, "
            "cat.display_name AS category_display_name, "
            "a.proposed_confidence AS proposed_confidence, "
            "ep.id AS episode_id, a.when AS when, a.seed AS seed",
            {"seed": seed},
        )
        rows = [r async for r in result]

    return [
        AssertionRecord(
            concept_id=str(r["concept_id"]),
            category_normalized_name=str(r["category_normalized_name"]),
            category_display_name=str(r["category_display_name"]),
            proposed_confidence=float(r["proposed_confidence"]),
            episode_id=str(r["episode_id"]),
            when=datetime.fromisoformat(str(r["when"])),
            seed=int(r["seed"]),
        )
        for r in rows
    ]


async def discover_seeds(driver: Neo4jDriver) -> list[int]:
    """Every distinct seed already present in the sandbox, ascending.

    Args:
        driver: Connected async Neo4j driver pointed at the study sandbox.

    Returns:
        Sorted distinct seed values.
    """
    async with driver.session() as session:
        result = await session.run("MATCH (a:MembershipAssertion) RETURN DISTINCT a.seed AS seed")
        rows = [r async for r in result]
    return sorted({int(r["seed"]) for r in rows})


def chronological_episode_order(ledger: list[AssertionRecord]) -> list[str]:
    """Distinct episode ids, ordered by each episode's earliest assertion timestamp."""
    earliest: dict[str, datetime] = {}
    for record in ledger:
        current = earliest.get(record.episode_id)
        if current is None or record.when < current:
            earliest[record.episode_id] = record.when
    return sorted(earliest, key=lambda ep: earliest[ep])


def permuted_orders(episode_ids: list[str], *, n_permutations: int, seed: int) -> list[list[str]]:
    """`n_permutations` deterministic shuffles of `episode_ids`.

    Each permutation uses its own derived seed (`seed * 1000 + i + 1`) so
    different permutation indices never collide, and the whole set is
    reproducible given the same `(episode_ids, n_permutations, seed)` —
    the ADR's "≥2 pre-registered permutations" requirement (AC-3) needs
    permutations fixed before results are seen, not regenerated per run.

    Args:
        episode_ids: The chronological episode order to permute.
        n_permutations: How many permutations to produce.
        seed: The base seed permutations are derived from.

    Returns:
        `n_permutations` shuffled copies of `episode_ids`.
    """
    orders = []
    for i in range(n_permutations):
        rng = random.Random(seed * 1000 + i + 1)
        order = list(episode_ids)
        rng.shuffle(order)
        orders.append(order)
    return orders


def build_snapshot(
    ledger: list[AssertionRecord], episodes_included: set[str]
) -> dict[str, CategoryMembers]:
    """Group ledger records restricted to `episodes_included` into a category snapshot.

    Args:
        ledger: The full (or partial) assertion ledger.
        episodes_included: Only records whose `episode_id` is in this set count.

    Returns:
        `{normalized_name: CategoryMembers}`, mirroring
        `consolidator.fetch_category_membership_snapshot`'s shape but computed
        in memory from a ledger slice instead of a live `MEMBER_OF` read.
    """
    display_name_by_category: dict[str, str] = {}
    concept_ids_by_category: dict[str, set[str]] = {}
    for record in ledger:
        if record.episode_id not in episodes_included:
            continue
        display_name_by_category.setdefault(
            record.category_normalized_name, record.category_display_name
        )
        concept_ids_by_category.setdefault(record.category_normalized_name, set()).add(
            record.concept_id
        )

    return {
        name: CategoryMembers(
            normalized_name=name,
            display_name=display_name_by_category[name],
            concept_ids=frozenset(concept_ids),
        )
        for name, concept_ids in concept_ids_by_category.items()
    }


def category_count_curve(
    ledger: list[AssertionRecord],
    order: list[str],
    *,
    tau_merge: float,
    checkpoint_every: int,
    top_k: int = _DEFAULT_TOP_K,
    min_jaccard: float = _DEFAULT_MIN_JACCARD,
) -> list[CurvePoint]:
    """Replay `order` in chunks of `checkpoint_every`, recording the raw
    (no-consolidator) and canonical category counts at each checkpoint.

    A thin wrapper over `build_checkpoint_snapshots` + `curve_from_checkpoints`
    kept for its simple single-τ_merge signature (existing callers/tests).
    `_amain`'s sweep loop calls the two underlying functions directly instead
    — Stage 1 (this function's `build_snapshot`/`generate_candidates_pairwise`
    calls) does not depend on `tau_merge`, so computing it once per checkpoint
    and reusing it across an entire τ_merge grid (code-review finding,
    FRE-842) avoids redundant O(n²) Jaccard work per grid value.

    Args:
        ledger: The full assertion ledger for one seed.
        order: The episode-id order to replay (chronological or a permutation).
        tau_merge: The merge threshold for this curve.
        checkpoint_every: Record a point every N conversations processed.
        top_k: Stage-1 candidate cap.
        min_jaccard: Stage-1 minimum member-overlap to consider a candidate.

    Returns:
        One `CurvePoint` per checkpoint, in `order`'s processing sequence.
    """
    checkpoints = build_checkpoint_snapshots(
        ledger, order, checkpoint_every=checkpoint_every, top_k=top_k, min_jaccard=min_jaccard
    )
    curve, _final_canonicalization, _final_memberships = curve_from_checkpoints(
        checkpoints, tau_merge=tau_merge
    )
    return curve


@dataclass(frozen=True)
class CheckpointSnapshot:
    """Stage-1 output for one checkpoint — independent of τ_merge, so it is
    computed once per (seed, ordering) and reused across an entire τ_merge
    grid by `curve_from_checkpoints`.
    """

    conversations_processed: int
    memberships: dict[str, CategoryMembers]
    candidates: list[CandidatePair]


def build_checkpoint_snapshots(
    ledger: list[AssertionRecord],
    order: list[str],
    *,
    checkpoint_every: int,
    top_k: int = _DEFAULT_TOP_K,
    min_jaccard: float = _DEFAULT_MIN_JACCARD,
) -> list[CheckpointSnapshot]:
    """Replay `order` in chunks of `checkpoint_every`, computing Stage 1
    (snapshot + candidate generation) ONCE per checkpoint — the τ_merge-
    independent work `category_count_curve`'s docstring already claimed was
    shared across a grid but, before this refactor, silently was not
    (code-review finding, FRE-842: Stage 1 was actually recomputed once per
    τ_merge value in the naive per-config loop).

    Name-cosine is intentionally NOT used here (no `name_embeddings` param) —
    the sweep's whole point is many τ_merge configs over the same snapshot;
    an embedder call per checkpoint would dominate runtime for no benefit
    Stage-1 candidate generation doesn't already get from the pure-Python
    Jaccard fallback at sandbox scale (see module docstring).

    Args:
        ledger: The full assertion ledger for one seed.
        order: The episode-id order to replay (chronological or a permutation).
        checkpoint_every: Record a point every N conversations processed.
        top_k: Stage-1 candidate cap.
        min_jaccard: Stage-1 minimum member-overlap to consider a candidate.

    Returns:
        One `CheckpointSnapshot` per checkpoint, in `order`'s processing sequence.
    """
    checkpoints: list[CheckpointSnapshot] = []
    included: set[str] = set()
    for i, episode_id in enumerate(order, start=1):
        included.add(episode_id)
        if i % checkpoint_every != 0 and i != len(order):
            continue
        snapshot = build_snapshot(ledger, included)
        candidates = generate_candidates_pairwise(snapshot, top_k=top_k, min_jaccard=min_jaccard)
        checkpoints.append(
            CheckpointSnapshot(
                conversations_processed=i, memberships=snapshot, candidates=candidates
            )
        )
    return checkpoints


def curve_from_checkpoints(
    checkpoints: list[CheckpointSnapshot], *, tau_merge: float
) -> tuple[list[CurvePoint], CanonicalizationResult | None, dict[str, CategoryMembers] | None]:
    """Stage 2 (τ_merge-dependent) over precomputed checkpoints.

    Returns the curve AND the final checkpoint's `CanonicalizationResult` +
    memberships snapshot, so callers (the sweep CLI) can feed them directly
    into `distinctness_check`/`top20_and_tail_tables` instead of recomputing
    Stage 1 + Stage 2 a second time from scratch (code-review finding,
    FRE-842).

    Args:
        checkpoints: `build_checkpoint_snapshots`' output for one (seed, ordering).
        tau_merge: The merge threshold for this curve.

    Returns:
        `(curve, final_canonicalization, final_memberships)` — the latter two
        are `None` only when `checkpoints` is empty.
    """
    curve: list[CurvePoint] = []
    final_canonicalization: CanonicalizationResult | None = None
    final_memberships: dict[str, CategoryMembers] | None = None
    for checkpoint in checkpoints:
        result = canonicalize(checkpoint.memberships, checkpoint.candidates, tau_merge=tau_merge)
        curve.append(
            CurvePoint(
                conversations_processed=checkpoint.conversations_processed,
                raw_category_count=len(checkpoint.memberships),
                canonical_category_count=result.canonical_category_count,
            )
        )
        final_canonicalization = result
        final_memberships = checkpoint.memberships
    return curve, final_canonicalization, final_memberships


def plateau_check(
    curve: list[CurvePoint],
    *,
    first_tertile_frac: float = 1 / 3,
    final_tertile_rate_ceiling: float = 0.25,
) -> PlateauResult:
    """AC-3(a): marginal new-canonical-categories rate in the final tertile
    must be at most `final_tertile_rate_ceiling` of the first-tertile rate.

    Args:
        curve: A `category_count_curve` result, checkpoints in order.
        first_tertile_frac: Fraction of checkpoints defining the first tertile.
        final_tertile_rate_ceiling: Max allowed final/first rate ratio.

    Returns:
        The plateau result. `passes` is `False` (not a crash) when the curve
        is too short to have two distinct tertiles, or when the first-tertile
        rate is zero (nothing to plateau from).
    """
    n = len(curve)
    tertile_size = max(1, int(n * first_tertile_frac))
    # The first window is curve[:tertile_size+1] (indices 0..tertile_size) and the
    # final window is curve[-(tertile_size+1):] (indices n-1-tertile_size..n-1) —
    # they share no index only when tertile_size < n-1-tertile_size, i.e.
    # 2*tertile_size + 1 < n. Code-review finding (FRE-842): the old guard
    # (`tertile_size * 2 >= n`) let n=3 through with tertile_size=1, computing
    # first=[0,1] and final=[1,2] — overlapping, correlated windows.
    if n < 2 or 2 * tertile_size + 1 >= n:
        return PlateauResult(0.0, 0.0, final_tertile_rate_ceiling, passes=False)

    def _rate(points: list[CurvePoint]) -> float:
        if len(points) < 2:
            return 0.0
        delta_count = points[-1].canonical_category_count - points[0].canonical_category_count
        delta_conversations = points[-1].conversations_processed - points[0].conversations_processed
        if delta_conversations <= 0:
            return 0.0
        return delta_count / delta_conversations

    first_rate = _rate(curve[: tertile_size + 1])
    final_rate = _rate(curve[-(tertile_size + 1) :])

    if first_rate <= 0.0:
        passes = final_rate <= 0.0
    else:
        passes = (final_rate / first_rate) <= final_tertile_rate_ceiling

    return PlateauResult(
        first_tertile_rate=first_rate,
        final_tertile_rate=final_rate,
        rate_ceiling=final_tertile_rate_ceiling,
        passes=passes,
    )


def non_collapse_check(curve: list[CurvePoint], *, floor: int) -> bool:
    """AC-3(e): the final canonical-category count must be at least `floor`."""
    if not curve:
        return False
    return curve[-1].canonical_category_count >= floor


def distinctness_check(
    memberships: dict[str, CategoryMembers],
    canonicalization: CanonicalizationResult,
    *,
    overlap_ceiling: float,
) -> DistinctnessResult:
    """AC-3(d): pairwise member-overlap Jaccard between DISTINCT canonical groups.

    Args:
        memberships: The snapshot canonicalization was computed from.
        canonicalization: The `canonicalize()` result.
        overlap_ceiling: Pairs at or above this Jaccard are "too overlapping".

    Returns:
        The overlap-pair histogram (every pairwise Jaccard among canonical
        groups) and the fraction exceeding `overlap_ceiling`.
    """
    from scripts.study.consolidator import _jaccard  # noqa: PLC0415

    group_members: dict[str, set[str]] = {}
    for original_name, canonical_name in canonicalization.canonical_of.items():
        group_members.setdefault(canonical_name, set()).update(
            memberships[original_name].concept_ids
        )

    canonical_names = sorted(group_members)
    histogram: list[float] = []
    for i, name_a in enumerate(canonical_names):
        for name_b in canonical_names[i + 1 :]:
            histogram.append(
                _jaccard(frozenset(group_members[name_a]), frozenset(group_members[name_b]))
            )

    exceeding = sum(1 for overlap in histogram if overlap >= overlap_ceiling)
    pct_exceeding = exceeding / len(histogram) if histogram else 0.0

    return DistinctnessResult(
        pct_exceeding_ceiling=pct_exceeding,
        overlap_histogram=histogram,
        passes=pct_exceeding < 0.10,
    )


def stochastic_stability_check(
    curves_by_seed: dict[int, list[CurvePoint]], *, variance_bound: float
) -> StabilityResult:
    """AC-3(f): variance of the final canonical-category count across seeds.

    Args:
        curves_by_seed: One curve (same ordering/τ_merge) per seed.
        variance_bound: Maximum acceptable population variance.

    Returns:
        `passes=None` (an explicit insufficient-seeds caveat, never a silent
        pass) when fewer than 2 seeds have a non-empty curve.
    """
    final_counts = [
        curve[-1].canonical_category_count for curve in curves_by_seed.values() if curve
    ]
    if len(final_counts) < 2:
        return StabilityResult(n_seeds=len(final_counts), variance=None, passes=None)

    variance = statistics.pvariance(final_counts)
    return StabilityResult(
        n_seeds=len(final_counts), variance=variance, passes=variance <= variance_bound
    )


def top20_and_tail_tables(
    memberships: dict[str, CategoryMembers],
    canonicalization: CanonicalizationResult,
    *,
    tail_sample_size: int,
    seed: int,
) -> dict[str, list[dict[str, object]]]:
    """The top-20 and a random tail sample of canonical categories, by member count.

    Produces tables ready for a human/LLM coherence rating pass (AC-3(b)/(c))
    — the rating itself is out of scope here (FRE-843's job at the chosen
    τ_merge*, see module docstring).

    Args:
        memberships: The snapshot canonicalization was computed from.
        canonicalization: The `canonicalize()` result.
        tail_sample_size: How many categories to sample from below the top-20.
        seed: Seed for the tail sample's deterministic random draw.

    Returns:
        `{"top20": [...], "tail_sample": [...]}`, each row
        `{"normalized_name": str, "member_count": int}`.
    """
    group_members: dict[str, set[str]] = {}
    for original_name, canonical_name in canonicalization.canonical_of.items():
        group_members.setdefault(canonical_name, set()).update(
            memberships[original_name].concept_ids
        )

    ranked = sorted(group_members.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    top20 = ranked[:20]
    remainder = ranked[20:]

    rng = random.Random(seed)
    tail_sample = rng.sample(remainder, k=min(tail_sample_size, len(remainder)))

    return {
        "top20": [
            {"normalized_name": name, "member_count": len(members)} for name, members in top20
        ],
        "tail_sample": [
            {"normalized_name": name, "member_count": len(members)} for name, members in tail_sample
        ],
    }


async def _amain(args: argparse.Namespace) -> dict[str, Any]:
    from neo4j import AsyncGraphDatabase  # noqa: PLC0415

    settings = StudySettings()
    _assert_study_target(settings)
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        seeds = args.seeds if args.seeds else await discover_seeds(driver)
        if not seeds:
            log.warning("sweep_no_seeds_found")
        report: dict[str, Any] = {
            "seeds": seeds,
            "tau_merge_grid": args.tau_merge_grid,
            "configs": [],
        }

        curves_by_seed_chronological: dict[float, dict[int, list[CurvePoint]]] = {
            tau: {} for tau in args.tau_merge_grid
        }

        for seed_value in seeds:
            ledger = await fetch_seeded_ledger(driver, seed=seed_value)
            if not ledger:
                continue
            chronological = chronological_episode_order(ledger)
            orderings = {"chronological": chronological}
            for i, permutation in enumerate(
                permuted_orders(
                    chronological, n_permutations=args.n_permutations, seed=args.permutation_seed
                )
            ):
                orderings[f"permutation_{i}"] = permutation

            for ordering_name, order in orderings.items():
                # Stage 1 (snapshot + candidate generation) does not depend on
                # tau_merge -- computed ONCE per (seed, ordering) and reused
                # across the whole grid below (code-review finding, FRE-842).
                checkpoints = build_checkpoint_snapshots(
                    ledger, order, checkpoint_every=args.checkpoint_every
                )

                for tau_merge in args.tau_merge_grid:
                    curve, final_canonicalization, final_snapshot = curve_from_checkpoints(
                        checkpoints, tau_merge=tau_merge
                    )
                    if ordering_name == "chronological":
                        curves_by_seed_chronological[tau_merge][seed_value] = curve
                    if final_canonicalization is None or final_snapshot is None:
                        continue  # empty ledger slice -- nothing to report for this config

                    plateau = plateau_check(curve)
                    non_collapse = non_collapse_check(curve, floor=_DEFAULT_NON_COLLAPSE_FLOOR)
                    distinctness = distinctness_check(
                        final_snapshot,
                        final_canonicalization,
                        overlap_ceiling=_DEFAULT_OVERLAP_CEILING,
                    )
                    tables = top20_and_tail_tables(
                        final_snapshot,
                        final_canonicalization,
                        tail_sample_size=_DEFAULT_TAIL_SAMPLE_SIZE,
                        seed=args.permutation_seed,
                    )

                    report["configs"].append(
                        {
                            "seed": seed_value,
                            "ordering": ordering_name,
                            "tau_merge": tau_merge,
                            "curve": [asdict(p) for p in curve],
                            "plateau": asdict(plateau),
                            "non_collapse_passes": non_collapse,
                            "distinctness": asdict(distinctness),
                            "top20_and_tail_tables": tables,
                        }
                    )

        stability_by_tau_merge = {
            tau: asdict(
                stochastic_stability_check(curves, variance_bound=_DEFAULT_STABILITY_VARIANCE_BOUND)
            )
            for tau, curves in curves_by_seed_chronological.items()
        }
        report["stability_by_tau_merge"] = stability_by_tau_merge
        return report
    finally:
        await driver.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--seeds",
        type=str,
        default="",
        help="Comma-separated seeds (default: discover from the sandbox).",
    )
    parser.add_argument(
        "--tau-merge-grid",
        type=str,
        default=",".join(str(t) for t in _DEFAULT_TAU_MERGE_GRID),
        help="Comma-separated τ_merge values to sweep.",
    )
    parser.add_argument("--checkpoint-every", type=int, default=_DEFAULT_CHECKPOINT_EVERY)
    parser.add_argument("--n-permutations", type=int, default=_DEFAULT_N_PERMUTATIONS)
    parser.add_argument("--permutation-seed", type=int, default=0)
    parser.add_argument("--run-id", type=str, default="")
    namespace = parser.parse_args()
    namespace.seeds = (
        [int(s) for s in namespace.seeds.split(",") if s.strip()] if namespace.seeds else []
    )
    namespace.tau_merge_grid = [float(t) for t in namespace.tau_merge_grid.split(",") if t.strip()]
    return namespace


def main() -> None:
    """CLI entrypoint."""
    args = _parse_args()
    report = asyncio.run(_amain(args))

    run_id = args.run_id or uuid.uuid4().hex[:8]
    out_dir = Path("scripts/study/snapshots")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"consolidator-sweep-{run_id}.json"
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
    log.info("sweep_run_done", out=str(out_path), configs=len(report["configs"]))

    print(f"Wrote {out_path}")
    for config in report["configs"]:
        print(
            f"seed={config['seed']} ordering={config['ordering']} tau_merge={config['tau_merge']} "
            f"plateau_passes={config['plateau']['passes']} "
            f"non_collapse={config['non_collapse_passes']} "
            f"distinctness_passes={config['distinctness']['passes']}"
        )
    for tau, stability in report["stability_by_tau_merge"].items():
        print(f"tau_merge={tau} stability={stability}")


if __name__ == "__main__":
    main()
