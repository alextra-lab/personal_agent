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
from collections.abc import Sequence  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import structlog  # noqa: E402
from neo4j import AsyncGraphDatabase as Neo4jAsyncGraphDatabase  # noqa: E402
from scripts.eval.fre435_memory_recall.metrics import WriteOutcome  # noqa: E402
from scripts.eval.fre435_memory_recall.probes import ProbeCase, load_probe_set  # noqa: E402
from scripts.eval.fre435_memory_recall.report import (  # noqa: E402
    RunReport,
    render_json,
    render_markdown,
)
from scripts.eval.fre435_memory_recall.scoring import flatten_recall, score_case  # noqa: E402

from personal_agent.config import settings  # noqa: E402
from personal_agent.config.env_loader import Environment  # noqa: E402
from personal_agent.memory.embeddings import generate_embedding  # noqa: E402
from personal_agent.memory.fact import PromotionCandidate  # noqa: E402
from personal_agent.memory.models import Entity, Relationship, TurnNode  # noqa: E402
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


#: Per-case isolation wipe. ``DETACH DELETE`` removes nodes + relationships but
#: leaves schema (the ``entity_embedding`` vector index, constraints) intact, so
#: the next case still has a working vector recall path.
WIPE_CYPHER = "MATCH (n) DETACH DELETE n"


async def wipe_substrate(service: MemoryService, trace_id: str) -> None:
    """Wipe all graph data for per-case isolation (FRE-491; codex plan-review).

    The bespoke gate reuses entity names across cases under first-write-wins, so
    without a wipe an earlier case can satisfy a later case's query (false pass)
    or freeze its description (false fail), and the true-negative abstention
    controls get polluted by every prior case's entities. Wiping before each
    case makes every case start from an empty graph — the precondition for a
    valid per-case baseline.

    Guarded to the FRE-375 **test** substrate: refuses unless ``environment`` is
    ``TEST`` (belt-and-suspenders with ``MemoryService.connect``'s prod-URI
    refusal), since a ``DETACH DELETE`` is irreversible.

    Args:
        service: A connected memory service pinned to the test substrate.
        trace_id: Identity to thread onto the wipe for event correlation.

    Raises:
        RuntimeError: If not on the TEST substrate, or the service is not
            connected.
    """
    if settings.environment != Environment.TEST:
        raise RuntimeError(
            "wipe_substrate refused: environment is "
            f"{settings.environment!r}, not TEST — per-case isolation only runs "
            "against the FRE-375 test stack (Neo4j:7688 / ES:9201 / Postgres:5433)."
        )
    if service.driver is None:
        raise RuntimeError("wipe_substrate: memory service is not connected")
    async with service.driver.session() as session:
        await session.run(WIPE_CYPHER)
    log.info("harness_substrate_wiped", trace_id=trace_id)


#: Live corpus the distractor background is mined from (read-only). Overridable so
#: nothing about the deployment is hard-coded into the public repo.
DISTRACTOR_LIVE_NEO4J_URI = os.environ.get(
    "FRE435_LIVE_NEO4J_URI",
    "bolt://localhost:7687",  # fre-375-allow: READ-ONLY distractor mine (ADR-0087 §D7); never writes to prod
)


async def fetch_live_distractors(limit: int) -> list[dict[str, Any]]:
    """Read real Turns from the live corpus (READ-ONLY) for a distractor background.

    Per-case isolation leaves one candidate Turn, so the recency-ordered
    candidate gate (``MATCH (c:Turn) ... ORDER BY timestamp DESC LIMIT k``) is
    never under pressure and recall is trivially ~1.0 (FRE-491 codex review).
    Loading real recent Turns as a background reproduces the owner's actual
    failure mode: an older relevant Turn falling outside the recency window.

    ADR-0087 §D7 permits read-only retrieval probes against live. Raw turn text is
    **never committed** — it lands only in the ephemeral test graph; the gitignored
    run report carries aggregates only.

    Args:
        limit: Number of recent live Turns to fetch (``<= 0`` returns none).

    Returns:
        Turn dicts with ``turn_id``/``user_message``/``assistant_response``/
        ``key_entities``.
    """
    if limit <= 0:
        return []
    if Neo4jAsyncGraphDatabase is None:
        raise RuntimeError("neo4j driver unavailable for distractor fetch")
    password = os.environ.get("AGENT_NEO4J_PASSWORD") or settings.neo4j_password
    driver = Neo4jAsyncGraphDatabase.driver(  # fre-375-allow: READ-ONLY distractor mine (ADR-0087 §D7); separate driver, never writes
        DISTRACTOR_LIVE_NEO4J_URI, auth=(settings.neo4j_user, password)
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                """
                MATCH (t:Turn)
                WHERE t.user_message IS NOT NULL
                  AND t.key_entities IS NOT NULL AND size(t.key_entities) > 0
                RETURN t.turn_id AS turn_id,
                       t.user_message AS user_message,
                       t.assistant_response AS assistant_response,
                       t.key_entities AS key_entities
                ORDER BY t.timestamp DESC
                LIMIT $limit
                """,
                limit=limit,
            )
            rows = [dict(record) async for record in result]
    finally:
        await driver.close()
    log.info("distractors_fetched", count=len(rows), uri=DISTRACTOR_LIVE_NEO4J_URI)
    return rows


async def load_distractors(
    service: MemoryService,
    rows: Sequence[dict[str, Any]],
    base_time: datetime,
    trace_id: str,
) -> None:
    """Replay distractor Turns into the test graph, newer than the case Turn.

    Each distractor is timestamped strictly after ``base_time`` so the case's own
    (older) relevant Turn sinks *behind* them in the recency-ordered candidate
    window. When the background exceeds the retrieval limit the relevant Turn is
    never fetched — the recency-first/semantic-second failure (FRE-491).

    Args:
        service: Connected memory service (test substrate).
        rows: Distractor turn dicts from :func:`fetch_live_distractors`.
        base_time: The case Turn's timestamp; distractors are placed after it.
        trace_id: Identity to thread onto each write.
    """
    for index, row in enumerate(rows):
        await store_turn(
            service,
            turn_id=f"distractor:{row['turn_id']}:{index}",
            session_id="fre435-distractor",
            trace_id=trace_id,
            sequence=index,
            user_message=row.get("user_message") or "",
            assistant_response=row.get("assistant_response"),
            key_entities=list(row.get("key_entities") or []),
            timestamp=base_time + timedelta(hours=1, seconds=index),
        )


async def store_turn(
    service: MemoryService,
    *,
    turn_id: str,
    session_id: str,
    trace_id: str,
    sequence: int,
    user_message: str,
    assistant_response: str | None,
    key_entities: Sequence[str],
    timestamp: datetime,
) -> None:
    """Store a ``:Turn`` the way the production write path does.

    The recall path (``query_memory``) retrieves ``:Turn`` nodes and surfaces the
    entities they ``DISCUSS`` via ``key_entities``; it never returns bare entity
    nodes (FRE-491 — verified in code). Seeding only entities therefore measured
    an empty recall path. Reproducing the real ``create_conversation`` structure
    (Turn + ``key_entities`` + ``Turn-[:DISCUSSES]->Entity``) is what makes the
    harness exercise the path the system actually uses.

    Args:
        service: Connected memory service.
        turn_id: Unique turn id (also the DISCUSSES anchor).
        session_id: Originating session id.
        trace_id: Identity to thread onto the write (ADR-0074).
        sequence: Position within the session (for ordering).
        user_message: The setup turn's user text.
        assistant_response: The setup turn's assistant text (may be ``None``).
        key_entities: Canonical names of the entities this turn discusses.
        timestamp: Turn timestamp (the recall MATCH orders by it).
    """
    turn = TurnNode(
        turn_id=turn_id,
        trace_id=trace_id,
        session_id=session_id,
        sequence_number=sequence,
        timestamp=timestamp,
        user_message=user_message,
        assistant_response=assistant_response,
        key_entities=list(key_entities),
    )
    await service.create_conversation(turn)


async def seed_replay(
    service: MemoryService, case: ProbeCase, trace_id: str, session_id: str
) -> WriteOutcome:
    """Seed a case's pre-extracted entities/relationships directly (offline).

    Entities are created first (so the ``Entity.embedding`` vector index has
    vectors to rank on), then the setup turns are stored via the production
    ``create_conversation`` path so the entities are reachable through
    ``Turn-[:DISCUSSES]->Entity`` (FRE-491). A case with no history still seeds a
    single Turn from its entities so the prior discussion exists to be recalled.

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

    # Store the setup turns so the seeded entities are retrievable (FRE-491).
    key_entities = list(canonical.values()) or [s.name for s in case.seed_entities]
    now = datetime.now(timezone.utc)
    if case.history:
        for index, turn in enumerate(case.history):
            await store_turn(
                service,
                turn_id=f"{session_id}:{index}",
                session_id=session_id,
                trace_id=trace_id,
                sequence=index,
                user_message=turn.user,
                assistant_response=turn.assistant,
                key_entities=key_entities,
                timestamp=now + timedelta(seconds=index),
            )
    elif key_entities:
        await store_turn(
            service,
            turn_id=f"{session_id}:0",
            session_id=session_id,
            trace_id=trace_id,
            sequence=0,
            user_message="; ".join(s.description or s.name for s in case.seed_entities),
            assistant_response=None,
            key_entities=key_entities,
            timestamp=now,
        )
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
        turn_entity_names: list[str] = []
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
            turn_entity_names.append(name)
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
        # Store the turn via the production write path so the extracted entities
        # are reachable through Turn-[:DISCUSSES]->Entity (FRE-491).
        await store_turn(
            service,
            turn_id=turn_id,
            session_id=session_id,
            trace_id=trace_id,
            sequence=index,
            user_message=turn.user,
            assistant_response=turn.assistant,
            key_entities=turn_entity_names,
            timestamp=now + timedelta(seconds=index),
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
    # The Entity.embedding vector index is the retrieval path's substrate. A
    # freshly-rebuilt test Neo4j has none, so vector search would silently fall
    # back to keyword-only and turn every case into a false negative (FRE-491:
    # caught when the seed AC's zero-vector run never needed the index). It is a
    # schema object, so the per-case DETACH DELETE wipe preserves it — ensure it
    # once, here, before any case runs.
    if not await service.ensure_vector_index():
        log.error("vector_index_unavailable", uri=os.environ.get("AGENT_NEO4J_URI"))
        await service.disconnect()
        return 3
    adapter = MemoryServiceAdapter(service)
    embedding_backend = await detect_embedding_backend()
    distractors = await fetch_live_distractors(args.distractor_background)
    log.info(
        "harness_start",
        run_id=args.run_id,
        write_mode=args.write_mode,
        embedding_backend=embedding_backend,
        wipe_between_cases=args.wipe_between_cases,
        distractor_background=len(distractors),
        cases=len(cases),
    )

    results = []
    try:
        for case in cases:
            trace_id = str(uuid.uuid4())
            session_id = f"fre435-{args.run_id}-{case.case_id}"
            if args.wipe_between_cases:
                await wipe_substrate(service, trace_id)
            # Capture the case time BEFORE seeding so the distractor background
            # (placed an hour later) is strictly newer than the case's own Turn —
            # sinking the relevant Turn behind the recency window (FRE-491).
            case_time = datetime.now(timezone.utc)
            if args.write_mode == "extract":
                write_outcome = await seed_extract(service, case, trace_id, session_id)
            else:
                write_outcome = await seed_replay(service, case, trace_id, session_id)
            if distractors:
                await load_distractors(service, distractors, case_time, trace_id)
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
        wipe_between_cases=args.wipe_between_cases,
        distractor_background_n=len(distractors),
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
    parser.add_argument(
        "--wipe-between-cases",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Wipe the test substrate before each case for per-case isolation "
            "(default on; required for a valid baseline — FRE-491 codex review). "
            "Use --no-wipe-between-cases only for a deliberate cross-contamination probe."
        ),
    )
    parser.add_argument(
        "--distractor-background",
        type=int,
        default=0,
        help=(
            "Number of real live Turns (read-only) to load as a recency-window "
            "distractor background after each case seed (FRE-491). 0 = pure "
            "isolation (recall is near-trivial); sweep this to find the recall cliff."
        ),
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
