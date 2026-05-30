#!/usr/bin/env python3
"""Replay Postgres sessions into Neo4j via the entity extractor (FRE-374 D3).

Reads all sessions from Postgres (messages JSONB), constructs TaskCapture objects
from user/assistant message pairs, and processes each through the consolidator to
re-populate entity descriptions from the current extractor model.

Usage:
    uv run python scripts/replay_sessions_to_neo4j.py --help
    uv run python scripts/replay_sessions_to_neo4j.py --dry-run --since 2026-01-01
    uv run python scripts/replay_sessions_to_neo4j.py --since 2025-01-01 --confirm-prod

IMPORTANT: Run this ONLY after:
  1. Taking a Neo4j snapshot (see ADR-0073 §D3)
  2. Optionally clearing the graph: MATCH (n) DETACH DELETE n

The script respects AGENT_* env vars — point AGENT_NEO4J_URI at the desired target.
Estimated LLM calls: ~3,000-5,000 for a full replay (1,025 sessions x ~3-5 pairs).
Use --limit to batch and --sleep-ms to stay within budget caps (FRE-303).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import structlog

log = structlog.get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--since",
        default="2025-01-01",
        help="Replay sessions created on or after this date (YYYY-MM-DD). Default: 2025-01-01",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max sessions to process. 0 = no limit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log what would be processed without calling extractor or writing to Neo4j.",
    )
    parser.add_argument(
        "--confirm-prod",
        action="store_true",
        default=False,
        help=(
            "Required when AGENT_ENVIRONMENT is not 'test'. "
            "Confirms intent to write to production substrate."
        ),
    )
    parser.add_argument(
        "--sleep-ms",
        type=int,
        default=0,
        help="Sleep this many milliseconds between captures (rate-limiting for budget-constrained runs).",
    )
    return parser.parse_args()


async def _fetch_sessions(since_date: str, limit: int) -> list[dict[str, Any]]:
    """Query Postgres for sessions with at least one user message.

    Args:
        since_date: ISO date string (YYYY-MM-DD).
        limit: Max sessions to return; 0 = no limit.

    Returns:
        List of dicts with keys: session_id, created_at, messages, metadata.
    """
    import asyncpg

    from personal_agent.config import get_settings

    settings = get_settings()
    db_url = settings.database_url
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    conn = await asyncpg.connect(db_url)
    try:
        limit_clause = f"LIMIT {limit}" if limit > 0 else ""
        rows = await conn.fetch(
            f"""
            SELECT session_id, created_at, messages, metadata
            FROM sessions
            WHERE created_at >= $1
              AND jsonb_array_length(messages) > 0
            ORDER BY created_at ASC
            {limit_clause}
            """,
            datetime.fromisoformat(since_date).replace(tzinfo=timezone.utc),
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


def _extract_message_pairs(
    session: dict[str, Any],
) -> list[tuple[str, str, datetime]]:
    """Extract (user_message, assistant_response, timestamp) pairs from session messages JSONB.

    Args:
        session: Session dict with 'messages' as a list of dicts.

    Returns:
        List of (user_message, assistant_response, timestamp) tuples.
        assistant_response is empty string if no following assistant message.
    """
    import json

    messages = session.get("messages") or []
    if isinstance(messages, str):
        messages = json.loads(messages)

    pairs: list[tuple[str, str, datetime]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "user":
            user_text = (msg.get("content") or "").strip()
            ts_raw = msg.get("timestamp") or session.get("created_at")
            if ts_raw:
                ts = datetime.fromisoformat(str(ts_raw)).replace(tzinfo=timezone.utc)
            else:
                ts = datetime.now(timezone.utc)
            assistant_text = ""
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                assistant_text = (messages[i + 1].get("content") or "").strip()
                i += 1
            if user_text:
                pairs.append((user_text, assistant_text, ts))
        i += 1
    return pairs


async def _replay_session(
    session: dict[str, Any],
    consolidator: Any,
    sleep_ms: int,
    dry_run: bool,
) -> dict[str, int]:
    """Process one session through the consolidator.

    Args:
        session: Session dict from Postgres.
        consolidator: SecondBrainConsolidator instance.
        sleep_ms: Milliseconds to sleep between captures.
        dry_run: If True, log only — no extractor calls or Neo4j writes.

    Returns:
        Dict with counts: turns_processed, entities_created, relationships_created, errors.
    """
    import json as _json

    from personal_agent.captains_log.capture import TaskCapture

    session_id = str(session["session_id"])
    raw_metadata = session.get("metadata") or {}
    if isinstance(raw_metadata, str):
        try:
            raw_metadata = _json.loads(raw_metadata)
        except (ValueError, TypeError):
            raw_metadata = {}
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    user_id_raw = metadata.get("user_id") or metadata.get("owner_id")
    try:
        user_id = UUID(str(user_id_raw)) if user_id_raw else uuid4()
    except ValueError:
        user_id = uuid4()

    pairs = _extract_message_pairs(session)
    if not pairs:
        return {
            "turns_processed": 0,
            "entities_created": 0,
            "relationships_created": 0,
            "errors": 0,
        }

    counts: dict[str, int] = {
        "turns_processed": 0,
        "entities_created": 0,
        "relationships_created": 0,
        "errors": 0,
    }

    for user_msg, assistant_msg, ts in pairs:
        if dry_run:
            log.info(
                "replay_dry_run_pair",
                session_id=session_id,
                user_message_preview=user_msg[:80],
            )
            counts["turns_processed"] += 1
            continue

        capture = TaskCapture(
            trace_id=str(uuid4()),
            session_id=session_id,
            timestamp=ts,
            user_message=user_msg,
            assistant_response=assistant_msg or None,
            outcome="completed",
            user_id=user_id,
            tools_used=[],
            duration_ms=None,
        )
        try:
            result = await consolidator._process_capture(capture)
            counts["turns_processed"] += 1
            counts["entities_created"] += result.get("entities_created", 0)
            counts["relationships_created"] += result.get("relationships_created", 0)
        except Exception as exc:
            log.warning("replay_capture_failed", session_id=session_id, error=str(exc))
            counts["errors"] += 1

        if sleep_ms > 0:
            await asyncio.sleep(sleep_ms / 1000.0)

    return counts


async def main() -> None:
    """Main entrypoint for the replay script."""
    from personal_agent.config import get_settings
    from personal_agent.config.env_loader import Environment
    from personal_agent.memory.service import MemoryService
    from personal_agent.second_brain.consolidator import SecondBrainConsolidator

    args = _parse_args()
    settings = get_settings()

    if settings.environment != Environment.TEST and not args.confirm_prod:
        print(
            "ERROR: Running against non-TEST environment without --confirm-prod.\n"
            "This script writes to the Neo4j substrate.\n"
            "Re-run with --confirm-prod to confirm intent.",
            file=sys.stderr,
        )
        sys.exit(2)

    log.info(
        "replay_starting",
        since=args.since,
        limit=args.limit,
        dry_run=args.dry_run,
        sleep_ms=args.sleep_ms,
        neo4j_uri=settings.neo4j_uri,
    )

    from personal_agent.cost_gate import CostGate, load_budget_config, set_default_gate

    memory_service = MemoryService()  # fre-375-allow: prod replay script, gated by --confirm-prod
    connected = await memory_service.connect()
    if not connected:
        log.error("replay_neo4j_connect_failed")
        sys.exit(1)

    # Register a CostGate so LiteLLM calls can reserve and commit budget.
    # Without this, every extraction call raises CostGateNotRegistered and the
    # cost_tracker pool leaks a connection per call.
    budget_config = load_budget_config()
    cost_gate = CostGate(config=budget_config, db_url=settings.database_url)
    await cost_gate.connect()
    set_default_gate(cost_gate)
    log.info("replay_cost_gate_opened", roles=len(budget_config.roles))

    consolidator = SecondBrainConsolidator(memory_service=memory_service)
    sessions = await _fetch_sessions(args.since, args.limit)
    log.info("replay_sessions_fetched", count=len(sessions))

    total: dict[str, int] = {
        "turns_processed": 0,
        "entities_created": 0,
        "relationships_created": 0,
        "errors": 0,
    }
    for i, session in enumerate(sessions, 1):
        log.info(
            "replay_session_start",
            n=i,
            total=len(sessions),
            session_id=str(session["session_id"]),
        )
        result = await _replay_session(session, consolidator, args.sleep_ms, args.dry_run)
        for k, v in result.items():
            total[k] += v

    await memory_service.disconnect()
    await cost_gate.disconnect()
    log.info("replay_complete", sessions_processed=len(sessions), **total)


if __name__ == "__main__":
    asyncio.run(main())
