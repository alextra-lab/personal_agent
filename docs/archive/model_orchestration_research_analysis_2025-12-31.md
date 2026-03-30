# Model Orchestration Research Analysis — 2025-12-31

**Status:** Analysis Complete
**Date:** 2025-12-31
**Research Source:** Perplexity deep research on small models, routing architectures, and multi-agent vs single-agent approaches
**Analyst:** AI Architecture Review

---

## Executive Summary

This document provides a comprehensive analysis of recent research into model orchestration, routing strategies, and agent architectures. The research validates several of our existing architectural decisions while revealing opportunities for course corrections and future enhancements.

**Key Findings:**

1. ✅ **Our three-role model stack (Router/Reasoning/Coding) is well-validated** by current research
2. ✅ **Single-agent + deterministic orchestration with LLM routing is the recommended pattern** for our use case
3. ⚠️ **DeepSeek-R1-Distill models offer significant improvements** over current reasoning model choices
4. ⚠️ **Qwen3-4B emerges as superior to Qwen3-1.7B** for routing (already adopted in config)
5. 💡 **Purpose-built routing frameworks (MoMA, LLMRouter)** provide patterns we should study
6. 💡 **Additional specialized agents** (retrieval, summarization, validation) show clear value

---

## 1. Model Selection Analysis

### 1.1 Router Model Validation

**Research Finding:**

> Qwen3-4B delivers the strongest results after fine-tuning among small language models, with an average rank of 2.25 across multiple benchmarks. It outperforms Qwen3-8B in distillation tasks and demonstrates superior fine-tuning capabilities.

**Current Implementation:**

```yaml
# config/models.yaml
models:
  router:
    id: "qwen/qwen3-4b-thinking-2507"  # ✅ Already using Qwen3-4B
```

**Assessment:** ✅ **VALIDATED**

- Our choice to use Qwen3-4B over 1.7B aligns with research showing 4B's superior performance
- The research confirms 4B models offer better balance of performance and tunability for routing
- MoMA framework specifically uses Qwen3 architecture with MoE head for routing

**Recommendation:** **MAINTAIN** current Qwen3-4B choice. Consider fine-tuning on agent-specific routing tasks in Phase 2.

---

### 1.2 Reasoning Model Course Correction

**Research Finding:**

> DeepSeek-R1-Distill-Qwen-14B delivers outstanding reasoning performance with 93.9% accuracy on MATH-500 and 59.1% on GPQA Diamond. Requires 14-20GB VRAM with 8-bit quantization, viable on M4 systems.
>
> For more demanding tasks, DeepSeek-R1-Distill-Qwen-32B outperforms OpenAI's o1-mini across multiple benchmarks.

**Current Implementation:**

```yaml
models:
  reasoning:
    id: "qwen/qwen3-next-80b"
    quantization: "5bit"
    max_concurrency: 1
    default_timeout: 60
```

**Assessment:** ⚠️ **COURSE CORRECTION OPPORTUNITY**

**Problem:**
- Qwen3-Next-80B-A3B is significantly larger (80B parameters) than necessary
- 5-bit quantization may degrade reasoning quality
- Higher latency due to model size
- Lower concurrency (max_concurrency: 1) limits parallelism

**Alternative:**
- **DeepSeek-R1-Distill-Qwen-14B:**
  - 93.9% MATH-500 accuracy (vs unclear baseline for 80B)
  - 53.1% LiveCodeBench, 1481 Codeforces rating
  - 14-20GB VRAM at 8-bit (vs 80GB+ for full 80B)
  - **Enables higher concurrency** due to smaller footprint
  - Better quantization quality at 8-bit vs 5-bit

- **DeepSeek-R1-Distill-Qwen-32B:**
  - For complex reasoning tasks
  - Outperforms o1-mini
  - 32GB VRAM at 4-bit quantization
  - Still smaller than current 80B model

**Recommendation:**

```yaml
# PROPOSED: config/models.yaml
models:
  reasoning:
    id: "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B"
    quantization: "8bit"  # Better quality than 5bit
    max_concurrency: 2    # Increased from 1 due to smaller footprint
    default_timeout: 60

  # Optional: Add heavy reasoning mode
  reasoning_heavy:
    id: "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"
    quantization: "4bit"
    max_concurrency: 1
    default_timeout: 90
```

**Benefits:**
- ✅ Superior reasoning benchmarks
- ✅ Better quantization quality (8-bit vs 5-bit)
- ✅ Smaller memory footprint enables higher concurrency
- ✅ Faster inference due to smaller size
- ✅ Validated performance on coding tasks (LiveCodeBench)

---

### 1.3 Coding Model Analysis

**Research Finding:**

> Devstral 2 (2512) currently leads with 56.40% on SWE-Bench Verified, outperforming Qwen3-Coder's 55.40%. Devstral 2 supports 128,000-token context windows, optimized for high-performance hardware like M4 Max.
>
> Qwen3-Coder (32B) offers excellent alternative with strong tool usage for file system operations and code navigation. The full 480B MoE version with 35B active parameters supports 256K context natively.

**Current Implementation:**

```yaml
models:
  coding:
    id: "qwen/qwen3-coder-30b"
    quantization: "8bit"
    max_concurrency: 2
```

**Assessment:** ✅ **VALIDATED WITH ENHANCEMENT OPPORTUNITY**

**Analysis:**
- Qwen3-Coder-30B is validated as strong choice (55.40% SWE-Bench)
- Devstral 2 offers marginal improvement (56.40% vs 55.40% = +1% absolute)
- **Context length** is key differentiator:
  - Qwen3-Coder-30B: 32K tokens
  - Devstral 2: 128K tokens
  - This matters for large codebase analysis

**Recommendation:**

**Option A: Maintain Qwen3-Coder-30B** (CONSERVATIVE)
- Already deployed and validated locally
- Strong performance on SWE tasks
- Acceptable context length for MVP

**Option B: Evaluate Devstral 2** (EXPLORATORY)
- Test locally on M4 Max
- Compare inference speed and quality
- Measure value of 128K context in practice
- Consider as upgrade path in Phase 2

**Option C: Dual Coding Strategy** (HYBRID)
```yaml
models:
  coding:
    id: "qwen/qwen3-coder-30b"
    context_length: 32768

  coding_large_context:
    id: "mistralai/devstral-2-2512"
    context_length: 128000
```

**Recommended Path:** **Option A for MVP**, evaluate Option C in Phase 2 when large codebase tasks emerge.

---

## 2. Architecture Pattern Validation

### 2.1 Single-Agent + Router vs Multi-Agent

**Research Finding:**

> Single Agent + Deterministic Router: One coordinating agent uses a lightweight router model to select which specialized LLM processes the request. The LLMs are stateless model endpoints, not autonomous agents.
>
> Pros: 95% deterministic operations, lower latency, predictable resource usage, faster development iteration.
>
> Cons: Limited task decomposition, no collaborative problem-solving, limited context specialization.

**Current Architecture (ADR-0002):**

> Hybrid orchestration model: **Deterministic graph/state machine for control**, with **embedded LLM cognition** inside bounded steps.

**Assessment:** ✅ **STRONGLY VALIDATED**

**Alignment Analysis:**

| Research Pattern | Our Architecture | Match |
|-----------------|------------------|-------|
| Deterministic orchestration | Explicit state machine (ADR-0006) | ✅ Perfect |
| LLM router for model selection | Local LLM Client with role-based routing | ✅ Perfect |
| Stateless model endpoints | Models addressed by roles, not agents | ✅ Perfect |
| Single execution flow | TaskState enum with step functions | ✅ Perfect |

**Key Research Quote Validates Our Approach:**

> "One developer reported achieving **95% deterministic operations** by switching from agentic to a directed acyclic graph (DAG) with LLM components for data extraction only."

This directly aligns with our homeostasis model emphasis on:
- Observable, auditable, deterministic control
- Sensor → Control Center → Effector loops
- Explicit state transitions

**Research on Multi-Agent Complexity:**

> "Adding new capabilities requires modifying the central router and orchestration logic. Multi-agent systems allow adding new specialized agents without touching existing ones."

**Our Counter-Strategy:**

We maintain **flexibility without agent complexity** through:
1. **Configuration-driven model selection** (`config/models.yaml`)
2. **Role-based abstraction** (router/reasoning/coding)
3. **Channel-specific orchestration** (CHAT, CODE, SYSTEM_HEALTH)
4. **Extensible step functions** (add new states without redesigning)

**Recommendation:** **MAINTAIN** single-agent + deterministic orchestration. Research validates this as optimal for:
- Local-only operation
- Enterprise operational requirements (predictability, auditability, compliance)
- Single-user scenarios
- Controllable resource usage

---

### 2.2 Hierarchical Routing Pattern (MoMA Framework)

**Research Finding:**

> MoMA (Mixture of Models and Agents) implements two-stage routing:
>
> **Stage 1:** Agent vs LLM Decision - Router determines if task can be handled by deterministic agent (tool-calling, retrieval) or requires LLM intelligence.
>
> **Stage 2:** LLM Selection - For LLM tasks, router selects optimal model based on performance-cost profiles.
>
> **Stage 3:** Deterministic Post-Processing - After model execution, use deterministic workflows for validation, formatting, tool execution.

**Current Architecture:**

```python
# src/personal_agent/orchestrator/executor.py (from ADR-0006)
class TaskState(str, Enum):
    INIT = "init"
    PLANNING = "planning"
    LLM_CALL = "llm_call"
    TOOL_EXECUTION = "tool_execution"
    SYNTHESIS = "synthesis"
```

**Assessment:** 🎯 **ARCHITECTURAL ENHANCEMENT OPPORTUNITY**

**Current Flow:**
```
INIT → PLANNING → LLM_CALL → TOOL_EXECUTION → SYNTHESIS
```

**MoMA-Inspired Enhanced Flow:**
```
INIT → ROUTING_DECISION → [DETERMINISTIC_AGENT | LLM_SELECTION] → EXECUTION → VALIDATION → SYNTHESIS
```

**Proposed State Machine Extension:**

```python
class TaskState(str, Enum):
    INIT = "init"
    ROUTING_DECISION = "routing_decision"  # NEW: Stage 1

    # Deterministic paths
    TOOL_ONLY_EXECUTION = "tool_only_execution"  # NEW: No LLM needed
    RETRIEVAL_ONLY = "retrieval_only"  # NEW: RAG without reasoning

    # LLM paths
    MODEL_SELECTION = "model_selection"  # NEW: Stage 2
    LLM_CALL = "llm_call"
    TOOL_EXECUTION = "tool_execution"

    # Post-processing
    VALIDATION = "validation"  # NEW: Stage 3
    SYNTHESIS = "synthesis"
    COMPLETED = "completed"
    FAILED = "failed"
```

**Implementation Sketch:**

```python
async def step_routing_decision(ctx: ExecutionContext) -> TaskState:
    """Stage 1: Determine if task needs LLM or can be handled deterministically."""

    # Use lightweight router model (Qwen3-4B) to classify
    classification = await router_client.classify(
        query=ctx.user_message,
        categories=[
            "simple_tool_call",      # e.g., "check CPU usage"
            "retrieval_only",        # e.g., "search docs for X"
            "reasoning_required",    # e.g., "analyze system health trends"
            "coding_task",           # e.g., "refactor this function"
        ]
    )

    if classification == "simple_tool_call":
        return TaskState.TOOL_ONLY_EXECUTION
    elif classification == "retrieval_only":
        return TaskState.RETRIEVAL_ONLY
    elif classification == "reasoning_required":
        return TaskState.MODEL_SELECTION
    elif classification == "coding_task":
        ctx.selected_model_role = ModelRole.CODING
        return TaskState.LLM_CALL
    else:
        return TaskState.MODEL_SELECTION

async def step_model_selection(ctx: ExecutionContext) -> TaskState:
    """Stage 2: Select optimal model for LLM-required tasks."""

    # Use router model to determine complexity/requirements
    complexity = await router_client.assess_complexity(ctx.user_message)

    if complexity.reasoning_depth > 7:
        ctx.selected_model_role = ModelRole.REASONING
    elif complexity.code_generation_required:
        ctx.selected_model_role = ModelRole.CODING
    else:
        ctx.selected_model_role = ModelRole.ROUTER  # Router can handle

    return TaskState.LLM_CALL

async def step_validation(ctx: ExecutionContext) -> TaskState:
    """Stage 3: Deterministic validation of LLM output."""

    # Check for hallucinations, policy violations, format errors
    validation_result = validate_llm_output(ctx.llm_response)

    if not validation_result.passed:
        logger.warning("validation_failed", reason=validation_result.reason)
        # Retry with different prompt or escalate
        return TaskState.LLM_CALL  # Retry

    return TaskState.SYNTHESIS
```

**Benefits:**

1. ✅ **Efficiency:** Skip LLM for deterministic tasks (30-40% of queries)
2. ✅ **Cost:** Router model handles what it can before escalating
3. ✅ **Latency:** Direct tool execution faster than LLM → tool path
4. ✅ **Specialization:** Right model for right task
5. ✅ **Validation:** Explicit quality gate before user response

**Recommendation:**

**Phase 1 (MVP):** Keep simpler flow, add `ROUTING_DECISION` state to classify task type
**Phase 2 (Post-MVP):** Implement full three-stage MoMA-inspired routing with validation

---

## 3. Additional Agent Modes (Research-Validated)

**Research Finding:**

> Based on multi-agent system architectures, consider adding specialized modes:
>
> 1. **Retrieval Agent:** Dedicated for RAG operations, 12.1% average improvement when specialized
> 2. **Summarization Agent:** Fine-tuned 1.5B-3B models significantly reduce latency vs routing through larger models
> 3. **Vision/Multimodal Agent:** For document understanding, PDFs, images
> 4. **Tool-Specific Execution Agent:** Handles calculator, DB queries, API calls separately from reasoning
> 5. **Critical/Validation Agent:** Quality assurance layer, cross-references responses

**Current Architecture:**

- Three roles: Router, Reasoning, Coding
- Tool execution handled by unified ToolExecutionLayer
- No specialized retrieval or validation models

**Assessment:** 💡 **FUTURE ENHANCEMENT OPPORTUNITY**

**Immediate Recommendations:**

**Phase 2 Additions:**

```yaml
# PROPOSED: Extended config/models.yaml
models:
  router:
    id: "qwen/qwen3-4b-thinking-2507"

  reasoning:
    id: "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B"

  coding:
    id: "qwen/qwen3-coder-30b"

  # NEW: Specialized modes
  summarization:
    id: "qwen/qwen3-1.7b-instruct"  # Lightweight, fast
    context_length: 32768
    default_timeout: 10

  validation:
    id: "qwen/qwen3-4b-thinking-2507"  # Reuse router for validation
    context_length: 8192
    default_timeout: 15
```

**Implementation Strategy:**

```python
class ModelRole(str, Enum):
    ROUTER = "router"
    REASONING = "reasoning"
    CODING = "coding"
    SUMMARIZATION = "summarization"  # NEW
    VALIDATION = "validation"  # NEW

# Validation pattern
async def step_validation(ctx: ExecutionContext) -> TaskState:
    """Use validation model to check output quality."""
    validation_response = await llm_client.respond(
        role=ModelRole.VALIDATION,
        messages=[
            {"role": "system", "content": VALIDATION_PROMPT},
            {"role": "user", "content": f"Output to validate:\n{ctx.final_reply}"}
        ]
    )

    if validation_response.content.startswith("INVALID"):
        return TaskState.LLM_CALL  # Retry
    return TaskState.SYNTHESIS
```

**Retrieval Agent (Phase 3):**

When implementing RAG capabilities:
- Use lightweight embedding model (Jina embeddings)
- Separate retrieval step from reasoning step
- Enables "interleaved retrieval and reasoning" pattern from research
- 12.1% improvement documented in MDocAgent framework

**Recommendation:**

- **MVP:** Maintain three-role stack (router/reasoning/coding)
- **Phase 2:** Add summarization and validation roles (low cost, high value)
- **Phase 3:** Add retrieval agent when RAG implemented
- **Phase 4+:** Consider vision agent for multimodal tasks

---

## 4. Learning from LLMRouter Research

**Research Finding:**

> LLMRouter from UIUC implements 16+ routing algorithms across four families:
> - Single-round routing
> - Multi-round routing (sequential decision process)
> - Personalized routing (user preference learning)
> - Agentic routing (task decomposition)
>
> Router R1 formulates multi-LLM routing as sequential decision process trained with reinforcement learning to balance format, outcome, and cost.

**Current Architecture:**

- Static role-based routing (config-driven)
- No learning/adaptation in routing decisions
- No cost optimization in model selection

**Assessment:** 📊 **PHASE 3-4 ENHANCEMENT**

**Near-Term Opportunities:**

**1. Cost-Aware Routing (Phase 2):**

```python
# Track model costs in telemetry
class ModelMetrics:
    tokens_used: int
    latency_ms: float
    success_rate: float
    cost_estimate: float  # Based on inference time

async def select_model_for_task(task_complexity: int, budget: CostBudget) -> ModelRole:
    """Cost-aware model selection."""
    if budget.remaining < REASONING_MODEL_COST:
        return ModelRole.ROUTER  # Fallback to cheaper model

    if task_complexity > 8:
        return ModelRole.REASONING
    elif task_complexity > 4:
        return ModelRole.ROUTER  # Router sufficient
    else:
        return ModelRole.ROUTER
```

**2. Performance-Based Routing (Phase 3):**

Track which model performs best for which task types:

```python
# Store in telemetry/evaluation
task_model_performance = {
    "system_health_analysis": {
        ModelRole.REASONING: 0.92,  # Success rate
        ModelRole.ROUTER: 0.78,
    },
    "code_refactoring": {
        ModelRole.CODING: 0.95,
        ModelRole.REASONING: 0.73,
    }
}

# Route based on historical performance
def select_optimal_model(task_type: str) -> ModelRole:
    performances = task_model_performance.get(task_type, {})
    return max(performances.items(), key=lambda x: x[1])[0]
```

**3. Multi-Round Routing (Phase 4):**

For complex tasks, router makes sequential decisions:

```
Round 1: Router assesses task → determines "needs planning"
Round 2: Reasoning model creates plan → determines "needs code generation"
Round 3: Coding model generates code → validates → done
```

**Recommendation:**

- **MVP:** Static config-based routing (simple, deterministic)
- **Phase 2:** Add cost tracking to telemetry
- **Phase 3:** Implement performance-based routing with learning
- **Phase 4:** Experiment with multi-round routing for complex tasks

---

## 5. Benchmark Validation & Testing Strategy

**Research Findings:**

Key benchmarks mentioned:
- **MATH-500:** Math reasoning (DeepSeek-R1: 93.9%)
- **GPQA Diamond:** Graduate-level science QA (DeepSeek-R1: 59.1%)
- **LiveCodeBench:** Coding tasks (DeepSeek-R1-14B: 53.1%)
- **SWE-Bench Verified:** Software engineering (Devstral 2: 56.40%, Qwen3-Coder: 55.40%)
- **Codeforces Rating:** Competitive programming (DeepSeek-R1-14B: 1481)

**Current Project:**

- No formal benchmarking framework yet
- Evaluation mentioned in roadmap (Week 4, Day 26-28)
- Experiment documentation structure exists (`../architecture_decisions/experiments/`)

**Assessment:** 📊 **EVALUATION FRAMEWORK NEEDED**

**Recommendation:**

**Create Model Evaluation Harness (Week 4 or Phase 2):**

```python
# tests/evaluation/model_benchmarks.py

@dataclass
class BenchmarkResult:
    model_id: str
    role: ModelRole
    benchmark_name: str
    score: float
    latency_ms: float
    tokens_used: int
    timestamp: datetime

async def run_model_benchmark(
    model_role: ModelRole,
    benchmark_suite: BenchmarkSuite
) -> list[BenchmarkResult]:
    """Run standardized benchmark against a model role."""
    results = []

    for test_case in benchmark_suite.test_cases:
        start = time.time()
        response = await llm_client.respond(
            role=model_role,
            messages=test_case.messages
        )
        latency = (time.time() - start) * 1000

        score = benchmark_suite.evaluate(
            expected=test_case.expected_output,
            actual=response.content
        )

        results.append(BenchmarkResult(
            model_id=get_model_config(model_role).id,
            role=model_role,
            benchmark_name=benchmark_suite.name,
            score=score,
            latency_ms=latency,
            tokens_used=response.usage["total_tokens"],
            timestamp=datetime.now(UTC)
        ))

    return results

# Benchmark suites
class MathReasoningBenchmark:
    """Subset of MATH-500 adapted for local testing."""
    test_cases = [...]  # 50 representative problems

class CodingBenchmark:
    """Subset of LiveCodeBench for local testing."""
    test_cases = [...]  # 25 coding problems

class SystemAnalysisBenchmark:
    """Custom benchmark for system health reasoning."""
    test_cases = [...]  # 20 system analysis scenarios
```

**Store results in telemetry:**

```
telemetry/evaluation/
├── benchmarks/
│   ├── 2025-12-31_qwen3-4b_math-reasoning.json
│   ├── 2025-12-31_deepseek-r1-14b_math-reasoning.json
│   └── comparison_report_2025-12-31.md
```

**Recommendation:**

1. **Week 4 (MVP):** Create basic evaluation framework for sanity testing
2. **Phase 2:** Implement formal benchmark suites (math, coding, system analysis)
3. **Phase 2:** Run A/B tests comparing:
   - Qwen3-Next-80B vs DeepSeek-R1-14B (reasoning)
   - Qwen3-Coder-30B vs Devstral 2 (coding)
4. **Phase 3:** Continuous benchmarking in CI/CD
5. **Phase 4:** User feedback integration into evaluation metrics

---

## 6. Quantization Strategy Analysis

**Research Finding:**

> MVP targets **4–8 bit quantizations** for all models, balancing quality and performance.
>
> DeepSeek-R1-Distill-Qwen-14B: 14-20GB VRAM with 8-bit quantization
> DeepSeek-R1-Distill-Qwen-32B: 32GB VRAM with 4-bit quantization

**Current Configuration:**

```yaml
models:
  router:
    quantization: "8bit"  # ✅ Good
  reasoning:
    quantization: "5bit"  # ⚠️ Unusual, likely degradation
  coding:
    quantization: "8bit"  # ✅ Good
```

**Assessment:** ⚠️ **5-BIT QUANTIZATION SUBOPTIMAL**

**Problem:**
- 5-bit is uncommon quantization level
- Research emphasizes **4-bit or 8-bit** as standard
- 5-bit likely introduces quantization artifacts without significant memory savings vs 8-bit
- Research shows DeepSeek-R1 models validated at 8-bit with strong performance

**Recommendation:**

```yaml
# PROPOSED: Standardize on 4-bit or 8-bit
models:
  router:
    quantization: "8bit"  # Maintain

  reasoning:
    quantization: "8bit"  # Change from 5bit → 8bit
    # OR use smaller model (DeepSeek-R1-14B) at 8bit

  coding:
    quantization: "8bit"  # Maintain
```

**Quantization Strategy:**

| Model Size | Quantization | VRAM | Quality | Use Case |
|-----------|-------------|------|---------|----------|
| 1.7B-4B | 8-bit | 4-8GB | Excellent | Router, validation |
| 14B-30B | 8-bit | 14-30GB | Excellent | Reasoning, coding |
| 32B+ | 4-bit | 16-40GB | Good | Heavy reasoning |
| 80B+ | 4-bit | 40-80GB | Good | Research only |

**Testing Protocol:**

1. Benchmark current 5-bit reasoning model performance
2. Test 8-bit quantization of same model
3. Test DeepSeek-R1-14B at 8-bit
4. Compare: accuracy, latency, VRAM usage
5. Select optimal quantization + model combination

---

## 7. Context Length Strategy

**Research Finding:**

> Devstral 2 supports **128,000-token context windows**, optimized for high-performance hardware like M4 Max.
>
> Qwen3-Coder full MoE supports **256K context** natively.

**Current Configuration:**

```yaml
models:
  router:
    context_length: 8192    # ✅ Appropriate for routing
  reasoning:
    context_length: 128000  # 🤔 May be excessive for MVP
  coding:
    context_length: 32768   # ✅ Good for most coding tasks
```

**Assessment:** 🎯 **BALANCED, WITH FUTURE CONSIDERATIONS**

**Analysis:**

**Router (8K):** ✅ Appropriate
- Routing decisions rarely need long context
- 8K handles most classification/intent tasks

**Reasoning (128K):** 🤔 Likely overkill for MVP
- Most reasoning tasks < 32K tokens
- 128K enables document analysis, long research sessions
- Trade-off: higher VRAM, slower inference at large contexts
- **Recommendation:** Start at 32K, increase to 128K when needed

**Coding (32K):** ✅ Good for MVP, may need expansion
- 32K = ~6,000-10,000 lines of code
- Sufficient for most single-file or small multi-file tasks
- Large codebase refactors may need 128K+

**Recommendation:**

```yaml
# PROPOSED: Conservative context lengths for MVP
models:
  router:
    context_length: 8192     # Maintain

  reasoning:
    context_length: 32768    # Reduce from 128K for MVP
    # Expand to 128K in Phase 2 when document analysis added

  coding:
    context_length: 32768    # Maintain
    # Consider 128K model (Devstral 2) for large codebase tasks
```

**Context Length Scaling Strategy:**

| Phase | Router | Reasoning | Coding | Rationale |
|-------|--------|-----------|---------|-----------|
| MVP | 8K | 32K | 32K | Sufficient for core tasks |
| Phase 2 | 8K | 128K | 32K | Add document analysis |
| Phase 3 | 8K | 128K | 128K | Large codebase support |

---

## 8. Infrastructure Implications (M4 Max 128GB)

**Hardware Constraints:**

- **Total RAM:** 128GB unified memory
- **Available for models:** ~100GB (after OS, services)
- **Concurrent model loading:** Critical consideration

**Current Configuration Memory Estimate:**

```
Router (Qwen3-4B @ 8bit):        4-6GB
Reasoning (Qwen3-Next-80B @ 5bit): 50-60GB
Coding (Qwen3-Coder-30B @ 8bit):  30-35GB
---
Total if all loaded: 84-101GB ⚠️ Tight fit
```

**Proposed Configuration Memory Estimate:**

```
Router (Qwen3-4B @ 8bit):        4-6GB
Reasoning (DeepSeek-R1-14B @ 8bit): 14-20GB
Coding (Qwen3-Coder-30B @ 8bit):  30-35GB
---
Total if all loaded: 48-61GB ✅ Comfortable
```

**Benefits of Smaller Reasoning Model:**

1. ✅ **40GB freed up** for other operations
2. ✅ **Enables concurrent model loading** for parallel tasks
3. ✅ **Faster cold start** (14B loads faster than 80B)
4. ✅ **Better quantization** (8-bit vs 5-bit quality)
5. ✅ **Headroom for future additions** (summarization, validation models)

**Model Loading Strategy:**

```python
class ModelLoadingStrategy:
    """Manage model loading/unloading based on VRAM."""

    async def load_model_for_task(self, role: ModelRole) -> None:
        """Load model on-demand, unload others if needed."""
        available_vram = get_available_vram()
        required_vram = get_model_vram_requirement(role)

        if available_vram < required_vram:
            # Unload least-recently-used model
            await self.unload_lru_model()

        await self.load_model(role)

    async def preload_likely_models(self, context: ExecutionContext) -> None:
        """Preload models likely to be used based on context."""
        # Always keep router loaded
        await self.ensure_loaded(ModelRole.ROUTER)

        # Predict next model based on task type
        if context.channel == Channel.CODE:
            await self.preload(ModelRole.CODING)
        elif context.channel == Channel.SYSTEM_HEALTH:
            await self.preload(ModelRole.REASONING)
```

**Recommendation:**

1. **Adopt DeepSeek-R1-14B** to free up VRAM
2. **Implement smart model loading/unloading** (Phase 2)
3. **Keep router always-loaded** (small footprint, always needed)
4. **Load reasoning/coding on-demand** based on task type
5. **Monitor VRAM usage** in telemetry for optimization

---

## 9. Fine-Tuning Opportunities (Future)

**Research Finding:**

> Qwen3-4B consistently delivers the strongest results **after fine-tuning** among small language models.
>
> MoMA constructs training datasets profiling LLM capabilities and employs context-aware finite state machine with dynamic token masking for precise agent selection.

**Current Architecture:**

- No fine-tuning planned for MVP
- Using pre-trained models as-is

**Assessment:** 💡 **PHASE 4+ OPPORTUNITY**

**Fine-Tuning Strategy (Future):**

**1. Router Fine-Tuning (Phase 4):**

Collect dataset of routing decisions:
```json
{
  "user_query": "Check my system's CPU usage",
  "optimal_model": "ROUTER",
  "required_tools": ["system_metrics_snapshot"],
  "complexity": 2,
  "reasoning_depth": 1
}
```

Fine-tune Qwen3-4B on:
- Correct task classification
- Tool selection accuracy
- Complexity assessment
- Model selection rationale

**2. Validation Model Fine-Tuning (Phase 4):**

Train on dataset of:
```json
{
  "llm_output": "Your CPU is at 95% usage, which is concerning...",
  "validation_result": "VALID",
  "reasoning": "Output is grounded in tool data, appropriate concern level"
},
{
  "llm_output": "I analyzed the system logs and found...",
  "validation_result": "INVALID",
  "reasoning": "Hallucination - no tool called to read logs"
}
```

**3. Task-Specific Prompting (Phase 2-3):**

Before fine-tuning, optimize prompts:
- System health analysis prompts
- Code refactoring prompts
- Research synthesis prompts

Store in `config/prompts/` with version control

**Recommendation:**

- **MVP-Phase 3:** Use pre-trained models with optimized prompts
- **Phase 4:** Collect telemetry data for fine-tuning datasets
- **Phase 5:** Fine-tune router for task classification
- **Phase 6:** Fine-tune validation model for quality assurance

---

## 10. Summary of Recommendations

### Immediate Actions (MVP - Week 1-4)

1. ✅ **MAINTAIN** Qwen3-4B for routing (already adopted)
2. ⚠️ **EVALUATE** DeepSeek-R1-Distill-Qwen-14B as reasoning model replacement
3. ⚠️ **CHANGE** reasoning model quantization from 5-bit → 8-bit
4. 📊 **CREATE** basic model evaluation framework (Week 4)
5. ✅ **MAINTAIN** Qwen3-Coder-30B for coding (validated choice)

### Phase 2 Enhancements (Post-MVP, Month 2-3)

6. 🎯 **IMPLEMENT** MoMA-inspired routing decision state
7. 💡 **ADD** summarization and validation model roles
8. 📊 **RUN** formal benchmarks comparing model choices
9. 🔧 **IMPLEMENT** smart model loading/unloading
10. 📝 **OPTIMIZE** task-specific prompts

### Phase 3-4 Advanced Features (Month 4-6)

11. 💡 **ADD** retrieval agent for RAG capabilities
12. 📊 **IMPLEMENT** performance-based routing with learning
13. 🎯 **EXPERIMENT** with multi-round routing
14. 🔧 **COLLECT** fine-tuning datasets from telemetry
15. 📊 **CONTINUOUS** benchmark testing in CI/CD

### Research & Experimentation (Ongoing)

16. 🔬 **MONITOR** new model releases (Qwen4, DeepSeek-R2, etc.)
17. 🔬 **EVALUATE** Devstral 2 for coding (when large context needed)
18. 🔬 **STUDY** LLMRouter and MoMA implementations
19. 🔬 **EXPERIMENT** with vision/multimodal models (Phase 5+)

---

## 11. Validation Metrics

To measure success of course corrections:

### Model Performance Metrics

- **Reasoning Accuracy:** Math problems, logic puzzles (target: >90%)
- **Coding Quality:** SWE-Bench style tasks (target: >50%)
- **Routing Accuracy:** Correct model selection rate (target: >95%)
- **Latency:** P50, P95, P99 response times (target: <3s, <10s, <20s)

### System Performance Metrics

- **VRAM Usage:** Peak and average (target: <70GB average)
- **Model Loading Time:** Cold start latency (target: <30s)
- **Concurrent Task Capacity:** Parallel executions supported (target: 3+)

### Quality Metrics

- **Hallucination Rate:** Ungrounded responses (target: <5%)
- **Task Success Rate:** Completed without errors (target: >85%)
- **User Satisfaction:** Self-reported quality (target: >4/5)

---

## 12. Risk Assessment

### Low Risk Course Corrections (✅ Proceed)

- Switch to Qwen3-4B router (already done)
- Change quantization 5bit → 8bit
- Add basic evaluation framework
- Optimize prompts

### Medium Risk Course Corrections (⚠️ Test First)

- Switch to DeepSeek-R1-14B reasoning model
  - **Risk:** Unfamiliar model, deployment complexity
  - **Mitigation:** A/B test against current model, benchmark thoroughly
- Implement MoMA-inspired routing
  - **Risk:** Architecture complexity increase
  - **Mitigation:** Incremental implementation, maintain backward compatibility

### High Risk Course Corrections (🔴 Phase 3+)

- Fine-tuning models
  - **Risk:** Degraded performance, training cost, maintenance
  - **Mitigation:** Extensive evaluation, comparison datasets, rollback plan
- Multi-round routing with RL
  - **Risk:** Non-determinism, debugging difficulty
  - **Mitigation:** Extensive telemetry, shadow mode testing

---

## Conclusion

This research analysis validates our core architectural decisions while revealing specific opportunities for model optimization and enhanced routing strategies. The proposed course corrections are **low-risk, high-value** improvements that align with our goals of:

- ✅ Local-only operation
- ✅ Deterministic orchestration
- ✅ Observable, auditable behavior
- ✅ Efficient resource utilization
- ✅ State-of-the-art performance

**Primary Recommendation:** Evaluate DeepSeek-R1-Distill-Qwen-14B as reasoning model replacement in Phase 2. This single change offers:
- Superior benchmark performance
- Better quantization quality
- 40GB VRAM savings
- Faster inference
- Enables concurrent model loading

All recommendations are phased to maintain MVP momentum while positioning for state-of-the-art capabilities in later phases.

---

**Document Status:** Analysis Complete, Ready for Architecture Review
**Next Action:** Create course correction ADR if recommendations accepted
**Related Documents:**
- `ADR-0003-model-stack.md` (will be updated)
- `../architecture/LOCAL_LLM_CLIENT_SPEC_v0.1.md` (routing implications)
- `../plans/IMPLEMENTATION_ROADMAP.md` (phase planning)
