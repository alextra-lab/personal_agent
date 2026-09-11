"""FRE-1479 -- calibrate the broad-recall relevance bound against the serving reranker.

ADR-0148 D4 requires a bound measured on the component that is actually serving, validated
before its numbers are trusted, and committed as the source of the configured value. This
driver produces that artifact for the broad-recall path.

WHY THIS PATH NEEDS ITS OWN BOUND
---------------------------------
FRE-1477 calibrated the proactive path against the serving embedder arm and reported an
**incompatibility**: on ``Qwen3-Embedding-8B`` @ 1024 the lowest bound that rejects the
median top-ranked non-match admits only 84.2% of the labelled positives, under D4's 90%.
D4 names the response, and it is not a lower bar: *"the response is a reranker-side bound or
a different arm, not a quietly chosen number."* This is that reranker-side bound.

Nothing here assumes it will succeed. FRE-695 measured Voyage rerank-2.5 at Youden J 0.73
and judged even its best reranker arm at roughly "88% recall @ ~9% FP". The harness reports
what it measures, and D4 reserves the incompatible case rather than filling it.

WHAT IT MEASURES
----------------
The **production** pair: ``MemoryService._resolve_item_texts`` builds the documents, and
``memory.reranker.rerank`` scores them. Not a re-implementation of either. That matters
because the two document shapes differ and the gate sees both:

* entity documents -- ``name + ' ' + description`` (``service.py:5255``);
* turn documents -- ``coalesce(summary, user_message, '')`` (``service.py:5266``), which
  reach the admission boundary as the entities the turn discusses (``service.py:5417``).

A bound measured only on entity-shaped documents would gate turn-derived entities with a
number that never described them, so both populations are measured and one bound governs
their union -- because one bound governs the path. The artifact records the two halves
separately as well, so a reader can see whether one shape drags the other.

The metric is FRE-694's and FRE-695's: per expected entity a positive, and per query the
strongest non-match as the negative.

THE PARITY GATE (all three checks must pass before any number is reported)
--------------------------------------------------------------------------
* **P1 -- cross-implementation, entity documents only.** ``separation_benchmark.py``'s
  ``voyage-rerank-2.5`` arm is an independently authored pipeline with its own corpus build
  (``"{name}: {description}"``), its own HTTP client and its own response parser. Its three
  aggregates are compared against the production path's at FRE-694's own 0.02 tolerance.
  This is what catches shared corpus, formatting or aggregation error, and it is what a
  single pipeline compared against itself cannot provide.

  **Restricted to entity documents deliberately.** The independent harness reads entity rows
  from the probe YAML and builds no turn documents at all, so a comparison over the full
  production population would put 93 candidates against 49 and call the difference a parity
  delta. The first run of this harness did exactly that and failed -- neg_max 0.0664,
  pos_median 0.0469, both over tolerance -- which is the gate working rather than the number
  being wrong. Restricting the comparison to the population both sides actually build makes
  it a parity check again. The turn population rides the same production code path P1
  validates, and P2R and P3 cover it.
* **P2R -- instrument sanity.** FRE-695's own gate: a trivially relevant document must
  outrank a trivially irrelevant one. FRE-1477's P2 was a Neo4j index-fidelity check and has
  **no reranker analogue** -- no index sits between the model and the score -- so it is
  recorded as absent rather than replaced by a vacuous substitute that would report a
  validated instrument on no evidence.
* **P3 -- listwise sensitivity.** This harness scores the whole corpus per query; production
  reranks a fused set capped at ``reranker_input_cap``. A rerank request is listwise, so the
  wider candidate set could in principle move a pair's score. P3 measures how far it actually
  moves rather than asserting that it does not.

SUBSTRATE AND SPEND
-------------------
Writes to the **test** substrate only (FRE-375): :7688 / :9201 / :5433, pinned at module top
before any ``personal_agent`` import, and ``wipe_substrate`` refuses outside
``Environment.TEST``. The graph is needed because turn documents and their
``Turn-[:DISCUSSES]->Entity`` edges must exist to be built and labelled the way production
builds them. The Voyage key is read from ``pass`` at run time, never logged, never persisted.
Roughly 54 rerank requests per implementation.

Run (test substrate up)::

    uv run python -m scripts.eval.fre1479_reranker_calibration.calibrate --run-id cal-$(date +%Y%m%d)
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
# prod fingerprint under APP_ENV=test regardless of which this script uses.
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
# reranker is the component under measurement, so it must be the one that actually serves
# (ADR-0148 D4). Measuring a stand-in and committing the result as production's bound is the
# precise failure this pin prevents. The call is a read-only rerank request against a paid
# endpoint -- the same owner-authorized shape FRE-695 and FRE-1477 both recorded.
os.environ["AGENT_RERANKER_ENABLED"] = "true"
os.environ["AGENT_VOYAGE_API_KEY"] = _pass_show("VOYAGEAI_API_KEY")

# The embedder is pinned to the managed arm as well, and it is NOT under measurement here.
# `seed_replay` writes entities through the production `create_entity`, which embeds each
# one, so the seed needs a reachable embedder to complete at all. Nothing downstream reads
# those vectors: a reranker scores text, and this harness hands it documents built by
# `_resolve_item_texts` rather than anything the vector index returned. Pinning the same
# managed arm FRE-1477 used keeps the seed identical to that run rather than introducing a
# second corpus shape.
os.environ["AGENT_SUBSTRATE_PROFILE"] = "managed_embedder"
os.environ["AGENT_MANAGED_EMBEDDING_ENDPOINT"] = _pass_show("seshat/AGENT_OVH_AI_BASE_URL")
os.environ["AGENT_MANAGED_EMBEDDING_TOKEN"] = _pass_show("seshat/AGENT_MANAGED_EMBEDDING_TOKEN")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import date  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import structlog  # noqa: E402
from scripts.eval.fre435_memory_recall.harness import seed_replay, wipe_substrate  # noqa: E402
from scripts.eval.fre435_memory_recall.probes import ProbeCase, load_probe_set  # noqa: E402
from scripts.eval.fre435_memory_recall.separation_benchmark import (  # noqa: E402
    _SANITY_IRRELEVANT,
    _SANITY_QUERY,
    _SANITY_RELEVANT,
    RERANKER_ARMS,
    _build_corpus,
    _rerank,
    _rerank_headers,
    positives_negatives_for_case,
)
from scripts.eval.fre1479_reranker_calibration.analysis import (  # noqa: E402
    GATED_PARITY_STATISTICS,
    PARITY_TOLERANCE,
    BoundProposal,
    SanityResult,
    admitted_share,
    aggregate_deltas,
    choose_bound,
    reranker_parity_aggregates,
    sanity_holds,
)

from personal_agent.memory.fusion import FusedResult  # noqa: E402
from personal_agent.memory.reranker import rerank  # noqa: E402
from personal_agent.memory.service import MemoryService  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_PROBE = "scripts/eval/fre435_memory_recall/semantic_probe.yaml"
DEFAULT_ARTIFACT = "config/calibration/broad_recall_relevance_bound.json"

#: The independently authored comparison arm for parity check P1.
INDEPENDENT_ARM = "voyage-rerank-2.5"


def _expected_names(case: ProbeCase) -> frozenset[str]:
    """The case's labelled positives, lowercased.

    ``expected.entity_names`` only -- FRE-694's metric and ADR-0148 AC-5's denominator.
    Deliberately not unioned with ``seed_entities``: a seeded entity the case does not label
    as expected is a co-resident distractor, and counting it as a positive would inflate the
    admitted share the bound is judged on.
    """
    return frozenset(n.strip().lower() for n in case.expected.entity_names if n.strip())


async def _corpus_items(service: MemoryService) -> tuple[list[FusedResult], dict[str, str]]:
    """Read back every seeded entity and turn as the fused items production would rerank.

    Returns:
        ``(items, labels)`` -- the fused items in the shape ``_resolve_item_texts`` consumes,
        and each item's label key: an entity's own lowercased name, and for a turn the
        lowercased names of the entities it discusses, joined by ``|``. The label is what
        makes a turn attributable to the expected set, exactly as
        ``_multipath_broad_entities`` expands it.
    """
    items: list[FusedResult] = []
    labels: dict[str, str] = {}
    async with service.driver.session() as session:  # type: ignore[union-attr]
        result = await session.run("MATCH (e:Entity) RETURN elementId(e) AS id, e.name AS name")
        for rank, row in enumerate(await result.data(), start=1):
            name = str(row.get("name") or "").strip().lower()
            if not name:
                continue
            items.append(FusedResult(row["id"], 1.0 / rank, 1, kind="entity"))
            labels[row["id"]] = name
        result = await session.run(
            """
            MATCH (t:Turn)
            OPTIONAL MATCH (t)-[:DISCUSSES]->(e:Entity)
            RETURN t.turn_id AS id, collect(DISTINCT e.name) AS names
            """
        )
        for rank, row in enumerate(await result.data(), start=1):
            turn_id = row.get("id")
            if not turn_id:
                continue
            discussed = [str(n).strip().lower() for n in row["names"] if n]
            items.append(FusedResult(turn_id, 1.0 / rank, 1, kind="turn"))
            labels[turn_id] = "|".join(discussed)
    return items, labels


def _is_positive(label: str, expected: frozenset[str]) -> bool:
    """Whether a candidate's label intersects the case's expected entity names."""
    return any(part in expected for part in label.split("|") if part)


@dataclass(frozen=True)
class ProductionMeasurement:
    """What the production document + rerank pair produced.

    Attributes:
        positives: Every labelled positive, both document populations combined. This is
            the population the bound is chosen on, because it is the population the gate
            governs.
        negatives: Each query's strongest non-match over both populations.
        entity_positives: The entity-document half of ``positives``.
        turn_positives: The turn-document half.
        entity_negatives: Each query's strongest non-match **restricted to entity
            documents**. Used only for parity check P1, because the independently authored
            harness has no turn documents to compare against -- see ``_report_parity``.
        items: The fused items scored, in document order.
        documents: The documents, as ``_resolve_item_texts`` built them.
    """

    positives: list[float]
    negatives: list[float]
    entity_positives: list[float]
    turn_positives: list[float]
    entity_negatives: list[float]
    items: list[FusedResult]
    documents: list[str]


async def _score_one_query(query: str, documents: Sequence[str], case_id: str) -> list[float]:
    """Score every document for one query through the production ``rerank()`` call.

    Two conditions abort the run rather than degrade it, because a bound measured under
    either would describe nothing:

    * **Passthrough.** ``rerank()`` never raises -- it returns fabricated ``1 / (i + 1)``
      scores with ``model_id`` None. Those are rank order, and calibrating on them would
      produce a bound describing the corpus's iteration order.
    * **A truncated response.** ``_attempt_rerank`` accepts however many entries the backend
      supplies with no completeness check, so a partial answer would silently leave
      unscored documents sitting at 0.0 and drag the negative distribution down.

    Args:
        query: The recall query.
        documents: The candidate documents.
        case_id: Probe case identifier, for the failure message.

    Returns:
        Scores in input-document order.

    Raises:
        SystemExit: On a passthrough or truncated response.
    """
    results = await rerank(query=query, documents=list(documents), top_k=len(documents))
    scored = [0.0] * len(documents)
    seen = 0
    for res in results:
        if res.model_id is None:
            raise SystemExit(
                "[fail-loud] the reranker degraded to passthrough -- its scores are rank "
                "order, not relevance, and a bound measured on them would be meaningless"
            )
        scored[res.index] = res.score
        seen += 1
    if seen != len(documents):
        raise SystemExit(
            f"[fail-loud] truncated rerank response for case {case_id}: "
            f"{seen} scores for {len(documents)} documents"
        )
    return scored


async def _measure_production_side(
    service: MemoryService, cases: Sequence[ProbeCase]
) -> ProductionMeasurement:
    """Measure both document populations through the production document + rerank pair.

    Args:
        service: Connected memory service on the test substrate, holding the seeded corpus.
        cases: The probe cases.

    Returns:
        The measurement, all scores in the serving reranker's own score space.
    """
    items, labels = await _corpus_items(service)
    texts = await service._resolve_item_texts(items)
    documents = [texts.get(item.item_id, "") for item in items]

    positives: list[float] = []
    negatives: list[float] = []
    entity_positives: list[float] = []
    turn_positives: list[float] = []
    entity_negatives: list[float] = []

    for case in cases:
        scored = await _score_one_query(case.query, documents, case.case_id)
        expected = _expected_names(case)
        labelled = [
            (item, _is_positive(labels[item.item_id], expected), score)
            for item, score in zip(items, scored, strict=True)
        ]
        non_matches = [score for _, positive, score in labelled if not positive]
        if non_matches:
            negatives.append(max(non_matches))
        entity_non_matches = [
            score for item, positive, score in labelled if not positive and item.kind == "entity"
        ]
        if entity_non_matches:
            entity_negatives.append(max(entity_non_matches))
        for item, positive, score in labelled:
            if not positive:
                continue
            positives.append(score)
            (entity_positives if item.kind == "entity" else turn_positives).append(score)

        log.info(
            "calibrate_case", case=case.case_id, expected=len(expected), candidates=len(documents)
        )
    return ProductionMeasurement(
        positives=positives,
        negatives=negatives,
        entity_positives=entity_positives,
        turn_positives=turn_positives,
        entity_negatives=entity_negatives,
        items=items,
        documents=documents,
    )


async def _measure_independent_side(
    cases: Sequence[ProbeCase],
) -> tuple[list[float], list[float]]:
    """Measure the same two populations through ``separation_benchmark``'s Voyage arm (P1).

    A separate implementation of the same geometry: its own corpus build
    (``"{name}: {description}"`` rather than ``name + ' ' + description``), its own HTTP
    client, its own retry policy and its own response parser -- and it reads entity rows
    from the probe YAML rather than from Neo4j. Agreement between the two sides is the
    evidence that neither carries a construction error the other shares, which is the part
    a single pipeline compared against itself cannot supply.

    Args:
        cases: The probe cases.

    Returns:
        ``(positives, negatives)`` in the reranker's own score space.
    """
    arm_meta = RERANKER_ARMS[INDEPENDENT_ARM]
    notes_by_entity, corpus_cases = _build_corpus(cases)
    note_names = list(notes_by_entity)
    documents = [notes_by_entity[name] for name in note_names]

    positives: list[float] = []
    negatives: list[float] = []
    async with httpx.AsyncClient(timeout=240.0, headers=_rerank_headers(arm_meta)) as client:
        for case in corpus_cases:
            expected = {n.strip().lower() for n in case.expected.entity_names if n.strip()}
            scores = await _rerank(client, arm_meta, case.query, documents)
            case_positives, negative = positives_negatives_for_case(expected, note_names, scores)
            positives.extend(case_positives)
            negatives.append(negative)
    return positives, negatives


async def _listwise_sensitivity(
    service: MemoryService, case: ProbeCase, items: Sequence[FusedResult], documents: Sequence[str]
) -> float:
    """Maximum score movement when the candidate set shrinks to the production cap (P3).

    A declared limitation, measured rather than asserted. This harness scores the **whole**
    corpus per query, while production reranks the RRF-fused set capped at
    ``reranker_input_cap`` (``settings.py:616``). Reproducing production's cap would mean
    running the retrieval arms, which drags an embedder and a fusion step into a reranker
    calibration and makes the number describe three components instead of one.

    A rerank request is listwise, so the wider candidate set *could* move a pair's score.
    This measures how far it actually moves: the same query is scored over the full corpus
    and over the first ``reranker_input_cap`` documents, and the largest per-document
    difference across the shared documents is reported. A small value means the bound does
    not depend on the candidate-set width; a large one would invalidate the whole approach
    and is recorded rather than discovered later.

    Args:
        service: Unused, kept for symmetry with the other measurement helpers.
        case: One probe case to probe the sensitivity on.
        items: The fused items, aligned with ``documents``.
        documents: The full document set.

    Returns:
        The maximum absolute score difference over the shared documents.
    """
    from personal_agent.config import settings  # noqa: PLC0415 — after the module-top pin

    cap = min(settings.reranker_input_cap, len(documents))
    full = await rerank(query=case.query, documents=list(documents), top_k=len(documents))
    capped = await rerank(query=case.query, documents=list(documents[:cap]), top_k=cap)
    full_by_index = {r.index: r.score for r in full}
    return max(
        (abs(full_by_index.get(r.index, 0.0) - r.score) for r in capped),
        default=0.0,
    )


async def _run_sanity() -> SanityResult:
    """FRE-695's instrument-sanity gate, on the serving reranker."""
    results = await rerank(
        query=_SANITY_QUERY, documents=[_SANITY_RELEVANT, _SANITY_IRRELEVANT], top_k=2
    )
    by_index = {r.index: r.score for r in results}
    return SanityResult(relevant_score=by_index.get(0, 0.0), irrelevant_score=by_index.get(1, 0.0))


def _report_parity(
    production: dict[str, float],
    independent: dict[str, float],
    sanity: SanityResult,
    listwise_delta: float,
) -> bool:
    """Print every check and report whether the instrument is validated."""
    deltas = aggregate_deltas(production, independent)
    print(
        f"\n=== PARITY P1 -- production rerank path vs {INDEPENDENT_ARM} (independent), "
        "entity documents only ==="
    )
    print(
        "  Restricted to the entity-document population, because that is the only one the\n"
        "  independent harness builds -- it reads entity rows from the probe YAML and has no\n"
        "  turn documents at all. Comparing 93 production candidates against its 49 would be\n"
        "  two different measurements wearing one delta, and the first run of this harness\n"
        "  failed exactly that way (neg_max 0.0664, pos_median 0.0469). The turn population\n"
        "  rides the same code path this check validates, plus P2R and P3."
    )
    p1_ok = True
    for metric in sorted(deltas):
        gated = metric in GATED_PARITY_STATISTICS
        within = deltas[metric] <= PARITY_TOLERANCE
        if gated:
            p1_ok = p1_ok and within
        verdict = ("OK" if within else "MISMATCH") if gated else "reported, not gated"
        print(
            f"  {metric:11s} production={production[metric]:.4f}  "
            f"independent={independent[metric]:.4f}  delta={deltas[metric]:.4f}  "
            f"{verdict}"
        )
    print("\n=== PARITY P2R -- instrument sanity (FRE-695's own gate) ===")
    p2_ok = sanity_holds(sanity)
    print(
        f"  relevant={sanity.relevant_score:.4f}  irrelevant={sanity.irrelevant_score:.4f}  "
        f"{'OK' if p2_ok else 'MISMATCH'}"
    )
    print(
        "  (FRE-1477's P2 index-fidelity check has no reranker analogue -- no index sits "
        "between the model and the score -- so it is absent rather than simulated.)"
    )
    print("\n=== P3 -- listwise sensitivity to candidate-set width (declared limitation) ===")
    p3_ok = listwise_delta <= PARITY_TOLERANCE
    print(
        f"  max score movement, full corpus vs the production input cap: "
        f"{listwise_delta:.6f}  {'OK' if p3_ok else 'MATERIAL'}"
    )
    print(
        "  This harness scores the whole corpus per query; production reranks a capped "
        "fused set. A material movement would mean the bound describes a candidate-set "
        "width production never uses."
    )
    if p1_ok and p2_ok and p3_ok:
        print(f"\nPARITY HOLDS (deltas <= {PARITY_TOLERANCE}) -- numbers trustworthy.")
    else:
        print("\nPARITY FAILED -- STOP and reconcile. No bound reported.")
    return p1_ok and p2_ok and p3_ok


def _artifact_payload(
    proposal: BoundProposal,
    model: str,
    measurement: ProductionMeasurement,
    production: dict[str, float],
    independent: dict[str, float],
    sanity: SanityResult,
    listwise_delta: float,
    probe_set: str,
) -> dict[str, object]:
    """Build the committed artifact (schema: ``personal_agent.config.calibration``).

    ``dimensions`` is 1 because a reranker emits one scalar per pair and has no embedding
    width. The field belongs to ``CalibrationComponent``, which both roles share, and it is
    recorded rather than omitted so the shape stays uniform across the two artifacts.
    """
    return {
        "component": {"role": "reranker", "model": model, "dimensions": 1},
        "measured_on": date.today().isoformat(),
        "probe_set": probe_set,
        "incompatible": proposal.incompatible,
        "incompatible_reason": proposal.reason,
        "bound": proposal.bound,
        "positive_scores": [round(s, 6) for s in measurement.positives],
        "negative_scores": [round(s, 6) for s in measurement.negatives],
        "entity_positive_scores": [round(s, 6) for s in measurement.entity_positives],
        "turn_positive_scores": [round(s, 6) for s in measurement.turn_positives],
        "positive_admitted_share": proposal.positive_admitted_share,
        "negative_median_rerank": round(proposal.negative_median, 6),
        "parity": {
            "tolerance": PARITY_TOLERANCE,
            "p1_production_aggregates": {k: round(v, 6) for k, v in production.items()},
            "p1_independent_aggregates": {k: round(v, 6) for k, v in independent.items()},
            "sanity_relevant_score": round(sanity.relevant_score, 6),
            "sanity_irrelevant_score": round(sanity.irrelevant_score, 6),
            "listwise_max_delta": round(listwise_delta, 6),
        },
    }


async def run(args: argparse.Namespace) -> int:
    """Seed, measure, validate, choose, and write the artifact."""
    cases = load_probe_set(Path(args.probe_set))
    print(f"probe set: {args.probe_set} -- {len(cases)} cases")
    model = RERANKER_ARMS[INDEPENDENT_ARM]["model"]
    if args.dry_run:
        notes, _ = _build_corpus(cases)
        print(f"corpus: {len(notes)} entities, {len(cases)} queries (dry run, nothing scored)")
        return 0

    sanity = await _run_sanity()
    service = MemoryService()  # fre-375-allow: test stack pinned module-top (:7688)
    if not await service.connect():
        raise SystemExit("[fail-loud] test substrate unavailable")
    try:
        await wipe_substrate(service, str(uuid.uuid4()))
        for case in cases:
            await seed_replay(service, case, str(uuid.uuid4()), f"fre1479-cal-{case.case_id}")
        measurement = await _measure_production_side(service, cases)
        listwise_delta = await _listwise_sensitivity(
            service, cases[0], measurement.items, measurement.documents
        )
    finally:
        await service.disconnect()

    if not measurement.positives or not measurement.negatives:
        raise SystemExit(
            f"[fail-loud] empty score population (positives={len(measurement.positives)}, "
            f"negatives={len(measurement.negatives)}) -- the seed or the reranker is broken"
        )

    independent_positives, independent_negatives = await _measure_independent_side(cases)
    # P1 compares entity documents only -- see _report_parity for why.
    production_aggregates = reranker_parity_aggregates(
        measurement.entity_positives, measurement.entity_negatives
    )
    independent_aggregates = reranker_parity_aggregates(
        independent_positives, independent_negatives
    )
    if not _report_parity(production_aggregates, independent_aggregates, sanity, listwise_delta):
        return 1

    proposal = choose_bound(measurement.positives, measurement.negatives)
    # Named from the artifact rather than hard-coded (FRE-1480). This harness serves every
    # path whose scorer is the reranker -- `--artifact` is what selects which bound a run
    # produces -- and a header naming one of them would mislabel every other run.
    print(f"\n=== relevance bound for {Path(args.artifact).stem} -- {model} ===")
    print(
        f"positives n={len(measurement.positives)} "
        f"(entity {len(measurement.entity_positives)}, turn {len(measurement.turn_positives)})  "
        f"negatives n={len(measurement.negatives)}"
    )
    print(f"negative median (the value the bound must reject) = {proposal.negative_median:.4f}")
    if proposal.incompatible:
        print(f"\nINCOMPATIBLE -- no bound reported.\n  {proposal.reason}")
    else:
        assert proposal.bound is not None
        print(f"bound                  = {proposal.bound:.4f}   <- the configured value")
        print(f"positives admitted     = {proposal.positive_admitted_share:.1%}")
        print(f"negatives rejected     = {proposal.negative_rejected_share:.1%}")
        print(
            "  entity-doc positives admitted = "
            f"{admitted_share(measurement.entity_positives, proposal.bound):.1%}"
        )
        print(
            "  turn-doc positives admitted   = "
            f"{admitted_share(measurement.turn_positives, proposal.bound):.1%}"
        )

    payload = _artifact_payload(
        proposal,
        str(model),
        measurement,
        production_aggregates,
        independent_aggregates,
        sanity,
        listwise_delta,
        args.probe_set,
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
        "--dry-run", action="store_true", help="Report the corpus shape without scoring anything."
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
