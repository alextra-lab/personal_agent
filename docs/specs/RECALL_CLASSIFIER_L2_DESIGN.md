# Recall Classifier — Layer 2 (Implicit Reference) Design

> **Status:** Proposed  
> **Date:** 2026-03-30  
> **Author:** Project owner  
> **Related:** `src/personal_agent/request_gateway/recall_controller.py`, ADR-0037 (recall controller), Context Intelligence recall path

---

## Purpose

The recall controller uses a three-gate pipeline: (1) task type, (2) regex cue patterns, (3) noun-phrase extraction plus session scan. Layer 1 (regex) reliably catches explicit backward-reference phrasing (“what was our primary database again?”, “going back to earlier”, “remind me what”, …) but misses:

- **Implicit references** — e.g. “Can we refine it?”, “Let’s go with the second option”
- **Unresolved anaphora** — “it”, “that”, “the one we picked” without a local antecedent
- **Continuation intent** — e.g. “What about the performance implications?” when the topic is only clear from prior turns

This spec defines **Layer 2**: a classifier that runs **after** the regex gate fails and **before** the session fact gate, using local embedding similarity and lightweight **semantic completeness** heuristics to flag messages that likely depend on recent conversation context. When Layer 2 fires, processing continues into the same session fact machinery as Layer 1 (noun phrases + history scan), avoiding a parallel recall path.

**Non-goals:** Replace Layer 1 regex; add a second LLM classification pass; handle cross-session recall (in-session recent turns only for MVP).

---

## Architecture

### Placement in the pipeline

| Stage | Name | Behavior |
|-------|------|------------|
| Gate 1 | Task type | Only `CONVERSATIONAL` enters recall refinement (unchanged) |
| Layer 1 | Cue pattern (regex) | `_RECALL_CUE_PATTERNS` match → proceed to session fact gate |
| **Layer 2** | **Implicit reference** | If Layer 1 **does not** match → optionally run; if positive → proceed to session fact gate |
| Gate 3 | Session fact | `_extract_noun_phrases` + `_scan_session_facts` (unchanged contract) |

Layer 2 is a **branch**, not a replacement: if Layer 1 matches, Layer 2 is skipped. If Layer 1 misses, Layer 2 may set an internal “synthetic cue” or boolean so Gate 3 still runs with a consistent `RecallResult` / telemetry shape (implementation detail; see Integration).

### Data flow (conceptual)

```text
CONVERSATIONAL + no regex match
        │
        ▼
  Intent allowed for L2?
        │ no ──► return None (no recall refinement)
        │ yes
        ▼
  Embed current message + last N user/assistant turns
        │
        ▼
  max cosine sim to recent turns > T_emb?
        AND / OR
  completeness score < T_comp?
        │
        │ no ──► return None
        │ yes
        ▼
  Same path as regex hit → noun phrases + session scan
```

**Infrastructure:** The project uses a **local** embedding model and reranker (small Qwen-class models served on the same host as the agent). Layer 2 uses **embeddings only** for MVP; reranker is optional for future ranking of candidate turns, not required for this design.

---

## Layer 2 Signals

### 1. Embedding similarity

- **Inputs:** Embedding vector for the current user message; embedding vectors for text of the **last 5–10 conversational turns** (configurable window; user and assistant content as stored in session history).
- **Metric:** Cosine similarity between the current message and each turn in the window.
- **Rule:** If **any** similarity exceeds `embedding_similarity_threshold`, treat as **potential reference** to prior discourse (subject matter continuity).
- **Rationale:** Implicit follow-ups often sit in the same semantic neighborhood as the immediately preceding discussion even when regex finds no cue.

### 2. Semantic completeness scoring

A **lightweight, non-LLM** score estimating whether the message is likely **self-contained** vs **dependent** on context.

**Factors (examples; implement as weighted checklist or small linear model):**

| Signal | Direction |
|--------|-----------|
| Pronouns / demonstratives without clear local antecedent (`it`, `that`, `those`, `this`, `the second one`, …) | Lower completeness (more likely implicit reference) |
| Very short utterance with **no** clear subject or object | Lower completeness |
| Continuation markers (`also`, `and`, `what about`, `how about`, `same for`, …) at start or as dominant structure | Lower completeness |
| Explicit named entities, full noun phrases, or question that stands alone | Higher completeness |

**Rule:** If completeness score **falls below** `completeness_score_threshold`, treat as **potential reference** (in combination with intent filter; see below).

**Combination policy (recommended MVP):** Require **both** high similarity to at least one recent turn **and** low completeness, **or** define a single composite score with documented weights. The project owner should pick one rule in implementation and tune via telemetry; default recommendation: **(max_sim > T_emb) AND (completeness < T_comp)** to limit false positives when the user starts a new topic that happens to be semantically adjacent.

---

## Intent-Aware Filtering

Layer 2 MUST NOT run for every `CONVERSATIONAL` message. Gate on a **refined intent** (or equivalent `signals` from Stage 4) so self-contained tasks rarely pay latency or false-positive risk.

**Allow Layer 2 when intent is one of:**

- `troubleshooting` — often refers to an ongoing problem statement
- `refinement` — iterating on a prior answer or design
- `continuation` — extending or branching the current thread

**Skip Layer 2 when intent is one of:**

- `general_knowledge` — expected to be self-contained
- `analysis` — often greenfield reasoning from stated premises
- `code_generation` — usually specified in-message or via attached context

**Note:** Today’s `IntentResult` exposes `task_type`, `complexity`, `confidence`, and `signals`. Implementations may map Stage 4 outputs into the above labels via `signals` or a future field; this spec treats the **label set** as the contract for Layer 2 gating. If intent is unknown, default should be **conservative** (skip Layer 2 or require stricter thresholds — configurable).

---

## Token Cost and Latency

| Item | Estimate |
|------|----------|
| Single embedding (local ~0.6B class model) | ~50 ms order-of-magnitude |
| Comparisons | Up to **10** dot/cosine ops against cached recent-turn vectors |
| Increment when Layer 1 misses and intent allows L2 | ~**100 ms** per turn (acceptable for interactive use) |

No remote LLM tokens for Layer 2 in MVP. Embedding batching (current + N turns in one request) may reduce wall-clock time; document actual behavior in implementation notes.

---

## Integration with `recall_controller.py`

### New helper

- **`_check_implicit_reference(message, recent_turns, intent) -> bool`**
  - **`message`:** Current user text.
  - **`recent_turns`:** Bounded slice of session messages (same shape as existing session entries: e.g. role + content).
  - **`intent`:** Refined intent label or structure derived from `IntentResult` (see Intent-Aware Filtering).

### Call site

- After Gate 1 (`CONVERSATIONAL`), when `_RECALL_CUE_PATTERNS.search()` (or `_detect_recall_cues`) returns **None**:
  - If intent allows Layer 2 and `_check_implicit_reference(...)` is **True**, continue as if a cue matched: run **`_extract_noun_phrases`** then **`_scan_session_facts`**.
  - If Layer 2 is **False**, return **`None`** (unchanged pass-through).

### Telemetry and cue field

- Emit **`recall_l2_triggered`** when Layer 2 runs (after intent allow-list passes).
- Emit **`recall_l2_match`** when Layer 2 returns True and Gate 3 proceeds.
- Emit **`recall_l2_false_positive`** when Layer 2 returns True but Gate 3 finds no candidates or reclassification does not occur (align with existing `recall_cue_false_positive` semantics).

Preserve existing **`recall_cue_detected`** for regex matches only; Layer 2 should use distinct events so dashboards can separate regex vs implicit paths.

---

## Configuration

All thresholds and windows MUST be read from **`personal_agent.config.settings`** (not environment variables directly), with sensible defaults:

| Setting | Default | Purpose |
|---------|---------|---------|
| `recall_l2_embedding_similarity_threshold` | `0.7` | Min cosine similarity to any turn in window |
| `recall_l2_completeness_score_threshold` | `0.4` | Scores below this indicate “incomplete” / context-dependent (scale 0–1 per implementation doc) |
| `recall_l2_max_turn_embeddings` | `10` | Cap on recent turns to embed/compare |
| `recall_l2_enabled` | `true` | Kill switch |

Naming in code may use a nested settings group; defaults above are the **semantic** contract.

---

## Telemetry (Structured Logging)

| Event | When |
|-------|------|
| `recall_l2_triggered` | Layer 2 evaluation started (intent allowed) |
| `recall_l2_match` | Layer 2 positive → Gate 3 entered |
| `recall_l2_false_positive` | Layer 2 positive but no useful session outcome |

Include **`trace_id`**, and non-PII excerpts (e.g. message length, max similarity, completeness score) for tuning. Never log full message content if policy forbids it; use hashed or truncated excerpts consistent with existing recall logs.

---

## Scope

**In scope**

- Layer 2 placement between regex and session fact gate
- Embedding similarity over recent in-session turns
- Heuristic completeness scoring
- Intent allow/deny list for Layer 2
- Settings-backed thresholds and telemetry events
- Integration via `_check_implicit_reference` as specified

**Out of scope (MVP)**

- Cross-session memory or long-horizon retrieval
- Mandatory reranker usage
- Replacing noun-phrase extraction with dense retrieval only
- Training or fine-tuning embedding models

---

## Tradeoffs

| Benefit | Cost |
|---------|------|
| Catches implicit recall that regex misses | Extra latency (~100 ms) when Layer 1 misses and intent allows L2 |
| Local embeddings avoid LLM dollar cost | Operational dependency on local embedding service availability |
| Intent gating reduces false positives | Requires accurate Stage 4 labels or mappings; misclassification can skip or over-trigger L2 |
| Same Gate 3 as Layer 1 | Gate 3 still depends on noun phrases; implicit “it” may yield sparse phrases — may need follow-up heuristics |

---

## Acceptance Criteria

1. **Behavior:** For `CONVERSATIONAL` messages with **no** regex cue, Layer 2 runs only when intent is in the allow-list; on positive L2, Gate 3 runs and behavior matches regex-triggered path for success/failure outcomes.
2. **Config:** Thresholds and window size are configurable via `settings` with documented defaults (`0.7` / `0.4`).
3. **Observability:** `recall_l2_triggered`, `recall_l2_match`, and `recall_l2_false_positive` appear in structured logs with `trace_id`.
4. **Tests:** Unit tests cover (a) L2 skipped when regex matches, (b) L2 skipped when intent denied, (c) synthetic high-similarity + low-completeness message triggers Gate 3 invocation, (d) kill switch disables L2.
5. **Docs:** This spec and implementation cross-reference ADR-0037; if behavior materially changes recall guarantees, add or update an ADR.

---

## Open Questions

1. **Optimal `embedding_similarity_threshold`:** Is `0.7` right for the chosen model and conversation mix? Should it vary by conversation length or intent?
2. **Context compression summaries:** If recent turns are compressed into summaries, do embeddings represent raw turns, summaries, or both? Risk of similarity drift or stale anchors.
3. **Caching:** Should turn embeddings be **cached per session** (keyed by session id + turn index + content hash) to avoid re-embedding on every message?
4. **Combination rule:** Strict AND vs OR vs weighted composite — which minimizes false positives on eval sets (e.g. recall / continuity eval scenarios)?
5. **Intent source of truth:** Single enum on `IntentResult` vs derived solely from `signals` — which minimizes gateway churn?
6. **Assistant vs user turns:** Should both roles be embedded equally, or weight user turns higher for “what user is referring to”?

---

## References

- Recall controller module: `src/personal_agent/request_gateway/recall_controller.py`
- ADR-0037 (recall controller patterns and telemetry expectations)
- Context Intelligence spec (`docs/specs/CONTEXT_INTELLIGENCE_SPEC.md`) — recall / context management trajectory
