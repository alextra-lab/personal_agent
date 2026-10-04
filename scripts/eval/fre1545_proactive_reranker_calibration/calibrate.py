"""FRE-1545 -- calibrate the proactive path's reranker bound, and measure its latency.

ADR-0148 D4 requires a bound measured on the component that is actually serving, over the
population the gate actually sees, validated before its numbers are trusted, and committed as
the source of the configured value. This driver produces that artifact for the proactive path.

WHY THIS PATH NEEDS ITS OWN BOUND
---------------------------------
FRE-1477 measured the serving embedder on this path and reported an incompatibility. D4 names
the response -- a reranker-side bound. FRE-1479 measured the same reranker on broad recall
(bound 0.338891), but its population is the multipath fused set. The proactive gate sees the
proactive path's own candidates: the vector index's entities plus the lexical augment, each
split into an entity and its most recent cross-session turn. Same scorer, different
population, so the bound is measured here rather than copied (the ticket says so outright).

WHAT IT MEASURES
----------------
Production code end to end, not a re-implementation:

* ``generate_embedding(mode="query")`` and ``MemoryService.suggest_proactive_raw`` retrieve the
  rows, with a session id no seeded turn carries, so every turn is cross-session.
* ``memory.proactive.score_proactive_relevance`` splits, deduplicates, builds the documents and
  makes the one ``rerank()`` call -- the function the adapter calls on every proactive turn.

Labels follow FRE-1479: an entity candidate is positive when its name is an expected name; an
episode candidate is positive when any entity its turn ``DISCUSSES`` is expected. The negative
is each query's strongest non-match over both kinds -- FRE-694's metric.

Two passes per query:

* **Wide** -- ``proactive_memory_vector_top_k`` = 100, which covers the 49-entity corpus, so
  every labelled positive enters (FRE-1477 widened the same way). The bound is chosen here.
* **Production width** -- the deployed top_k, as a genuine retrieval rather than a cut of the
  wide one: a wide dense retrieval suppresses lexical hits that sit in dense ranks 21-100, so a
  cut would not reproduce production's candidate set (codex plan-review). This pass is timed
  (AC-7) and is the reference for P3.

THE CHECKS (all must pass before a number is reported)
------------------------------------------------------
* **P1 -- cross-implementation.** The proactive document builder plus the production ``rerank``
  call, over every corpus entity, against ``separation_benchmark``'s independently authored
  Voyage arm. Entity documents only, for FRE-1479's reason: the independent side builds no turn
  documents. This validates the instrument; the proactive population is then drawn with it.
* **P2R -- instrument sanity.** FRE-695's gate, unchanged from FRE-1479.
* **P3 -- listwise sensitivity.** Per candidate present in both passes, the score movement
  between the wide set and the production-width set, over every query. A rerank request is
  listwise, so the wider calibration set could move a score; this measures whether it does.

SUBSTRATE AND SPEND
-------------------
Test substrate only (FRE-375), pinned at module top before any ``personal_agent`` import.
``wipe_substrate`` refuses outside ``Environment.TEST``. The reranker and the managed embedder
are read from ``pass`` at run time, never logged, never persisted. About 54 rerank calls per
pass and for P1, plus the seed's and the queries' embedding calls.

Run (test substrate up)::

    uv run python -m scripts.eval.fre1545_proactive_reranker_calibration.calibrate
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
# singleton). Hard assignment, NOT setdefault (FRE-778): an ambient .env value must not win.
_TEST_SUBSTRATE_ENV = {
    "APP_ENV": "test",
    "AGENT_NEO4J_URI": "bolt://localhost:7688",
    # The test stack's own credentials (docker-compose.test.yml's default), pinned so an
    # ambient .env password for another Neo4j cannot be sent to the test one.
    "AGENT_NEO4J_USER": "neo4j",
    "AGENT_NEO4J_PASSWORD": "neo4j_dev_password",  # fre-375-allow: docker-compose.test.yml default, sent only to the :7688 test stack pinned above
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

# The reranker is the component under measurement, so it must be the one that serves.
os.environ["AGENT_RERANKER_ENABLED"] = "true"
os.environ["AGENT_VOYAGE_API_KEY"] = _pass_show("VOYAGEAI_API_KEY")
# score_proactive_relevance makes no call unless the gate is armed, and it reads only
# whether a bound exists, never its value. 0.0 arms it without presuming the answer.
os.environ["AGENT_PROACTIVE_MEMORY_RERANK_RELEVANCE_BOUND"] = "0.0"
os.environ["AGENT_PROACTIVE_MEMORY_RELEVANCE_GATE_ENABLED"] = "true"
# The managed embedder serves the vector index the proactive rows come from, exactly as in
# production, and the seed embeds through it (FRE-1477 / FRE-1479 pinned the same arm).
os.environ["AGENT_SUBSTRATE_PROFILE"] = "managed_embedder"
os.environ["AGENT_MANAGED_EMBEDDING_ENDPOINT"] = _pass_show("seshat/AGENT_OVH_AI_BASE_URL")
os.environ["AGENT_MANAGED_EMBEDDING_TOKEN"] = _pass_show("seshat/AGENT_MANAGED_EMBEDDING_TOKEN")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import statistics  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import date  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import structlog  # noqa: E402
from scripts.eval.fre435_memory_recall.harness import seed_replay, wipe_substrate  # noqa: E402
from scripts.eval.fre435_memory_recall.probes import ProbeCase, load_probe_set  # noqa: E402
from scripts.eval.fre1477_relevance_calibration.calibrate import (  # noqa: E402
    _ensure_index_at_serving_width,
)
from scripts.eval.fre1479_reranker_calibration.analysis import (  # noqa: E402
    admitted_share,
    choose_bound,
    reranker_parity_aggregates,
)
from scripts.eval.fre1479_reranker_calibration.calibrate import (  # noqa: E402
    _measure_independent_side,
    _report_parity,
    _run_sanity,
    _score_one_query,
)

from personal_agent.config import settings  # noqa: E402
from personal_agent.config.model_loader import resolve_role_definition  # noqa: E402
from personal_agent.memory.embeddings import generate_embedding  # noqa: E402
from personal_agent.memory.proactive import (  # noqa: E402
    CandidateIdentity,
    _candidate_identity,
    _deduped_candidates,
    _rerank_document,
    score_proactive_relevance,
)
from personal_agent.memory.service import MemoryService  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_PROBE = "scripts/eval/fre435_memory_recall/semantic_probe.yaml"
DEFAULT_ARTIFACT = "config/calibration/proactive_rerank_relevance_bound.json"

#: Wide-pass vector top_k. Above the corpus size, so every labelled positive is retrieved.
WIDE_TOP_K = 100


def _expected_names(case: ProbeCase) -> frozenset[str]:
    """The case's labelled positives, lowercased (``expected.entity_names`` only).

    Not unioned with ``seed_entities``: a seeded entity the case does not label as expected
    is a co-resident distractor, and counting it would inflate the admitted share.
    """
    return frozenset(n.strip().lower() for n in case.expected.entity_names if n.strip())


async def _turn_discussed_names(service: MemoryService) -> dict[str, frozenset[str]]:
    """Every seeded turn's ``DISCUSSES`` targets, lowercased -- the episode labels."""
    labels: dict[str, frozenset[str]] = {}
    async with service.driver.session() as session:  # type: ignore[union-attr]
        result = await session.run(
            """
            MATCH (t:Turn)
            OPTIONAL MATCH (t)-[:DISCUSSES]->(e:Entity)
            RETURN t.turn_id AS id, collect(DISTINCT e.name) AS names
            """
        )
        for row in await result.data():
            if row.get("id"):
                labels[row["id"]] = frozenset(str(n).strip().lower() for n in row["names"] if n)
    return labels


async def _corpus_entity_rows(service: MemoryService) -> list[dict[str, Any]]:
    """Every seeded entity's name and description, for P1's entity documents."""
    async with service.driver.session() as session:  # type: ignore[union-attr]
        result = await session.run(
            "MATCH (e:Entity) RETURN e.name AS name, e.description AS description"
        )
        return [row for row in await result.data() if row.get("name")]


def _scorable(rows: Sequence[dict[str, Any]]) -> list[tuple[CandidateIdentity, str, str]]:
    """The candidates ``score_proactive_relevance`` sends, as ``(identity, kind, label key)``.

    Mirrors the scorer's two exclusions (empty-description entity, empty document) so the
    harness can fail loud when a sent candidate comes back unscored. The label key is the
    entity's lowercased name, or the episode's turn id.
    """
    out: list[tuple[CandidateIdentity, str, str]] = []
    items, _ = _deduped_candidates(rows)
    for kind, payload, row in items:
        if kind == "entity" and not (payload.get("description") or "").strip():
            continue
        if not _rerank_document(kind, row).strip():
            continue
        key = (
            str(payload.get("name") or "").strip().lower()
            if kind == "entity"
            else str(payload.get("conversation_id") or "")
        )
        out.append((_candidate_identity(kind, payload), kind, key))
    return out


async def _retrieve_and_score(
    service: MemoryService, case: ProbeCase, session_id: str
) -> tuple[list[tuple[CandidateIdentity, str, str]], dict[CandidateIdentity, float], float]:
    """One production retrieval and one production scoring call, timed.

    Returns:
        ``(scorable, scores, scoring_ms)``. Fails loud if any sent candidate came back
        unscored, or scored by a model other than the serving one -- a bound measured on a
        passthrough, a truncated response or the fallback model would describe nothing.
    """
    trace_id = str(uuid.uuid4())
    query_vec = await generate_embedding(case.query, mode="query")
    if not any(x != 0.0 for x in query_vec):
        raise SystemExit(f"[fail-loud] degenerate query embedding for case {case.case_id}")
    rows = await service.suggest_proactive_raw(
        query_vec, session_id, trace_id, query_text=case.query
    )
    scorable = _scorable(rows)
    started = time.perf_counter()
    values = await score_proactive_relevance(rows, case.query, trace_id=trace_id)
    scoring_ms = (time.perf_counter() - started) * 1000.0

    serving = _serving_reranker()
    missing = [ident for ident, _, _ in scorable if ident not in values]
    if missing:
        raise SystemExit(
            f"[fail-loud] case {case.case_id}: {len(missing)} of {len(scorable)} candidates "
            "came back unscored (passthrough, truncation or a failed call)"
        )
    foreign = {v.model for v in values.values()} - {serving}
    if foreign:
        raise SystemExit(
            f"[fail-loud] case {case.case_id}: scores from {sorted(foreign)}, not the serving "
            f"reranker {serving} -- the fallback answered"
        )
    return scorable, {ident: v.score for ident, v in values.items()}, scoring_ms


def _serving_reranker() -> str:
    """The model id the ``reranker`` role resolves to."""
    model_def = resolve_role_definition("reranker")
    if model_def is None:
        raise SystemExit("[fail-loud] the reranker role resolves to no deployment")
    return model_def.id


@dataclass
class Population:
    """The proactive candidate population's scores, wide pass."""

    positives: list[float] = field(default_factory=list)
    negatives: list[float] = field(default_factory=list)
    entity_positives: list[float] = field(default_factory=list)
    episode_positives: list[float] = field(default_factory=list)
    candidate_counts: list[int] = field(default_factory=list)


def _label(
    population: Population,
    case: ProbeCase,
    scorable: Sequence[tuple[CandidateIdentity, str, str]],
    scores: dict[CandidateIdentity, float],
    turn_labels: dict[str, frozenset[str]],
) -> None:
    """Add one query's labelled scores to the population."""
    expected = _expected_names(case)
    non_matches: list[float] = []
    for identity, kind, key in scorable:
        score = scores[identity]
        positive = (
            key in expected
            if kind == "entity"
            else bool(turn_labels.get(key, frozenset()) & expected)
        )
        if not positive:
            non_matches.append(score)
            continue
        population.positives.append(score)
        (population.entity_positives if kind == "entity" else population.episode_positives).append(
            score
        )
    if non_matches:
        population.negatives.append(max(non_matches))
    population.candidate_counts.append(len(scorable))


async def _p1_production_side(
    entity_rows: Sequence[dict[str, Any]], cases: Sequence[ProbeCase]
) -> tuple[list[float], list[float]]:
    """P1's production side: the proactive document builder plus production ``rerank``."""
    documents = [_rerank_document("entity", row) for row in entity_rows]
    names = [str(row["name"]).strip().lower() for row in entity_rows]
    positives: list[float] = []
    negatives: list[float] = []
    for case in cases:
        scored = await _score_one_query(case.query, documents, case.case_id)
        expected = _expected_names(case)
        positives.extend(s for n, s in zip(names, scored, strict=True) if n in expected)
        non_matches = [s for n, s in zip(names, scored, strict=True) if n not in expected]
        if non_matches:
            negatives.append(max(non_matches))
    return positives, negatives


def _percentile(values: Sequence[float], pct: int) -> float:
    """Inclusive percentile (``statistics.quantiles``), for latency reporting."""
    return statistics.quantiles(values, n=100, method="inclusive")[pct - 1]


async def run(args: argparse.Namespace) -> int:
    """Seed, measure both passes, validate, choose, and write the artifact."""
    cases = load_probe_set(Path(args.probe_set))
    print(f"probe set: {args.probe_set} -- {len(cases)} cases")
    serving = _serving_reranker()
    production_top_k = settings.proactive_memory_vector_top_k
    print(f"serving reranker: {serving}   production top_k: {production_top_k}")

    sanity = await _run_sanity()
    service = MemoryService()  # fre-375-allow: test stack pinned module-top (:7688)
    if not await service.connect():
        raise SystemExit("[fail-loud] test substrate unavailable")
    probe_session = f"fre1545-cal-probe-{uuid.uuid4()}"
    population = Population()
    wide_scores: list[dict[CandidateIdentity, float]] = []
    latencies_ms: list[float] = []
    production_counts: list[int] = []
    listwise_delta = 0.0
    try:
        await _ensure_index_at_serving_width(service)
        await wipe_substrate(service, str(uuid.uuid4()))
        for case in cases:
            await seed_replay(service, case, str(uuid.uuid4()), f"fre1545-cal-{case.case_id}")
        turn_labels = await _turn_discussed_names(service)
        entity_rows = await _corpus_entity_rows(service)

        settings.proactive_memory_vector_top_k = WIDE_TOP_K
        for case in cases:
            scorable, scores, _ = await _retrieve_and_score(service, case, probe_session)
            _label(population, case, scorable, scores, turn_labels)
            wide_scores.append(scores)
            log.info("calibrate_case", case=case.case_id, candidates=len(scorable))

        settings.proactive_memory_vector_top_k = production_top_k
        for case, wide in zip(cases, wide_scores, strict=True):
            scorable, scores, scoring_ms = await _retrieve_and_score(service, case, probe_session)
            latencies_ms.append(scoring_ms)
            production_counts.append(len(scorable))
            shared = set(scores) & set(wide)
            listwise_delta = max([listwise_delta, *(abs(scores[i] - wide[i]) for i in shared)])
    finally:
        settings.proactive_memory_vector_top_k = production_top_k
        await service.disconnect()

    if not population.positives or not population.negatives:
        raise SystemExit(
            f"[fail-loud] empty score population (positives={len(population.positives)}, "
            f"negatives={len(population.negatives)})"
        )

    p1_positives, p1_negatives = await _p1_production_side(entity_rows, cases)
    independent_positives, independent_negatives = await _measure_independent_side(cases)
    production_aggregates = reranker_parity_aggregates(p1_positives, p1_negatives)
    independent_aggregates = reranker_parity_aggregates(
        independent_positives, independent_negatives
    )
    print(
        "\nP3 here compares, per candidate present in both passes, the wide-set score "
        f"(top_k {WIDE_TOP_K}) with the production-width score (top_k {production_top_k}), "
        "over every query -- not the input-cap comparison the shared report text names."
    )
    if not _report_parity(production_aggregates, independent_aggregates, sanity, listwise_delta):
        return 1

    proposal = choose_bound(population.positives, population.negatives)
    print(f"\n=== proactive reranker relevance bound -- {serving} ===")
    print(
        f"positives n={len(population.positives)} (entity {len(population.entity_positives)}, "
        f"episode {len(population.episode_positives)})  negatives n={len(population.negatives)}"
    )
    print(f"candidates per query (wide): median {statistics.median(population.candidate_counts)}")
    print(f"negative median (the value the bound must reject) = {proposal.negative_median:.4f}")
    if proposal.incompatible:
        print(f"\nINCOMPATIBLE -- no bound reported.\n  {proposal.reason}")
    else:
        assert proposal.bound is not None
        print(f"bound                  = {proposal.bound:.6f}   <- the configured value")
        print(f"positives admitted     = {proposal.positive_admitted_share:.1%}")
        print(f"negatives rejected     = {proposal.negative_rejected_share:.1%}")
        print(
            "  entity positives admitted  = "
            f"{admitted_share(population.entity_positives, proposal.bound):.1%}"
        )
        print(
            "  episode positives admitted = "
            f"{admitted_share(population.episode_positives, proposal.bound):.1%}"
        )

    print(f"\n=== AC-7 -- added time per proactive call (top_k {production_top_k}) ===")
    print(
        f"  calls n={len(latencies_ms)}  documents per call: median "
        f"{statistics.median(production_counts)}, max {max(production_counts)}"
    )
    print(f"  score_proactive_relevance p50 = {_percentile(latencies_ms, 50):.1f} ms")
    print(f"  score_proactive_relevance p95 = {_percentile(latencies_ms, 95):.1f} ms")
    print(f"  max = {max(latencies_ms):.1f} ms")

    payload = {
        "component": {"role": "reranker", "model": serving, "dimensions": 1},
        "measured_on": date.today().isoformat(),
        "probe_set": args.probe_set,
        "incompatible": proposal.incompatible,
        "incompatible_reason": proposal.reason,
        "bound": proposal.bound,
        "positive_scores": [round(s, 6) for s in population.positives],
        "negative_scores": [round(s, 6) for s in population.negatives],
        "entity_positive_scores": [round(s, 6) for s in population.entity_positives],
        # The schema's turn half: an episode candidate's document is its turn's text.
        "turn_positive_scores": [round(s, 6) for s in population.episode_positives],
        "positive_admitted_share": proposal.positive_admitted_share,
        "negative_median_rerank": round(proposal.negative_median, 6),
        "parity": {
            "tolerance": 0.02,
            "p1_production_aggregates": {k: round(v, 6) for k, v in production_aggregates.items()},
            "p1_independent_aggregates": {
                k: round(v, 6) for k, v in independent_aggregates.items()
            },
            "sanity_relevant_score": round(sanity.relevant_score, 6),
            "sanity_irrelevant_score": round(sanity.irrelevant_score, 6),
            "listwise_max_delta": round(listwise_delta, 6),
        },
    }
    out = Path(args.artifact)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nartifact written: {out}")
    return 0


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-set", default=DEFAULT_PROBE, help="Labelled probe set YAML.")
    parser.add_argument("--artifact", default=DEFAULT_ARTIFACT, help="Committed artifact path.")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
