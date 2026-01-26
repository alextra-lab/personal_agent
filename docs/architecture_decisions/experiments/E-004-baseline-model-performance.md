# E-004: Baseline Model Performance Measurement

**Status:** Ready to Execute
**Phase:** MVP Week 4 / Early Phase 2
**Date Created:** 2025-12-31
**Prerequisites:** Optimized model stack deployed (ADR-0008), benchmark framework ready

---

## 1. Hypothesis

**H-004a:** The optimized memory stack (DeepSeek-R1-14B @ 8bit) provides adequate reasoning performance for MVP tasks.

**H-004b:** The three-role model stack (router/reasoning/coding) shows measurably different performance characteristics across task types.

---

## 2. Objective

Establish performance baselines for all three models in the optimized stack:
- **Router:** Qwen3-4B-Thinking @ 8bit
- **Reasoning:** DeepSeek-R1-Distill-Qwen-14B @ 8bit
- **Coding:** Qwen3-Coder-30B @ 8bit

Measure:
1. Task success rates across different task types
2. Latency distributions (min, median, p95, p99)
3. Token generation rates
4. VRAM usage and concurrency capacity
5. Quality metrics (hallucination rate, grounding)

---

## 3. Method

### 3.1 Benchmark Suites

Run comprehensive benchmarks using `tests/evaluation/model_benchmarks.py`:

```bash
# Router model
python tests/evaluation/model_benchmarks.py --model router --suite simple_qa --runs 5
python tests/evaluation/model_benchmarks.py --model router --suite system_analysis --runs 5

# Reasoning model
python tests/evaluation/model_benchmarks.py --model reasoning --suite math --runs 5
python tests/evaluation/model_benchmarks.py --model reasoning --suite system_analysis --runs 5

# Coding model
python tests/evaluation/model_benchmarks.py --model coding --suite coding --runs 5
```

### 3.2 Resource Monitoring

**VRAM Usage:**
- Measure peak VRAM for each model individually
- Test concurrent loading: router + reasoning, router + coding, all three
- Document cold start times

**Commands:**
```bash
# Monitor via LM Studio or system monitor
# Record: peak VRAM, average VRAM, load time
```

### 3.3 Quality Metrics

Manual review of 20 randomly selected responses per model:
- [ ] Hallucination rate (0-5 scale)
- [ ] Grounding in context (0-5 scale)
- [ ] Completeness (0-5 scale)
- [ ] Clarity (0-5 scale)

---

## 4. Success Criteria

| Metric | Target | Measurement |
|--------|--------|-------------|
| **Router Success Rate** | >90% | Simple Q&A tasks |
| **Reasoning Success Rate** | >80% | Math + system analysis |
| **Coding Success Rate** | >70% | Code generation tasks |
| **Router Latency (P50)** | <2s | Median response time |
| **Reasoning Latency (P95)** | <10s | 95th percentile |
| **Coding Latency (P95)** | <15s | 95th percentile |
| **Hallucination Rate** | <10% | Manual review |
| **Peak VRAM (all models)** | <70GB | System monitoring |
| **Concurrent Models** | ≥3 | Load test |

---

## 5. Data Collection

**Automated Metrics:**
- Store all benchmark results in `telemetry/evaluation/benchmarks/`
- Format: `{timestamp}_baseline_{model}_{suite}.json`

**Manual Evaluation:**
- Create spreadsheet: `telemetry/evaluation/baseline_quality_review.csv`
- Columns: task_id, model, response, hallucination_score, grounding_score, completeness_score, clarity_score, notes

**Resource Metrics:**
- Document in: `telemetry/evaluation/baseline_resource_usage.md`
- Include: VRAM charts, load times, concurrent capacity

---

## 6. Timeline

**Day 1 (2 hours):**
- Set up LM Studio with all 3 models
- Run quick smoke test (1 query per model)
- Verify benchmark framework works

**Day 2 (4 hours):**
- Run full benchmark suites (router, reasoning, coding)
- Document results
- Analyze latency distributions

**Day 3 (3 hours):**
- Resource monitoring and concurrent loading tests
- Manual quality review (20 responses per model)
- Compile final baseline report

---

## 7. Deliverables

1. **Baseline Benchmark Report** (`experiments/E-004-results.md`)
   - Success rates per model per suite
   - Latency distributions
   - Token generation stats
   - Resource usage summary

2. **Quality Assessment** (`experiments/E-004-quality-analysis.md`)
   - Hallucination analysis
   - Grounding analysis
   - Representative examples

3. **Baseline Comparison Table** (for future experiments)
   - Reference values for all metrics
   - "Golden" responses for regression testing

---

## 8. Analysis Questions

After collecting data, answer:

1. **Which model is fastest? Slowest? Why?**
2. **Do any models show concerning hallucination rates?**
3. **Are latencies acceptable for interactive use?**
4. **Can we load all 3 models concurrently as planned?**
5. **Do reasoning performance match published benchmarks (93.9% MATH-500)?**
6. **Are there task types where models consistently fail?**

---

## 9. Next Experiments

Based on results, proceed to:

- **E-005:** DeepSeek-R1-14B vs Qwen3-Next-80B comparison (if baseline strong)
- **E-006:** DeepSeek-R1-32B evaluation (if 14B insufficient)
- **E-007:** Router model fine-tuning (if routing accuracy issues)

---

**Experiment Owner:** Project Owner
**Expected Duration:** 3 days
**Risk Level:** Low (baseline measurement, no changes)
**Status:** ⏳ Awaiting LM Studio setup
