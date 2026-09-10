"""FRE-1477 -- calibrate the proactive relevance bound against the serving embedder arm.

ADR-0148 D4 requires a bound measured on the arm that is actually serving, validated
before its numbers are trusted, and committed as the source of the configured value. This
driver produces that artifact.

WHAT IT MEASURES
----------------
The proactive path admits on ``vector_score``, which is the score
``db.index.vector.queryNodes`` returns for the entity vector index
(``service.py:1000-1016``, copied untransformed into the row at ``:1049-1054``). So the
calibration measures that index, on that arm, over a labelled probe set: per expected
entity a positive, and per query the strongest non-match as the negative -- FRE-694's own
metric, whose statistic is the item admission is actually decided on.

THE PARITY GATE (both checks must pass before any number is reported)
---------------------------------------------------------------------
FRE-694's discipline is to validate the instrument against the production path first. Its
literal reference is a 0.6B measurement (``separation_benchmark.py:285``) and no 0.6B
endpoint serves today, so the *structure* transfers rather than the constant:

* **P1 -- cross-implementation.** ``fre817_corpus_ab_embedder.corpus_ab`` is an
  independently authored offline pipeline with its own corpus build, text formatting,
  query mode and cosine. Its three aggregates are compared against the same three from the
  Neo4j index. This is what catches shared corpus, formatting, mode or aggregation error,
  and it is the part a single pipeline compared against itself cannot provide.
* **P2 -- index fidelity.** Per scored candidate, the index's score against the cosine
  computed client-side over the very vectors the index holds. This closes the caveat
  FRE-694 stated about its own check: *"an embedding-geometry test, not a Neo4j-HNSW
  index-fidelity test."* FRE-817 recorded that the OVH-8B arm had no live-Neo4j reference
  because no production index ran at 4096 dimensions; the served width is now 1024 and the
  production index runs at it, so the reference FRE-817 lacked exists.

SUBSTRATE AND SPEND
-------------------
Writes to the **test** substrate only (FRE-375): :7688 / :9201 / :5433, pinned at module
top before any ``personal_agent`` import, and ``wipe_substrate`` refuses outside
``Environment.TEST``. Reads the serving embedder's endpoint and token from ``pass`` at run
time, never persisted and never logged. The corpus is 49 entities and 54 queries per
implementation, so roughly 200 embedding calls in total.

Run (test substrate up)::

    uv run python -m scripts.eval.fre1477_relevance_calibration.calibrate --run-id cal-$(date +%Y%m%d)
"""

from __future__ import annotations

import os
import subprocess


def _pass_show(entry: str) -> str:
    """Read one secret from the ``pass`` store (never logged, never persisted)."""
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["pass", "show", entry], capture_output=True, text=True, check=True
    )
    value = out.stdout.splitlines()[0].strip() if out.stdout else ""
    if not value:
        raise SystemExit(f"[fail-loud] `pass show {entry}` returned empty")
    return value


# Pin the TEST substrate before importing personal_agent (settings is a cached import-time
# singleton). Hard assignment, NOT setdefault, per the FRE-778 review finding: an ambient
# .env value must not win. AppConfig's FRE-375 guard checks all five substrate URIs for a
# prod fingerprint under APP_ENV=test regardless of which this script uses, so the admin
# and sysgraph URLs are pinned too even though nothing here does DDL or a sysgraph write.
_TEST_SUBSTRATE_ENV = {
    "APP_ENV": "test",
    "AGENT_NEO4J_URI": "bolt://localhost:7688",
    "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
    "AGENT_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
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

# The ONE deliberate departure from the test profile, and the reason this script exists.
#
# Every store above stays on the test stack, so no production row is read or written. The
# embedder is pinned to the MANAGED arm because the calibration's whole claim is that its
# number describes the arm that is actually serving (ADR-0148 D4: "No FRE-694 number
# describes the deployed arm"). Measuring a local default and committing the result as
# production's bound is the precise failure this pin prevents. The call is a read-only
# embedding request against a paid endpoint -- the same owner-authorized shape FRE-817
# recorded for its own OVH arm.
os.environ["AGENT_SUBSTRATE_PROFILE"] = "managed_embedder"
os.environ["AGENT_MANAGED_EMBEDDING_ENDPOINT"] = _pass_show("seshat/AGENT_OVH_AI_BASE_URL")
os.environ["AGENT_MANAGED_EMBEDDING_TOKEN"] = _pass_show("seshat/AGENT_MANAGED_EMBEDDING_TOKEN")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from datetime import date  # noqa: E402
from pathlib import Path  # noqa: E402

import structlog  # noqa: E402
from scripts.eval.fre435_memory_recall.harness import seed_replay, wipe_substrate  # noqa: E402
from scripts.eval.fre435_memory_recall.probes import ProbeCase, load_probe_set  # noqa: E402
from scripts.eval.fre435_memory_recall.separation_report import (  # noqa: E402
    truncate_renormalize,
)
from scripts.eval.fre817_corpus_ab_embedder.corpus_ab import (  # noqa: E402
    _build_corpus,
    _embed_ovh,
    _sanity_check_ovh,
    _unit,
)
from scripts.eval.fre1477_relevance_calibration.analysis import (  # noqa: E402
    PARITY_TOLERANCE,
    BoundProposal,
    aggregate_deltas,
    choose_bound,
    pair_deltas,
    parity_aggregates,
)

from personal_agent.config import settings  # noqa: E402
from personal_agent.memory.embeddings import generate_embedding  # noqa: E402
from personal_agent.memory.service import MemoryService  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_PROBE = "scripts/eval/fre435_memory_recall/semantic_probe.yaml"
DEFAULT_ARTIFACT = "config/calibration/proactive_relevance_bound.json"


def _neo4j_space(cosine: float) -> float:
    """Neo4j's vector-index normalization of a cosine: ``(cosine + 1) / 2``."""
    return (cosine + 1.0) / 2.0


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine of two vectors, unit-normalizing defensively first."""
    a = _unit(left)
    b = _unit(right)
    return sum(x * y for x, y in zip(a, b, strict=True))


def _expected_names(case: ProbeCase) -> set[str]:
    """The case's labelled positives, lowercased.

    ``expected.entity_names`` only -- FRE-694's metric, and ADR-0148 AC-5's denominator.
    Deliberately not unioned with ``seed_entities``: a seeded entity the case does not
    label as expected is a co-resident distractor, and counting it as a positive would
    inflate the admitted share the bound is judged on.
    """
    return {n.strip().lower() for n in case.expected.entity_names if n.strip()}


async def _entity_vectors(service: MemoryService) -> dict[str, list[float]]:
    """Read back every seeded entity's stored embedding, keyed by lowercased name.

    These are the vectors the index itself holds, so a client-side cosine over them
    isolates the index's behaviour from the embedder's (parity check P2).
    """
    vectors: dict[str, list[float]] = {}
    async with service.driver.session() as session:  # type: ignore[union-attr]
        result = await session.run(
            "MATCH (e:Entity) WHERE e.embedding IS NOT NULL "
            "RETURN e.name AS name, e.embedding AS embedding"
        )
        for row in await result.data():
            name = str(row.get("name") or "").strip().lower()
            if name:
                vectors[name] = [float(x) for x in row["embedding"]]
    return vectors


async def _ensure_index_at_serving_width(service: MemoryService) -> None:
    """Make the test index match the serving embedding width, or fail loud.

    A stale index left at another dimension by an earlier run would reject or mis-rank
    every write, and the run would report a clean calibration over a broken corpus.
    """
    width = settings.embedding_dimensions
    async with service.driver.session() as session:  # type: ignore[union-attr]
        result = await session.run(
            "SHOW INDEXES YIELD name, options WHERE name = 'entity_embedding' "
            "RETURN options AS options"
        )
        rows = await result.data()
        existing: int | None = None
        if rows:
            config = (rows[0].get("options") or {}).get("indexConfig") or {}
            raw = config.get("vector.dimensions")
            existing = int(raw) if raw is not None else None
        if existing is not None and existing != width:
            log.warning("dropping_stale_vector_index", existing=existing, wanted=width)
            await session.run("DROP INDEX entity_embedding IF EXISTS")
    if not await service.ensure_vector_index():
        raise SystemExit("[fail-loud] could not create the entity_embedding vector index")


async def _measure_index_side(
    service: MemoryService, cases: Sequence[ProbeCase], top_k: int
) -> tuple[list[float], list[float], list[tuple[float, float]]]:
    """Measure positives, negatives and P2 pairs through the production index path.

    Args:
        service: Connected memory service on the test substrate.
        cases: The probe cases.
        top_k: Vector-index candidate width, sized to cover the whole corpus.

    Returns:
        ``(positives, negatives, pairs)``. Positives are per expected entity; the negative
        is each query's strongest non-match. ``pairs`` holds ``(index_score, client_side
        score)`` for exactly the candidates that entered those two populations, so parity
        is asserted over the measurement that produced the bound and not over a
        differently drawn sample.
    """
    stored = await _entity_vectors(service)
    positives: list[float] = []
    negatives: list[float] = []
    pairs: list[tuple[float, float]] = []

    for case in cases:
        query_vec = await generate_embedding(case.query, mode="query")
        if not any(x != 0.0 for x in query_vec):
            raise SystemExit(f"[fail-loud] degenerate query embedding for case {case.case_id}")
        async with service.driver.session() as session:  # type: ignore[union-attr]
            rows = await service._query_entity_vector_candidates(session, query_vec, top_k)

        expected = _expected_names(case)
        scored = [(str(r.get("name") or "").strip().lower(), float(r["score"])) for r in rows]

        # Exactly the candidates that enter the two score populations are also the ones
        # paired for P2, so parity is asserted over the measurement that produced the
        # bound rather than over a differently drawn sample.
        measured: list[tuple[str, float]] = [(n, s) for n, s in scored if n in expected]
        positives.extend(score for _, score in measured)
        non_matches = [(n, s) for n, s in scored if n not in expected]
        if non_matches:
            top_non_match = max(non_matches, key=lambda p: p[1])
            negatives.append(top_non_match[1])
            measured.append(top_non_match)
        for name, score in measured:
            vector = stored.get(name)
            if vector is not None:
                pairs.append((score, _neo4j_space(_cosine(query_vec, vector))))

        log.info(
            "calibrate_case",
            case=case.case_id,
            expected=len(expected),
            returned=len(scored),
        )
    return positives, negatives, pairs


async def _measure_offline_side(
    cases: Sequence[ProbeCase], model: str, base_url: str, token: str
) -> tuple[list[float], list[float]]:
    """Measure the same two populations through an independently authored pipeline (P1).

    Uses ``fre817_corpus_ab_embedder``'s corpus build, entity text, query prefix and OVH
    client -- a separate implementation of the same geometry. Agreement between the two is
    the evidence that neither carries a construction error the other shares.
    """
    notes, _ = _build_corpus(cases)
    names = list(notes)
    width = settings.embedding_dimensions
    # `_embed_ovh` sends no `dimensions` parameter, so the endpoint answers at the model's
    # native 4096 while production requests and stores `width` (1024). Comparing those two
    # geometries would not be a parity check at all -- FRE-694 measured separation as
    # materially dimension-dependent on this very model (Youden J 0.550 at 1024 against
    # 0.534 at native 4096), so the difference is a real one, not rounding. Reduce
    # client-side with the MRL truncate-and-renormalize FRE-694 validated as equivalent to
    # the server-side parameter (cosine ~0.999). Keeping the reduction here rather than
    # changing `_embed_ovh` leaves FRE-817's harness behaviour untouched, and it adds a
    # third independently authored element to this side of the comparison.
    note_vectors = [
        truncate_renormalize(v, width)
        for v in await _embed_ovh([notes[n] for n in names], "document", base_url, token, model)
    ]
    query_vectors = [
        truncate_renormalize(v, width)
        for v in await _embed_ovh([c.query for c in cases], "query", base_url, token, model)
    ]

    positives: list[float] = []
    negatives: list[float] = []
    for case, query_vec in zip(cases, query_vectors, strict=True):
        expected = _expected_names(case)
        scored = [
            (name, _neo4j_space(_cosine(query_vec, vec)))
            for name, vec in zip(names, note_vectors, strict=True)
        ]
        positives.extend(score for name, score in scored if name in expected)
        non_matches = [s for n, s in scored if n not in expected]
        if non_matches:
            negatives.append(max(non_matches))
    return positives, negatives


def _run_parity(
    index_positives: Sequence[float],
    index_negatives: Sequence[float],
    offline_positives: Sequence[float],
    offline_negatives: Sequence[float],
    pairs: Sequence[tuple[float, float]],
) -> tuple[dict[str, float], dict[str, float], bool]:
    """Run both parity checks and report whether the instrument is validated."""
    index_aggregates = parity_aggregates(index_positives, index_negatives)
    offline_aggregates = parity_aggregates(offline_positives, offline_negatives)
    deltas = aggregate_deltas(index_aggregates, offline_aggregates)
    p2 = pair_deltas(pairs)

    print(
        f"\n=== PARITY P1 -- Neo4j index vs independent offline pipeline (n={len(pairs)} pairs) ==="
    )
    p1_ok = True
    for metric in sorted(deltas):
        within = deltas[metric] <= PARITY_TOLERANCE
        p1_ok = p1_ok and within
        print(
            f"  {metric:11s} index={index_aggregates[metric]:.4f}  "
            f"offline={offline_aggregates[metric]:.4f}  delta={deltas[metric]:.4f}  "
            f"{'OK' if within else 'MISMATCH'}"
        )
    print("\n=== PARITY P2 -- index score vs client-side cosine over the stored vectors ===")
    p2_max = max(p2)
    p2_ok = p2_max <= PARITY_TOLERANCE
    print(f"  pairs={len(p2)}  max_delta={p2_max:.6f}  {'OK' if p2_ok else 'MISMATCH'}")

    if p1_ok and p2_ok:
        print(f"\nPARITY HOLDS (all deltas <= {PARITY_TOLERANCE}) -- numbers trustworthy.")
    else:
        print(
            f"\nPARITY FAILED (delta > {PARITY_TOLERANCE}) -- STOP and reconcile. No bound reported."
        )
    return index_aggregates, offline_aggregates, p1_ok and p2_ok


def _artifact_payload(
    proposal: BoundProposal,
    positives: Sequence[float],
    negatives: Sequence[float],
    index_aggregates: dict[str, float],
    offline_aggregates: dict[str, float],
    pairs: Sequence[tuple[float, float]],
    probe_set: str,
) -> dict[str, object]:
    """Build the committed artifact (schema: ``personal_agent.config.calibration``)."""
    bound_embedding_term = (
        None if proposal.bound is None else round(max(0.0, 2.0 * proposal.bound - 1.0), 6)
    )
    return {
        "component": {
            "role": "embedder",
            "model": settings.managed_embedding_model,
            "dimensions": settings.embedding_dimensions,
        },
        "measured_on": date.today().isoformat(),
        "probe_set": probe_set,
        "incompatible": proposal.incompatible,
        "incompatible_reason": proposal.reason,
        "bound_neo4j_space": proposal.bound,
        "bound_embedding_term": bound_embedding_term,
        "positive_scores": [round(s, 6) for s in positives],
        "negative_scores": [round(s, 6) for s in negatives],
        "positive_admitted_share": proposal.positive_admitted_share,
        "negative_median_neo4j": round(proposal.negative_median, 6),
        "parity": {
            "tolerance": PARITY_TOLERANCE,
            "p1_offline_aggregates": {k: round(v, 6) for k, v in offline_aggregates.items()},
            "p1_index_aggregates": {k: round(v, 6) for k, v in index_aggregates.items()},
            "p2_pairs": [[round(a, 6), round(b, 6)] for a, b in pairs],
        },
    }


async def run(args: argparse.Namespace) -> int:
    """Seed, measure, validate, choose, and write the artifact."""
    cases = load_probe_set(Path(args.probe_set))
    print(f"probe set: {args.probe_set} -- {len(cases)} cases")
    if args.dry_run:
        notes, _ = _build_corpus(cases)
        print(f"corpus: {len(notes)} entities, {len(cases)} queries (dry run, nothing embedded)")
        return 0

    base_url = _pass_show("seshat/AGENT_OVH_AI_BASE_URL")
    token = _pass_show("seshat/AGENT_MANAGED_EMBEDDING_TOKEN")
    model = settings.managed_embedding_model
    await _sanity_check_ovh(base_url, token, model)

    service = MemoryService()  # fre-375-allow: test stack pinned module-top (:7688)
    if not await service.connect():
        raise SystemExit("[fail-loud] test substrate unavailable")
    try:
        await _ensure_index_at_serving_width(service)
        await wipe_substrate(service, str(uuid.uuid4()))
        for case in cases:
            await seed_replay(service, case, str(uuid.uuid4()), f"fre1477-cal-{case.case_id}")
        top_k = max(settings.proactive_memory_vector_top_k, len(cases) * 2)
        positives, negatives, pairs = await _measure_index_side(service, cases, top_k)
    finally:
        await service.disconnect()

    if not positives or not negatives:
        raise SystemExit(
            f"[fail-loud] empty score population (positives={len(positives)}, "
            f"negatives={len(negatives)}) -- the seed or the index is broken"
        )
    offline_positives, offline_negatives = await _measure_offline_side(
        cases, model, base_url, token
    )

    index_aggregates, offline_aggregates, parity_ok = _run_parity(
        positives, negatives, offline_positives, offline_negatives, pairs
    )
    if not parity_ok:
        return 1

    proposal = choose_bound(positives, negatives)
    print(f"\n=== FRE-1477 relevance bound -- {model} @ {settings.embedding_dimensions} ===")
    print(f"positives n={len(positives)}  negatives n={len(negatives)}")
    print(f"negative median (the value the bound must reject) = {proposal.negative_median:.4f}")
    if proposal.incompatible:
        print(f"\nINCOMPATIBLE -- no bound reported.\n  {proposal.reason}")
    else:
        assert proposal.bound is not None
        term = max(0.0, 2.0 * proposal.bound - 1.0)
        print(f"bound (Neo4j space)    = {proposal.bound:.4f}")
        print(f"bound (embedding term) = {term:.4f}   <- the configured value")
        print(f"positives admitted     = {proposal.positive_admitted_share:.1%}")
        print(f"negatives rejected     = {proposal.negative_rejected_share:.1%}")

    payload = _artifact_payload(
        proposal, positives, negatives, index_aggregates, offline_aggregates, pairs, args.probe_set
    )
    out = Path(args.artifact)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nartifact written: {out}")
    return 0


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="local", help="Run label for logs.")
    parser.add_argument("--probe-set", default=DEFAULT_PROBE, help="Labelled probe set YAML.")
    parser.add_argument("--artifact", default=DEFAULT_ARTIFACT, help="Committed artifact path.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the corpus shape without embedding anything.",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
