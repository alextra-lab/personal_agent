# E-005: Reasoning Model Comparison (DeepSeek-R1-14B vs Qwen3-Next-80B)

**Status:** Planned (pending E-004 baseline)
**Phase:** Early Phase 2
**Date Created:** 2025-12-31
**Prerequisites:** E-004 complete, both models loaded in LM Studio

---

## 1. Hypothesis

**H-005a:** DeepSeek-R1-Distill-Qwen-14B (8bit) matches or exceeds Qwen3-Next-80B (5bit) on reasoning tasks, while using 70% less VRAM.

**H-005b:** The 8-bit quantization of DeepSeek-R1-14B provides better quality than 5-bit quantization of Qwen3-Next-80B.

**H-005c:** DeepSeek-R1-14B shows 2-3x faster inference due to smaller size, making it more suitable for interactive use.

---

## 2. Objective

**Primary Goal:** Validate that DeepSeek-R1-14B is a superior choice for the `reasoning` role compared to the originally proposed Qwen3-Next-80B.

**Decision Criteria:**
- If DeepSeek-R1-14B wins on quality OR speed (with acceptable quality): **Adopt DeepSeek-R1-14B**
- If Qwen3-Next-80B significantly better quality despite speed/resource costs: **Keep 80B**
- If both inadequate: **Evaluate DeepSeek-R1-32B** (E-006)

---

## 3. Method

### 3.1 A/B Testing

Use `tests/evaluation/ab_testing.py` to compare models head-to-head:

```bash
# Configure models.yaml with both models
# reasoning: deepseek-r1-distill-qwen-14b (current)
# reasoning_baseline: qwen/qwen3-next-80b (comparison)

# Run A/B test
python tests/evaluation/ab_testing.py \\
    --model-a reasoning \\
    --model-b reasoning_baseline \\
    --queries 50
```

### 3.2 Benchmark Comparison

Run identical benchmark suites on both models:

```bash
# DeepSeek-R1-14B (already in baseline from E-004)
python tests/evaluation/model_benchmarks.py --model reasoning --suite all --runs 5

# Qwen3-Next-80B (swap config, re-run)
# Update config: reasoning -> qwen/qwen3-next-80b @ 5bit
python tests/evaluation/model_benchmarks.py --model reasoning --suite all --runs 5
```

### 3.3 Resource Profiling

**VRAM Measurement:**
- Load each model individually, measure peak VRAM
- Test concurrent scenarios:
  - DeepSeek-R1-14B + router + coding = ?
  - Qwen3-Next-80B + router + coding = ?
- Document which combinations fit in 128GB

**Latency Profiling:**
- Cold start time (model load)
- First token latency
- Token generation rate
- Total response time for identical prompts

---

## 4. Test Queries

**Math Reasoning (20 queries):**
- Arithmetic, algebra, geometry
- Word problems requiring multi-step reasoning
- Calibrated difficulty: easy (5), medium (10), hard (5)

**Logical Reasoning (15 queries):**
- If-then statements
- Pattern recognition
- Inference from facts

**System Analysis (15 queries):**
- Interpret system metrics
- Trend analysis
- Root cause reasoning
- Recommendation generation

---

## 5. Evaluation Metrics

### 5.1 Quality Metrics

| Metric | DeepSeek-R1-14B Target | Qwen3-Next-80B Target |
|--------|------------------------|------------------------|
| **Math Success Rate** | >85% | >85% |
| **Logic Success Rate** | >80% | >80% |
| **System Analysis Success** | >80% | >80% |
| **Avg Score (0-1)** | >0.80 | >0.80 |
| **Hallucination Rate** | <8% | <8% |

### 5.2 Performance Metrics

| Metric | DeepSeek-R1-14B Target | Qwen3-Next-80B Expected |
|--------|------------------------|-------------------------|
| **Peak VRAM** | 14-20GB | 50-60GB |
| **Median Latency** | <5s | <8s |
| **P95 Latency** | <10s | <15s |
| **Tokens/sec** | >30 | >20 |
| **Cold Start** | <30s | <60s |

### 5.3 Head-to-Head Win Rate

| Outcome | Threshold | Action |
|---------|-----------|--------|
| DeepSeek wins >60% | Clear winner | **Adopt DeepSeek-R1-14B** |
| Tie (45-55% each) | Equivalent | Choose based on resources: **Adopt DeepSeek** |
| Qwen wins >60% | Clear winner | Reevaluate: test 32B or keep 80B |

---

## 6. Data Collection

### 6.1 Automated Data

**Benchmark Results:**
- `telemetry/evaluation/benchmarks/{timestamp}_reasoning_deepseek-14b_{suite}.json`
- `telemetry/evaluation/benchmarks/{timestamp}_reasoning_qwen-80b_{suite}.json`

**A/B Test Results:**
- `telemetry/evaluation/ab_tests/{timestamp}_ab_reasoning_vs_reasoning_baseline.json`

### 6.2 Manual Quality Review

**Sample 30 responses:**
- 10 math problems
- 10 logic problems
- 10 system analysis queries

**Rate each response (0-5 scale):**
- Correctness
- Completeness
- Clarity
- Reasoning quality
- Grounding

**Template:** `experiments/E-005-quality-comparison.csv`

---

## 7. Timeline

**Day 1 (3 hours):**
- Configure both models in models.yaml
- Run benchmark suite on DeepSeek-R1-14B (if not from E-004)
- Run benchmark suite on Qwen3-Next-80B

**Day 2 (4 hours):**
- Run A/B test (50 queries)
- Resource profiling (VRAM, latency, concurrency)
- Cold start measurements

**Day 3 (3 hours):**
- Manual quality review (30 responses)
- Data analysis and comparison
- Write experiment report

---

## 8. Decision Matrix

```
                     Quality     Latency    VRAM      Concurrent   Decision
                     (>80%)      (<10s P95)  (<25GB)  (≥2 models)
─────────────────────────────────────────────────────────────────────────────
DeepSeek-R1-14B      ✅          ✅         ✅        ✅           ADOPT
Qwen3-Next-80B       ✅          ❌         ❌        ❌           REJECT

DeepSeek-R1-14B      ❌          ✅         ✅        ✅           TEST E-006 (32B)
Qwen3-Next-80B       ✅          ❌         ❌        ❌

DeepSeek-R1-14B      ✅          ✅         ✅        ✅           ADOPT
Qwen3-Next-80B       ✅          ✅         ❌        ❌           ADOPT (resources win)

Both                 ❌          N/A        N/A       N/A          ESCALATE
```

---

## 9. Deliverables

1. **Comparison Report** (`experiments/E-005-results.md`)
   - Side-by-side metrics
   - Win/loss breakdown
   - Resource usage comparison
   - Representative examples

2. **Decision Document** (`experiments/E-005-decision.md`)
   - Recommendation: Adopt DeepSeek / Keep Qwen / Test 32B
   - Rationale
   - Risk assessment
   - Migration plan (if applicable)

3. **Updated ADR-0008** (if decision made)
   - Update status to "Accepted" or "Rejected"
   - Document evidence and rationale

---

## 10. Analysis Questions

1. **Is DeepSeek-R1-14B's reasoning quality comparable to Qwen3-Next-80B?**
2. **How significant is the latency improvement? (quantify)**
3. **Does 8-bit quantization show advantages over 5-bit?**
4. **Can we run 3+ models concurrently with DeepSeek vs Qwen?**
5. **Are there specific task types where one model significantly outperforms?**
6. **Do reasoning tokens (thinking process) improve quality noticeably?**

---

## 11. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Both models inadequate | Low | High | Escalate to E-006 (32B model) |
| Qwen wins but unaffordable | Medium | Medium | Accept quality/resource tradeoff |
| DeepSeek underperforms benchmarks | Low | Medium | Investigate quantization, prompts |
| Benchmarks don't reflect real use | Medium | High | Supplement with real-world tasks |

---

## 12. Next Steps

**If DeepSeek-R1-14B wins:**
→ **E-007:** Coding model comparison (Qwen3-Coder vs Devstral 2)
→ **E-008:** Three-stage routing implementation

**If Qwen3-Next-80B wins:**
→ **E-006:** DeepSeek-R1-32B evaluation
→ Reconsider VRAM allocation strategy

**If both inadequate:**
→ Investigate prompt engineering
→ Consider cloud-based reasoning model for complex tasks
→ Revisit model selection criteria

---

**Experiment Owner:** Project Owner
**Expected Duration:** 3 days
**Risk Level:** Low (comparison, no production impact)
**Dependencies:** E-004 complete, Qwen3-Next-80B loaded
**Status:** ⏳ Awaiting E-004 baseline
