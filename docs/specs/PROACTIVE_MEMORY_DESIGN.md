# Proactive Memory — Design Specification

> **Status:** Proposed  
> **Version:** 0.1  
> **Date:** 2026-03-30  
> **Author:** Project owner  
> **Related ADR:** `docs/architecture_decisions/ADR-0039-proactive-memory.md`  
> **Related specs:** `docs/specs/CONTEXT_INTELLIGENCE_SPEC.md` (Phase 4), `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` (memory)  
> **Code touchpoints:** `src/personal_agent/memory/protocol.py`, `src/personal_agent/memory/protocol_adapter.py`, `src/personal_agent/request_gateway/context.py`

---

## Purpose

Evolve long-term memory (Seshat / Neo4j) from **passive** to **proactive**: inject **relevant cross-session context** into the primary agent’s assembled prompt **without** requiring the user to ask “what did we discuss about X?” or relying on brittle surface cues (e.g. capitalized words).

**Why this matters:** Today, memory enrichment for most intents either skips entirely (no capitalized entity-like tokens) or only fires on explicit memory-recall paths. That misses continuity the project owner expects from a personal agent with a graph memory.

---

## Current Behavior (Baseline)

- **`MemoryProtocol`** (`src/personal_agent/memory/protocol.py`) exposes `recall()`, `recall_broad()`, `store_episode()`, `promote()`, `is_connected()`.
- **`MemoryServiceAdapter`** (`src/personal_agent/memory/protocol_adapter.py`) implements the protocol against `MemoryService`.
- **`assemble_context()`** (`src/personal_agent/request_gateway/context.py`) loads session history, then calls **`_query_memory_for_intent()`**, then injects recall-controller output and the current user message.
- **`_query_memory_for_intent()`** today:
  - For **`MEMORY_RECALL`**: uses `recall_broad()` and formats broad graph recall.
  - For **other intents**: extracts capitalized words longer than three characters as entity names; if none, returns **`None`** (no memory query).

This design is intentionally simple for Slice 2; proactive memory replaces the non-recall path with a **structured, scored retrieval** path.

---

## Scope

**In scope**

- New protocol method **`suggest_relevant()`** returning **scored candidate memories** (entities, episodes, optional session summaries) for **all** task types when memory is connected.
- **Multi-signal relevance scoring** (embedding similarity, entity overlap, recency, session topic coherence).
- **Noise and budget controls**: minimum score threshold, max token budget for injected memory, diminishing-returns cutoff.
- **Integration** in context assembly: proactive block **after session history**, **before** the current user message (same relative position as today’s `memory_context` usage; see Integration Point).
- **Evaluation methodology**: EVAL runs with proactive memory on vs off.
- **Observability**: structured logs and metrics for latency, counts, scores, and budget trims.

**Out of scope (initial MVP)**

- Replacing **`MEMORY_RECALL`** broad recall with `suggest_relevant()` (may remain a distinct UX-heavy path until unified).
- Automatic **writing** or **promotion** of memory based on proactive suggestions (read path only).
- New UI or product surfaces for “why this memory appeared.”
- Full **topic model** training; session “topic” may be a lightweight proxy (see Scoring).

---

## Protocol Extension: `suggest_relevant()`

### Signature (conceptual)

Add to **`MemoryProtocol`**:

```text
async def suggest_relevant(
    user_message: str,
    session_entity_ids: list[str],  # or names — align with Neo4j model
    session_topic_hint: str | None,
    current_session_id: str,
    trace_id: str,
) -> ProactiveMemorySuggestions
```

### Result type (conceptual)

A frozen dataclass or Pydantic model (implementation choice at build time), e.g.:

- **`candidates`**: ordered list of items, each with:
  - **`kind`**: `entity` | `episode` | `session_summary` (extensible)
  - **`payload`**: same shape as today’s memory context dicts where possible (reuse formatters)
  - **`relevance_score`**: float in **[0, 1]**
  - **`score_components`**: optional breakdown for debugging (embedding, overlap, recency, topic) — **must not** log PII; safe for dev traces only
- **`query_embedding_ms`**: optional timing helper for observability

**Error handling:** On backend failure, return empty suggestions and log with `trace_id` (do not fail the request). Same spirit as `recall()` failures in `_query_memory_for_intent()`.

---

## Relevance Scoring Algorithm

Combine signals into a **single ranking score** per candidate. Weights are **configuration** (e.g. under `settings` / YAML), not hard-coded magic numbers in call sites.

### 1. Embedding similarity

- Embed the **current user message** (and optionally a short rolling summary of the session) with the **same embedding model** used for (or compatible with) entity / episode vectors in Neo4j.
- Compare against stored embeddings (cosine similarity or backend-native vector index score).
- Normalize to **[0, 1]** for combination with other terms.

**Why:** Captures paraphrases and concepts that capitalized-word heuristics miss.

### 2. Entity overlap count

- Build a set of **entities mentioned in the current session** (from `session_entity_ids` / names + lightweight extraction if needed).
- For each candidate memory, count **overlap** with its linked entities.
- Map overlap to a sub-score in **[0, 1]** (e.g. saturating function so that 3+ overlaps hit ceiling).

**Why:** Grounds vector similarity in **explicit graph links** the project owner already curated through conversation.

### 3. Recency weighting

- Prefer memories from **more recent sessions** (exponential or piecewise decay by session end time or last mention time).
- Combine as a multiplicative factor or additive term on the normalized scale (implementation detail; document chosen formula in code comments + config).

**Why:** Recent context is usually more salient; old edges should not dominate unless similarity is very high.

### 4. Session-level topic coherence

- **Topic proxy** for the current session: e.g. keywords from recent user turns, existing session summary node, or embedding centroid of last *k* user messages.
- Compare to a **topic proxy** for the candidate’s source session (stored summary, dominant entities, or session embedding).
- Produce a coherence sub-score in **[0, 1]**.

**Why:** Reduces pulling in **semantically similar but task-unrelated** memories from unrelated past sessions.

### Final combination

- **Final score** = configurable weighted blend or learned-style formula, e.g.  
  `w_emb * sim + w_ent * overlap + w_rec * recency + w_top * topic` with weights summing to 1, then optional nonlinearity (e.g. squashing).
- **Minimum threshold:** discard candidates with **final score < 0.3** (default; tunable).
- **Diminishing returns:** after selecting top items, stop when **marginal information gain** falls below epsilon (e.g. score drop > 0.15 vs previous item, or N items cap).

---

## Noise Control and Token Budget

| Control | Default (proposal) | Rationale |
|--------|-------------------|-----------|
| Minimum relevance threshold | **0.3** | Cuts low-confidence graph hits that distract the model |
| Maximum proactive memory budget | **500 tokens** (estimated) | Bounded cost to context window and attention |
| Max candidate count | **10** (before budget trim) | Hard ceiling before formatting |
| Diminishing returns | Stop after **5** injected items or when next item < **0.35** score | Avoid long tail of weak matches |

**Formatting:** Reuse or extend existing memory context formatting so downstream consumers (orchestrator, trimming) stay consistent.

**Interaction with trimming:** If global context trimming evicts proactive memory, telemetry must show whether **proactive** or **session** content was dropped (see Observability).

---

## Integration Point

**Location:** `assemble_context()` in `src/personal_agent/request_gateway/context.py`.

**Order (unchanged intent, refined behavior):**

1. Append **`session_messages`** (session history).
2. Call **`_query_memory_for_intent()`** when `memory_adapter` is present.
3. Apply recall-controller system section (if any).
4. Append **current user message**.

**Required behavioral change:** `_query_memory_for_intent()` should drive **proactive** retrieval for **all intents** (including non-`MEMORY_RECALL`), not only capitalized-word `recall()`.

**Recommended composition:**

- **`MEMORY_RECALL`:** Keep **`recall_broad()`** for explicit “what’s in my memory” questions **or** merge into a unified ranked list (decision in implementation; spec allows either if UX and tests agree).
- **All other intents:** Call **`suggest_relevant()`** with session entity context + user message; merge results into `memory_context` when scores pass threshold and budget.

**Session entity context:** Assembled from the same sources the graph already uses (entity nodes linked to session, recent turns). Exact field list is an implementation detail but **must** be documented on the dataclass passed into `suggest_relevant()`.

---

## Observability

- **Structured events** (examples): `proactive_memory_suggest_start`, `proactive_memory_suggest_complete`, `proactive_memory_suggest_empty`, `proactive_memory_budget_trimmed`.
- **Fields:** `trace_id`, `candidate_count`, `injected_count`, `latency_ms`, `embedding_latency_ms`, `threshold`, `token_estimate`, `task_type` (intent).
- **Never** log raw user message content at INFO in production if policy forbids; prefer hashes or lengths unless redaction is guaranteed.

---

## A/B Testing Methodology

**Goal:** Measure whether proactive memory improves **factual continuity** and **task success** without harming **noise** or **latency**.

**Design**

1. **Flag:** `proactive_memory_enabled` (or equivalent) in unified config.
2. **Runs:** Execute the same **EVAL paths** twice — **control** (flag off) vs **treatment** (flag on). Same model stack and prompts otherwise.
3. **Primary metrics:**
   - **Assertion pass rate** (or path-level success) — same as existing EVAL scoring.
   - **Memory-related rubric** scores if present (e.g. Memory Quality category).
4. **Secondary metrics:**
   - p95 **turn latency** delta (embedding + Neo4j).
   - **Injected token count** per turn (distribution).
   - Manual or LLM-judge **relevance** sample on a fixed subset of turns (optional).
5. **Stopping rule:** Adopt if primary metrics improve or hold with **acceptable** latency regression (project-owner-defined threshold); roll back if false positives dominate qualitative review.

---

## Open Questions

1. **Cost of embedding calls per turn:** One embedding per user message is likely; whether to **cache** embeddings for near-duplicate messages in the same session.
2. **False positive rate:** How often does proactive memory inject **plausible but wrong** associations? Needs labeled spot-checks or EVAL extensions.
3. **Interaction with context compression (ADR-0038):** If older turns are compressed, **session topic** and **entity lists** must be fed from **compressed summaries + working state** so proactive retrieval still sees stable signals.
4. **Unified ranking vs dual path:** Should `MEMORY_RECALL` eventually use the same scorer as proactive memory for one coherent behavior?
5. **Cold start:** Empty or sparse graph — define graceful no-op and avoid useless embedding calls when `is_connected()` is true but index is empty.

---

## MVP Exit Criteria (spec level)

- [ ] `suggest_relevant()` documented on `MemoryProtocol` and implemented in `MemoryServiceAdapter`.
- [ ] Scoring uses at least **embedding similarity + entity overlap + recency**; topic coherence **stubbed or simplified** is acceptable for MVP if documented.
- [ ] Threshold **0.3** and budget **500 tokens** enforced with tests.
- [ ] `_query_memory_for_intent()` invokes proactive path for non-recall intents.
- [ ] EVAL plan executed with flag on/off and results recorded under `telemetry/evaluation/`.

---

## References

- `docs/architecture_decisions/ADR-0035-seshat-backend-decision.md` — Neo4j / Seshat direction  
- `docs/architecture_decisions/ADR-0039-proactive-memory.md` — decision record for this feature  
- `docs/architecture_decisions/ADR-0038-context-compressor-model.md` — compression interaction  
