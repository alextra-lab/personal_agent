# E-006: Coding Model Evaluation (Qwen3-Coder vs Devstral 2)

**Status:** Planned (Phase 2)
**Phase:** Phase 2 Month 3
**Date Created:** 2025-12-31
**Prerequisites:** E-004 baseline complete, Devstral 2 loaded

---

## 1. Hypothesis

**H-006a:** Qwen3-Coder-30B (32K context) is sufficient for most coding tasks in MVP and Phase 2.

**H-006b:** Devstral-2-2512 (128K context, 6bit quant) shows advantages only for large codebase analysis requiring >32K tokens.

**H-006c:** The 1% absolute improvement in SWE-Bench (56.40% vs 55.40%) is not significant enough to warrant switching models.

---

## 2. Objective

Determine if Devstral 2 offers meaningful advantages over Qwen3-Coder-30B to justify:
- Additional VRAM usage (32GB vs 30GB)
- Learning curve with new model
- Potential deployment complexity

**Decision:** Keep Qwen3-Coder-30B OR add Devstral 2 as `coding_large_context` role.

---

## 3. Method

### 3.1 Benchmark Comparison

```bash
# Qwen3-Coder-30B (current)
python tests/evaluation/model_benchmarks.py --model coding --suite coding --runs 5

# Devstral 2 (swap config)
# Update config: coding -> mistralai/devstral-small-2-2512
python tests/evaluation/model_benchmarks.py --model coding --suite coding --runs 5
```

### 3.2 Context Length Stress Test

Create test cases specifically designed to stress context length:

**Small Context (<10K tokens):**
- Single function implementation
- Bug fix in isolated module
- Unit test generation

**Medium Context (10-30K tokens):**
- Multi-file refactoring
- Class hierarchy understanding
- Cross-file dependency analysis

**Large Context (30-100K tokens):**
- Entire small codebase analysis
- Large-scale refactoring
- Architecture understanding

**Evaluation:**
- Does Qwen3-Coder-30B hit context limits?
- Does Devstral 2's 128K context provide better understanding?

### 3.3 A/B Testing

```bash
python tests/evaluation/ab_testing.py \\
    --model-a coding \\
    --model-b coding_large_context \\
    --queries 30
```

Use real coding tasks from personal_agent codebase:
- Implement feature X
- Refactor module Y
- Find bugs in Z
- Generate tests for W

---

## 4. Test Suite

**Basic Coding (15 tasks):**
- Function implementation
- Algorithm coding
- Data structure manipulation
- Simple bug fixes

**SWE-Style Tasks (10 tasks):**
- Read code, understand intent
- Propose solution to issue
- Implement fix
- Explain changes

**Large Context Tasks (5 tasks):**
- Analyze entire module (>30K tokens)
- Propose architectural changes
- Cross-file refactoring
- Dependency graph analysis

---

## 5. Success Criteria

| Metric | Qwen3-Coder Target | Devstral 2 Target | Winner If... |
|--------|-------------------|-------------------|--------------|
| **Basic Coding Success** | >80% | >80% | Tie unless quality diff >10% |
| **SWE Task Success** | >60% | >65% | Devstral if +5% or more |
| **Large Context Success** | >40% | >70% | Devstral shows clear advantage |
| **Median Latency** | <5s | <5s | Tie if within 20% |
| **Peak VRAM** | ~30GB | ~32GB | Qwen if Devstral >35GB |
| **Context Limit Hits** | Acceptable if <10% | Should be 0% | Qwen if low, Devstral if Qwen high |

---

## 6. Decision Matrix

```
Scenario                              Context    Quality   Latency  Decision
                                      Advantage  Advantage Penalty
────────────────────────────────────────────────────────────────────────────
Devstral wins large context           ✅         ✅        ❌       ADD as coding_large_context
Qwen wins most tasks                  ❌         ✅        ✅       KEEP Qwen3-Coder-30B only
Tie on quality, Devstral slower       ~          ~         ❌       KEEP Qwen (simpler)
Devstral significantly better all     ✅         ✅        ~        REPLACE with Devstral

Large context rarely needed           N/A        ~         ~        KEEP Qwen (defer Devstral)
Large context critical for Phase 3    ✅         N/A       N/A      ADD Devstral for future
```

---

## 7. Timeline

**Day 1 (3 hours):**
- Load Devstral 2 in LM Studio
- Run basic coding benchmark on both
- Compare success rates and latency

**Day 2 (4 hours):**
- Context length stress tests
- Identify where Qwen3-Coder hits limits
- Test Devstral on same large-context tasks

**Day 3 (3 hours):**
- A/B testing on real codebase tasks
- Manual quality review
- Compile comparison report

---

## 8. Deliverables

1. **Comparison Report** (`experiments/E-006-results.md`)
   - Benchmark results side-by-side
   - Context length analysis
   - VRAM and latency comparison
   - A/B test win rates

2. **Context Length Analysis** (`experiments/E-006-context-analysis.md`)
   - When does 32K context become limiting?
   - Real-world usage patterns from telemetry
   - Projection: % of tasks needing >32K

3. **Decision Document** (`experiments/E-006-decision.md`)
   - Recommendation: Keep Qwen / Add Devstral / Replace with Devstral
   - Cost-benefit analysis
   - Implementation plan if adding

---

## 9. Analysis Questions

1. **How often do real coding tasks exceed 32K tokens?**
2. **Does Devstral 2's 128K context meaningfully improve code understanding?**
3. **Is the +1% SWE-Bench improvement observable in practice?**
4. **Can we defer large-context support to Phase 3 without impact?**
5. **What's the latency/VRAM tradeoff for 128K context?**

---

## 10. Possible Outcomes

**Outcome A: Keep Qwen3-Coder-30B only**
- Qwen performs well, large context rarely needed
- Simpler deployment, lower VRAM
- Monitor for context issues in Phase 2-3

**Outcome B: Add Devstral 2 as dual coding strategy**
```yaml
models:
  coding:
    id: "qwen/qwen3-coder-30b"
    context_length: 32768
  coding_large_context:
    id: "mistralai/devstral-small-2-2512"
    context_length: 128000
```
- Router decides based on context requirements
- Enables large codebase analysis when needed

**Outcome C: Replace with Devstral 2**
- Devstral significantly better across the board
- Worth the slight VRAM increase
- 128K context enables future features

---

## 11. Next Experiments

**If keeping Qwen3-Coder:**
→ E-008: Three-stage routing implementation

**If adding Devstral:**
→ E-007: Routing logic for context-based model selection
→ E-009: Large codebase analysis benchmark

**If replacing with Devstral:**
→ E-010: Devstral optimization (quantization, prompts)

---

**Experiment Owner:** Project Owner
**Expected Duration:** 3 days
**Risk Level:** Low
**Dependencies:** E-004 baseline, Devstral 2 model loaded
**Status:** ⏳ Planned for Phase 2 Month 3
