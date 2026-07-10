"""ADR-0114 D2/D3/D4 corpus ingest driver (FRE-839).

Drives the ingest categorizer + accretion writer over the frozen study
corpus (FRE-838): for each conversation (`Session`), reads its raw
transcript and the concepts the frozen corpus already knows it discussed
(`Session-[:DISCUSSES]->Entity` — not rediscovered), categorizes them
in-context, resolves each concept's `Concept` hub, and writes the evidence
+ derived layers.

Safety/cost posture mirrors `export_snapshot.py`'s spirit (a cheap default,
an explicit flag for the consequential/costly action) without being a
literal dry run — there's no meaningful no-LLM dry run for a categorizer
script beyond `--limit 0`:

    uv run python -m scripts.study.run_ingest --limit 5       # small, cheap sample
    uv run python -m scripts.study.run_ingest --execute-full  # all 102 sessions — real cost

Usage note: `--execute-full` makes real, paid LLM calls (`budget_role=study`,
isolated from `entity_extraction`'s cap) against the real conversation
corpus — get an explicit owner go-ahead before running it, per this
project's "confirm before consequential/cost-incurring actions" norm.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.study.categorizer import (  # noqa: E402
    CATEGORIZER_PROMPT_VERSION,
    categorize_conversation,
    get_categorizer_model_id,
)
from scripts.study.neo4j_types import Neo4jDriver, Neo4jSession  # noqa: E402
from scripts.study.schema import apply_schema  # noqa: E402
from scripts.study.writer import (  # noqa: E402
    AssertionProvenance,
    ProposedMembership,
    ResolvedConceptMemberships,
    recompute_member_of_batch,
    resolve_concept_hubs_batch,
    write_episode,
    write_mentions_and_assertions,
)

log = structlog.get_logger(__name__)


async def _fetch_sessions(session: Neo4jSession, limit: int | None) -> list[dict[str, Any]]:
    """Every `Session` (conversation) in the frozen corpus, oldest first."""
    query = (
        "MATCH (s:Session) "
        "RETURN s.session_id AS session_id, s.raw_messages_json AS raw_messages_json "
        "ORDER BY s.started_at ASC"
    )
    params: dict[str, Any] = {}
    if limit is not None:
        query += " LIMIT $limit"
        params["limit"] = limit
    result = await session.run(query, params)
    return [dict(r) async for r in result]


async def _fetch_discussed_entities(session: Neo4jSession, session_id: str) -> list[dict[str, Any]]:
    """The concepts (name/kind/embedding) the frozen corpus already knows this
    conversation discussed — read off the prod-computed `DISCUSSES` edge, not
    rediscovered by the categorizer.
    """
    result = await session.run(
        "MATCH (s:Session {session_id: $session_id})-[:DISCUSSES]->(e:Entity) "
        "RETURN e.name AS name, e.entity_type AS kind, e.embedding AS embedding",
        {"session_id": session_id},
    )
    return [dict(r) async for r in result]


def _conversation_text(raw_messages_json: str | None) -> str:
    """Render the raw message trace as a plain `role: content` transcript."""
    if not raw_messages_json:
        return ""
    messages = json.loads(raw_messages_json)
    return "\n".join(f"{m.get('role', 'unknown')}: {m.get('content', '')}" for m in messages)


async def _episode_already_processed(session: Neo4jSession, episode_id: str) -> bool:
    """True if this session was already ingested in a prior run.

    Code-review finding (FRE-839): without this check, re-running
    `run_ingest` over overlapping sessions (e.g. a `--limit 5` sample
    followed later by `--execute-full`) would re-categorize and re-write
    those sessions — `write_mentions_and_assertions` always `CREATE`s fresh
    `MembershipAssertion`s by design (accretion), so a repeat run silently
    doubles their assertion count, inflates `support_count`/degree in the
    AC-1 report with duplicate provenance from what is really the same
    conversation, and doubles LLM cost for the overlapping sessions.
    `write_episode` only ever runs after a non-empty categorizer result
    (see below), so an existing `Episode` node reliably means "already
    processed", not merely "attempted".
    """
    result = await session.run(
        "MATCH (e:Episode {id: $episode_id}) RETURN count(e) > 0 AS already_processed",
        {"episode_id": episode_id},
    )
    record = await result.single()
    return bool(record["already_processed"]) if record else False


async def _process_session(
    driver: Neo4jDriver, *, session_id: str, raw_messages_json: str | None, seed: int
) -> int:
    """Categorize + write one conversation's memberships. Returns the count
    of `MembershipAssertion`s written (0 if the categorizer returned nothing
    or this session was already processed by a prior run).
    """
    async with driver.session() as session:
        if await _episode_already_processed(session, session_id):
            log.info("study_ingest_session_already_processed", session_id=session_id)
            return 0
        entities = await _fetch_discussed_entities(session, session_id)
    if not entities:
        return 0

    conversation_text = _conversation_text(raw_messages_json)
    concepts = [(e["name"], e["kind"]) for e in entities]

    memberships = await categorize_conversation(
        conversation_text, concepts, seed=seed, trace_id=session_id
    )
    if not memberships:
        return 0

    embedding_by_name = {e["name"]: e.get("embedding") for e in entities}
    kind_by_name = {e["name"]: e["kind"] for e in entities}

    async with driver.session() as session:
        await write_episode(session, episode_id=session_id, source_session_id=session_id)

        distinct_names = {m.concept_name for m in memberships}
        concept_id_by_name = await resolve_concept_hubs_batch(
            session,
            surfaces=[
                (name, kind_by_name[name], embedding_by_name.get(name)) for name in distinct_names
            ],
        )

        grouped: dict[str, list[ProposedMembership]] = {}
        for m in memberships:
            grouped.setdefault(m.concept_name, []).append(m)

        resolved = [
            ResolvedConceptMemberships(concept_id=concept_id_by_name[name], memberships=ms)
            for name, ms in grouped.items()
        ]
        provenance = AssertionProvenance(
            model=get_categorizer_model_id(),
            prompt_version=CATEGORIZER_PROMPT_VERSION,
            seed=seed,
            when=datetime.now(timezone.utc),
        )
        pairs = await write_mentions_and_assertions(
            session, episode_id=session_id, resolved=resolved, provenance=provenance
        )
        await recompute_member_of_batch(session, pairs=pairs)

    return len(memberships)


async def run_ingest(driver: Neo4jDriver, *, limit: int | None, seed: int) -> dict[str, Any]:
    """Drive the categorizer+writer over the frozen corpus.

    Args:
        driver: Connected async Neo4j driver pointed at the study sandbox.
        limit: Cap on sessions processed (`None` = all — the real,
            cost-incurring corpus run).
        seed: Run identifier stamped as provenance on every assertion this
            run creates (an honest run-identifier, not a determinism
            guarantee — see `categorizer.py`).

    Returns:
        Summary counts: sessions processed, assertions written, distinct
        concepts touched. No raw conversation content included.
    """
    await apply_schema(driver)

    async with driver.session() as session:
        sessions = await _fetch_sessions(session, limit)

    sessions_processed = 0
    sessions_failed = 0
    assertions_written = 0
    for sess in sessions:
        session_id = str(sess["session_id"])
        try:
            written = await _process_session(
                driver,
                session_id=session_id,
                raw_messages_json=sess.get("raw_messages_json"),
                seed=seed,
            )
        except Exception:
            # Code-review finding (FRE-839): one session's failure (e.g. a
            # malformed categorizer response — see categorizer.py's
            # fail-open contract) must not abort the entire multi-session
            # corpus run, losing already-incurred LLM spend on every prior
            # session in a real `--execute-full` run. Logged with the full
            # traceback and skipped; `_episode_already_processed` makes a
            # re-run safe to resume from the top without re-processing
            # already-completed sessions.
            log.exception("study_ingest_session_failed", session_id=session_id)
            sessions_failed += 1
            continue
        assertions_written += written
        sessions_processed += 1
        log.info(
            "study_ingest_session_processed",
            session_id=session_id,
            assertions_written=written,
        )

    return {
        "sessions_processed": sessions_processed,
        "sessions_failed": sessions_failed,
        "assertions_written": assertions_written,
    }


async def _setup_cost_gate() -> Any:
    """Construct, connect, and register a `CostGate` (mirrors
    `migrate_fre772_entity_type_v2.py`'s standalone-script shape) — a
    standalone script gets no cost-gate wiring for free.
    """
    from personal_agent.config import settings
    from personal_agent.cost_gate import CostGate, load_budget_config, set_default_gate

    budget_config = load_budget_config()
    gate = CostGate(config=budget_config, db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    return gate


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Process at most N sessions (default: 5, a small/cheap sample). "
        "Ignored if --execute-full is passed.",
    )
    parser.add_argument(
        "--execute-full",
        action="store_true",
        default=False,
        help="Process all sessions in the frozen corpus (real LLM cost — "
        "get an owner go-ahead first).",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="Run identifier stamped as provenance (default: 0)."
    )
    return parser.parse_args()


async def _amain(args: argparse.Namespace) -> dict[str, Any]:
    from neo4j import AsyncGraphDatabase

    from scripts.study.config import StudySettings

    settings = StudySettings()
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    cost_gate = None
    try:
        cost_gate = await _setup_cost_gate()
        limit = None if args.execute_full else args.limit
        return await run_ingest(driver, limit=limit, seed=args.seed)
    finally:
        if cost_gate is not None:
            from personal_agent.cost_gate import set_default_gate

            set_default_gate(None)
            await cost_gate.reap_stale()
            await cost_gate.disconnect()
        await driver.close()


def main() -> None:
    """CLI entrypoint."""
    args = _parse_args()
    summary = asyncio.run(_amain(args))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
