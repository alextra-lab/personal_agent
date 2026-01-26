# E-007: Thinking Router Model Optimization

**Status**: 🎯 Proposed for Future Testing
**Priority**: Medium
**Category**: Performance Optimization
**Related**: E-005 (Parameter Passing), E-006 (Output Format Detection)

---

## 📋 Experiment Overview

### Goal

Evaluate whether using a smaller thinking model (2-4B) for the router role can provide better performance than the current 1.7B non-thinking router, by enabling the router to handle simple queries directly while maintaining fast routing decisions.

### Hypothesis

A smaller thinking model (2-4B) for the router role will:

1. **Route queries quickly** (<5s, similar to 1.7B)
2. **Handle simple queries directly** with quality responses (unlike 1.7B)
3. **Provide a true fast path** for simple Q&A (reducing total latency)
4. **Improve overall system efficiency** by reducing unnecessary delegations

---

## 🔍 Background & Motivation

### Current State (1.7B Router)

**Strengths**:

- ✅ Very fast routing decisions (~3-4s)
- ✅ Low token usage (1,200 tokens/request)
- ✅ Accurate routing decisions with improved prompts

**Limitations**:

- ❌ Cannot handle simple queries directly (HANDLE decisions lack quality)
- ❌ All queries must be delegated (even simple ones like "Hello")
- ❌ No fast path for simple Q&A
- ❌ Router role reduced to pure classification

### Previous State (4B Thinking Router)

**Strengths**:

- ✅ Could route queries correctly
- ✅ Could handle simple queries directly with quality responses
- ✅ Provided fast path for simple Q&A

**Limitations**:

- ❌ Slower routing decisions (~17s)
- ❌ Higher token usage (2,000+ tokens/request)
- ❌ Thinking traces add overhead

### Insight

**Architectural Trade-off**:

- **1.7B Router**: Fast but limited → All queries routed (pure classifier)
- **4B Thinking Router**: Capable but slow → Fast path enabled but expensive

**Hypothesis**: A **smaller thinking model (2-4B)** might provide:

- Fast routing (faster than 4B, maybe 5-8s vs 17s)
- Quality HANDLE responses (like 4B)
- Best of both worlds: Speed + Capability

---

## 🎯 Success Criteria

### Primary Metrics

1. **Routing Latency**:
   - Target: <5s (vs 17s for 4B, vs 3-4s for 1.7B)
   - Acceptable: 5-8s if HANDLE quality justifies it

2. **HANDLE Response Quality**:
   - Target: >80% quality score (human evaluation)
   - Simple queries ("Hello", "What time is it?") should be handled directly
   - Response should be helpful and complete (not just routing JSON)

3. **Total E2E Latency**:
   - Simple queries (HANDLE): <8s total (fast path enabled)
   - Complex queries (DELEGATE): Competitive with current (1.7B router → specialized model)

4. **Token Efficiency**:
   - Routing: <1,500 tokens/request (vs 1,200 for 1.7B, 2,000+ for 4B)
   - Acceptable trade-off if HANDLE responses are high quality

### Secondary Metrics

- Routing accuracy (should remain >90%)
- Confidence scores (should remain high)
- Cost per request (if using cloud models)
- Model availability/compatibility

---

## 🧪 Methodology

### Models to Test

1. **Baseline**: `qwen/qwen3-4b-2507` (current router)
2. **Previous**: `qwen/qwen3-1.7b` (reference)
3. **Candidates** (if available):
   - Smaller thinking models (2-3B)
   - 4B thinking models with optimized prompts
   - Other thinking models that might be faster

### Test Queries

**Category 1: Simple Queries (Should HANDLE)**

- "Hello"
- "Hi, how are you?"
- "What time is it?"
- "Thanks"
- "Goodbye"

**Category 2: Simple Factual (Should HANDLE or DELEGATE?)**

- "What is 2+2?"
- "What is the capital of France?"
- "How many days in a week?"

**Category 3: Explanation Queries (Should DELEGATE)**

- "What is Python?"
- "Explain quantum physics"
- "How does a computer work?"

**Category 4: Code Queries (Should DELEGATE)**

- "Write a Python function to calculate factorial"
- "Debug this code: ..."
- "How do I implement X in Python?"

**Total**: 20-30 queries across categories

### Testing Procedure

1. **A/B Testing**:
   - Run same queries through 1.7B router (baseline)
   - Run same queries through candidate thinking router
   - Compare routing decisions, latency, and response quality

2. **Quality Evaluation**:
   - Human evaluation of HANDLE responses (1-5 scale)
   - Routing decision accuracy (correct HANDLE vs DELEGATE)
   - Response completeness and helpfulness

3. **Performance Benchmarks**:
   - Measure routing latency (time to routing decision)
   - Measure total E2E latency (query → response)
   - Measure token usage (input + output tokens)
   - Measure routing overhead (time between router completion and delegation)

### Variables to Control

- Same prompt templates (use current `ROUTER_SYSTEM_PROMPT_BASIC`)
- Same model configuration (timeout, context length, etc.)
- Same test environment (LM Studio, same hardware)
- Same test queries (exact same strings)

---

## 📊 Expected Outcomes

### Scenario 1: Smaller Thinking Model Works Well

**Outcome**: 2-3B thinking model provides:

- Routing latency: 5-8s (acceptable, <5s ideal)
- HANDLE quality: >80% (good)
- Fast path enabled: Simple queries <8s total

**Decision**: Switch to smaller thinking model for router role

### Scenario 2: No Viable Smaller Thinking Model

**Outcome**: Available thinking models are:

- Too slow (>10s routing latency)
- Too large (high token usage)
- Poor quality (HANDLE responses <70%)

**Decision**: Keep 1.7B router, accept limitation that router cannot handle queries directly

### Scenario 3: 4B Thinking Model with Optimized Prompts

**Outcome**: 4B thinking model with prompt optimization:

- Routing latency: 10-12s (better than 17s, but still slow)
- HANDLE quality: >90% (excellent)
- Acceptable trade-off

**Decision**: Consider 4B thinking with optimized prompts if no smaller options

---

## 🔬 Experiment Design

### Phase 1: Model Discovery (1-2 days)

**Goal**: Identify available smaller thinking models (2-4B)

**Tasks**:

- Research available thinking models in 2-4B range
- Check LM Studio compatibility
- Identify quantization options (4bit, 8bit)
- Document model specifications

**Deliverables**:

- List of candidate models with specs
- Compatibility assessment
- Estimated routing latency (based on model size)

### Phase 2: Baseline Testing (1 day)

**Goal**: Establish baseline metrics with current 1.7B router

**Tasks**:

- Run test query suite through 1.7B router
- Collect routing decisions, latency, token usage
- Document HANDLE response quality (should be poor/absent)
- Establish baseline for comparison

**Deliverables**:

- Baseline metrics report
- Test results dataset

### Phase 3: Candidate Testing (2-3 days)

**Goal**: Test candidate thinking models

**Tasks**:

- Test each candidate model with test query suite
- Measure routing latency, token usage, response quality
- Evaluate HANDLE response quality (human evaluation)
- Compare against baseline

**Deliverables**:

- Candidate comparison report
- Performance metrics table
- Quality evaluation scores

### Phase 4: Analysis & Decision (1 day)

**Goal**: Analyze results and make recommendation

**Tasks**:

- Compare all candidates against baseline
- Identify best candidate (if any)
- Document trade-offs
- Make recommendation (switch/keep/optimize)

**Deliverables**:

- Analysis report
- Recommendation document
- Updated router prompt (if switching)

---

## 📈 Metrics & KPIs

### Routing Performance

| Metric | Baseline (1.7B) | Target (Thinking) | Measurement |
|--------|----------------|-------------------|-------------|
| Routing latency (p50) | 3-4s | <5s | Time to routing decision |
| Routing latency (p95) | 5s | <8s | Time to routing decision |
| Token usage | 1,200 | <1,500 | Input + output tokens |
| Routing accuracy | >90% | >90% | Correct HANDLE vs DELEGATE |

### HANDLE Response Quality

| Metric | Baseline (1.7B) | Target (Thinking) | Measurement |
|--------|----------------|-------------------|-------------|
| HANDLE response quality | <50% (poor) | >80% | Human evaluation (1-5) |
| Response completeness | 20% | >90% | % of queries with complete responses |
| Response helpfulness | 30% | >80% | Human evaluation (1-5) |

### End-to-End Performance

| Metric | Baseline (1.7B) | Target (Thinking) | Measurement |
|--------|----------------|-------------------|-------------|
| Simple query latency | 17s (routed) | <8s (HANDLE) | Total time (query → response) |
| Complex query latency | 20-35s | 20-35s | Total time (query → response) |
| Fast path enabled | ❌ No | ✅ Yes | % of simple queries HANDLED |

---

## 🎓 Lessons Learned

### Key Insight

**Router Capability Spectrum**:

- **1.7B (current)**: Fast classifier, cannot handle queries
- **2-4B thinking (candidate)**: Fast classifier + simple Q&A handler
- **4B thinking (previous)**: Capable but slow

**Architectural Decision**:

- If thinking router works: Router = Fast classifier + Simple Q&A handler (fast path enabled)
- If thinking router doesn't work: Router = Pure classifier (all queries routed, no fast path)

### Trade-offs

**Speed vs Capability**:

- Faster models: Better routing latency, but limited HANDLE capability
- Thinking models: Better HANDLE capability, but slower routing

**Optimal Point**: Find smallest thinking model that provides acceptable routing speed (<5s) while enabling quality HANDLE responses (>80%)

---

## 📚 References

- **Current Implementation**: `src/personal_agent/orchestrator/prompts.py`
- **Model Config**: `config/models.yaml`
- **Performance Comparison**: `../plans/ROUTER_MODEL_PERFORMANCE_COMPARISON.md`
- **Router Fix**: `../plans/ROUTER_1.7B_FIX_SUMMARY.md`
- **Related Experiments**:
  - E-005: Parameter Passing Evaluation
  - E-006: Output Format Detection

---

## ✅ Success Criteria Summary

**Experiment is successful if**:

1. ✅ Candidate thinking model routes queries in <5s (ideally) or <8s (acceptable)
2. ✅ HANDLE response quality >80% (human evaluation)
3. ✅ Simple queries can use fast path (<8s total latency)
4. ✅ Routing accuracy remains >90%
5. ✅ Token usage <1,500 tokens/request

**Experiment suggests optimization if**:

- Candidate model meets all success criteria
- Performance improvement justifies model switch
- Fast path provides measurable user experience improvement

**Experiment suggests keeping current model if**:

- No candidate model meets success criteria
- Performance trade-offs not justified
- Fast path doesn't provide significant benefit

---

**Status**: 🎯 Ready for future testing when:

- Thinking models in 2-4B range become available
- Performance optimization becomes priority
- Router capability limitations become user-facing issue
