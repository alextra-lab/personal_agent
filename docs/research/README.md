# Research Knowledge Base

This directory contains research findings, external system analyses, and architectural insights that inform the Personal Agent's design and evolution.

---

## 📚 Index of Research Documents

### Model & Routing Research (December 2025)

**Latest Research Analysis:**

1. **Model Orchestration Research Analysis** ⭐ `model_orchestration_research_analysis_2025-12-31.md`
   - Comprehensive analysis of small model performance for routing
   - Single-agent vs multi-agent architecture comparison
   - DeepSeek-R1, Qwen3, and Mistral model evaluations
   - Quantization strategy recommendations
   - **Status:** Complete, informs ADR-0008

2. **Raw Research Data** `temp_perplexity_research.md`
   - Original Perplexity deep research output
   - Model comparison tables
   - Architecture pattern descriptions
   - **Status:** Processed into analysis document, retained for reference

**Related Architecture Documents:**

- `../architecture/INTELLIGENT_ROUTING_PATTERNS_v0.1.md` — Routing pattern inspiration
- `../architecture_decisions/ADR-0008-model-stack-course-correction.md` — Proposed changes
- `../architecture_decisions/RESEARCH_ANALYSIS_SUMMARY_2025-12-31.md` — Executive summary

---

### Core Research Topics

#### Agent Architecture & Orchestration

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

### For Routing & Orchestration

When designing routing or orchestration logic:

1. **Study patterns** in `../architecture/INTELLIGENT_ROUTING_PATTERNS_v0.1.md`
2. **Reference frameworks** in `orchestration-survey.md`
3. **Apply safety constraints** from `agent-safety.md`
4. **Implement evaluation** using `evaluation-observability.md` patterns

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

### Example: Model Stack Evolution

1. **Research:** Perplexity research on DeepSeek-R1 models → `temp_perplexity_research.md`
2. **Analysis:** Deep dive into findings → `model_orchestration_research_analysis_2025-12-31.md`
3. **Architecture:** Pattern extraction → `../architecture/INTELLIGENT_ROUTING_PATTERNS_v0.1.md`
4. **Decision:** Formal proposal → `../architecture_decisions/ADR-0008-model-stack-course-correction.md`
5. **Summary:** Executive overview → `../architecture_decisions/RESEARCH_ANALYSIS_SUMMARY_2025-12-31.md`
6. **Implementation:** Code changes based on ADR
7. **Evaluation:** Benchmark results → `../architecture_decisions/experiments/E-004-reasoning-model-comparison.md`
8. **Update:** Findings feed back into research knowledge base

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
- [Intelligent Routing Patterns](../architecture/INTELLIGENT_ROUTING_PATTERNS_v0.1.md)
- [Local LLM Client Spec](../architecture/LOCAL_LLM_CLIENT_SPEC_v0.1.md)

**Decisions:**
- [ADR-0003: Model Stack](../architecture_decisions/ADR-0003-model-stack.md)
- [ADR-0008: Model Stack Course Correction](../architecture_decisions/ADR-0008-model-stack-course-correction.md)
- [Research Analysis Summary](../architecture_decisions/RESEARCH_ANALYSIS_SUMMARY_2025-12-31.md)

**Implementation:**
- [Implementation Roadmap](../plans/IMPLEMENTATION_ROADMAP.md)
- [Model Configuration](../config/models.yaml)

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

**Last Updated:** 2026-01-28
**Next Review:** After Phase 2 model evaluation (Month 2-3)
