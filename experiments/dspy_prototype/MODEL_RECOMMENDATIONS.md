# Model Stack Analysis & Recommendations

**Date**: 2026-01-17
**Context**: Post-DSPy evaluation, reviewing model stack for optimal performance
**Hardware**: Apple Silicon (M-series) with LM Studio

---

## Current Model Stack

### Active Models

| Role | Current Model | Size | Quant | Context | Timeout | Performance |
|------|--------------|------|-------|---------|---------|-------------|
| **Router** | `qwen/qwen3-1.7b` | 1.7B | 8bit | 8K | 10s | Fast (~3-4s typical) |
| **Standard** | `qwen/qwen3-4b-2507` | 4B | 8bit | 40K | 45s | Moderate (tool use) |
| **Reasoning** | `qwen/qwen3-8b` | 8B | 8bit | 32K | 60s | 11-15s (DSPy tests) |
| **Coding** | `mistralai/devstral-small-2-2512` | ~22B | 8bit | 32K | 45s | Moderate-slow |

### Experimental Models (configured but not active)

- `qwen/qwen3-next-80b` (4bit/5bit) - Heavy reasoning/baseline
- Alternative endpoints commented out

---

## DSPy Evaluation Insights

From E-008 test results, key findings affecting model selection:

### Latency Observations

1. **Router (qwen3-1.7b)**: 3-4s typical, very fast
   - Test Case B: 3,045ms average (manual), 3,900ms (DSPy)
   - ✅ **Performance**: Excellent for routing decisions

2. **Reasoning (qwen3-8b)**: 11-15s typical
   - Test Case A: 11,835ms average (manual), 14,337ms (DSPy)
   - ✅ **Performance**: Acceptable for structured outputs

3. **Standard (qwen3-4b-2507)**: 2-3s typical
   - Test Case C: 2,776ms average (manual tool use)
   - ✅ **Performance**: Good for tool orchestration

### Key Insights

- ✅ **Qwen3 models perform well** across all test cases
- ✅ **Function calling support is critical** (all Qwen3 models have native support)
- ⚠️ **Reasoning model is slow but necessary** for complex tasks
- ⚠️ **Coding model not tested** in DSPy evaluation

---

## Benchmark Comparison: Qwen3 vs Alternatives

### Small Models (1-4B): Router & Standard Roles

| Model | Size | Math (MATH 500) | GSM-SYM p2 | Robustness | Memory (8bit) | Function Calling |
|-------|------|----------------|------------|------------|---------------|------------------|
| **Qwen3-1.7B** ⭐ | 1.7B | **84.57%** | **71.11%** | –6.67% drop | ~2-3 GB | ✅ Native |
| Qwen3-4B | 4B | **91.37%** | 83.10% | –8.89% drop | ~5-6 GB | ✅ Native |
| Phi-4 mini | 3.84B | 88.60% | **85.31%** | –12.22% drop | ~5-6 GB | ⚠️ Limited |
| Llama 3.2-3B | 3B | ~75% | ~65% | –15% drop | ~4-5 GB | ⚠️ Limited |
| SmolLM3-3B | 3B | ~70% | ~60% | N/A | ~4-5 GB | ❌ No |

**Verdict**: ✅ **Qwen3 models are best-in-class** for small model reasoning and function calling

### Mid-Size Models (8B): Reasoning Role

| Model | Size | Math (MATH 500) | Reasoning Quality | Memory (8bit) | Function Calling |
|-------|------|----------------|-------------------|---------------|------------------|
| **Qwen3-8B** ⭐ | 8B | **~93%** (est.) | Excellent | ~10-12 GB | ✅ Native |
| Llama-3-8B | 8B | ~85% | Very Good | ~10-12 GB | ⚠️ Limited |
| Phi-4 (14B) | 14.7B | **~95%** | Excellent | ~18-20 GB | ⚠️ Limited |

**Verdict**: ✅ **Qwen3-8B is optimal** for 8B reasoning with function calling

---

## MLX-Optimized Alternatives (Apple Silicon)

### Worth Testing

| Model | Size | Type | Best For | Quantization | MLX Ready |
|-------|------|------|----------|--------------|-----------|
| **LFM2-8B-A1B** | 8B (1B active) | MoE | Fast reasoning, structured outputs | 6bit/8bit | ✅ Yes |
| **MiniCPM4-8B** | 8B | Dense | General purpose, fast generation | 4bit/8bit | ✅ Yes |
| **Granite-4.0-H-Tiny** | ~7B (1B active) | MoE | Long context, structured extraction | 6bit | ✅ Yes |

### Not Recommended (for now)

- SmolLM variants: Too small for reasoning tasks
- SmolVLM: Vision-language, not needed yet
- Devstral-Small-4bit: Current Devstral is sufficient

---

## Recommendations

### Option A: Keep Current Stack ✅ (Recommended)

**Verdict**: **No changes needed** - Current stack is well-optimized

**Rationale**:
1. ✅ **Qwen3 models are best-in-class** for function calling and reasoning
2. ✅ **DSPy evaluation validates performance** - latencies are acceptable
3. ✅ **Native function calling critical** for orchestrator (Test Case C showed DSPy ReAct limitations)
4. ✅ **8bit quantization appropriate** - balances quality and memory
5. ✅ **Context lengths optimized** - 8K/40K/32K are appropriate for use cases

**Performance Evidence**:
- Router: 3-4s (fast enough for routing decisions)
- Standard: 2-3s (fast enough for tool orchestration)
- Reasoning: 11-15s (acceptable for complex structured outputs)

**Only Concern**: Coding model (`devstral-small-2-2512`) not tested in DSPy evaluation - see Option B

---

### Option B: Test Coding Model Performance ⚠️ (Optional)

**Issue**: `mistralai/devstral-small-2-2512` not tested in DSPy evaluation

**Recommendation**: Create Test Case D to evaluate coding model

**Test Plan**:
1. Code generation task (5 queries: bug fixes, refactoring, implementation)
2. Compare latency and code quality
3. Measure function calling support (currently disabled: `supports_function_calling: false`)

**Alternative Coding Models** (if Devstral underperforms):
- `qwen/qwen3-coder-30b` (commented out in models.yaml)
- `codellama/CodeLlama-13b-Instruct` (if available in LM Studio)
- Keep Devstral if performance is acceptable

---

### Option C: Experiment with MLX-Optimized MoE ⚠️ (Future)

**Motivation**: MoE models offer better efficiency (fewer active parameters per token)

**Candidates**:
1. **LFM2-8B-A1B** (8B total, 1B active) - 6bit or 8bit
   - Pros: Faster than dense 8B, good instruction following
   - Cons: May require MLX-specific tooling, not tested with LM Studio

2. **Granite-4.0-H-Tiny** (~7B total, 1B active) - 6bit
   - Pros: Excellent structured outputs, long context
   - Cons: Unknown LM Studio compatibility

**Recommendation**: ⚠️ **Defer to post-MVP** (Week 6+)
- Current Qwen3 stack is working well
- MoE models need compatibility testing with LM Studio
- Risk/benefit doesn't justify change during MVP

---

### Option D: Optimize Quantization (Not Recommended)

**Consideration**: Could reduce quantization for speed (6bit or 4bit)

**Analysis**:
- Current 8bit is already fast enough (DSPy tests show acceptable latencies)
- Lower quantization risks quality degradation
- Memory savings minimal on M-series hardware
- ADR-0008 explicitly chose 8bit for quality

**Verdict**: ❌ **Do not change quantization** - 8bit is optimal balance

---

## Specific Recommendations by Role

### Router: qwen/qwen3-1.7b ✅ Keep

**Performance**: Excellent (3-4s routing decisions)
**Alternatives Considered**:
- Phi-4 mini (3.84B): Better math but slower, no clear routing advantage
- SmolLM3-3B: Smaller but no function calling

**Verdict**: ✅ **Keep qwen3-1.7b** - optimal for fast routing with function calling

---

### Standard: qwen/qwen3-4b-2507 ✅ Keep

**Performance**: Good (2-3s tool orchestration, Test Case C: 2,776ms)
**Alternatives Considered**:
- Qwen3-4B (non-2507): Possibly older version, unclear advantage
- Phi-4 mini: Better math (91% vs 88%) but no native function calling

**Verdict**: ✅ **Keep qwen3-4b-2507** - instruct model with excellent tool orchestration

**Note**: Confirmed `qwen3-4b-2507` IS the instruct/non-thinking model (no separate "-Instruct" variant)

---

### Reasoning: qwen/qwen3-8b ✅ Keep

**Performance**: Acceptable (11-15s, Test Case A: 11,835ms)
**Alternatives Considered**:
- LFM2-8B-A1B: Potentially faster (MoE) but untested with LM Studio
- Phi-4 (14.7B): Better math (95%) but much slower and 2x memory
- Llama-3-8B: No clear advantage, limited function calling

**Verdict**: ✅ **Keep qwen3-8b** - best balance of quality, speed, and function calling

---

### Coding: mistralai/devstral-small-2-2512 ⚠️ Test

**Performance**: Unknown (not tested in DSPy evaluation)
**Concern**: `supports_function_calling: false` limits tool use in code generation

**Recommendation**: ⚠️ **Create Test Case D** to evaluate coding model
- Test 5 coding queries (bug fixes, refactoring, implementation)
- Measure latency and code quality
- Compare with `qwen/qwen3-coder-30b` if available

**Alternative**: Consider `qwen/qwen3-coder-30b` (commented out in models.yaml)
- Pros: Native function calling, Qwen ecosystem consistency
- Cons: Larger (30B vs 22B), may be slower

---

## Implementation Plan

### Immediate Actions ✅

1. ✅ **No model changes needed** - current stack is optimal
2. ⚠️ **Optional: Test coding model** (Test Case D)
   - Create `test_case_d_coding.py`
   - Compare Devstral vs Qwen3-Coder
   - Measure latency and quality

### Future Considerations (Post-MVP)

1. **Experiment with MLX-optimized MoE** (Week 6+)
   - Test LFM2-8B-A1B for reasoning role
   - Evaluate Granite-H-Tiny for structured outputs
   - Requires LM Studio compatibility testing

2. **Monitor model releases**
   - Qwen3 updates (new versions, optimizations)
   - New MLX-optimized models
   - Better coding models

3. **Evaluate DSPy optimizers** (Week 6+)
   - MIPROv2 for reflection quality improvement
   - Requires labeled data for optimization

---

## Key Insights

### Why Current Stack is Optimal

1. ✅ **Qwen3 ecosystem consistency**: All core models (router, standard, reasoning) use Qwen3
   - Consistent behavior, prompt engineering
   - Native function calling across the board
   - Best-in-class math/reasoning for size

2. ✅ **8bit quantization sweet spot**: ADR-0008 decision validated
   - Fast enough (DSPy tests show acceptable latencies)
   - High quality (zero parse failures in tests)
   - Memory efficient (10-15GB total for active models)

3. ✅ **Context lengths appropriate**: 8K/40K/32K
   - Router: 8K sufficient for routing decisions
   - Standard: 40K supports long tool conversations
   - Reasoning: 32K balances quality and speed

4. ✅ **Native function calling critical**: Test Case C showed importance
   - DSPy ReAct had +237% latency overhead
   - Manual orchestrator with native function calling is faster
   - Qwen3 native support enables efficient tool use

### What DSPy Evaluation Revealed

1. **Model performance is good**: Zero parse failures, acceptable latencies
2. **Function calling is critical**: Cannot compromise on this for orchestrator
3. **Reasoning model slowness is acceptable**: 11-15s for complex structured outputs is fine
4. **Coding model needs testing**: Only gap in current evaluation

---

## Conclusion

**Recommendation**: ✅ **Keep current model stack** with optional coding model testing

**Confidence**: High (95%)
- DSPy evaluation validates performance
- Qwen3 models are best-in-class for function calling
- 8bit quantization is optimal balance
- No clear alternatives offer significant improvement

**Only Action Item**: ⚠️ **Optional Test Case D** for coding model evaluation

**Future**: Consider MLX-optimized MoE models (LFM2, Granite) post-MVP for potential speed improvements
