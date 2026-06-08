# Research Knowledge Base

This directory contains research findings, external system analyses, and architectural insights that inform the Personal Agent's design and evolution.

---

## 📚 Index of Research Documents

### Model & routing research (December 2025 — archived sources)

Router-era write-ups now live under **`../archive/`** (e.g. `model_orchestration_research_analysis_2025-12-31.md`, `router_prompt_patterns_best_practices_2025-12-31.md`, `RESEARCH_ANALYSIS_SUMMARY_2025-12-31.md`). Current architecture is **`../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`**; use ADR-0008 and later ADRs for model stack truth.

**Active research drivers (2026):**

- **`EVAL_08_SLICE_3_PRIORITIES.md`** — Eval priorities that shaped Slice 3
- **`context_management_research.md`** — Context window management (inputs to `CONTEXT_INTELLIGENCE_SPEC.md`)
- **`2026-06-02-cache-aware-prompt-layout-and-compaction.md`** — Cross-turn KV-cache reuse via a frozen append-only layout + cost-optimal compaction scheduler (ADR-0081 §D2/D3, FRE-433/434); prefill economics, the two-property theorem, byte-identity invariant, the `L*` scheduler, dev/testing process, diagrams
- **`2026-06-04-artifact-turn-cost-latency-forensics.md`** — Per-round forensics of the first post-FRE-469 artifact-build turn (`a0a07227`): the uncached context tail re-billed every round (768k fresh input), the generation tail that dominates wall-time and hits the 16k output cap, and the `TOOL_USE`→`SIMPLE` complexity pin that blocks decomposition; ranks four optimization levers (compression, decomposition, bash batching, output cap → FRE-475/476/477/478) with a reproducible measurement recipe
- **`2026-06-05-tool-result-compression-park-decision.md`** — Why ADR-0085 (intra-turn tool-result compression, FRE-475) is **parked**: the bash digest head/tail-truncates file content the model reads via `cat`/`grep`/`sed`, corrupting source; the harness already bounds reads via the injected `bash`/`read` grep→ranged-read contract, so the digest is a redundant second truncation layer. Records the keep-dormant-don't-remove disposition, why the `read` tool stays, and the pivot to FRE-476 (decomposition)
- **`2026-06-06-decomposition-first-run-findings.md`** — First live run of artifact decomposition (ADR-0086, FRE-480/481, trace `87cbd720`): production-quality artifact at **lower cost** than the 20+-round single-agent path (real win), but four gaps — (1) **shallow memory grounding** on build/teach `TOOL_USE` requests: deep `recall_controller` is gated to `CONVERSATIONAL`, so only proactive vector-KG retrieval (cross-session entity embeddings, top-k) grounded it; (2) cost/token meter under-counts sub-agents (`$0.57` shown vs **`$0.90`** true); (3) blind live status (no `turn_status` from the sub-agent path); (4) `planner_failed: schema_validation_failed` degraded discovery to generate-from-knowledge
- **`2026-06-08-fre-533-telemetry-surface-reconciliation.md`** — Three-way reconciliation (emit-site ↔ ES mapping ↔ Kibana dashboard) for every field in all six `agent-*` index families (FRE-533, Telemetry Surface Audit). 1023 rows: 304 aligned, 643 emitted-but-unmapped dynamic sprawl (`agent-logs-*` alone = 768 fields), 30 trap rows (float→`long`, `ignore_above:1024` long-text drop, join-key-as-`text`), and **14 broken/risky Kibana panels across 6 of 12 dashboards** (mostly `.keyword` aggs on bare-`keyword` fields). Answers the dashboard-provenance question (NDJSON in git but repo↔live drifted; 3 families have no index-pattern). Routes fixes to FRE-534/535/537/538/540. Companion CSV: `2026-06-08-fre-533-reconciliation-table.csv`
- **`2026-06-08-fre-534-template-reindex-plan.md`** — Reindex/rollover plan accompanying the FRE-534 (A2) ES template corrections: agent-logs trap fixes (added `ms_fields_as_float` rule + explicit threshold floats + selective `free_text` extension; `denial_reason` kept `keyword` to preserve its donut agg), the captains 3-way split (captures/reflections/subagents, priority ladder 110/110/120), and the two newly-authored family templates (insights, slm-health — fixing `text` join keys). Net: **no backfill** for any family (corrected types only matter for new joins/aggs with no historical consumer, or leave `_source` readable). Documents the master deploy step (DELETE retired template → re-run setup) and the build-session temp-index verification.
- **`2026-06-08-fre-536-cost-budget-dashboard.md`** — Cost & budget dashboard (C1, cost_gate/ADR-0065). Root-caused the cost panels' blocker: `gate.py` emitted money as `str(Decimal)` (and `es_handler` stringifies any non-JSON-serializable value) → ES `keyword`, unsummable. Fix: emit `float(...)` under namespaced `*_usd` names + explicit `double` mappings (no destructive reindex — new names), `role` added to commit/refund for budget-role attribution. Live `_field_caps` proof, 6 legacy-aggs panels (not Lens, per FRE-546), cap-utilization deferred to **FRE-547** (needs a Postgres `budget_counters`→ES snapshot emitter).
- **`2026-06-08-fre-537-traversal-gate-dashboard.md`** — Traversal ledger & gate-decision dashboard (C2). Measured each of the ticket's four L0-traversal candidates against live ES before designing: built the two genuinely-unviewed ES-backed surfaces — gate decisions (`tool_loop_gate`, 8 998 docs; `decision`/`reason`/`tool_name` bare `keyword`) and the route-trace ledger ES slice (`route_trace_written`: `gateway_label` vs `orchestration_event`) — as 6 legacy-aggs panels. Deferred execution-topology (its `topology`/`cost_authoritative_usd` are **absent** from ES `_field_caps`; live only in the Postgres ledger + transient AG-UI `turn_status`) to a follow-up ES emitter, and skipped decomposition-strategy distribution (already viewed on `expansion_decomposition`/`intent_classification`). Live import + per-panel agg proof; static test enforces the no-`.keyword`-on-bare-keyword A1 trap guard.
- **`EVALUATION_PHASE_FINDINGS.md`** — Cross-eval synthesis

2. **Raw Research Data** `temp_perplexity_research.md`
   - Original Perplexity deep research output
   - Model comparison tables
   - Architecture pattern descriptions
   - **Status:** Processed into analysis document, retained for reference

**Related decisions:** `../architecture_decisions/ADR-0008-model-stack-course-correction.md` and subsequent ADRs

---

### Core Research Topics

#### Agent Architecture & Orchestration

- **context-switching-task-segmentation.md** — Automatic task boundary detection, task registries, and context switching within agent systems; hypothesis-driven research with 3 experiments planned (originally targeting ADR-0017, now applicable to Redesign v2 decomposition/expansion model)
- **orchestration-survey.md** — Survey of orchestration frameworks (LangGraph, AutoGen, etc.)
- **cognitive_architecture_principles.md** — Brain-inspired cognitive patterns
- **external_systems_analysis.md** — Analysis of production AI systems (Factory.ai, Cursor, etc.)

#### Safety & Governance

- **agent-safety.md** — Safety patterns for autonomous AI systems
- **evaluation-observability.md** — Evaluation frameworks and observability patterns

#### Learning & Adaptation

- **learning-self-improvement-patterns.md** — Self-improvement and learning patterns
- **world-modeling.md** — World model construction and maintenance

#### Infrastructure

- **mac-local-models.md** — Running LLMs locally on Apple Silicon
- **temp_perplexity_research.md** — Latest model benchmarks and routing research

#### Structured Extraction & Frameworks

- **langextract_library_review_2026-01-28.md** — LangExtract (Google) review for entity extraction and reflection; experiment E-018
- **dspy_framework_analysis_2026-01-15.md** — DSPy framework analysis (E-008a complete)
- **dspy_quick_reference_2026-01-15.md** — DSPy quick reference

---

## 🎯 How to Use This Knowledge Base

### For Architecture Decisions

When making architectural decisions:

1. **Check relevant research documents** for validated patterns
2. **Reference in ADRs** with specific findings and citations
3. **Update research documents** when new findings emerge
4. **Create new research documents** when exploring new domains

### For Model Selection

When evaluating or selecting models:

1. **Start with** `model_orchestration_research_analysis_2025-12-31.md`
2. **Check benchmarks** in `temp_perplexity_research.md`
3. **Consider hardware constraints** in `mac-local-models.md`
4. **Align with governance** using findings in `agent-safety.md`

### For routing & orchestration

When designing gateway or orchestration behavior:

1. **Start from** `../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` (request gateway, single primary agent)
2. **Historical router patterns** (if needed): `../archive/INTELLIGENT_ROUTING_PATTERNS_v0.1.md`
3. **Reference frameworks** in `orchestration-survey.md`
4. **Apply safety constraints** from `agent-safety.md`
5. **Evaluate** using `evaluation-observability.md` patterns

---

## 📊 Research-to-Implementation Pipeline

```
Research Discovery → Analysis Document → Architecture Doc → ADR → Implementation
                                                              ↓
                                                         Experiment
                                                              ↓
                                                         Evaluation
                                                              ↓
                                                    Update Research ←
```

### Example: Model stack evolution (historical)

1. **Research:** Perplexity research on DeepSeek-R1 models → `temp_perplexity_research.md`
2. **Analysis:** Deep dive → `../archive/model_orchestration_research_analysis_2025-12-31.md`
3. **Architecture (historical):** `../archive/INTELLIGENT_ROUTING_PATTERNS_v0.1.md`
4. **Decision:** `../architecture_decisions/ADR-0008-model-stack-course-correction.md`
5. **Summary:** `../archive/RESEARCH_ANALYSIS_SUMMARY_2025-12-31.md`
6. **Implementation:** Code + `config/models.yaml`
7. **Evaluation:** Harness outputs under `telemetry/evaluation/`
8. **Update:** Findings feed back into this README and ADRs

---

## 🔍 Quick Reference

### What Research Says About...

#### **Router Models**
- ✅ **Qwen3-4B:** Best choice for routing (rank 2.25, superior fine-tuning)
- 🔬 **MoMA pattern:** Three-stage routing (classify → select → validate)
- 🔬 **LLMRouter:** 16+ algorithms, RL-trained Router R1

#### **Reasoning Models**
- ⚠️ **DeepSeek-R1-14B:** 93.9% MATH-500, 59.1% GPQA, recommended over Qwen3-Next-80B
- ✅ **8-bit quantization:** Superior to 5-bit for 14B-30B models
- 🔬 **Context needs:** 32K sufficient for most tasks, 128K for document analysis

#### **Coding Models**
- ✅ **Qwen3-Coder-30B:** 55.40% SWE-Bench, strong tool usage
- 🔬 **Devstral 2:** 56.40% SWE-Bench, 128K context (evaluate for large codebases)

#### **Architecture Patterns**
- ✅ **Single-agent + router:** 95% deterministic, optimal for local governed systems
- ❌ **Multi-agent conversation:** Less deterministic, harder to audit (not for MVP)
- 🔬 **Agents as tools:** Coordinator invokes specialized models (our current pattern)

#### **Hardware (M4 Max 128GB)**
- ✅ **DeepSeek-R1-14B:** 14-20GB @ 8bit (comfortable fit)
- ✅ **Concurrent models:** 3-4 models simultaneously with proposed stack
- ⚠️ **Qwen3-Next-80B:** 50-60GB @ 5bit (tight, limits concurrency)

---

## 🚀 Recent Updates

### March 10, 2026
- **Added:** Context switching and task segmentation research (`context-switching-task-segmentation.md`)
- **Scope:** Automatic task boundary detection, task registries, per-task context assembly, multi-task orchestration
- **Hypotheses:** H1 (4B instruct boundary detection accuracy), H2 (task-scoped context quality), H3 (Task node behavioral analysis)
- **Status:** Research documented; experimentation can now proceed using Redesign v2 decomposition/expansion infrastructure (ADR-0017 superseded)

### January 28, 2026
- **Added:** LangExtract library review (`langextract_library_review_2026-01-28.md`)
- **Added:** E-018 experiment spec and hypothesis-driven design (entity extraction parse rate, code size, latency, grounding)
- **Status:** Review documented; experiments in `experiments/langextract_evaluation/`

### December 31, 2025
- **Added:** Model orchestration research analysis
- **Added:** Intelligent routing patterns inspiration doc
- **Created:** ADR-0008 (model stack course correction)
- **Created:** Research analysis summary
- **Status:** Comprehensive model & routing research complete

### December 28-29, 2025
- **Added:** Initial research documents (orchestration, safety, learning)
- **Added:** External systems analysis
- **Added:** Cognitive architecture principles

---

## 📝 Contributing to Research Knowledge

### When to Add Research

Add research documents when:
- Exploring new architectural patterns
- Evaluating technology choices
- Analyzing external systems
- Documenting performance benchmarks
- Synthesizing academic papers or industry reports

### Document Format

```markdown
# [Topic] — [Date or Version]

**Status:** [Draft | In Review | Complete | Superseded]
**Date:** YYYY-MM-DD
**Sources:** [Citations, links, papers]

## Executive Summary
[2-3 paragraph overview]

## Detailed Analysis
[Sections with findings]

## Recommendations
[Actionable insights]

## Related Documents
[Links to ADRs, architecture docs, other research]
```

### Research → Decision Flow

1. **Document research findings** in this directory
2. **Create analysis document** synthesizing findings
3. **Reference in ADRs** when making decisions
4. **Update architecture docs** with patterns
5. **Implement** based on ADRs
6. **Evaluate** and feed results back into research

---

## 🔗 Quick Links

**Architecture:**
- [Homeostasis Model](../architecture/HOMEOSTASIS_MODEL.md)
- [Redesign v2 (canonical)](../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md)
- [Archived v0.1 routing patterns](../archive/INTELLIGENT_ROUTING_PATTERNS_v0.1.md)

**Decisions:**
- [ADR-0003: Model Stack](../architecture_decisions/ADR-0003-model-stack.md)
- [ADR-0008: Model Stack Course Correction](../architecture_decisions/ADR-0008-model-stack-course-correction.md)
- [Archived research summary](../archive/RESEARCH_ANALYSIS_SUMMARY_2025-12-31.md)

**Implementation:**
- [Master Plan](../plans/MASTER_PLAN.md)
- Model configuration: `<project-root>/config/models.yaml` (see repo; not linked as web URL)

---

## 📚 External Resources

### Key Papers & Articles

**MoMA (Mixture of Models and Agents):**
- arXiv: 2509.07571v1
- Topic: Model and agent orchestration for adaptive inference
- Key insight: Three-stage routing (classify → select → validate)

**LLMRouter (UIUC):**
- Topic: Intelligent routing system with 16+ algorithms
- Key insight: Router R1 as sequential decision process with RL

**Multi-Agent RAG:**
- Source: Pathway.com
- Topic: Interleaved retrieval and reasoning for long-context tasks
- Key insight: 12.1% improvement over single-shot retrieval

**DeepSeek-R1:**
- Source: DataCamp, HuggingFace
- Topic: Distilled reasoning models (14B, 32B variants)
- Key insight: 93.9% MATH-500, outperforms o1-mini

### Benchmark Leaderboards

- **SWE-Bench:** https://www.swebench.com (software engineering)
- **LiveCodeBench:** Coding task benchmarks
- **MATH-500:** Mathematical reasoning
- **GPQA Diamond:** Graduate-level science QA

---

**Last Updated:** 2026-03-30  
**Next Review:** After Context Intelligence phase milestones (see `CONTEXT_INTELLIGENCE_SPEC.md`)
