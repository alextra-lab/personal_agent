# ADR-0025: Memory Recall Intent Detection

**Status**: Accepted  
**Date**: 2026-03-07  
**Deciders**: Project owner  

---

## Context

The agent's memory graph (Neo4j, Phase 2.2) stores rich conversational history: Turn nodes, Session nodes, Entity nodes with types, and typed relationships. However, a user query like *"What Greek locations have I asked about in the past?"* receives no memory context at all.

The root cause is the entity extraction heuristic in `step_init` (`src/personal_agent/orchestrator/executor.py`, lines 807–810):

```python
words = ctx.user_message.split()
potential_entities = [
    w.strip('",.:;!?') for w in words if len(w) > 3 and w[0].isupper()
]
```

For the query above this extracts only `["What"]`. Neo4j searches for an entity named `"What"`, finds nothing, and the agent answers without memory context.

This heuristic was designed for **task-assist queries** — *"What is the weather in Paris?"* extracts `["Paris"]` and retrieves past Paris conversations as context. It was never designed for **memory-recall queries** — questions *about* what the user has previously discussed.

### Two query classes

| Class | Example | Current behaviour |
|---|---|---|
| Task-assist | "What is the weather in Crete?" | Works: extracts "Crete", injects past turns as context |
| Memory-recall | "What Greek locations have I asked about?" | Broken: no proper nouns extracted, no context injected |

Memory-recall queries are identifiable by characteristic language: *"have I", "did I", "do you remember", "last time we", "what topics"*, etc.

---

## Decision

Add a `_MEMORY_RECALL_PATTERNS` regex to `src/personal_agent/orchestrator/routing.py`, and detect recall intent in `step_init` before the entity extraction heuristic runs.

When recall intent is detected, bypass the entity heuristic and run a **broad graph query** — returning all entities by type, dominant session topics, or recent turns — rather than searching for specific entity names.

---

## Implementation

### Step 1 — Add pattern to `routing.py`

File: `src/personal_agent/orchestrator/routing.py`

Add after the existing `_REASONING_PATTERNS` block:

```python
# MEMORY RECALL: questions about the user's own history
_MEMORY_RECALL_PATTERNS = re.compile(
    r"(?:"
    r"what\s+(?:have\s+I|did\s+I|topics?\s+have\s+I|things?\s+have\s+I)|"
    r"have\s+I\s+(?:ever|asked|mentioned|talked|discussed)|"
    r"did\s+I\s+(?:ask|mention|talk|discuss)|"
    r"do\s+you\s+remember|"
    r"(?:my|our)\s+(?:past|previous|earlier|last)\s+(?:question|conversation|session|discussion)|"
    r"last\s+time\s+(?:I|we)\s+(?:asked|talked|discussed)|"
    r"remind\s+me\s+(?:what|about)|"
    r"what\s+(?:else\s+)?(?:have\s+we|have\s+I)\s+(?:talked|discussed|covered)"
    r")",
    re.IGNORECASE,
)


def is_memory_recall_query(user_message: str) -> bool:
    """Return True if the user is asking about their own history.

    Used by step_init to select the broad-recall memory query path.

    Args:
        user_message: Raw user input.

    Returns:
        True if message matches a memory-recall intent pattern.
    """
    return bool(_MEMORY_RECALL_PATTERNS.search(user_message or ""))
```

### Step 2 — Add broad-recall query to `memory/service.py`

File: `src/personal_agent/memory/service.py`

Add a new public method to `MemoryService`:

```python
async def query_memory_broad(
    self,
    entity_types: list[str] | None = None,
    recency_days: int = 90,
    limit: int = 20,
) -> dict[str, Any]:
    """Broad memory recall: return entities and session summaries.

    Used for recall-intent queries ("what have I asked about?") where
    there are no specific entity names to search for.

    Args:
        entity_types: Optional filter e.g. ["Location", "Person"]. None = all types.
        recency_days: How far back to look.
        limit: Maximum entities to return.

    Returns:
        Dict with keys:
          - entities: list of {name, type, mentions, description}
          - sessions: list of {session_id, dominant_entities, turn_count, started_at}
          - turns_summary: list of recent turn summaries
    """
    if not self.connected or not self.driver:
        return {"entities": [], "sessions": [], "turns_summary": []}

    cutoff = (datetime.utcnow() - timedelta(days=recency_days)).isoformat()

    try:
        async with self.driver.session() as db_session:
            # Entities (optionally filtered by type)
            if entity_types:
                entity_q = """
                    MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)
                    WHERE e.entity_type IN $entity_types
                      AND t.timestamp >= $cutoff
                    RETURN e.name as name, e.entity_type as type,
                           e.description as description,
                           count(t) as mentions
                    ORDER BY mentions DESC LIMIT $limit
                """
                r = await db_session.run(entity_q,
                    entity_types=entity_types, cutoff=cutoff, limit=limit)
            else:
                entity_q = """
                    MATCH (e:Entity)<-[:DISCUSSES]-(t:Turn)
                    WHERE t.timestamp >= $cutoff
                    RETURN e.name as name, e.entity_type as type,
                           e.description as description,
                           count(t) as mentions
                    ORDER BY mentions DESC LIMIT $limit
                """
                r = await db_session.run(entity_q, cutoff=cutoff, limit=limit)
            entities = await r.data()

            # Recent sessions with dominant topics
            session_q = """
                MATCH (s:Session)
                WHERE s.started_at >= $cutoff
                RETURN s.session_id as session_id,
                       s.dominant_entities as dominant_entities,
                       s.turn_count as turn_count,
                       s.started_at as started_at
                ORDER BY s.started_at DESC LIMIT 10
            """
            r = await db_session.run(session_q, cutoff=cutoff)
            sessions = await r.data()

            # Recent turn summaries
            turn_q = """
                MATCH (t:Turn)
                WHERE t.timestamp >= $cutoff
                RETURN t.summary as summary, t.key_entities as entities,
                       t.timestamp as ts
                ORDER BY t.timestamp DESC LIMIT 10
            """
            r = await db_session.run(turn_q, cutoff=cutoff)
            turns = await r.data()

            return {
                "entities": entities,
                "sessions": sessions,
                "turns_summary": turns,
            }

    except Exception as e:
        log.error("query_memory_broad_failed", error=str(e), exc_info=True)
        return {"entities": [], "sessions": [], "turns_summary": []}
```

### Step 3 — Branch in `step_init` (`executor.py`)

File: `src/personal_agent/orchestrator/executor.py`

Import the new function at the top of the file (with existing routing imports):

```python
from personal_agent.orchestrator.routing import (
    heuristic_routing, resolve_role, is_memory_recall_query
)
```

Replace the memory query block (starting at line ~788) with:

```python
# Query memory graph for relevant context (Phase 2.2)
if settings.enable_memory_graph:
    if timer:
        timer.start_span("memory_query")
    try:
        from personal_agent.memory.models import MemoryQuery
        from personal_agent.memory.service import MemoryService

        memory_service = None
        try:
            from personal_agent.service.app import memory_service as global_memory_service
            if global_memory_service and global_memory_service.connected:
                memory_service = global_memory_service
        except (ImportError, AttributeError):
            memory_service = MemoryService()
            await memory_service.connect()

        if memory_service and memory_service.connected:
            conversations_found = 0

            if is_memory_recall_query(ctx.user_message):
                # --- Broad recall path ---
                # Extract optional entity type hint from message
                # e.g. "locations" → "Location", "people" → "Person"
                entity_type_hints = _extract_entity_type_hints(ctx.user_message)
                broad = await memory_service.query_memory_broad(
                    entity_types=entity_type_hints or None,
                    recency_days=90,
                    limit=20,
                )
                ctx.memory_context = _format_broad_recall(broad)
                conversations_found = len(ctx.memory_context)
                log.info(
                    "memory_recall_broad_query",
                    trace_id=ctx.trace_id,
                    entity_type_hints=entity_type_hints,
                    entities_found=len(broad.get("entities", [])),
                )
            else:
                # --- Entity-name match path (existing) ---
                words = ctx.user_message.split()
                potential_entities = [
                    w.strip('",.:;!?') for w in words if len(w) > 3 and w[0].isupper()
                ]
                if potential_entities:
                    query = MemoryQuery(
                        entity_names=potential_entities[:5],
                        limit=5,
                        recency_days=30,
                    )
                    result = await memory_service.query_memory(
                        query,
                        feedback_key=ctx.session_id,
                        query_text=ctx.user_message,
                    )
                    ctx.memory_context = [
                        {
                            "conversation_id": conv.conversation_id,
                            "timestamp": conv.timestamp.isoformat(),
                            "user_message": conv.user_message,
                            "summary": conv.summary or conv.user_message[:200],
                            "key_entities": conv.key_entities,
                        }
                        for conv in result.conversations
                    ]
                    conversations_found = len(ctx.memory_context)
                    log.info(
                        "memory_enrichment_completed",
                        trace_id=ctx.trace_id,
                        conversations_found=conversations_found,
                    )
        # ... rest of existing cleanup / timer code unchanged
```

### Step 4 — Helper functions (add to `executor.py`)

```python
# Keyword → entity_type mapping for type-hint extraction
_ENTITY_TYPE_KEYWORDS: dict[str, str] = {
    "location": "Location", "locations": "Location", "place": "Location",
    "places": "Location", "city": "Location", "cities": "Location",
    "country": "Location", "countries": "Location",
    "person": "Person", "people": "Person", "someone": "Person",
    "organization": "Organization", "org": "Organization", "company": "Organization",
    "companies": "Organization", "tool": "Technology", "tools": "Technology",
    "technology": "Technology", "topic": "Topic", "topics": "Topic",
    "concept": "Concept", "concepts": "Concept",
}


def _extract_entity_type_hints(user_message: str) -> list[str]:
    """Map words in the query to entity_type values.

    e.g. "What Greek locations" → ["Location"]
         "What tools have I used" → ["Technology"]
         "What have I discussed" → []
    """
    words = user_message.lower().split()
    types: set[str] = set()
    for w in words:
        clean = w.strip('",.:;!?')
        if clean in _ENTITY_TYPE_KEYWORDS:
            types.add(_ENTITY_TYPE_KEYWORDS[clean])
    return list(types)


def _format_broad_recall(broad: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert query_memory_broad result to memory_context format.

    The list is injected into the system prompt; keep it concise.
    """
    items: list[dict[str, Any]] = []
    for e in broad.get("entities", []):
        items.append({
            "type": "entity",
            "name": e["name"],
            "entity_type": e["type"],
            "mentions": e["mentions"],
            "description": e.get("description", ""),
        })
    for s in broad.get("sessions", []):
        items.append({
            "type": "session",
            "session_id": s["session_id"],
            "dominant_entities": s.get("dominant_entities", []),
            "turn_count": s["turn_count"],
        })
    return items
```

### Step 5 — Update the system prompt injection

File: `src/personal_agent/orchestrator/executor.py`, near line 1022.

The existing block injects conversation summaries. When `memory_context` contains entity/session items (from the broad path), the format should differ:

```python
if model_role != ModelRole.ROUTER and ctx.memory_context and len(ctx.memory_context) > 0:
    if ctx.memory_context[0].get("type") in ("entity", "session"):
        # Broad recall path — format as a direct knowledge summary
        entity_lines = [
            f"- [{m['entity_type']}] {m['name']}: {m.get('description', '')} "
            f"(mentioned {m.get('mentions', 1)}x)"
            for m in ctx.memory_context if m.get("type") == "entity"
        ]
        memory_section = "\n\n## Your Memory Graph — Known Entities\n"
        memory_section += "\n".join(entity_lines[:15])
        memory_section += (
            "\n\nUse this list to directly answer questions about what the user "
            "has previously discussed. Do NOT say you have no memory."
        )
    else:
        # Existing task-assist path — inject conversation summaries
        memory_section = "\n\n## Relevant Past Conversations\n"
        # ... existing code unchanged
```

---

## Alternatives Considered

**A. Use the LLM router to classify recall intent.**
Rejected: adds ~3–5s latency per request; heuristic is fast (< 1ms) and sufficient for the well-defined recall vocabulary.

**B. Always run the broad query on every request.**
Rejected: the broad query touches every Turn node in Neo4j; at scale this is expensive. Only run it when intent is detected.

**C. Rely on `search_memory` tool (ADR-0026) instead.**
Partially overlapping. ADR-0026 handles in-conversation tool calls when the agent explicitly needs to query history mid-task. ADR-0025 handles the entry-point injection so context is already present when the LLM first sees the user message. Both are needed.

---

## Consequences

**Positive:**
- Recall queries ("what have I asked about?") now receive accurate memory context
- No latency impact for the common case (heuristic runs in < 1ms)
- `is_memory_recall_query` is unit-testable independently of the full orchestrator

**Negative:**
- False positives on the recall pattern will trigger broad Neo4j queries unnecessarily. Mitigation: the broad query is read-only and fast on small graphs.
- The `_ENTITY_TYPE_KEYWORDS` mapping must be maintained as the entity taxonomy evolves.

---

## Acceptance Criteria

- [ ] `uv run agent "What Greek locations have I asked about in the past?"` returns a list of Location entities from the graph
- [ ] `uv run agent "What tools have I used recently?"` returns Technology entities
- [ ] `uv run agent "What is the weather in Crete?"` continues to use the entity-name match path (not broad)
- [ ] Unit tests for `is_memory_recall_query` cover at least 10 positive and 10 negative cases
- [ ] Unit tests for `_extract_entity_type_hints` cover all 7 entity types
