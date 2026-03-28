# Evaluation Orchestration Analysis — Run-04 Synthesis with Second Opinion

> **Date:** 2026-03-28
> **Inputs:** 4 evaluation runs (baseline through run-04), GPT-5.4 second-opinion review + proposed remediation
> **Status:** Analysis complete. Orchestration remediation parked pending memory/Graphiti evaluation.

---

## 1. Evaluation Run Trajectory

| Run | Context | Paths | Assertions | Rate | Latency |
|-----|---------|-------|------------|------|---------|
| Baseline | First instrumented run | 22/25 | 118/127 | 92.9% | 33.6s |
| Run-02 | Subagent fix attempt (regression) | 7/25 | 78/127 | 61.4% | 23.4s |
| Run-03 | Three targeted fixes | 20/25 | 111/127 | 87.4% | 37.3s |
| **Run-04** | Fixes + SearXNG | **22/25** | **119/127** | **93.7%** | **33.6s** |

Run-04 is the best assertion pass rate across all runs. The system is at a stable plateau.

### What Run-04 Fixed

- **CP-10 (DECOMPOSE Strategy):** Failed all prior runs, now passing. Complex multi-part analysis correctly triggers expansion.
- **CP-11 (Complexity Escalation):** Failed all prior runs, now passing. Per-turn complexity re-assessment works.
- **CP-09 (HYBRID Strategy):** Regressed in run-03, restored in run-04.

### Remaining Failures (3 paths, 8 assertions)

| Path | Category | Failing Assertions | Pattern |
|------|----------|-------------------|---------|
| CP-16 | Expansion & Sub-Agents | 4/9 — `hybrid_expansion_start/complete` absent | Intermittent (passed baseline + run-03) |
| CP-17 | Expansion & Sub-Agents | 3/6 — same + LLM timeout at 187s | Persistent since run-03 |
| CP-19 | Context Management | 1/2 — intent `conversational` instead of `memory_recall` | **Never passed in any run** |

---

## 2. Root Cause Analysis

### The Gateway→Agent Gap (CP-16, CP-17)

**Diagnosis (confirmed by second opinion):** The gateway deterministically classifies the strategy correctly (HYBRID/DECOMPOSE), but the primary agent treats the expansion flag as advice rather than a contract. The LLM can choose to answer directly rather than generating a decomposition plan, silently bypassing the sub-agent expansion path.

This is a **workflow determinism problem**, not a model capability problem. The gateway makes a control-plane decision; the agent treats it as a suggestion.

**Evidence:**
- CP-16: Gateway says HYBRID, agent answers in 29s without expansion (quality is fine, but telemetry contract violated)
- CP-17: Gateway says DECOMPOSE, agent attempts a long single-pass answer, hits 180s LLM timeout
- CP-09 and CP-10 (identical mechanism) successfully expand in the same run — proving the expansion code works, it just doesn't always execute

**Cross-run flakiness pattern:**

| Run | CP-09 HYBRID | CP-10 DECOMPOSE | CP-16 HYBRID | CP-17 DECOMPOSE |
|-----|-------------|-----------------|-------------|-----------------|
| Baseline | ✅ | ❌ | ✅ | ✅ |
| Run-03 | ❌ | ❌ | ✅ | ❌ |
| Run-04 | ✅ | ✅ | ❌ | ❌ timeout |

Same prompts, same model, same gateway output → different expansion behavior. Non-deterministic because the decision lives in LLM sampling.

### Implicit Recall Classification (CP-19)

**Diagnosis:** The intent classifier uses first-match regex on the raw message text. The phrase "Going back to the beginning — what was our primary database again?" doesn't match any `MEMORY_RECALL` patterns. The patterns are anchored on explicit phrases like "do you remember", "what did I say", "recall our".

**Compounding failure:** Even though PostgreSQL was mentioned in Turn 2 of the same 10-turn session, the agent claims not to know. The second opinion correctly identifies this as "Lost in the Middle" territory — the fact is present in context but the model under-attends it, and without `memory_recall` scaffolding, no recall mechanism activates.

---

## 3. Second Opinion Synthesis

### Core Thesis (GPT-5.4)

> "The system currently mixes deterministic routing decisions with non-deterministic execution decisions in a way that leaves a control gap."

The recommended architectural shift: **move "whether expansion must happen" out of the model and into code.** The gateway decides IF. The code enforces it. The LLM generates plan content and synthesis only.

### Key Recommendations

| # | Recommendation | Our Assessment |
|---|---------------|----------------|
| A | Deterministic expansion controller when HYBRID/DECOMPOSE | **Agreed** — correct fix, aligns with LangGraph/ADK patterns |
| B | Tool-call enforcement (spawn_sub_agents tool) over prompt compliance | **Agreed** — makes expansion observable and enforceable |
| C | Deterministic fallback planner when LLM planner fails | **Agreed with scope caveat** — works for enumerated comparisons, not open-ended analysis |
| D | Per-phase time budgets (planner/worker/synthesizer) | **Agreed** — prevents CP-17 timeout surfacing to users |
| E | Session fact lookup controller for implicit recall | **Agreed** — safety net independent of model attention patterns |

### Where We Push Back

1. **Deterministic fallback planner (C)** is narrower than presented. Works when entities are explicitly named ("Redis, Memcached, Hazelcast"). Fails for open-ended prompts ("Research the best approach to scaling our API layer"). Spec must scope this to enumerated comparison prompts.

2. **Making expansion mandatory closes a research avenue.** The project is a cognitive architecture research platform. One research question is: can a 35B model learn to self-orchestrate? Making expansion deterministic answers that by removing it from the model's decision space. This is the right *production* move but a deliberate *research* trade-off. Needs an ADR.

3. **Hybrid classifier (Q3)** changes Stage 4's contract. Should be implemented as a Stage 4b (post-classification refinement) to preserve the clean deterministic boundary.

4. **SearXNG as confound.** Run-04 added web search to the tool set. If the agent now prefers search-and-answer over sub-agent expansion, that's a tool-availability confound — not addressed in the second opinion.

### What It Missed

- Temperature/sampling non-determinism as a contributing factor
- SearXNG tool addition as a potential expansion confound
- The "strategy mismatch rate" metric (item 10 in its concrete changes) is the most actionable immediate step — instrument before building new architecture

---

## 4. Decision: Park Orchestration, Complete Memory Evaluation

The orchestration analysis is complete. We have:
- Precise diagnosis of the gateway→agent gap
- A strong architectural remediation plan (second opinion + our assessment)
- Full assertion-level data across 4 runs

**What's NOT done from the Evaluation Phase Guide:**
- Graphiti experiment (Seshat backend decision)
- Memory promotion quality evaluation
- Context budget behavior review
- Kibana dashboard import for Slice 2
- Delegation outcome logging
- Evaluation findings synthesis
- Seshat backend ADR
- Slice 3 priority ranking

**The orchestration fixes are Slice 3 work.** The remediation plan designs new architectural components (expansion controller, recall controller, phase budgets). These should be designed alongside Slice 3's other deliverables, informed by memory evaluation data.

**Next steps:** Complete the memory/Graphiti side of evaluation, then synthesize all findings into the Slice 3 scope.

---

## 5. Reference Documents

| Document | Location |
|----------|----------|
| Run-04 results (markdown) | `telemetry/evaluation/run-04-fixes-and-searchxng/evaluation_results.md` |
| Run-04 results (JSON) | `telemetry/evaluation/run-04-fixes-and-searchxng/evaluation_results.json` |
| Second opinion brief (sent to GPT-5.4) | `docs/research/evaluation-run-04-second-opinion-brief.md` |
| Second opinion response | `docs/research/evaluation-run-04-second-opinion-response.md` |
| Second opinion remediation plan | `docs/research/evaluation-run-04-second-opinion-proposed-remediation.md` |
| Evaluation Phase Guide | `docs/guides/EVALUATION_PHASE_GUIDE.md` |
| Graphiti experiment template | `docs/research/GRAPHITI_EXPERIMENT_REPORT.md` |

---

*This document captures the orchestration evaluation as complete. Memory evaluation and Graphiti experiment are next.*
