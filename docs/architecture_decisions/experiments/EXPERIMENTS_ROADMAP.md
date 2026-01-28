# Experiments Roadmap — Model Optimization & Routing

**Purpose:** Systematic experimentation to optimize model selection, routing strategies, and system performance through empirical evidence.

**Philosophy:** **Measure → Experiment → Decide → Document → Iterate**

**Last Updated:** 2026-01-18 (resolved numbering conflicts, created E-007 through E-009 specs)

---

## Recent Updates (2026-01-18)

**Resolved Numbering Conflicts:**
- Renamed `E-007-inference-server-evaluation.md` → `E-007a-inference-server-evaluation.md`
- Renamed `E-008-dspy-prototype-evaluation.md` → `E-008a-dspy-prototype-evaluation.md` (✅ Complete)
- Created new specs for main track:
  - `E-007-three-stage-routing.md` (MoMA-inspired routing)
  - `E-008-validation-agent-effectiveness.md` (validation stage impact)
  - `E-009-performance-based-routing.md` (learned routing from outcomes)

**Experiment Tracks:**
- **Main Track (E-001→E-012):** Model optimization, routing strategies, quality improvements
- **Parallel Track (E-###a):** Infrastructure (inference servers) and framework adoption (DSPy)

---

## Experiment Status Overview

### Main Track: Model Optimization & Routing

| ID | Title | Phase | Status | Priority | Duration |
|----|-------|-------|--------|----------|----------|
| E-001 | Orchestration Reliability | Future | 📋 Planned | LOW | TBD |
| E-002 | Planner-Critic Quality | Future | 📋 Planned | LOW | TBD |
| E-003 | Safety Gateway Effectiveness | Future | 📋 Planned | LOW | TBD |
| E-004 | Baseline Model Performance | MVP/Phase 2 | ⏳ **READY** | **HIGH** | 3 days |
| E-005 | Reasoning Model Comparison | Phase 2 | 📋 Planned | **HIGH** | 3 days |
| E-006 | Coding Model Evaluation | Phase 2 | 📋 Planned | MEDIUM | 3 days |
| E-007 | Three-Stage Routing | Phase 2 | 📋 Planned | **HIGH** | 5 days |
| E-008 | Validation Agent Effectiveness | Phase 2 | 📋 Planned | MEDIUM | 4 days |
| E-009 | Performance-Based Routing | Phase 3 | 📋 Planned | MEDIUM | 1 week |
| E-010 | Context Length Optimization | Phase 3 | 💡 Future | LOW | 3 days |
| E-011 | Quantization Quality Study | Phase 3 | 💡 Future | LOW | 4 days |
| E-012 | Router Fine-Tuning | Phase 4 | 💡 Future | LOW | 2 weeks |

### Parallel Track: Infrastructure & Framework

| ID | Title | Phase | Status | Priority | Duration |
|----|-------|-------|--------|----------|----------|
| E-007a | Inference Server Evaluation | Phase 2-3 | 📋 Planned | MEDIUM | 1 week |
| E-008a | DSPy Prototype Evaluation | Week 5 | ✅ **COMPLETE** | - | 2 days |
| E-018 | LangExtract Evaluation | Phase 2.2 | 📋 Planned | MEDIUM | 2 days |

**Note on Numbering:** E-007a and E-008a were originally numbered E-007 and E-008 but were renumbered to avoid conflicts with the main model optimization track. E-018 evaluates Google's LangExtract library for structured extraction (entity extraction, optionally reflection); see `docs/research/langextract_library_review_2026-01-28.md` and `experiments/langextract_evaluation/`.

---

## Phase-by-Phase Experimental Plan

### Week 4 / Early Phase 2: Foundation (E-004, E-005)

**Goal:** Validate optimized model stack and establish baselines

**E-004: Baseline Model Performance** ⏳ READY
- **Priority:** **CRITICAL** - Required before all other experiments
- **Objective:** Measure performance of optimized stack (DeepSeek-R1-14B, Qwen3-4B, Qwen3-Coder-30B)
- **Deliverables:**
  - Success rates per model across task types
  - Latency distributions (P50, P95, P99)
  - VRAM usage and concurrency capacity
  - Quality baselines (hallucination, grounding)
- **Timeline:** 3 days
- **Success Criteria:** All models meet MVP targets (>70% success, <10s P95 latency)

**E-005: Reasoning Model Comparison** 📋 PLANNED
- **Priority:** **HIGH** - Informs final model stack decision
- **Objective:** DeepSeek-R1-14B vs Qwen3-Next-80B head-to-head
- **Key Question:** Is 14B adequate, or do we need 80B despite resource costs?
- **Deliverables:**
  - A/B test results (win rates)
  - Quality comparison
  - Resource usage comparison
  - Final recommendation for ADR-0008
- **Timeline:** 3 days
- **Decision Point:** Adopt DeepSeek-R1-14B permanently OR evaluate 32B variant

---

### Phase 2 Month 2-3: Model Optimization (E-006, E-007, E-008)

**Goal:** Optimize individual model choices and add intelligent routing

**E-006: Coding Model Evaluation** 📋 PLANNED
- **Priority:** MEDIUM - Can defer if Qwen3-Coder performing well
- **Objective:** Qwen3-Coder-30B vs Devstral 2 comparison
- **Key Question:** Is 128K context worth deployment complexity?
- **Timeline:** 3 days
- **Decision Point:** Keep Qwen only / Add Devstral as large-context option / Replace

**E-007: Three-Stage Routing** 📋 PLANNED
- **Priority:** **HIGH** - Core architectural enhancement
- **Objective:** Implement and validate MoMA-inspired routing (Classify → Select → Validate)
- **Key Metrics:**
  - % queries taking direct tool path (target: 30-40%)
  - Routing accuracy (target: >95%)
  - Validation catch rate for hallucinations (target: >80%)
- **Timeline:** 5 days (implementation + testing)
- **Success Criteria:**
  - 30%+ efficiency gain (direct tool execution)
  - No degradation in response quality
  - <200ms routing overhead

**E-008: Validation Agent Effectiveness** 📋 PLANNED
- **Priority:** MEDIUM - Quality improvement
- **Objective:** Measure impact of validation agent on output quality
- **Method:** A/B test with/without validation
- **Timeline:** 4 days
- **Success Criteria:** 10-15% reduction in hallucinations (research prediction)

---

### Phase 3 Month 4-5: Advanced Optimization (E-009, E-010, E-011)

**Goal:** Performance-based routing and resource optimization

**E-009: Performance-Based Routing** 📋 PLANNED
- **Priority:** MEDIUM
- **Objective:** Implement routing decisions based on historical performance
- **Method:**
  - Collect 1000+ routing decisions + outcomes
  - Train routing classifier on telemetry data
  - A/B test learned routing vs static routing
- **Timeline:** 1 week
- **Success Criteria:** >5% improvement in task success rate vs static routing

**E-010: Context Length Optimization** 💡 FUTURE
- **Priority:** LOW - Optimization
- **Objective:** Determine optimal context lengths per model/task type
- **Key Question:** Can we reduce context (save VRAM/latency) without quality loss?
- **Timeline:** 3 days

**E-011: Quantization Quality Study** 💡 FUTURE
- **Priority:** LOW - Research
- **Objective:** Systematic study of 4-bit vs 8-bit quantization quality
- **Method:** Same model at different quantizations, blind quality comparison
- **Timeline:** 4 days

---

### Phase 4+ Month 6+: Advanced Features (E-012+)

**Goal:** Fine-tuning and specialized optimizations

**E-012: Router Fine-Tuning** 💡 FUTURE
- **Priority:** LOW - Advanced optimization
- **Objective:** Fine-tune Qwen3-4B router on agent-specific task taxonomy
- **Prerequisites:** 1000+ routing decisions with outcomes collected
- **Timeline:** 2 weeks (data prep + training + evaluation)
- **Success Criteria:** >5% improvement in routing accuracy vs base model

**Future Experiments (TBD):**
- E-013: Multi-round routing for complex tasks
- E-014: Cost-aware routing optimization
- E-015: Routing decision caching effectiveness
- E-016: Vision/multimodal agent evaluation

---

## Experiment Dependencies

### Main Track

```
E-004 (Baseline) ⏳ READY — NEXT TO RUN
    ├─→ E-005 (Reasoning Comparison) ─→ ADR-0008 Decision
    ├─→ E-006 (Coding Comparison)
    └─→ E-007 (Three-Stage Routing)
            ├─→ E-008 (Validation Agent)
            └─→ E-009 (Performance Routing)
                    └─→ E-012 (Router Fine-Tuning)

E-010 (Context Length) ←─ E-006 findings
E-011 (Quantization) ←─ E-005 findings

E-001, E-002, E-003 (Future/Low Priority)
```

### Parallel Track

```
E-007a (Inference Server) ←─ E-004 baseline (for comparison)
E-008a (DSPy) ✅ COMPLETE (2026-01-17)
```

---

## Experiment Templates & Infrastructure

### Standard Experiment Structure

Each experiment document includes:

1. **Hypothesis** - What we're testing
2. **Objective** - What we're trying to learn/decide
3. **Method** - How we'll test it
4. **Success Criteria** - Measurable targets
5. **Data Collection** - What we'll measure and where
6. **Timeline** - Expected duration
7. **Deliverables** - Reports and artifacts
8. **Analysis Questions** - What we'll investigate
9. **Decision Matrix** - How we'll interpret results
10. **Next Steps** - Follow-on experiments

### Experiment Documentation

All experiments stored in: `./experiments/`

**Format:**
```
E-{number}-{title}.md              # Experiment specification
E-{number}-results.md              # Results and findings
E-{number}-decision.md             # Decision and rationale (if applicable)
```

**Data Storage:**
```
telemetry/evaluation/
├── benchmarks/                    # Automated benchmark results
├── ab_tests/                      # A/B test results
├── experiments/                   # Per-experiment data
│   └── E-{number}/
│       ├── raw_data/
│       ├── analysis/
│       └── visualizations/
└── reports/                       # Compiled reports
```

---

## Evaluation Framework Components

### 1. Model Benchmarks (`tests/evaluation/model_benchmarks.py`)

**Features:**
- Automated benchmark suites (math, coding, system analysis)
- Success rate calculation
- Latency profiling (min, median, P95, P99)
- Token usage tracking
- JSON result storage

**Usage:**
```bash
python tests/evaluation/model_benchmarks.py \\
    --model reasoning \\
    --suite math \\
    --runs 5
```

### 2. A/B Testing (`tests/evaluation/ab_testing.py`)

**Features:**
- Head-to-head model comparison
- LLM judge for quality assessment
- Win rate calculation
- Latency and resource comparison
- Detailed result logging

**Usage:**
```bash
python tests/evaluation/ab_testing.py \\
    --model-a reasoning \\
    --model-b reasoning_baseline \\
    --queries 50
```

### 3. Response Time Benchmarks (`tests/test_llm_client/benchmark_response_times.py`)

**Features:**
- Quick latency benchmarking
- Token generation rate measurement
- Warmup detection
- Multi-model comparison

**Usage:**
```bash
python tests/test_llm_client/benchmark_response_times.py
```

---

## Continuous Evaluation Process

### Weekly Regression Testing (Once Phase 2 starts)

```bash
# Run baseline benchmarks weekly
python tests/evaluation/model_benchmarks.py --model all --suite all --runs 3

# Compare to baseline (E-004 results)
# Flag any regressions >10%
```

### Monthly Performance Review

1. **Review telemetry:**
   - Task success rates per model
   - Average latencies
   - Error rates
   - User satisfaction (Captain's Log notes)

2. **Identify issues:**
   - Models underperforming expectations
   - Task types with low success rates
   - Latency outliers

3. **Plan experiments:**
   - Investigate root causes
   - Propose improvements
   - Design targeted experiments

---

## Key Metrics Dashboard (Future)

Track across all experiments:

| Metric | Target | Current | Trend |
|--------|--------|---------|-------|
| **Router Success Rate** | >90% | TBD | - |
| **Reasoning Success Rate** | >85% | TBD | - |
| **Coding Success Rate** | >75% | TBD | - |
| **Avg P95 Latency** | <10s | TBD | - |
| **Hallucination Rate** | <5% | TBD | - |
| **Direct Tool Exec Rate** | 30-40% | 0% | - |
| **Validation Catch Rate** | >80% | N/A | - |
| **Routing Accuracy** | >95% | N/A | - |

---

## Experiment Backlog (Beyond Phase 4)

**Research-Oriented:**
- Interleaved RAG vs single-shot retrieval
- Multi-agent debate patterns
- RL-trained routing (Router R1 style)
- Retrieval agent specialization

**Optimization-Oriented:**
- Prompt engineering systematic study
- Model-specific prompt templates
- Chain-of-thought vs direct answers
- Temperature and sampling optimization

**Infrastructure:**
- Model loading/unloading strategies
- Smart VRAM management
- Concurrent execution patterns
- Batch processing optimization

---

## Lessons Learned (To be populated)

After each experiment, document:
- What worked well
- What didn't work
- Unexpected findings
- Recommendations for future experiments
- Changes to experimental methodology

---

**Document Status:** Living Document
**Last Updated:** 2026-01-18 (numbering resolved, E-007→E-009 specs created)
**Next Review:** After E-004 and E-005 complete
**Owner:** Project Owner

---

## Quick Start

**To run your first experiment (E-004):**

1. **Start LM Studio** with optimized models loaded
2. **Run baseline benchmarks:**
   ```bash
   python tests/evaluation/model_benchmarks.py --model router --suite simple_qa --runs 5
   python tests/evaluation/model_benchmarks.py --model reasoning --suite math --runs 5
   python tests/evaluation/model_benchmarks.py --model coding --suite coding --runs 5
   ```
3. **Review results** in `telemetry/evaluation/benchmarks/`
4. **Document findings** in `experiments/E-004-results.md`
5. **Proceed to E-005** (reasoning model comparison)

**Remember:** Each experiment informs the next. Don't skip baselines!
