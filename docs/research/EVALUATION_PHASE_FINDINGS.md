# Evaluation Phase Findings

**Date:** 2026-03-28
**Author:** Alex (project lead) + Claude Code (synthesis)
**Linear Issue:** FRE-152 (EVAL-07)
**Status:** Complete
**Depends on:** EVAL-01 through EVAL-06, orchestration analysis (Run-01 through Run-04)

---

## Purpose

This document synthesizes all evaluation data gathered during the Evaluation Phase (Week 1–3) of the Cognitive Architecture Redesign v2. It covers orchestration correctness, memory quality, context management, delegation effectiveness, and the Graphiti backend experiment. The findings here drive Slice 3 priority decisions (EVAL-08 / FRE-153).

**Source data index:**

| Source | Location |
|--------|----------|
| Orchestration analysis (4 runs + second opinion) | `docs/research/evaluation-orchestration-analysis.md` |
| Second opinion brief | `docs/research/evaluation-run-04-second-opinion-brief.md` |
| Second opinion response | `docs/research/evaluation-run-04-second-opinion-response.md` |
| Proposed remediation plan | `docs/research/evaluation-run-04-second-opinion-proposed-remediation.md` |
| Graphiti experiment report (EVAL-02) | `docs/research/GRAPHITI_EXPERIMENT_REPORT.md` |
| Memory promotion report (EVAL-03) | `docs/research/EVAL_03_MEMORY_PROMOTION_REPORT.md` |
| Context budget report (EVAL-04) | `docs/research/EVAL_04_CONTEXT_BUDGET_REPORT.md` |
| Delegation outcome log (EVAL-05) | `docs/research/DELEGATION_OUTCOME_LOG.md` |
| Seshat backend ADR (EVAL-06) | `docs/architecture_decisions/ADR-0035-seshat-backend-decision.md` |
| Kibana dashboards (EVAL-05) | `config/kibana/dashboards/slice2_*.ndjson` |
| Raw telemetry | `telemetry/evaluation/` |

---

## 1. Intent Classification Accuracy

**Source:** Orchestration runs (Run-01 through Run-04), EVAL-04 harness results, critical path assertions.

### Estimated Accuracy

Intent classification is handled by Stage 4 of the Pre-LLM Gateway (`request_gateway/intent.py`) using regex-based signal matching with weighted scoring across 7 intent types: `MEMORY_RECALL`, `SELF_IMPROVE`, `DELEGATION`, `PLANNING`, `ANALYSIS`, `TOOL_USE`, `CONVERSATIONAL`.

Across 25 critical paths × 4 runs (100 classifications):

| Metric | Value |
|--------|-------|
| Correct classifications (estimated) | ~92% |
| Known systematic misclassifications | 1 pattern (see below) |
| Confidence score range | 0.0–1.0 (heuristic, not calibrated) |

### Known Misclassification: `conversational` vs `memory_recall`

**Pattern:** Implicit backward-reference questions are consistently classified as `conversational` instead of `memory_recall`.

**Example:** *"Going back to the beginning — what was our primary database again?"*
- **Expected:** `memory_recall` (references earlier conversation fact)
- **Actual:** `conversational` (no explicit recall keyword match)

**Impact:** CP-19 failed across all 4 runs. CP-28 failed in EVAL-04. The regex patterns only match explicit recall phrases ("do you remember", "what did I say") but not implicit references ("going back", "what was our X again", "earlier").

**Frequency:** Affects ~8% of evaluation queries — specifically any backward-reference that doesn't use explicit recall phrasing.

### Recommendation for Slice 3

Implement a **Recall Controller** (proposed in second opinion analysis, supported by EVAL-04 findings). Two-stage approach:
1. Primary lexical/regex classifier (current Stage 4)
2. Constrained booster for ambiguous retrospective questions — detect cues like "again", "going back", "earlier", "what was our", "what did we decide"
3. Small session-fact peek when ambiguity booster fires

See: FRE-155 (Recall Controller ADR), `docs/research/evaluation-run-04-second-opinion-proposed-remediation.md` §7.

---

## 2. Decomposition Effectiveness

**Source:** Orchestration analysis (4-run trajectory), second opinion, critical path assertions CP-09/CP-10/CP-16/CP-17.

### Strategy Distribution

The Pre-LLM Gateway (Stage 5, `request_gateway/decomposition.py`) assigns one of four strategies: `SINGLE`, `HYBRID`, `DECOMPOSE`, `DELEGATE`.

| Strategy | When Triggered | Observed Behavior |
|----------|---------------|-------------------|
| `SINGLE` | Low complexity, single-domain | Consistently correct. No issues observed. |
| `HYBRID` | Multi-faceted but synthesizable | Gateway classifies correctly; **agent execution unreliable** |
| `DECOMPOSE` | Multi-step requiring parallel work | Gateway classifies correctly; **agent execution unreliable** |
| `DELEGATE` | Requires external agent | Classification correct in all test paths |

### When Does HYBRID Help?

HYBRID is correctly triggered for multi-faceted analytical requests (e.g., "Compare Redis, Memcached, and Hazelcast for our session caching" — CP-16). When sub-agents **actually execute**, results are comprehensive and well-synthesized.

**Problem:** The agent treats the gateway's expansion decision as advisory, not mandatory. Across runs:

| Critical Path | Baseline | Run-03 | Run-04 | Pattern |
|---------------|----------|--------|--------|---------|
| CP-09 (HYBRID) | ✅ | ❌ | ✅ | Intermittent |
| CP-10 (DECOMPOSE) | ❌ | ❌ | ✅ | Fixed in Run-04 |
| CP-16 (HYBRID synthesis) | ✅ | ✅ | ❌ | Intermittent |
| CP-17 (Sub-agent concurrency) | ✅ | ❌ | ❌ timeout | Regressed |

### Strategy Mismatch Rate

**Definition:** % of HYBRID/DECOMPOSE requests where the gateway signals expansion but no sub-agent trace appears.

| Run | HYBRID/DECOMPOSE Paths | Expansion Actually Executed | Mismatch Rate |
|-----|------------------------|----------------------------|---------------|
| Baseline | 4 | 3 | 25% |
| Run-03 | 4 | 1 | 75% |
| Run-04 | 4 | 2 | 50% |

**Root cause (from second opinion):** *"The system currently mixes deterministic routing decisions with non-deterministic execution decisions in a way that leaves a control gap."* The gateway decides *whether* to expand; the agent decides *how* — and can decide not to.

### When Is HYBRID Overhead?

No cases observed where HYBRID classification was clearly wrong. The issue is execution fidelity, not classification accuracy.

### Recommendation for Slice 3

Move expansion enforcement into deterministic code:
1. **Expansion controller** — when Stage 5 returns HYBRID/DECOMPOSE, code enforces sub-agent dispatch (not prompt compliance)
2. **Tool-call enforcement** — require `spawn_sub_agents` tool call rather than hoping prompt produces expansion
3. **Phase budgets** — planner (5–12s), workers (60–90s), synthesis (20–40s) to prevent planner monopolizing the full 180s timeout
4. **Strategy mismatch metric** — first-class telemetry for "gateway said expand but no expansion trace"

See: `docs/research/evaluation-run-04-second-opinion-proposed-remediation.md` §§2–5.

---

## 3. Context Budget Adequacy

**Source:** EVAL-04 (`docs/research/EVAL_04_CONTEXT_BUDGET_REPORT.md`), `telemetry/evaluation/eval-04-context-budget/results.json`.

### Key Finding: Budget Never Triggers

The Stage 7 context budget (`request_gateway/budget.py`) has a 65,536-token ceiling. In a 12-turn stress test with verbose responses, peak utilization was **2.5%**.

| Turn | Estimated Tokens | % of 65K Ceiling |
|------|-----------------|------------------|
| 1 | 31 | 0.0% |
| 7 | 1,309 | 2.0% |
| 11 (peak) | **1,645** | **2.5%** |
| 12 | 1,536 | 2.3% |

**No trimming was triggered.** `overflow_action = None` on every turn.

### Two-Layer Context Management (Architectural Gap)

Two independent systems manage context, with no coordination:

| Layer | Mechanism | Effect |
|-------|-----------|--------|
| **Executor** (`apply_context_window`) | Caps conversation history to ~11 messages | **Actually governs context size** |
| **Gateway Stage 7** (`apply_budget`) | Trims if token estimate exceeds ceiling | **Never triggers** (executor pre-caps input) |

The executor is the real governor. Stage 7 only sees session messages (~1.6K tokens for 12 turns), which never approach the 65K ceiling.

### Token Estimation Gap

The budget stage estimates tokens from `messages + memory_context + tool_definitions` in assembled context. However:
- System prompt is added by the executor, not the gateway
- Tool definitions are added by the executor, not assembled context
- Actual LLM call tokens (system + messages + tools + memory) are significantly higher than the 1,645-token budget estimate

### Trimming Priority Order (Verified Sound)

1. Drop oldest conversation history (preserves last user message + system)
2. Drop memory context (can be re-queried)
3. Drop tool definitions (last resort — breaking tools breaks the agent)

**No changes needed** to trimming priority.

### Recommendations for Slice 3

1. **Unify token accounting** — single layer that sees true LLM token footprint (system + messages + tools + memory)
2. **Document the two-layer gap** as a known architectural limitation
3. **Consider whether Stage 7 is load-bearing** — if the executor always governs, Stage 7 may be dead code in practice

---

## 4. Memory Promotion Quality

**Source:** EVAL-03 (`docs/research/EVAL_03_MEMORY_PROMOTION_REPORT.md`), `telemetry/evaluation/eval-03-memory-promotion/results.json`.

### Entity Extraction Quality: Production-Ready

The `gpt-4.1-nano` extraction model achieves excellent results:

| Metric | Result |
|--------|--------|
| Entity extraction rate | **100%** (22/22 seeded entities found in Neo4j) |
| First-run accuracy | 95.5% (1 complex name missed: "Dr. Amara Osei") |
| Second-run accuracy | 100% (after name appeared in follow-up) |
| Entity types handled | People, projects, technologies, organizations |

### Promotion Pipeline: Fixed but Threshold Broken

**Critical defect found and fixed (FRE-148):** The episodic→semantic promotion pipeline was not connected to any automatic process. After wiring it into `SecondBrainConsolidator`:

| Metric | Before Fix | After Fix |
|--------|-----------|-----------|
| Promotion pipeline connected | No | Yes |
| Entities in graph | 606 (5 semantic) | 990 (990 semantic) |
| Captures processed | — | 194 |
| New turns created | — | 15 |
| New entities created | — | 144 |

**Stability score formula prevents organic promotion:**

```python
score = min(mention_count / 100.0, 0.5) + min(days_span / 90.0, 0.5)
```

This requires **50 mentions** to max out the mention factor or **90 days** of temporal spread. For a research project with ~456 captures over 5 days, no entity would ever reach organic promotion. Only high-frequency system entities (Python, Neo4j, Docker) would qualify.

### Session-Scoped Recall: Excellent

| Scenario | Recall Rate |
|----------|------------|
| DataForge Project | 100% (5/5 entities) |
| ML Infrastructure | 100% (4/4 entities) |
| Team Tech Decisions | 100% (5/5 entities) |
| Research Findings | 100% (4/4 entities) |
| Architecture Proposal | 100% (4/4 entities) |

Within-session recall is effectively **perfect** — the LLM uses conversation history + instruction to reference prior context.

### Cross-Session Recall: Critical Unknown

The 990-entity semantic graph has **not been validated for cross-session retrieval**. Session-scoped recall (100%) relies on conversation history, not Neo4j semantic memory. This is the most important gap for Slice 3's proactive memory feature.

### Defect Summary

| # | Defect | Severity | Status |
|---|--------|----------|--------|
| 1 | Promotion pipeline not connected to scheduler | High | **Fixed (FRE-148)** |
| 2 | Neo4j `DateTime` → Python `datetime` timezone mismatch | Medium | **Fixed (FRE-148)** |
| 3 | Stability threshold prevents organic promotion | Medium | **Open — Slice 3** |
| 4 | Cross-session recall not validated | Medium | **Open — follow-up** |

### Recommendations for Slice 3

1. **Redesign stability threshold:** Recency boost (24h window), relative top-N per session, or lower `min_mentions` to 3–5
2. **Validate cross-session recall** before shipping proactive memory
3. **Entity extraction is a strength** — no changes needed to the extraction pipeline
4. **990-entity graph is a production corpus** for proactive memory development

---

## 5. Delegation Gaps

**Source:** EVAL-05 delegation outcome log (`docs/research/DELEGATION_OUTCOME_LOG.md`), 3 tracked delegations.

### Delegation Performance Summary

| DEL ID | Task | Rounds | Success | Satisfaction | Context Quality |
|--------|------|--------|---------|--------------|-----------------|
| DEL-001 | Wire promotion pipeline (FRE-148) | 2 | Yes | 4/5 | Partial |
| DEL-002 | Context budget review (FRE-149) | 1 | Yes | 5/5 | Sufficient |
| DEL-003 | CP-05 delegation intent path | 3 | Yes | 3/5 | Partial |

| Metric | Value |
|--------|-------|
| Success rate | 100% (3/3) |
| Average rounds needed | 2.0 |
| Average satisfaction | 4.0/5 |
| Context sufficiency: full | 1/3 (33%) |
| Context sufficiency: partial | 2/3 (67%) |

### Systematic Context Gaps

| Gap | Frequency | Impact |
|-----|-----------|--------|
| **Missing module placement guidance** | 2/3 (67%) | Agent creates files in wrong location; requires correction round |
| **Missing ES index/field names** | 2/3 (67%) | Agent must discover `agent-logs-*` index and event field naming independently |
| **Underused `known_pitfalls` field** | 2/3 (67%) | Pitfalls from past delegations not carried forward |
| **Missing `captains_log` module path** | 1/3 (33%) | Needed for telemetry event structure but not included in package |

### What Works Well

1. **Evaluation-type delegations** (run script → extract data → write report) work in **1 round** with minimal iteration. This is the strongest delegation pattern.
2. **`relevant_files` list accuracy** — file pointers in delegation packages are consistently correct.
3. **`conventions` entries followed without prompting** — coding standards (structlog, no bare except, Google docstrings) are respected when specified.
4. **Acceptance criteria drive implementation** — clear checklists produce deliverables matching expectations.

### Recommendations for Slice 3

1. **Add default file placement convention** to `compose_delegation_package()`: `"place new modules under src/personal_agent/<relevant_submodule>/"`
2. **Auto-populate `known_pitfalls`** from this delegation log in future packages
3. **Include ES index name** (`agent-logs-*`) and key event types in all evaluation delegations
4. **Template the evaluation delegation pattern** — it's repeatable and efficient
5. **Build a delegation context checklist** derived from these gaps for the programmatic delegation pipeline (Slice 3)

---

## 6. Graphiti Experiment Results

**Source:** EVAL-02 (`docs/research/GRAPHITI_EXPERIMENT_REPORT.md`), raw data in `telemetry/evaluation/graphiti/`, decision in ADR-0035.

### Experiment Design

6 scenarios, 50 real + 500 synthetic episodes, two LLM A/B configurations:
- **OpenAI:** gpt-4.1-mini (extraction) + gpt-4.1-nano (search)
- **Anthropic:** claude-haiku-4-5 (both roles)

### Head-to-Head: Seshat vs Graphiti

| Dimension | Seshat | Graphiti | Winner | Gap |
|-----------|--------|----------|--------|-----|
| Episodic retrieval precision | 0%* | 70% | Graphiti | *Measurement artifact on Seshat side |
| Entity deduplication | 500 entities from 40 mentions | 10 canonical entities | **Graphiti** | 50× better |
| Ingest latency (p50) | 1.3 ms | 8,224 ms (Anthropic) | **Seshat** | 6,300× faster |
| Ingest latency (p95) | 10.5 ms | 14,025 ms (Anthropic) | **Seshat** | 1,300× faster |
| Query latency (p50) | 2.0 ms | 326 ms | **Seshat** | 163× faster |
| 500-episode total ingest | 3.7 s | 62 min (Anthropic) | **Seshat** | 1,000× faster |
| Weighted score (5-point) | **2.65** | **3.65** | Graphiti | +1.0 |

*Seshat's 0% retrieval score was a keyword-matching measurement artifact — entity name variations weren't found by exact search.

### LLM Provider A/B Test

Both providers produce **identical quality** on entity extraction and deduplication.

| Metric | OpenAI (gpt-4.1-mini) | Anthropic (claude-haiku-4-5) |
|--------|----------------------|------------------------------|
| Ingest speed | 9,549 ms p50 | 8,224 ms p50 (14% faster) |
| Ingest at scale | 9,657 ms p50 | 7,484 ms p50 (22% faster) |
| Cost per MTok | $0.40–$1.60 | $1.00–$5.00 (2.5× more expensive) |
| Quality | Identical | Identical |

**If Graphiti were adopted:** Use OpenAI gpt-4.1-mini — same quality, 60% cheaper.

### Decision: Enhanced Seshat (ADR-0035)

Graphiti's quality advantage (3.65 vs 2.65) traces to two specific capabilities that can be added to Seshat without the framework:

| Enhancement | Priority | What It Solves |
|-------------|----------|----------------|
| **Embedding vectors + hybrid search** | P0 | Closes 0% → 70% retrieval gap |
| **Fuzzy entity deduplication** | P1 | Closes 500 → 10 dedup gap |
| **Bi-temporal fields** | P2 | Enables point-in-time queries |
| **Promotion threshold redesign** | P3 | Fixes organic promotion (from EVAL-03) |

**Why not adopt Graphiti:**
- 1,000× ingestion latency blocks conversation flow (8–10s per episode)
- Less control over memory lifecycle than Seshat's explicit promotion pipeline
- Young framework (v0.28) with opaque `add_episode()` internals
- Token cost tracking broken in Graphiti — actual costs unquantified

See: `docs/architecture_decisions/ADR-0035-seshat-backend-decision.md` for full analysis.

---

## 7. Orchestration Analysis

**Source:** `docs/research/evaluation-orchestration-analysis.md`, second opinion response, Run-01 through Run-04 telemetry.

### Four-Run Trajectory

| Run | Context | Paths Passed | Assertions | Pass Rate | Avg Latency |
|-----|---------|-------------|------------|-----------|-------------|
| Baseline (Run-01) | First instrumented run | 22/25 | 118/127 | 92.9% | 33.6s |
| Run-02 | Sub-agent fix attempt (regression) | 7/25 | 78/127 | 61.4% | 23.4s |
| Run-03 | Three targeted fixes | 20/25 | 111/127 | 87.4% | 37.3s |
| Run-04 | Fixes + SearXNG integration | **22/25** | **119/127** | **93.7%** | 33.6s |

Run-04 achieved the **best assertion pass rate** across all runs, recovering from the Run-02 regression and fixing CP-10 (DECOMPOSE strategy) and CP-11 (complexity escalation).

### Remaining Failures (3 paths, 8 assertions)

**CP-16 — HYBRID Synthesis Quality (intermittent):**
Gateway correctly classifies as HYBRID, but agent answers directly without spawning sub-agents. When sub-agents execute: 162s with comprehensive analysis. When bypassed: 29s with shallow answer. Same prompt, same gateway output — different agent behavior.

**CP-17 — Sub-Agent Concurrency (persistent since Run-03):**
DECOMPOSE triggers correctly but agent attempts a single long-pass answer instead of expansion. Timeouts at 187s vs 272s success at baseline with actual expansion.

**CP-19 — Long Conversation Implicit Recall (never passed):**
"Going back to the beginning — what was our primary database again?" classified as `conversational` instead of `memory_recall`. Agent claims not to know PostgreSQL despite it being in-context and not trimmed. Same root cause as intent classification gap in §1.

### Cross-Run Flakiness (Non-Determinism)

Same prompts, same model, same gateway output produce different expansion behavior:

| Run | CP-09 HYBRID | CP-10 DECOMPOSE | CP-16 HYBRID | CP-17 DECOMPOSE |
|-----|-------------|-----------------|-------------|-----------------|
| Baseline | ✅ | ❌ | ✅ | ✅ |
| Run-03 | ❌ | ❌ | ✅ | ❌ |
| Run-04 | ✅ | ✅ | ❌ | ❌ timeout |

This confirms **expansion decisions live in LLM sampling**, not in deterministic code — the core architectural gap.

### Second Opinion (GPT-5.4 External Review)

Core thesis: *"The system currently mixes deterministic routing decisions with non-deterministic execution decisions in a way that leaves a control gap."*

Five recommendations, all agreed:

| # | Recommendation | Assessment |
|---|---------------|-----------|
| A | Deterministic expansion controller when HYBRID/DECOMPOSE | Agreed — aligns with LangGraph/ADK patterns |
| B | Tool-call enforcement (`spawn_sub_agents` tool) over prompt compliance | Agreed — makes expansion observable and enforceable |
| C | Deterministic fallback planner when LLM planner fails | Agreed with scope caveat (enumerated comparisons only, not open-ended) |
| D | Per-phase time budgets (planner/worker/synthesizer) | Agreed — prevents CP-17 timeout |
| E | Session fact lookup controller for implicit recall | Agreed — safety net for CP-19 |

### Target Architecture Shift

```
Current:  Gateway decides → Model decides → Code executes
Proposed: Gateway decides → Code enforces → Model plans/synthesizes
```

See: `docs/research/evaluation-orchestration-analysis.md` for full run-by-run analysis.

---

## 8. SearXNG Impact

**Source:** Run-04 telemetry (SearXNG added in Run-04), second opinion analysis.

### What Changed

SearXNG web search was integrated as a new tool in the MCP Gateway between Run-03 and Run-04. This gave the agent access to live web search results alongside existing tools.

### Observed Effects

**Positive:**
- Run-04 achieved the highest pass rate (93.7%), surpassing even baseline (92.9%)
- CP-10 (DECOMPOSE) and CP-11 (complexity escalation) fixed in Run-04
- Latency returned to baseline levels (33.6s average)

**Confounding:**
- CP-16 (HYBRID synthesis) **regressed** in Run-04 after passing in baseline and Run-03
- CP-17 (sub-agent concurrency) remained broken, with timeout
- Both regressions are in expansion paths — the agent may prefer search-and-answer over sub-agent expansion when web search is available

### Analysis Gap

**SearXNG as a confound is not yet isolated.** The second opinion explicitly flagged this:

> *"If the agent now prefers search-and-answer over sub-agent expansion, that's a tool-availability confound not yet analyzed."*

Run-04 combined SearXNG integration with other fixes, making it impossible to attribute behavior changes to any single variable. Specifically:
- Did CP-16 regress because of SearXNG availability, or LLM sampling non-determinism?
- Is the agent choosing search-then-answer as a shortcut when HYBRID/DECOMPOSE would produce better results?

### Recommendation for Slice 3

1. **Measure before enforcing** — before implementing expansion enforcement, check if SearXNG is the actual driver of the remaining pass-rate improvement
2. **If expansion enforcement is implemented**, it should explicitly prevent search-then-answer fallback when HYBRID/DECOMPOSE is set
3. **Run an isolated SearXNG A/B test** — same critical paths with and without web search tool available — to quantify the confound

---

## 9. Key Surprises

Unexpected findings that emerged during the evaluation phase, not predicted by pre-evaluation assumptions.

### Surprise 1: The Promotion Pipeline Was Disconnected

**Expected:** Promotion pipeline was operational since Slice 1.
**Actual:** `run_promotion_pipeline()` existed but was never called by any scheduler or consolidation process. The pipeline was fully implemented but not wired in.
**Impact:** All 990 semantic entities in the graph were created only after EVAL-03 manually connected the pipeline. Pre-EVAL-03, only 5 entities (out of 606) had semantic status — all were hardcoded seeds.
**Lesson:** Integration testing must verify end-to-end data flow, not just individual component correctness.

### Surprise 2: Budget Stage Is Effectively Dead Code

**Expected:** Context budget management (Stage 7) would be a meaningful governor, especially for long conversations.
**Actual:** The executor's `apply_context_window` caps messages to ~11 before Stage 7 ever sees them. Peak Stage 7 utilization was 2.5% of the 65K ceiling. The budget trimming logic is correct but never executes.
**Impact:** Stage 7 provides no protection against context overflow in its current position in the pipeline.
**Lesson:** End-to-end token accounting must span the full path from gateway to LLM call, not just the gateway's view.

### Surprise 3: Graphiti's Ingestion Latency Is Prohibitive (1,000× Slower)

**Expected:** Graphiti would be slower than raw Neo4j but within usable range (maybe 10–50× slower).
**Actual:** 8–10 seconds per episode ingestion due to LLM calls for extraction + deduplication. 500 episodes: 62–80 minutes (Graphiti) vs 3.7 seconds (Seshat).
**Impact:** Full Graphiti adoption is architecturally incompatible with a conversational agent. The quality advantages (dedup, embeddings) must be added to Seshat independently.
**Lesson:** Framework benchmarks should test at realistic scale early; the 1,000× gap was not apparent from Graphiti's documentation or small-scale tests.

### Surprise 4: Expansion Non-Determinism Is the Architecture's Biggest Gap

**Expected:** If the gateway classifies HYBRID/DECOMPOSE, the agent would reliably expand.
**Actual:** Same prompt + same gateway output → different expansion behavior across runs. The agent can decide to skip expansion entirely, producing shallow answers on prompts designed for multi-agent synthesis.
**Impact:** The gateway-to-agent boundary is advisory, not contractual. This is the single largest architectural issue discovered during evaluation — it undermines the entire decomposition strategy.
**Lesson:** Control-flow decisions (expand vs. don't expand) must be in deterministic code, not in LLM sampling. Prompt compliance is insufficient for workflow enforcement.

### Surprise 5: Session-Scoped Recall Is Perfect, Cross-Session Is Untested

**Expected:** Memory recall would show some degradation within sessions due to context window limitations.
**Actual:** 100% recall across all 5 scenarios within a session. The LLM uses conversation history directly — it doesn't even need Neo4j for within-session retrieval.
**Impact:** The real test of the memory system (cross-session recall via Neo4j semantic graph) was never run. Slice 3's proactive memory feature depends on exactly the capability that remains unvalidated.
**Lesson:** Evaluation must test the hard case (cross-session), not just the easy case (within-session where conversation history provides a shortcut).

### Surprise 6: Geospatial Context Elevated to P2

**Expected:** Neuroscience-inspired features (geospatial memory scaffolding) would be deprioritized as speculative.
**Actual:** ADR-0035 elevated geospatial context from "nice-to-have" to P2, based on hippocampal spatial scaffolding theory suggesting location is a fundamental retrieval cue.
**Impact:** Slice 3 may include geospatial coordinates on Entity nodes and spatial indexing alongside vector search.
**Lesson:** Research projects benefit from periodically revisiting speculative features against new evidence — the Graphiti experiment's focus on retrieval quality made the neuroscience alignment more concrete.

---

## Summary: Slice 3 Priority Inputs

| Finding | Severity | Slice 3 Action |
|---------|----------|----------------|
| Expansion non-determinism | **Critical** | Deterministic expansion controller + tool-call enforcement |
| Intent classification gap (recall) | **High** | Recall Controller with two-stage classification |
| Entity dedup broken (500 → 10) | **High** | Fuzzy dedup pipeline (vector similarity + LLM merge) |
| No embedding-based search | **High** | P0: Add vector embeddings to Seshat |
| Promotion threshold blocks organic use | **Medium** | Redesign stability formula (recency boost or relative threshold) |
| Cross-session recall untested | **Medium** | Validate before shipping proactive memory |
| Two-layer context management gap | **Medium** | Unify executor + gateway token accounting |
| Delegation context gaps | **Medium** | Auto-populate file placement + ES index in packages |
| SearXNG expansion confound | **Low** | Isolated A/B test before expansion enforcement |
| Budget stage effectively dead | **Low** | Document or remove; unify with executor |

**Top 3 priorities for Slice 3 (based on evaluation data):**

1. **Expansion enforcement** — the single largest reliability gap; non-deterministic expansion undermines the decomposition strategy
2. **Seshat P0: embeddings + hybrid search** — required for proactive memory (the core Slice 3 feature); Graphiti experiment proved embeddings are non-negotiable for retrieval quality
3. **Recall Controller** — the only persistent failure across all 4 orchestration runs; blocks implicit memory recall which is central to the user experience

---

*This document synthesizes findings from EVAL-01 through EVAL-06 and 4 orchestration analysis runs conducted during March 2026. It is the primary input for EVAL-08 (FRE-153): Slice 3 priority decisions.*
