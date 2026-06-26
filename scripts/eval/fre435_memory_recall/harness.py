r"""FRE-488 — memory-recall quality harness (ADR-0087 §D1/D3).

Drives a probe set end-to-end against the **test substrate** (FRE-375: Neo4j
:7688 / ES :9201 / Postgres :5433), exercising the memory **write path**
(``second_brain.entity_extraction`` → ``memory.promote`` → ``MemoryService``) and
the **retrieval path** (``MemoryServiceAdapter.recall`` → ``MemoryService.query_memory``
→ ``memory.reranker``), then scores the D1 metrics and emits a structured report.

This is the FRE-435 analog of ``scripts/eval/fre433_cache_ab/``. Phase 1 (ADR-0087)
changes no production behaviour: the harness only *calls* existing APIs.

Two write modes:

* ``replay`` (default, offline) — seed the case's pre-extracted entities directly
  via ``MemoryService.create_entity`` / ``create_relationship``. No LLM. This is
  the path the seed AC runs.
* ``extract`` (real) — run ``extract_entities_and_relationships`` over each setup
  turn, land the entities, and promote them via ``run_promotion_pipeline``. Needs
  the SLM server; meaningful vector-path measurement runs here (FRE-491).

Backend-aware truth-source (FRE-433 discipline): the retrieval outcome is read
from the **actual** ``recall()`` return, never a proxy log. The run report also
stamps ``embedding_backend`` (``real`` vs ``zero-vector``): offline ``replay``
without an embedding model persists zero-vector embeddings, so recall degrades to
keyword-only — the stamp keeps that from being misread (codex review).

Usage::

    make test-infra-up            # start the isolated test substrate
    uv run python scripts/eval/fre435_memory_recall/harness.py \\
        --run-id seed-2026-06-26 \\
        --probe-set scripts/eval/fre435_memory_recall/seed_probe.yaml \\
        --write-mode replay

Raw run dumps stay out of git; output lands in the gitignored
``telemetry/evaluation/fre435-memory-recall/`` directory.
"""

from __future__ import annotations

import os

# FRE-375 / codex Q1: point at the TEST substrate BEFORE importing any
# personal_agent code. ``settings`` is a cached import-time singleton, so env set
# after the first import is a no-op. ``setdefault`` lets the caller pre-override.
_TEST_SUBSTRATE_ENV = {
    "APP_ENV": "test",
    "AGENT_NEO4J_URI": "bolt://localhost:7688",
    "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
    "AGENT_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_ELASTICSEARCH_INDEX_PREFIX": "agent-logs-test",
    "AGENT_CAPTAINS_LOG_INDEX_PREFIX": "agent-captains-test",
}
for _key, _value in _TEST_SUBSTRATE_ENV.items():
    os.environ.setdefault(_key, _value)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import structlog  # noqa: E402
from scripts.eval.fre435_memory_recall.metrics import WriteOutcome  # noqa: E402
from scripts.eval.fre435_memory_recall.probes import ProbeCase, load_probe_set  # noqa: E402
from scripts.eval.fre435_memory_recall.report import (  # noqa: E402
    RunReport,
    render_json,
    render_markdown,
)
from scripts.eval.fre435_memory_recall.scoring import flatten_recall, score_case  # noqa: E402

from personal_agent.memory.embeddings import generate_embedding  # noqa: E402
from personal_agent.memory.fact import PromotionCandidate  # noqa: E402
from personal_agent.memory.models import Entity, Relationship  # noqa: E402
from personal_agent.memory.promote import run_promotion_pipeline  # noqa: E402
from personal_agent.memory.protocol import MemoryRecallQuery  # noqa: E402
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter  # noqa: E402
from personal_agent.memory.service import MemoryService  # noqa: E402
from personal_agent.second_brain.entity_extraction import (  # noqa: E402
    extract_entities_and_relationships,
)

log = structlog.get_logger(__name__)

DEFAULT_PROBE_SET = "scripts/eval/fre435_memory_recall/seed_probe.yaml"
DEFAULT_OUT = "telemetry/evaluation/fre435-memory-recall"
DEFAULT_K_SWEEP = (1, 3, 5, 10)
DEFAULT_PROD_K = 5


async def detect_embedding_backend() -> str:
    """Probe whether a real embedding model is reachable.

    Returns:
        ``"real"`` if the probe embedding is non-zero, else
        ``"zero-vector"`` (recall degrades to keyword-only — codex review).
    """
    try:
        vec = await generate_embedding("diffraction limit", mode="query")
    except Exception as exc:  # noqa: BLE001 — diagnostic probe; any failure ⇒ degraded
        log.warning("embedding_probe_failed", error=str(exc))
        return "zero-vector"
    return "real" if any(v != 0.0 for v in vec) else "zero-vector"


async def seed_replay(
    service: MemoryService, case: ProbeCase, trace_id: str, session_id: str
) -> WriteOutcome:
    """Seed a case's pre-extracted entities/relationships directly (offline).

    Args:
        service: Connected memory service.
        case: The probe case.
        trace_id: Identity to thread onto every write (ADR-0074).
        session_id: Originating session id to thread onto every write.

    Returns:
        The write outcome (expected = seeded entities, landed = successful writes).
    """
    landed = 0
    canonical: dict[str, str] = {}
    for seed in case.seed_entities:
        entity = Entity(
            name=seed.name,
            entity_type=seed.entity_type,
            description=seed.description or None,
        )
        name = await service.create_entity(
            entity,
            originating_trace_id=trace_id,
            originating_session_id=session_id,
        )
        if name:
            landed += 1
            canonical[seed.name] = name
    for rel in case.seed_relationships:
        relationship = Relationship(
            source_id=canonical.get(rel.source, rel.source),
            target_id=canonical.get(rel.target, rel.target),
            relationship_type=rel.rel_type,
        )
        await service.create_relationship(relationship, trace_id=trace_id)
    return WriteOutcome(
        extraction_fired=bool(case.seed_entities),
        entities_landed=landed,
        entities_expected=len(case.seed_entities),
    )


async def seed_extract(
    service: MemoryService, case: ProbeCase, trace_id: str, session_id: str
) -> WriteOutcome:
    """Seed a case via the real extraction + promotion write path (needs SLM).

    Args:
        service: Connected memory service.
        case: The probe case.
        trace_id: Identity to thread onto every write (ADR-0074).
        session_id: Originating session id to thread onto every write.

    Returns:
        The write outcome over the extracted entities.
    """
    now = datetime.now(timezone.utc)
    candidates: list[PromotionCandidate] = []
    landed = 0
    extracted_total = 0
    for index, turn in enumerate(case.history):
        result = await extract_entities_and_relationships(
            turn.user,
            turn.assistant,
            trace_id=trace_id,
            session_id=session_id,
        )
        entities = result.get("entities", []) or []
        extracted_total += len(entities)
        turn_id = f"{session_id}:{index}"
        for ent in entities:
            name = str(ent.get("name", "")).strip()
            if not name:
                continue
            entity = Entity(
                name=name,
                entity_type=str(ent.get("entity_type", "concept")),
                description=(ent.get("description") or None),
            )
            created = await service.create_entity(
                entity,
                originating_trace_id=trace_id,
                originating_session_id=session_id,
            )
            if created:
                landed += 1
            candidates.append(
                PromotionCandidate(
                    entity_name=name,
                    entity_type=entity.entity_type,
                    mention_count=1,
                    first_seen=now,
                    last_seen=now,
                    source_turn_ids=[turn_id],
                    description=entity.description,
                )
            )
    if candidates:
        await run_promotion_pipeline(service, candidates, trace_id)
    return WriteOutcome(
        extraction_fired=extracted_total > 0,
        entities_landed=landed,
        entities_expected=extracted_total,
    )


async def retrieve(
    adapter: MemoryServiceAdapter, case: ProbeCase, trace_id: str, limit: int
) -> tuple[tuple[str, ...], bool]:
    """Run the case query through the retrieval path (the truth-source).

    Args:
        adapter: Protocol adapter over the connected service.
        case: The probe case.
        trace_id: Request trace id.
        limit: Max results to request (the widest swept ``k``).

    Returns:
        ``(ordered namespaced ids, denied)`` where ``denied`` means the recall
        returned no episodes and no entities.
    """
    query = MemoryRecallQuery(query_text=case.query, limit=limit)
    result = await adapter.recall(query, trace_id=trace_id)
    denied = not result.episodes and not result.entities
    retrieved = flatten_recall(result.episodes, result.entities, result.relevance_scores)
    return retrieved, denied


async def run(args: argparse.Namespace) -> int:
    """Drive the probe set and write the run report.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code (0 on success, non-zero on a substrate failure).
    """
    cases = load_probe_set(Path(args.probe_set))
    k_sweep = tuple(sorted({*args.k_sweep, args.prod_k}))
    service = MemoryService()  # fre-375-allow: settings-driven; module top pins the test substrate (:7688/:9201/:5433) + APP_ENV=test
    if not await service.connect():
        log.error("memory_service_unreachable", uri=os.environ.get("AGENT_NEO4J_URI"))
        return 2
    adapter = MemoryServiceAdapter(service)
    embedding_backend = await detect_embedding_backend()
    log.info(
        "harness_start",
        run_id=args.run_id,
        write_mode=args.write_mode,
        embedding_backend=embedding_backend,
        cases=len(cases),
    )

    results = []
    try:
        for case in cases:
            trace_id = str(uuid.uuid4())
            session_id = f"fre435-{args.run_id}-{case.case_id}"
            if args.write_mode == "extract":
                write_outcome = await seed_extract(service, case, trace_id, session_id)
            else:
                write_outcome = await seed_replay(service, case, trace_id, session_id)
            retrieved, denied = await retrieve(adapter, case, trace_id, max(k_sweep))
            case_result = score_case(
                case=case,
                retrieved=retrieved,
                denied=denied,
                write_outcome=write_outcome,
                prod_k=args.prod_k,
                k_sweep=k_sweep,
            )
            results.append(case_result)
            log.info(
                "case_scored",
                case=case.case_id,
                trace_id=trace_id,
                false_negative=case_result.false_negative,
                recall_prod=case_result.recall_by_k.get(args.prod_k),
                hypothesis=case_result.hypothesis.value,
            )
    finally:
        await service.disconnect()

    report = RunReport(
        run_id=args.run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        write_mode=args.write_mode,
        embedding_backend=embedding_backend,
        prod_k=args.prod_k,
        k_sweep=k_sweep,
        probe_set=args.probe_set,
        cases=tuple(results),
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{args.run_id}.json").write_text(render_json(report))
    (out_dir / f"{args.run_id}.md").write_text(render_markdown(report))
    log.info("harness_written", out=str(out_dir / f"{args.run_id}.md"), cases=len(results))
    return 0


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="FRE-435 memory-recall quality harness")
    parser.add_argument("--run-id", required=True, help="Run identifier (tag in output).")
    parser.add_argument("--probe-set", default=DEFAULT_PROBE_SET, help="Probe-set YAML path.")
    parser.add_argument(
        "--write-mode",
        default="replay",
        choices=["replay", "extract"],
        help="replay = seed entities offline; extract = real extraction+promotion (needs SLM).",
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output directory (gitignored).")
    parser.add_argument(
        "--prod-k", type=int, default=DEFAULT_PROD_K, help="Production k for headline metrics."
    )
    parser.add_argument(
        "--k-sweep",
        type=int,
        nargs="+",
        default=list(DEFAULT_K_SWEEP),
        help="k values to sweep for recall/precision.",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
