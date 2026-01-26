# ADR-0008: Model Stack Course Correction Based on December 2025 Research

**Status:** Proposed
**Date:** 2025-12-31
**Decision Owner:** Project Owner
**Supersedes:** Portions of ADR-0003 (Model Stack)
**Research Basis:** Perplexity deep research analysis, documented in `../research/model_orchestration_research_analysis_2025-12-31.md`

---

## 1. Context

Since ADR-0003 was written (2025-12-28), comprehensive research into current model performance benchmarks and routing architectures has revealed specific opportunities to optimize our model stack while maintaining our core architectural principles.

**Key Research Findings:**

1. **DeepSeek-R1-Distill models** (released January 2025) show superior reasoning performance compared to Qwen3-Next-80B
2. **Qwen3-4B** (already adopted) is validated as optimal router choice
3. **5-bit quantization** of reasoning model is suboptimal; research emphasizes 4-bit or 8-bit
4. **MoMA and LLMRouter frameworks** provide validated patterns for intelligent routing
5. **M4 Max 128GB** can support **concurrent model loading** with smaller reasoning model

**Previous Implementation (before ADR-0008):**

```yaml
models:
  router:
    id: "qwen/qwen3-1.7b"
    quantization: "8bit"

  reasoning:
    id: "qwen/qwen3-next-80b"
    quantization: "5bit"  # ⚠️ Suboptimal

  coding:
    id: "qwen/qwen3-coder-30b"
    quantization: "8bit"
```

**Current Implementation (after ADR-0008 and subsequent updates):**

```yaml
models:
  router:
    id: "qwen/qwen3-4b-2507"
    quantization: "8bit"

  reasoning:
    id: "deepseek-r1-distill-qwen-14b"
    quantization: "8bit"

  coding:
    id: "mistralai/devstral-small-2-2512"
    quantization: "8bit"
```

**This ADR proposes:**

1. **Reasoning model replacement:** Qwen3-Next-80B (5bit) → DeepSeek-R1-Distill-Qwen-14B (8bit)
2. **Context length optimization:** Reasoning model 128K → 32K (MVP), expand to 128K in Phase 2
3. **Addition of specialized models:** Validation and summarization roles (Phase 2)
4. **Explicit routing strategy:** Implement MoMA-inspired three-stage routing (Phase 2-3)

---

## 2. Decision

### 2.1 Reasoning Model Replacement (Immediate - MVP or Phase 1)

**Replace:**
```yaml
reasoning:
  id: "qwen/qwen3-next-80b"
  quantization: "5bit"
  max_concurrency: 1
```

**With:**
```yaml
reasoning:
  id: "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B"
  quantization: "8bit"
  context_length: 32768    # Reduced from 128K for MVP
  max_concurrency: 2       # Increased from 1 (smaller footprint)
  default_timeout: 60
```

**Rationale:**

| Metric | Qwen3-Next-80B @ 5bit | DeepSeek-R1-14B @ 8bit | Improvement |
|--------|----------------------|------------------------|-------------|
| **MATH-500 Accuracy** | Unknown | 93.9% | ✅ Validated performance |
| **GPQA Diamond** | Unknown | 59.1% | ✅ Validated performance |
| **LiveCodeBench** | Unknown | 53.1% | ✅ Strong coding reasoning |
| **Codeforces Rating** | Unknown | 1481 | ✅ Competitive programming |
| **VRAM (8bit)** | ~50-60GB | 14-20GB | ✅ **40GB freed** |
| **Quantization Quality** | 5-bit (degraded) | 8-bit (excellent) | ✅ Better quality |
| **Max Concurrency** | 1 | 2+ | ✅ Enables parallelism |
| **Inference Speed** | Slower (80B) | Faster (14B) | ✅ Lower latency |

**Benefits:**

1. ✅ **Superior validated performance** on reasoning benchmarks
2. ✅ **Better quantization quality** (8-bit vs 5-bit)
3. ✅ **40GB VRAM savings** enables concurrent model loading
4. ✅ **Faster inference** due to smaller size
5. ✅ **Higher concurrency** (2x models loaded simultaneously)
6. ✅ **Validated on coding tasks** (LiveCodeBench, Codeforces)

**Migration Path:**

```bash
# Phase 1: Test DeepSeek-R1-14B locally
1. Download and load DeepSeek-R1-Distill-Qwen-14B in LM Studio
2. Run benchmark suite (tests/evaluation/model_benchmarks.py)
3. Compare performance against Qwen3-Next-80B baseline

# Phase 2: A/B Testing (Week 4 or early Phase 2)
1. Configure both models in models.yaml
2. Run 50 test queries through each
3. Measure: accuracy, latency, VRAM usage, quality
4. Document results in ./experiments/E-004-reasoning-model-comparison.md

# Phase 3: Switchover (if A/B tests pass)
1. Update config/models.yaml to DeepSeek-R1-14B as primary
2. Keep Qwen3-Next-80B as fallback (reasoning_heavy role)
3. Monitor telemetry for regressions
4. Deprecate 80B model after 2 weeks if no issues
```

---

### 2.2 Router Model Validation (Updated to Qwen3-4B-2507)

**Current:**
```yaml
router:
  id: "qwen/qwen3-4b-2507"
  quantization: "8bit"
```

**Decision:** ✅ **UPDATED** from Qwen3-1.7B to Qwen3-4B-2507 (per research validation)

**Validation from Research:**

> Qwen3-4B delivers the strongest results after fine-tuning among small language models, with an average rank of 2.25 across multiple benchmarks. It outperforms Qwen3-8B in distillation tasks.

**Action:** No change needed. Consider fine-tuning in Phase 4.

---

### 2.3 Coding Model Validation (Updated to Devstral Small 2)

**Current:**
```yaml
coding:
  id: "mistralai/devstral-small-2-2512"
  quantization: "8bit"
  context_length: 32768
```

**Decision:** ✅ **UPDATED** from Qwen3-Coder-30B to Devstral-Small-2-2512 (per evaluation)

**Rationale:**

- Qwen3-Coder-30B: 55.40% SWE-Bench Verified (strong)
- Devstral 2: 56.40% SWE-Bench Verified (+1% absolute)
- Context: Qwen 32K vs Devstral 128K (significant for large codebases)

**Phase 2 Evaluation Plan:**

```yaml
# CURRENT: Devstral Small 2 is primary coding model
# PROPOSED: Add Qwen3-Coder-30B as alternative for large context if needed
models:
  coding:
    id: "mistralai/devstral-small-2-2512"
    context_length: 32768
    quantization: "8bit"

  coding_large_context:  # Future: if 128K context needed
    id: "qwen/qwen3-coder-30b"
    context_length: 128000
    quantization: "8bit"
    max_concurrency: 1
    default_timeout: 60
```

**Usage Pattern:**

```python
# Orchestrator logic
if estimated_code_context_size > 20000:  # >20K tokens
    model_role = ModelRole.CODING_LARGE_CONTEXT
else:
    model_role = ModelRole.CODING
```

---

### 2.4 Context Length Optimization

**Current Configuration:**

```yaml
reasoning:
  context_length: 128000  # Excessive for MVP
```

**Proposed Configuration:**

```yaml
# MVP (Phase 1)
reasoning:
  context_length: 32768   # Sufficient for most reasoning tasks

# Phase 2+ (when document analysis added)
reasoning:
  context_length: 128000  # Expand for long documents
```

**Rationale:**

- MVP tasks rarely need >32K tokens
- 128K context = higher VRAM + slower inference
- Research shows 32K sufficient for most reasoning scenarios
- Can expand in Phase 2 when use cases emerge

**Impact on M4 Max Resource Usage:**

| Context Length | VRAM Impact | Inference Speed | Use Cases |
|----------------|-------------|----------------|-----------|
| 8K | Minimal | Fastest | Router, classification |
| 32K | Moderate | Fast | Standard reasoning, most code |
| 128K | Significant | Slower | Long documents, large codebases |

---

### 2.5 Addition of Specialized Model Roles (Phase 2)

**Current:** 3 roles (router, reasoning, coding)

**Proposed (Phase 2):**

```yaml
models:
  router:
    id: "qwen/qwen3-4b-2507"
    quantization: "8bit"

  reasoning:
    id: "deepseek-r1-distill-qwen-14b"
    quantization: "8bit"

  coding:
    id: "mistralai/devstral-small-2-2512"
    quantization: "8bit"

  # NEW: Specialized roles
  summarization:
    id: "qwen/qwen3-1.7b-instruct"  # Lightweight, fast
    quantization: "8bit"
    context_length: 32768
    max_concurrency: 4
    default_timeout: 10

  validation:
    id: "qwen/qwen3-4b-2507"  # Reuse router model
    quantization: "8bit"
    context_length: 16384  # Smaller context for validation
    max_concurrency: 3
    default_timeout: 15
```

**Rationale:**

**Summarization Model (Qwen3-1.7B):**
- Research finding: "Fine-tuning 1.5B-3B models for domain-specific summarization significantly reduces latency vs routing through larger models"
- Use case: Summarizing tool outputs, telemetry logs, long conversations
- Benefit: Fast, efficient, always available

**Validation Model (Reuse Qwen3-4B):**
- Research finding: "Critical/validation agent acts as quality assurance layer"
- Use case: Check LLM outputs for hallucinations, policy violations, format errors
- Benefit: Reuses router model (no additional VRAM), fast validation

**Python Implementation:**

```python
class ModelRole(str, Enum):
    ROUTER = "router"
    REASONING = "reasoning"
    CODING = "coding"
    SUMMARIZATION = "summarization"  # NEW
    VALIDATION = "validation"  # NEW

# Validation usage
async def step_validation(ctx: ExecutionContext) -> TaskState:
    """Validate LLM output before returning to user."""

    validation_result = await llm_client.respond(
        role=ModelRole.VALIDATION,
        messages=[
            {"role": "system", "content": VALIDATION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": format_validation_request(
                    query=ctx.user_message,
                    response=ctx.final_reply,
                    tool_results=ctx.tool_results
                )
            }
        ]
    )

    if validation_result.content.startswith("INVALID"):
        logger.warning("validation_failed", reason=validation_result.content)
        # Retry with corrective prompt
        return TaskState.LLM_CALL

    return TaskState.SYNTHESIS

# Summarization usage
async def summarize_tool_results(tool_results: list[ToolResult]) -> str:
    """Summarize tool outputs for inclusion in context."""

    summary = await llm_client.respond(
        role=ModelRole.SUMMARIZATION,
        messages=[{
            "role": "user",
            "content": f"Summarize these tool results concisely:\n\n{format_tool_results(tool_results)}"
        }],
        max_tokens=500  # Force conciseness
    )

    return summary.content
```

**VRAM Impact:**

```
Current (3 models):
  Router: 4-6GB
  DeepSeek-R1-14B: 14-20GB
  Coding: 30-35GB
  Total: 48-61GB

With specialized roles (reusing models):
  Router/Validation: 4-6GB (same instance)
  Summarization: 2-3GB
  DeepSeek-R1-14B: 14-20GB
  Coding: 30-35GB
  Total: 50-64GB (minimal increase)
```

---

### 2.6 Intelligent Routing Strategy (Phase 2-3)

**Current (MVP):** Static config-based routing

```python
# Current approach
if channel == Channel.CODE:
    model_role = ModelRole.CODING
elif channel == Channel.SYSTEM_HEALTH:
    model_role = ModelRole.REASONING
else:
    model_role = ModelRole.ROUTER
```

**Proposed (Phase 2):** MoMA-inspired three-stage routing

```python
# Phase 2: Intelligent routing
class TaskState(str, Enum):
    INIT = "init"
    ROUTING_DECISION = "routing_decision"  # NEW: Stage 1

    # Deterministic paths
    TOOL_ONLY_EXECUTION = "tool_only_execution"  # NEW

    # LLM paths
    MODEL_SELECTION = "model_selection"  # NEW: Stage 2
    LLM_CALL = "llm_call"
    TOOL_EXECUTION = "tool_execution"

    # Validation
    VALIDATION = "validation"  # NEW: Stage 3
    SYNTHESIS = "synthesis"
    COMPLETED = "completed"
    FAILED = "failed"

async def step_routing_decision(ctx: ExecutionContext) -> TaskState:
    """Stage 1: Determine if task needs LLM or can be handled deterministically."""

    # Use router model to classify
    classification = await llm_client.respond(
        role=ModelRole.ROUTER,
        messages=[{
            "role": "system",
            "content": TASK_CLASSIFICATION_PROMPT
        }, {
            "role": "user",
            "content": ctx.user_message
        }],
        tools=CLASSIFICATION_TOOLS
    )

    task_type = parse_classification(classification)

    if task_type == "simple_tool_call":
        # Skip LLM, execute tool directly
        ctx.bypass_llm = True
        return TaskState.TOOL_ONLY_EXECUTION
    elif task_type in ["reasoning_required", "coding_required"]:
        return TaskState.MODEL_SELECTION
    else:
        # Router can handle
        ctx.selected_model_role = ModelRole.ROUTER
        return TaskState.LLM_CALL

async def step_model_selection(ctx: ExecutionContext) -> TaskState:
    """Stage 2: Select optimal model for LLM-required tasks."""

    # Analyze complexity
    complexity = await router_client.assess_complexity(ctx.user_message)

    if complexity.reasoning_depth > 7:
        ctx.selected_model_role = ModelRole.REASONING
    elif complexity.code_generation_required:
        ctx.selected_model_role = ModelRole.CODING
    else:
        ctx.selected_model_role = ModelRole.ROUTER

    logger.info(
        "model_selected",
        model=ctx.selected_model_role,
        complexity=complexity.reasoning_depth,
        trace_id=ctx.trace_id
    )

    return TaskState.LLM_CALL

async def step_validation(ctx: ExecutionContext) -> TaskState:
    """Stage 3: Validate LLM output before user delivery."""

    validation_result = await validate_response(
        query=ctx.user_message,
        response=ctx.final_reply,
        tool_results=ctx.tool_results,
        mode=ctx.mode
    )

    if not validation_result.passed:
        logger.warning(
            "validation_failed",
            failures=validation_result.failures,
            confidence=validation_result.confidence
        )
        # Retry with corrective prompt
        ctx.validation_failures = validation_result.failures
        return TaskState.LLM_CALL

    return TaskState.SYNTHESIS
```

**Benefits:**

1. ✅ **30-40% of queries skip LLM** (research finding) → lower latency, lower cost
2. ✅ **Right model for right task** → higher accuracy
3. ✅ **Validation gate** → fewer hallucinations, better quality
4. ✅ **Maintains determinism** → explicit state transitions, full observability

**Implementation Timeline:**

- **Week 4 (MVP):** Add `ROUTING_DECISION` state, simple classification
- **Phase 2:** Full three-stage routing with validation
- **Phase 3:** Performance-based routing (learn from telemetry)

---

## 3. Decision Drivers

### 3.1 Research Validation

- ✅ **DeepSeek-R1-14B validated** on MATH-500 (93.9%), GPQA (59.1%), LiveCodeBench (53.1%)
- ✅ **8-bit quantization validated** as superior to 5-bit
- ✅ **Qwen3-4B validated** as optimal router choice
- ✅ **MoMA pattern validated** as deterministic + intelligent approach
- ✅ **Three-stage routing validated** in production systems

### 3.2 Resource Optimization

- ✅ **40GB VRAM savings** (80B @ 5bit → 14B @ 8bit)
- ✅ **Enables concurrent model loading** (critical for parallelism)
- ✅ **Faster inference** (14B << 80B)
- ✅ **Better quantization quality** (8bit > 5bit)

### 3.3 Performance Improvement

- ✅ **Superior reasoning** (validated benchmarks)
- ✅ **Lower latency** (smaller model)
- ✅ **Higher concurrency** (2x models simultaneously)
- ✅ **Better quality** (8-bit quantization)

### 3.4 Architectural Alignment

- ✅ **Maintains single-agent + deterministic orchestration** (no multi-agent complexity)
- ✅ **Enhances homeostasis model** (better resource utilization)
- ✅ **Preserves observability** (explicit routing decisions logged)
- ✅ **Aligns with governance** (mode-aware routing)

---

## 4. Alternatives Considered

### 4.1 Keep Qwen3-Next-80B @ 5bit

**Pros:**
- Already proposed in ADR-0003
- No migration effort

**Cons:**
- ❌ Suboptimal quantization (5-bit)
- ❌ Excessive VRAM (50-60GB)
- ❌ Lower concurrency (max 1)
- ❌ Slower inference
- ❌ No validated benchmarks

**Rejected:** Research clearly shows better alternatives exist.

### 4.2 Use DeepSeek-R1-Distill-Qwen-32B Instead

**Pros:**
- Even stronger performance than 14B
- Outperforms o1-mini

**Cons:**
- ❌ 32GB VRAM @ 4-bit (vs 14-20GB for 14B)
- ❌ Still quite large, limits concurrency
- ❌ 4-bit quantization lower quality than 8-bit

**Decision:** Use 14B for MVP/Phase 1-2, evaluate 32B for "reasoning_heavy" role in Phase 3.

### 4.3 Switch to All-Mistral Stack

**Pros:**
- Single vendor consistency
- Strong reasoning (Magistral Small)
- Strong coding (Devstral 2)

**Cons:**
- ❌ Loses Qwen3-Coder-30B (already validated locally)
- ❌ Reduces diversity for research
- ❌ Magistral not clearly superior to DeepSeek-R1

**Rejected:** Mixed stack provides better coverage and learning opportunities.

---

## 5. Consequences

### 5.1 Positive

✅ **Superior reasoning performance** with validated benchmarks
✅ **40GB VRAM freed** enables future enhancements
✅ **Higher concurrency** (2x models) enables parallelism
✅ **Better quantization quality** (8-bit vs 5-bit)
✅ **Faster inference** (14B vs 80B)
✅ **Validated coding performance** (LiveCodeBench, Codeforces)
✅ **Aligns with research best practices** (MoMA, LLMRouter)
✅ **Maintains architectural principles** (determinism, observability)

### 5.2 Negative / Trade-offs

⚠️ **Migration effort** required (test, benchmark, switch)
⚠️ **New model deployment** (download, configure, validate)
⚠️ **Potential unknown issues** with DeepSeek-R1 (mitigated by A/B testing)
⚠️ **Context length reduced** for reasoning (128K → 32K for MVP)
⚠️ **Additional complexity** with specialized roles (Phase 2)

### 5.3 Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| DeepSeek-R1-14B underperforms in practice | Low | High | A/B test before full switch, keep 80B as fallback |
| Model incompatibility with LM Studio | Medium | Medium | Test locally before committing, use Ollama as alternative |
| VRAM savings insufficient for concurrency | Low | Low | Monitor telemetry, adjust loading strategy |
| Validation model creates bottleneck | Low | Medium | Make validation optional, skip for low-risk queries |
| Context length 32K insufficient | Medium | Low | Easy to increase to 128K if needed |

---

## 6. Implementation Plan

### Phase 1: Model Replacement (Week 4 or early Phase 2)

**Timeline:** 3-5 days

```bash
# Day 1: Local deployment
1. Download DeepSeek-R1-Distill-Qwen-14B (14GB)
2. Load in LM Studio with 8-bit quantization
3. Test basic inference (simple prompts)
4. Verify VRAM usage (~14-20GB)

# Day 2: Benchmark testing
1. Run benchmark suite (MATH, coding, reasoning tasks)
2. Compare vs Qwen3-Next-80B baseline
3. Measure: accuracy, latency, VRAM, quality
4. Document results

# Day 3: Integration testing
1. Update config/models.yaml with DeepSeek-R1-14B
2. Run integration tests (test_orchestrator, test_llm_client)
3. Test full E2E flows
4. Verify telemetry logging

# Day 4: A/B testing
1. Configure both models (DeepSeek-R1 + Qwen3-Next-80B)
2. Run 50 queries through each
3. Blind comparison of outputs
4. Measure user-perceived quality

# Day 5: Switchover (if tests pass)
1. Make DeepSeek-R1-14B primary reasoning model
2. Keep Qwen3-Next-80B as fallback (reasoning_heavy)
3. Monitor telemetry for 1 week
4. Deprecate 80B if no issues
```

### Phase 2: Specialized Roles (Month 2-3)

**Timeline:** 1-2 weeks

```bash
# Week 1: Add summarization and validation
1. Configure Qwen3-1.7B for summarization
2. Reuse Qwen3-4B router for validation
3. Implement validation prompts and logic
4. Test validation accuracy (hallucination detection)

# Week 2: Three-stage routing
1. Add ROUTING_DECISION state to orchestrator
2. Implement task classification logic
3. Add MODEL_SELECTION state
4. Add VALIDATION state
5. Test routing accuracy (>90% correct model selection)
```

### Phase 3: Performance-Based Routing (Month 4-5)

**Timeline:** 2-3 weeks

```bash
# Track model performance per task type
1. Collect telemetry on model success rates
2. Build task_type → best_model mapping
3. Implement learned routing logic
4. A/B test vs static routing
5. Measure improvement in success rate + latency
```

---

## 7. Success Metrics

### 7.1 Model Performance

| Metric | Baseline (Qwen3-Next-80B) | Target (DeepSeek-R1-14B) | Measurement |
|--------|--------------------------|-------------------------|-------------|
| **Reasoning Accuracy** | TBD | >90% | Math problems, logic puzzles |
| **Coding Quality** | TBD | >50% | SWE-Bench style tasks |
| **Response Latency (P50)** | TBD | <3s | E2E query to response |
| **VRAM Usage (Peak)** | 50-60GB | 14-20GB | LM Studio monitoring |
| **Concurrent Models** | 1-2 | 3-4 | Parallel execution tests |

### 7.2 Routing Performance (Phase 2)

| Metric | Target | Measurement |
|--------|--------|-------------|
| **Routing Accuracy** | >95% | Correct model selected |
| **Direct Tool Execution Rate** | 30-40% | % queries skipping LLM |
| **Validation Catch Rate** | >80% | % hallucinations caught |
| **Routing Overhead** | <200ms | Time to make routing decision |

### 7.3 System Performance

| Metric | Baseline | Target | Measurement |
|--------|----------|--------|-------------|
| **Task Success Rate** | 80% | >90% | % tasks completed without errors |
| **Hallucination Rate** | TBD | <5% | Ungrounded responses |
| **User Satisfaction** | TBD | >4/5 | Self-reported quality |

---

## 8. Evaluation Framework

### 8.1 Benchmark Suite (Create in Week 4)

```python
# tests/evaluation/model_benchmarks.py

class ReasoningBenchmark:
    """Math reasoning benchmark (subset of MATH-500)."""
    test_cases = [...]  # 50 problems

class CodingBenchmark:
    """Coding benchmark (subset of LiveCodeBench)."""
    test_cases = [...]  # 25 problems

class SystemAnalysisBenchmark:
    """Custom benchmark for system health reasoning."""
    test_cases = [...]  # 20 scenarios

async def run_full_benchmark(model_role: ModelRole) -> BenchmarkReport:
    """Run all benchmarks against a model."""
    results = await asyncio.gather(
        run_benchmark(ReasoningBenchmark, model_role),
        run_benchmark(CodingBenchmark, model_role),
        run_benchmark(SystemAnalysisBenchmark, model_role)
    )
    return BenchmarkReport(results=results)
```

### 8.2 A/B Testing Protocol

```python
# tests/evaluation/ab_testing.py

async def ab_test_models(
    queries: list[str],
    model_a: ModelRole,
    model_b: ModelRole
) -> ABTestResult:
    """Run A/B test comparing two models."""

    results_a = []
    results_b = []

    for query in queries:
        # Run both models
        response_a = await llm_client.respond(role=model_a, messages=[...])
        response_b = await llm_client.respond(role=model_b, messages=[...])

        # Blind evaluation (LLM judges which is better)
        judgment = await validation_model.compare_responses(
            query=query,
            response_a=response_a.content,
            response_b=response_b.content
        )

        results_a.append((query, response_a, judgment.a_score))
        results_b.append((query, response_b, judgment.b_score))

    return ABTestResult(
        model_a=model_a,
        model_b=model_b,
        a_wins=sum(1 for _, _, score in results_a if score > ...),
        b_wins=sum(1 for _, _, score in results_b if score > ...),
        ties=...,
        detailed_results=...
    )
```

---

## 9. Open Questions

1. **How will DeepSeek-R1-14B perform on Mac-specific system analysis tasks?**
   - **Answer:** Benchmark on real system health queries (create custom test suite)

2. **Is 32K context sufficient for all MVP reasoning tasks?**
   - **Answer:** Monitor telemetry for context overflow, expand if needed

3. **Should we add "reasoning_heavy" role with DeepSeek-R1-32B for complex tasks?**
   - **Answer:** Evaluate in Phase 3 based on task difficulty distribution

4. **Can we fine-tune Qwen3-4B router on our task taxonomy?**
   - **Answer:** Phase 4 experiment after collecting 1000+ routing decisions

5. **Should we implement routing decision caching?**
   - **Answer:** Phase 4, after establishing routing patterns

---

## 10. Related Documents

**Research & Analysis:**
- `../research/model_orchestration_research_analysis_2025-12-31.md` — Comprehensive analysis
- `../research/temp_perplexity_research.md` — Raw research data

**Architecture:**
- `ADR-0003-model-stack.md` — Original model stack decision (partially superseded)
- `../architecture/INTELLIGENT_ROUTING_PATTERNS_v0.1.md` — Routing patterns inspiration
- `../architecture/LOCAL_LLM_CLIENT_SPEC_v0.1.md` — LLM client specification

**Implementation:**
- `config/models.yaml` — Model configuration (to be updated)
- `../plans/IMPLEMENTATION_ROADMAP.md` — Implementation timeline
- `./experiments/` — Future experiment documentation

---

## 11. Approval Criteria

This ADR is accepted when:

1. ✅ **Research analysis reviewed** by project owner
2. ✅ **Benchmark framework created** (tests/evaluation/model_benchmarks.py)
3. ✅ **DeepSeek-R1-14B tested locally** (validated deployment)
4. ✅ **A/B test results documented** (DeepSeek-R1 vs Qwen3-Next-80B)
5. ✅ **Performance meets targets** (>90% accuracy, <20GB VRAM, <3s latency)
6. ✅ **Migration plan approved** by project owner

---

## 12. Rollback Plan

If DeepSeek-R1-14B underperforms or causes issues:

```yaml
# Rollback configuration
models:
  reasoning:
    id: "qwen/qwen3-next-80b"  # Revert to original
    quantization: "8bit"  # Upgrade from 5bit if possible
    max_concurrency: 1
```

**Rollback Triggers:**

- Reasoning accuracy < 80% on benchmark suite
- Inference failures or crashes
- VRAM usage > 25GB (unexpected)
- User-reported quality degradation

**Rollback Process:**

1. Revert config/models.yaml to previous version
2. Restart LLM client (reload models)
3. Run regression tests
4. Document failure reasons in experiment log
5. Plan alternative approach

---

**Document Status:** Proposed, Awaiting Approval
**Next Actions:**
1. Project owner review and approval
2. Create benchmark framework (Week 4)
3. Test DeepSeek-R1-14B locally (Week 4 or early Phase 2)
4. Run A/B tests and document results
5. Execute migration if tests pass

**Decision Deadline:** Before Phase 2 begins (Month 2)
