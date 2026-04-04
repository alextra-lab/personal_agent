# ADR-0039: Proactive Memory via `suggest_relevant()`

**Status:** Accepted (MVP implemented 2026-04-04; FRE-174–176)  
**Date:** 2026-03-30  
**Deciders:** Project owner  
**Depends on:** ADR-0035 (Seshat / Neo4j backend), ADR-0024 (session graph model)  
**Related spec:** `docs/specs/PROACTIVE_MEMORY_DESIGN.md`

## Context

The personal agent persists long-term memory in **Seshat** (Neo4j) behind `MemoryProtocol` (`src/personal_agent/memory/protocol.py`). Context assembly (`assemble_context()` in `src/personal_agent/request_gateway/context.py`) enriches prompts via `_query_memory_for_intent()`.

Today, **non-`MEMORY_RECALL` intents** rely on a **crude heuristic**: treat capitalized words longer than three characters as entity names and call `recall()`; if none match, **no memory query runs**. That misses most relevant cross-session context (lowercase entities, paraphrases, implicit references).

**`MEMORY_RECALL` intents** use `recall_broad()`, which is appropriate for explicit “what’s in my memory” questions but is not a substitute for **continuous, ranked relevance** on every turn.

The project owner wants **proactive** memory: Seshat should **suggest** relevant past context **without** being asked, while controlling **noise** and **token cost**.

## Decision

1. Extend **`MemoryProtocol`** with **`suggest_relevant()`**, taking the **current user message**, **session entity context**, and identifiers needed for recency/topic signals, and returning **scored candidate memories** (see design spec for shape).

2. Implement **multi-signal relevance scoring** combining:
   - **Embedding similarity** between the user message (and optional session hint) and stored memory vectors in Neo4j  
   - **Entity overlap** between current session entities and memory-linked entities  
   - **Recency weighting** favoring newer sessions  
   - **Session-level topic coherence** between current session topic proxy and candidate session topic proxy  

3. Enforce **noise controls**: default **minimum relevance threshold 0.3**, **maximum ~500 tokens** of proactive memory injection, and **diminishing-returns** cutoff when additional items add little score.

4. Integrate in **`_query_memory_for_intent()`** so **all intents** participate (not only `MEMORY_RECALL`), with **`assemble_context()`** ordering unchanged in spirit: session history first, then memory enrichment, then user message. Keep **`recall_broad()`** for explicit recall until a follow-up ADR unifies paths.

**Why this approach:** A dedicated **scored retrieval** API keeps the graph as the source of truth, avoids dumping the whole graph into the prompt (“always-on broad recall”), and fixes the **coverage gap** of capitalized-word matching without requiring the user to phrase questions as memory queries.

## Alternatives Considered

### 1. Enhanced keyword extraction (no embeddings)

- **Pro:** No extra embedding latency or embedding API cost; simpler operations.  
- **Con:** Still brittle for paraphrases, multilingual text, and implicit references; does not use the vector capabilities already implied by a modern memory stack.  
- **Rejected:** Insufficient lift for “personal agent” continuity goals.

### 2. Always-on broad recall (query everything, let the LLM filter)

- **Pro:** Simple caller code; maximum recall.  
- **Con:** Blows context budget, adds noise, increases latency and cost; forces the primary model to do retrieval work it is not optimized for.  
- **Rejected:** Conflicts with bounded context and predictable behavior.

### 3. User-initiated only (status quo)

- **Pro:** Zero extra per-turn cost; no risk of spurious injections.  
- **Con:** Most turns never touch memory; cross-session value is **latent** unless the user explicitly asks or capitalizes entities.  
- **Rejected:** Does not meet proactive memory product intent.

## Consequences

### Positive

- **Richer context** on ordinary turns; better **cross-session continuity** for the project owner.  
- **Ranked, explainable (internally)** scores enable tuning thresholds and debugging bad suggestions.  
- Clear extension point on **`MemoryProtocol`** for alternate backends that support vectors + graph.

### Negative

- **Additional embedding call per turn** (typical order ~50ms plus network variance, model-dependent) and small ongoing cost.  
- **Risk of noise injection** if thresholds are wrong or the graph is stale — can mislead the model if not bounded by budget and minimum score.  
- **Implementation complexity** in Neo4j queries (vector + graph filters) and in tests (fixtures for embeddings).

### Neutral / follow-up

- **Interaction with context compression (ADR-0038):** Session-level signals for proactive memory must remain coherent when older turns are compressed; may require feeding summaries into `suggest_relevant()` inputs.

## Acceptance Criteria

- [x] `suggest_relevant()` is declared on `MemoryProtocol` and implemented on `MemoryServiceAdapter` with structured logging and `trace_id` on all paths (`proactive_memory_suggest_*` events).
- [x] Final relevance score combines embedding similarity, entity overlap, recency, and topic coherence (topic term is an MVP stub in `memory/proactive.py`; hook extensible).
- [x] Candidates below **0.3** relevance are excluded; injected proactive memory respects **~500 token** budget and diminishing-returns cutoff (`AppConfig` + `build_proactive_suggestions`).
- [x] `_query_memory_for_intent()` uses proactive suggestion for **non-`MEMORY_RECALL`** intents when `proactive_memory_enabled`; **`MEMORY_RECALL`** still uses `recall_broad()` only.
- [x] Feature flag **`AGENT_PROACTIVE_MEMORY_ENABLED`** supports EVAL A/B (default off).
- [x] EVAL procedure and comparison template: `telemetry/evaluation/EVAL-proactive-memory/README.md`. **Numerical** assertion/latency deltas require running the harness twice (control vs treatment); fill the README table after those runs (FRE-177).
- [x] Unit tests: `tests/personal_agent/memory/test_proactive.py`, context integration `tests/personal_agent/request_gateway/test_context.py` (empty / failure / budget paths).

**Follow-up:** Publish `MemoryAccessedEvent` from `suggest_relevant()` when `freshness_enabled` (ADR-0042 checklist — still deferred in code).
