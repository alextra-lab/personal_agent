# EVAL-08: Slice 3 Priority Ranking

**Date:** 2026-03-28
**Author:** Alex (project lead) + Claude Code (synthesis)
**Linear Issue:** FRE-153
**Status:** Complete
**Depends on:** EVAL-06 (ADR-0035 Seshat backend), EVAL-07 (findings synthesis)
**Blocks:** Slice 3 spec writing

---

## Purpose

Rank the 8 Slice 3 candidates from Redesign v2 spec Section 8.3 by value, using evaluation data as evidence. Not everything proposed in the spec is worth building — the evaluation tells us what matters.

**Decision framework:** Each candidate is ranked by:
- **Priority:** Must-have / Should-have / Nice-to-have / Defer
- **Evidence:** Which evaluation findings support this ranking
- **Effort:** S (≤1 week) / M (1–2 weeks) / L (3+ weeks)
- **Dependencies:** What must be done first

---

## Priority Ranking

### Rank 1 — MUST-HAVE: Expansion Controller

**Candidate:** Deterministic workflow enforcement — when the gateway says HYBRID/DECOMPOSE, code enforces sub-agent dispatch.

| Field | Value |
|-------|-------|
| Priority | **Must-have** |
| Effort | **M** |
| Dependencies | None (standalone; per-phase budgets should be co-implemented) |
| ADR | ADR-0036 (accepted, design complete) |
| Linear | FRE-154 |

**Evidence:**

This is the **single largest architectural issue** found during evaluation. The data is unambiguous:

- **Strategy mismatch rate:** 25% (baseline) → 75% (run-03) → 50% (run-04). The gateway correctly identifies HYBRID/DECOMPOSE, but the agent silently bypasses expansion in 25–75% of cases. *(EVAL-07 §2)*
- **Cross-run non-determinism:** Same prompts, same model, same gateway output produce different expansion behavior across runs. CP-09, CP-10, CP-16, CP-17 flip between pass and fail randomly. *(Orchestration analysis §2)*
- **Quality impact when bypassed:** CP-16 produces a 29s shallow answer instead of a 162s comprehensive multi-agent analysis. *(EVAL-07 §2)*
- **Timeout failure when bypassed:** CP-17 hits 187s timeout when the agent attempts a single long-pass answer instead of expanding. *(Orchestration analysis §2)*
- **External validation:** GPT-5.4 second opinion independently identifies this as the core issue: *"The system currently mixes deterministic routing decisions with non-deterministic execution decisions in a way that leaves a control gap."* *(Second opinion response §3)*
- **Industry alignment:** Anthropic, LangGraph, and Google ADK all recommend deterministic code for workflow decisions, reserving LLM autonomy for content generation. *(ADR-0036 §Industry Alignment)*

**Target architecture shift:**
```
Current:  Gateway decides → Model decides → Code executes
Proposed: Gateway decides → Code enforces → Model plans/synthesizes
```

**What it delivers:** Near-zero strategy mismatch rate. Observable, enforceable, testable expansion. Eliminates the entire category of "gateway says expand but nothing happens."

---

### Rank 2 — MUST-HAVE: Seshat Enhancements (Embeddings + Fuzzy Dedup)

**Candidate:** Seshat backend migration — reframed as **Enhanced Seshat** per ADR-0035 decision (Graphiti rejected, enhancements adopted).

| Field | Value |
|-------|-------|
| Priority | **Must-have** |
| Effort | **M** |
| Dependencies | None (standalone; prerequisite for proactive memory) |
| ADR | ADR-0035 (accepted) |
| Linear | To be created |

**Evidence:**

ADR-0035 decided Enhanced Seshat over Graphiti adoption. The two must-have enhancements close the quality gap that Graphiti demonstrated:

**P0 — Embedding vectors + hybrid search:**
- Seshat scored 0% episodic retrieval precision vs Graphiti's 70%. The 0% was partly a measurement artifact (keyword matching), but the underlying gap is real: Seshat has no semantic search. *(EVAL-02, EVAL-07 §6)*
- Cross-session recall — the real test of the memory system — is **untested and depends on semantic search working**. Session-scoped recall (100%) uses conversation history, not Neo4j. *(EVAL-03 Finding 3, EVAL-07 §4)*
- Proactive memory (rank 5) is architecturally impossible without embedding-based retrieval. `suggest_relevant()` needs vector similarity, not keyword matching. *(ADR-0035 P0)*

**P1 — Fuzzy entity deduplication:**
- 40 mentions of 10 entities → 500 nodes in Seshat vs 10 canonical entities in Graphiti. The knowledge graph **degrades with use** rather than improving. *(EVAL-02 Scenario 4, EVAL-07 §6)*
- ADR-0035 lists this as P1: vector similarity + LLM merge for entity deduplication. *(ADR-0035 enhancement table)*

**Why this is must-have, not should-have:** Without embeddings, the entire "Intelligence" theme of Slice 3 collapses. Proactive memory, cross-session recall, and knowledge graph quality all depend on these two capabilities. The Graphiti experiment proved that semantic search and dedup are non-negotiable for a functional memory system.

**Note on Graphiti migration:** ADR-0035 explicitly rejected full Graphiti adoption due to 1,000× ingestion latency (8–10s per episode vs Seshat's 1–7ms). The quality advantages are achievable as Seshat enhancements without the framework overhead. This candidate is therefore **not a migration** but an enhancement project.

---

### Rank 3 — SHOULD-HAVE: Per-Phase Time Budgets

**Candidate:** Planner/worker/synthesizer time budgets for expansion phases.

| Field | Value |
|-------|-------|
| Priority | **Should-have** |
| Effort | **S** |
| Dependencies | Expansion controller (rank 1) — co-implement |
| Linear | To be created |

**Evidence:**

- **CP-17 timeout:** The agent attempts a single long-pass answer on a DECOMPOSE request and hits the 180s LLM timeout at 187s. No graceful degradation. *(Orchestration analysis §2, EVAL-07 §7)*
- **Planner monopolization:** Without phase budgets, the planner phase can consume the entire wall-clock budget, leaving no time for workers or synthesis. *(Second opinion remediation §2)*
- **Recommended budgets:** Planner 5–12s, workers 60–90s, synthesis 20–40s. *(Second opinion remediation §4)*
- **Coupled with expansion controller:** Phase budgets are a natural component of the expansion controller design. ADR-0036 already references them. Implementing one without the other leaves the timeout problem unsolved.

**Why should-have, not must-have:** The expansion controller alone eliminates the "no expansion at all" failure. Per-phase budgets address a finer-grained problem: ensuring expansion phases don't starve each other. This is important but the controller is the load-bearing fix.

---

### Rank 4 — SHOULD-HAVE: Recall Controller

**Candidate:** Session fact lookup — two-stage classification with ambiguity booster for implicit backward-reference questions.

| Field | Value |
|-------|-------|
| Priority | **Should-have** |
| Effort | **S** |
| Dependencies | None |
| ADR | FRE-155 (proposed) |
| Linear | FRE-155 |

**Evidence:**

- **Only never-passing failure:** CP-19 has failed in ALL 4 runs — baseline, run-02, run-03, run-04. No other critical path has a 0% pass rate. *(Orchestration analysis §2, EVAL-07 §1)*
- **CP-28 same root cause:** Also fails on implicit recall ("Given everything we've discussed..."). *(EVAL-04 Finding 2)*
- **Affects ~8% of queries:** Any backward-reference that doesn't use explicit recall phrasing ("do you remember", "what did I say") is misclassified. *(EVAL-07 §1)*
- **Well-scoped solution:** Add ambiguity booster patterns ("again", "going back", "earlier", "what was our") + small session-fact peek when booster fires. *(Second opinion recommendation E, EVAL-07 §1)*
- **Classification vs quality impact:** Even misclassified, the agent may still answer correctly from conversation history at short session lengths. The classification gap affects routing, telemetry accuracy, and recall scaffolding more than end-user experience for short conversations. Impact grows with session length. *(EVAL-04 Finding 2)*

**Why should-have, not must-have:** The intent classification is correct ~92% of the time. The recall gap affects a specific pattern (implicit backward-reference) that the agent can sometimes compensate for via conversation history. Important to fix, but not architecturally critical like expansion enforcement.

---

### Rank 5 — SHOULD-HAVE: Proactive Memory

**Candidate:** Seshat injects relevant context unprompted via `suggest_relevant()` during context assembly.

| Field | Value |
|-------|-------|
| Priority | **Should-have** |
| Effort | **L** |
| Dependencies | Seshat embeddings (rank 2), stability threshold redesign |
| Linear | To be created |

**Evidence:**

- **Core Slice 3 feature:** The spec's theme is "The agent gets smarter about itself and its world." Proactive memory is the primary mechanism. *(Spec §8.3)*
- **990-entity semantic graph available:** After EVAL-03 wired the promotion pipeline, the graph has a real corpus to work with. *(EVAL-03 Finding 5)*
- **Cross-session recall untested:** The 100% session recall rate relies on conversation history, not Neo4j. The real test — can the agent recall facts from prior sessions via semantic memory? — has never been run. *(EVAL-03 Finding 3, EVAL-07 Surprise 5)*
- **Stability threshold blocks organic use:** The formula requires 50 mentions or 90 days for high stability scores. In a research project with ~456 captures over 5 days, no entity reaches organic promotion. *(EVAL-03 Finding 1)*
- **Proactive memory spec question unanswered:** "Does proactive memory improve conversations or add noise?" This can only be answered by building and testing it. *(Spec §8.3 "What you learn")*

**Why should-have, not must-have:** Proactive memory is the aspirational feature, but it has significant prerequisites (embeddings, threshold redesign, cross-session validation) and an open research question (does it help or add noise?). It should be attempted in Slice 3 but the must-haves (expansion controller, Seshat enhancements) deliver more certain value.

**Implementation sequence:**
1. Seshat embeddings (rank 2)
2. Redesign stability threshold (recency boost or relative top-N)
3. Validate cross-session recall
4. Implement `suggest_relevant()` with relevance scoring
5. A/B test: conversations with vs without proactive injection

---

### Rank 6 — NICE-TO-HAVE: Procedural Memory

**Candidate:** Store tool patterns, delegation templates, and learned operational knowledge.

| Field | Value |
|-------|-------|
| Priority | **Nice-to-have** |
| Effort | **M** |
| Dependencies | More delegation data needed; proactive memory (rank 5) for retrieval |
| Linear | To be created |

**Evidence:**

- **Delegation context gaps are real:** 67% of delegations had partial context. Missing file placement guidance (2/3), missing ES index names (2/3), underused `known_pitfalls` field (2/3). *(EVAL-05, EVAL-07 §5)*
- **Evaluation delegation pattern works well:** "Run script → extract data → write report" delegations succeed in 1 round with minimal iteration. This pattern should be templated. *(DEL-002, EVAL-05)*
- **Only 3 delegations to learn from:** The dataset is too small to build reliable patterns. Delegation templates derived from 3 examples would be overfitted. *(EVAL-05)*
- **Quick wins exist without full procedural memory:** Adding default file placement convention to `compose_delegation_package()` and auto-populating `known_pitfalls` are code-level fixes, not a memory system feature. *(EVAL-05 recommendations)*

**Why nice-to-have:** The immediate delegation context gaps can be fixed with simple code changes to `delegation.py`. Full procedural memory (data model, storage, retrieval, auto-learning) is architecturally interesting but premature with 3 delegation examples. Build the delegation pipeline first (expansion controller + more delegations), then extract patterns.

---

### Rank 7 — NICE-TO-HAVE: Self-Improvement Loop

**Candidate:** Captain's Log → proposals → promotion pipeline — closed loop where the agent implements its own approved improvements.

| Field | Value |
|-------|-------|
| Priority | **Nice-to-have** |
| Effort | **L** |
| Dependencies | Expansion controller (rank 1) for reliable delegation, procedural memory (rank 6) for learning |
| Linear | To be created |

**Evidence:**

- **Insights engine built but not evaluated:** The Slice 2 insights engine exists (`insights/engine.py`) and detects delegation patterns, cost anomalies, and strategy effectiveness. However, no evaluation task specifically assessed proposal quality or actionability. *(EVAL-07 source index — no dedicated insights evaluation)*
- **Captain's Log captures data:** The capture pipeline works and feeds proposals. But the quality of those proposals — are they useful? actionable? correct? — is unknown. *(Spec §8.3 question: "What's the ROI of the self-improvement loop?")*
- **Closing the loop requires reliable delegation:** The self-improvement loop's output is a delegation to Claude Code to implement an approved proposal. Without the expansion controller (rank 1) ensuring reliable orchestration, this delegation chain is fragile. *(Spec §8.3 acceptance criteria)*
- **High ambition, insufficient evidence:** This is the most speculative Slice 3 feature. The spec asks "Can the agent reliably implement its own improvements?" — there's no evaluation data to suggest it can. *(Spec §8.3 "What you learn")*

**Why nice-to-have:** The self-improvement loop is the project's long-term vision, but it depends on reliable expansion (rank 1), proactive memory (rank 5), and procedural memory (rank 6). Building it before those foundations are solid would produce an unreliable pipeline. Better to validate the building blocks first and attempt the loop in late Slice 3 or Slice 4.

---

### Rank 8 — DEFER: Decomposition Learning

**Candidate:** Tune SINGLE/HYBRID thresholds from data — learn which requests benefit from expansion.

| Field | Value |
|-------|-------|
| Priority | **Defer** (Slice 4 or never) |
| Effort | **M** |
| Dependencies | Expansion controller (rank 1) must produce reliable data first |
| Linear | To be created (if ever needed) |

**Evidence:**

- **Classification is not the problem:** Intent classification accuracy is ~92%. No cases were observed where HYBRID/DECOMPOSE classification was clearly wrong. The gateway's decomposition assessment is sound. *(EVAL-07 §2: "No cases observed where HYBRID classification was clearly wrong")*
- **Execution fidelity is the problem:** The expansion controller (rank 1) fixes the actual failure mode — the agent ignoring the gateway's correct classification. *(EVAL-07 §2, Orchestration analysis §2)*
- **No reliable training data yet:** Current telemetry cannot distinguish "expansion was correct and executed well" from "expansion was correct but agent skipped it." The expansion controller must produce clean execution data before threshold tuning is meaningful. *(EVAL-07 §7)*
- **Premature optimization:** With 25 critical paths across 4 runs, the dataset is too small for meaningful threshold learning. The current heuristic (complexity scoring + intent signals) produces correct strategy selection. Tuning requires hundreds of labeled examples with quality ratings. *(EVAL-07 §2)*
- **SearXNG confound unresolved:** Run-04 introduced SearXNG alongside other fixes. The agent may prefer search-and-answer over expansion when web search is available. This confound must be isolated before learning thresholds from expansion data. *(EVAL-07 §8)*

**Why defer:** Fix execution first (expansion controller), generate clean data, then evaluate whether thresholds need tuning. The current thresholds are correct — it's the enforcement that's broken. Threshold learning without reliable execution data would be training on noise.

---

## Summary Table

| Rank | Candidate | Priority | Effort | Key Evidence |
|------|-----------|----------|--------|--------------|
| 1 | **Expansion Controller** | Must-have | M | 25–75% strategy mismatch, cross-run non-determinism, second opinion validation |
| 2 | **Seshat Enhancements** (embeddings + dedup) | Must-have | M | 0% vs 70% retrieval, 500-entity dedup explosion, prerequisite for proactive memory |
| 3 | **Per-Phase Time Budgets** | Should-have | S | CP-17 timeout at 187s, planner monopolization, coupled with expansion controller |
| 4 | **Recall Controller** | Should-have | S | CP-19 0% pass rate across all runs, ~8% of queries affected |
| 5 | **Proactive Memory** | Should-have | L | Core Slice 3 feature, 990-entity corpus, cross-session recall untested |
| 6 | **Procedural Memory** | Nice-to-have | M | Only 3 delegation examples, quick wins possible without full system |
| 7 | **Self-Improvement Loop** | Nice-to-have | L | No evaluation of proposal quality, depends on ranks 1, 5, 6 |
| 8 | **Decomposition Learning** | Defer | M | Classification correct; execution broken; fix enforcement first |

---

## Slice 3 Scope vs Slice 4

### Slice 3 Scope (commit to deliver)

| # | Deliverable | Effort | Cumulative |
|---|-------------|--------|------------|
| 1 | Expansion controller + per-phase budgets | M+S | ~2 weeks |
| 2 | Seshat embeddings + hybrid search | M | ~2 weeks |
| 3 | Seshat fuzzy entity deduplication | M | ~1.5 weeks |
| 4 | Recall controller | S | ~1 week |

**Total committed scope: ~6.5 weeks** (spec estimated 4–5 weeks — scope is larger than originally planned due to the Seshat enhancements being more clearly defined post-evaluation).

### Slice 3 Stretch Goals (attempt if time permits)

| # | Deliverable | Effort | Prerequisite |
|---|-------------|--------|--------------|
| 5 | Proactive memory (`suggest_relevant()`) | L | Seshat embeddings |
| 6 | Stability threshold redesign | S | Seshat embeddings |
| 7 | Cross-session recall validation | S | Seshat embeddings |

### Slice 4 or Never

| # | Deliverable | Reason for deferral |
|---|-------------|-------------------|
| 8 | Decomposition learning | Classification correct; fix execution first |
| 9 | Full procedural memory system | Insufficient data; quick wins don't need it |
| 10 | Self-improvement loop | Insufficient evidence of proposal quality |
| 11 | Graphiti migration | Rejected in ADR-0035 |

---

## Dependency Graph

```
                    ┌──────────────────┐
                    │  1. Expansion    │
                    │  Controller      │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  3. Per-Phase    │
                    │  Time Budgets    │
                    └──────────────────┘


                    ┌──────────────────┐
                    │  2. Seshat       │
                    │  Embeddings      │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  2b. Fuzzy       │
                    │  Entity Dedup    │
                    └────────┬─────────┘
                             │
              ┌──────────────▼──────────────┐
              │  5. Proactive Memory        │
              │  (+ threshold redesign)     │
              │  (+ cross-session validate) │
              └──────────────┬──────────────┘
                             │
                    ┌────────▼─────────┐
                    │  7. Self-Improve │
                    │  Loop            │
                    └──────────────────┘


                    ┌──────────────────┐
                    │  4. Recall       │
                    │  Controller      │  (independent)
                    └──────────────────┘
```

**Two independent tracks:**
- **Track A (Orchestration):** Expansion controller → per-phase budgets
- **Track B (Memory):** Seshat embeddings → fuzzy dedup → proactive memory

Recall controller is independent and can be parallelized with either track.

---

## Acceptance Criteria Check

- [x] All 8 candidates ranked with evidence
- [x] Rankings traceable to specific evaluation findings (EVAL-01 through EVAL-07, orchestration analysis, second opinion)
- [x] Clear "Slice 3 scope" vs "Slice 4 or never" boundary
- [x] Ready to drive Slice 3 spec writing

---

*This document is the final Evaluation Phase deliverable. It converts 3 weeks of research findings into actionable priorities for Slice 3 spec writing.*
