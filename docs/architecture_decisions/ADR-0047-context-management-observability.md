# ADR-0047: Context Management & Observability

**Status**: Accepted
**Date**: 2026-04-13
**Deciders**: Project owner
**Extends**: ADR-0042 (Knowledge Graph Freshness), ADR-0039 (Proactive Memory)
**Related**: ADR-0043 (Three-Layer Separation), ADR-0036 (Expansion Controller), ADR-0037 (Recall Controller), ADR-0038 (Context Compressor Model)
**Linear**: Context Intelligence — Stretch Goals project (4.S1, 4.S2, 4.7)

---

## Context

### Context management exists but isn't observable

The current system has substantial context management infrastructure:

- **Context assembly** (`request_gateway/context.py`): Assembles near context (conversation window), episodic context (memory search), and long-term context (knowledge graph entities) into the primary agent's prompt.
- **Context budget** (`request_gateway/budget.py`, S2-03): Token budget management with expansion/contraction signals from the brainstem.
- **Recall controller** (ADR-0037): Multi-layer recall — L0 (explicit entity extraction), L1 (intent-based semantic search). L2 (implicit reference detection via embeddings) is in stretch goals.
- **Context compressor** (ADR-0038): Summarization and compression to fit within token budgets.
- **Proactive memory** (ADR-0039): `suggest_relevant()` on MemoryProtocol for unsolicited context injection.
- **Knowledge freshness** (ADR-0042): Access tracking on knowledge graph entities via event bus.

What's missing is **observability into these processes**. The user (and the agent itself) cannot currently answer:

1. **What's in the context window right now?** How much is near context vs. episodic vs. long-term? How full is the budget?
2. **What was compacted, and was that a good decision?** When context is compressed, what information was lost? Did subsequent requests need that information?
3. **How fresh is the knowledge being used?** The freshness tracking (ADR-0042) captures access patterns, but this data isn't surfaced to the user or used in context assembly decisions beyond decay scoring.
4. **How confident should we be in a fact?** Knowledge has no confidence metadata. An entity extracted from a casual mention gets the same weight as one confirmed by multiple sources.
5. **What patterns does the agent see in its own performance?** The insights engine (Slice 2) analyzes delegation patterns, but there's no self-monitoring loop that feeds context management quality back into the system.

### Why observability is a context management decision, not just a dashboard

This is not about building pretty graphs. Context management happens automatically — the gateway assembles context, the compressor summarizes, the recall controller retrieves. If these processes are invisible, the user can't diagnose quality issues ("why didn't you remember X?") and the agent can't improve its own context strategies.

The three-layer architecture (ADR-0043) makes this explicit: context management is an Execution Layer concern, but context _quality_ is an Observation Layer concern that feeds back into execution.

---

## Decision

### D1: Three-tier context model with explicit boundaries

Formalize the three context tiers already implicit in the codebase:

| Tier | Scope | Source | Lifetime | Current impl |
|------|-------|--------|----------|-------------|
| **Near context** | Current conversation window | In-memory message list | Request/session | `session_context` in context assembly |
| **Episodic context** | Recent session memory | PostgreSQL (messages, task captures) | Days–weeks | `memory_search` results in context assembly |
| **Long-term context** | Knowledge base retrieval | Neo4j (entities, relationships, semantic memories) | Indefinite | `_query_memory_for_intent()` in context assembly |

Each tier has an explicit **token budget allocation** within the overall context budget:

```python
@dataclass(frozen=True)
class ContextBudgetAllocation:
    """Token budget allocation across context tiers."""
    total_budget: int          # Total tokens available for context
    near_context: int          # Conversation messages
    episodic_context: int      # Recent memory
    long_term_context: int     # Knowledge retrieval
    system_prompt: int         # Fixed system prompt overhead
    tool_definitions: int      # Tool schema overhead
    response_reserve: int      # Reserved for model output
```

Budget allocation is dynamic — a request about recent conversation history should favor near context; a knowledge question should favor long-term. The gateway's intent classification (Stage 4) informs the allocation.

### D2: Context size monitoring with UI visibility

Expose context composition as real-time data via AG-UI state events (ADR-0046):

```python
@dataclass(frozen=True)
class ContextWindowState:
    """Published as AG-UI STATE_DELTA on every request."""
    total_tokens: int
    budget_limit: int
    utilization_pct: float
    tiers: dict[str, TierUsage]  # near, episodic, long_term
    compaction_applied: bool
    compaction_reason: str | None
```

The frontend (ADR-0048) renders this as a **context window usage meter** — a visual indicator showing how full the context is, broken down by tier. The user can see at a glance whether the agent is operating near its context limit and what's consuming space.

**Alerting**: When context utilization exceeds 80%, emit a warning event. When compaction is triggered, emit an explanation event. These are AG-UI events, not background alerts — they appear in the conversation flow.

### D3: Compaction logging with feedback loops

When the context compressor (ADR-0038) runs, log a structured compaction record:

```python
@dataclass(frozen=True)
class CompactionRecord:
    """Logged to Elasticsearch and published as event."""
    trace_id: str
    session_id: str
    timestamp: datetime
    trigger: str              # "budget_exceeded", "tier_rebalance", "manual"
    tier_affected: str        # "near", "episodic", "long_term"
    tokens_before: int
    tokens_after: int
    tokens_removed: int
    strategy: str             # "summarize", "truncate", "drop_oldest"
    content_summary: str      # Brief description of what was compacted
    entities_preserved: list[str]  # Key entities kept in compacted summary
    entities_dropped: list[str]    # Entities that were in removed content
```

**Compaction feedback loop**: After compaction, if a subsequent request in the same session references information that was compacted away (detected by recall controller finding a high-relevance match to compacted content), log a `compaction_quality: poor` event. Over time, this data tells us whether the compaction strategy is too aggressive, which content types should be preserved longer, and whether certain entities should be pinned.

This is not automated self-correction (Slice 3 territory) — it's data collection that enables future self-correction.

### D4: Knowledge freshness surfacing

ADR-0042 implemented access tracking (last_accessed_at, access_count, access_context). This ADR adds **freshness surfacing** — making freshness data usable:

1. **In context assembly**: The recall controller's relevance scoring already includes recency and entity match signals. Add a **freshness modifier**: entities with `last_accessed_at` within 7 days get a slight boost; entities not accessed in 90+ days get a decay penalty. This extends ADR-0042's decay function into the context assembly hot path (read-only — no writes on the hot path).

2. **In the UI**: When the agent uses knowledge in a response, it can annotate facts with freshness metadata ("based on information from March 2026, last confirmed 3 days ago"). The frontend renders this as subtle provenance indicators.

3. **In observation**: A periodic freshness summary (weekly, via brainstem lifecycle loop) identifies:
   - **High-churn entities**: Accessed frequently, potentially outdated if sourced from volatile data.
   - **Stale entities**: Not accessed in 90+ days — candidates for review or archival.
   - **Never-retrieved entities**: Created but never used in context assembly — potentially low-value.

### D5: Knowledge weighting (confidence and source authority)

Introduce lightweight confidence metadata on knowledge graph entities:

```python
class KnowledgeWeight(BaseModel):
    model_config = ConfigDict(frozen=True)

    confidence: float         # 0.0–1.0, default 0.5 for unscored
    source_type: str          # "conversation", "tool_result", "web_search", "manual", "inferred"
    corroboration_count: int  # How many independent sources support this fact
    last_confirmed: datetime | None  # When was this fact last confirmed (not just accessed)
```

**Scoring rules**:
- Facts from direct user statements: `confidence=0.8`, `source_type="conversation"`
- Facts from tool results (web search, API calls): `confidence=0.7`, `source_type="tool_result"`
- Facts inferred by the agent during consolidation: `confidence=0.4`, `source_type="inferred"`
- Each independent corroboration increases confidence by 0.1 (capped at 1.0).
- Manual user confirmation sets `confidence=1.0`.

Confidence scores modulate recall relevance scoring — high-confidence facts are preferred when multiple results compete for limited context budget. This is a **soft signal**, not a hard filter: low-confidence facts still surface when they're the best available match.

### D6: Self-monitoring loop

The agent should observe its own context management performance. This is an Observation Layer function that feeds data to the Execution Layer:

**Metrics captured** (per-session, aggregated weekly):

| Metric | Source | What it tells us |
|--------|--------|-----------------|
| Context utilization at response time | Context assembly | How often we're near the limit |
| Compaction frequency | Compressor | How often context overflows |
| Compaction feedback score | Compaction records | How often compacted content was needed later |
| Recall hit rate | Recall controller | How often recalled context was actually used in the response |
| Freshness distribution | Knowledge graph | Age distribution of knowledge used in responses |
| Confidence distribution | Knowledge weights | Reliability of knowledge used in responses |

**User access**: The user can ask the agent "how is your context management doing?" and get a structured answer based on these metrics, rendered with MCP App visualizations (ADR-0046). This is the "discuss observations with the agent through the UI" requirement from the brief.

---

## Consequences

### Positive

- **Diagnosable context quality**: When the agent forgets something, the user (and the agent itself) can trace why — was it compacted? Was it low-relevance? Was it stale?
- **Visible context pressure**: The context meter in the UI gives the user an intuitive sense of how much room the agent has to work with. No more mysterious "the agent seems to have forgotten our earlier discussion" moments.
- **Structured compaction data**: Instead of compaction being a black box, every compaction is logged with what was lost and why. Over time, this data enables policy tuning.
- **Knowledge provenance**: Confidence and source metadata let the agent (and user) distinguish between well-established facts and speculative inferences.
- **Self-improvement foundation**: The self-monitoring metrics provide the input data that Slice 3's self-improvement loop needs to optimize context strategies.

### Negative

- **Observation overhead**: Every compaction, recall, and context assembly now generates structured logs. At current request volume (dozens/day) this is negligible. At higher volumes, Elasticsearch indexing load could become noticeable.
- **Confidence scoring is approximate**: The initial scoring rules are heuristic, not ML-based. They'll be wrong sometimes — a user's casual mention ("I think the meeting is Tuesday") gets the same `conversation` confidence as a definitive statement ("the deadline is April 30th"). Fixing this requires NLP-level analysis of statement certainty, which is Slice 3 scope.
- **Freshness surfacing in UI adds clutter**: Provenance annotations ("last confirmed 3 days ago") are useful but can overwhelm casual conversation. Needs thoughtful UI design — subtle by default, detailed on hover/tap.

### Neutral

- **Extends existing infrastructure**: All metrics flow through existing telemetry (structlog → Elasticsearch) and event bus (Redis Streams). No new infrastructure.
- **Context Intelligence stretch goals (4.S1, 4.S2, 4.7) are separate**: This ADR provides the observability foundation. LLM-as-Judge (4.S1), Context Gap Score (4.S2), and Recall L2 (4.7) are implementation tasks that build on this foundation but are tracked independently in the Context Intelligence — Stretch Goals project.
