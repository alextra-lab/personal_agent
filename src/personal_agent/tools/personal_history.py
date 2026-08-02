"""FRE-343 — recall_personal_history tool.

Retrieves the connected user's own past turns within a time window. Reachability
(FRE-1119) is property-authoritative with edge-fallback: a turn is the caller's
if its own `Turn.user_id` property says so; only when that property is absent
does a (:Person)-[:PARTICIPATED_IN]->(:Turn) edge count. This order matters —
a stale or conflicting edge can never override an authoritative property, which
matters because both signals are independently, imperfectly written elsewhere.
Use only when the user explicitly refers to their personal history; general
knowledge questions stay on search_memory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from personal_agent.captains_log.turn_evidence import mark_truncated
from personal_agent.telemetry import get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)


recall_personal_history_tool = ToolDefinition(
    name="recall_personal_history",
    description=(
        "Retrieve the connected user's own past turns within a time window. "
        "Use ONLY when the user explicitly refers to their personal history — "
        "phrasing like 'we talked about', 'what did I ask', 'remind me what I said', "
        "'my conversation last week'. For general knowledge questions "
        "('what do we know about X', 'tell me about Y'), use search_memory instead — "
        "that searches the full shared graph."
    ),
    category="memory",
    parameters=[
        ToolParameter(
            name="days_ago",
            type="number",
            description=(
                "How many days back to look. 1 = last 24 hours, 7 = past week. Range 1..365."
            ),
            required=True,
        ),
        ToolParameter(
            name="topic",
            type="string",
            description=(
                "Optional topic hint (case-insensitive, checked against the message, "
                "response, summary, and discussed entities). Matching turns are ranked "
                "first, but non-matching turns in the time window are still returned — "
                "this is not a semantic search, so an unrelated phrasing of the same "
                "topic may not rank first."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description="Max turns to return (1..50, default 10).",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=None,
)


def _get_memory_service() -> Any:
    """Resolve the global MemoryService at call time.

    Indirection makes the dependency monkeypatch-able in tests. Returns Any
    rather than MemoryService to avoid an import cycle with service.app.
    """
    try:
        from personal_agent.service.app import memory_service as global_memory_service
    except (ImportError, AttributeError):
        return None
    return global_memory_service


async def recall_personal_history_executor(
    days_ago: int,
    topic: str | None = None,
    limit: int | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Retrieve the connected user's own past turns within a time window.

    Args:
        days_ago: How many days back to look (1..365).
        topic: Optional case-insensitive topic hint; ranks matching turns
            first without excluding non-matching ones.
        limit: Max turns to return (1..50, default 10).
        ctx: Trace context. ``ctx.user_id`` must be present — matched against
            each Turn's own ``user_id`` property, falling back to the
            PARTICIPATED_IN edge only when that property is absent (FRE-1119).

    Returns:
        Dict with turns, total, window_days, and user_id (for trace correlation).

    Raises:
        ToolExecutionError: ctx.user_id missing, days_ago out of range, or
            memory service unavailable.
    """
    user_id = getattr(ctx, "user_id", None) if ctx else None
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    if user_id is None:
        raise ToolExecutionError(
            "missing_user_id — this is a bug; report it (FRE-343). "
            "recall_personal_history requires ctx.user_id."
        )

    days_ago_int = int(days_ago)
    if days_ago_int < 1 or days_ago_int > 365:
        raise ToolExecutionError(f"days_ago must be between 1 and 365, got {days_ago_int}")

    effective_limit = min(max(int(limit) if limit is not None else 10, 1), 50)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_ago_int)).isoformat()

    log.info(
        "recall_personal_history_called",
        trace_id=trace_id,
        user_id=str(user_id),
        days_ago=days_ago_int,
        topic_set=topic is not None,
        limit=effective_limit,
    )

    svc = _get_memory_service()
    if svc is None or not getattr(svc, "connected", False):
        raise ToolExecutionError("Memory service unavailable or not connected.")

    cypher = """
        CALL {
          MATCH (t:Turn {user_id: $user_id})
          RETURN t
          UNION ALL
          MATCH (:Person {user_id: $user_id})-[:PARTICIPATED_IN]->(t:Turn)
          WHERE t.user_id IS NULL
          RETURN t
        }
        WITH t
        WHERE t.timestamp >= $cutoff
        OPTIONAL MATCH (t)-[:DISCUSSES]->(e:Entity)
        WITH t, collect(DISTINCT e.name) AS entities
        WITH t, entities,
             $topic IS NULL
               OR toLower(t.user_message) CONTAINS toLower($topic)
               OR toLower(coalesce(t.assistant_response, '')) CONTAINS toLower($topic)
               OR toLower(coalesce(t.summary, '')) CONTAINS toLower($topic)
               OR any(name IN entities WHERE toLower(name) CONTAINS toLower($topic))
             AS topic_matched
        RETURN t.turn_id            AS turn_id,
               t.timestamp          AS timestamp,
               t.session_id         AS session_id,
               t.user_message       AS user_message,
               t.assistant_response AS assistant_response,
               t.summary            AS summary,
               entities             AS entities,
               topic_matched        AS topic_matched
        ORDER BY topic_matched DESC, t.timestamp DESC
        LIMIT $limit
    """

    async with svc.driver.session() as session:
        result = await session.run(
            cypher,
            user_id=str(user_id),
            cutoff=cutoff,
            topic=topic,
            limit=effective_limit,
        )
        records = await result.data()

    turns = [
        {
            "turn_id": r["turn_id"],
            "timestamp": r["timestamp"],
            "session_id": r["session_id"],
            "user_message": mark_truncated(r.get("user_message") or "", 400),
            "assistant_response": mark_truncated(r.get("assistant_response") or "", 400),
            "summary": r.get("summary") or "",
            "entities": r.get("entities") or [],
            **({"topic_matched": bool(r.get("topic_matched"))} if topic is not None else {}),
        }
        for r in records
    ]

    log.info(
        "personal_history_recalled",
        trace_id=trace_id,
        turn_count=len(turns),
        days_ago=days_ago_int,
        topic_set=topic is not None,
        user_id=str(user_id),
    )

    return {
        "turns": turns,
        "total": len(turns),
        "window_days": days_ago_int,
        "user_id": str(user_id),
    }
