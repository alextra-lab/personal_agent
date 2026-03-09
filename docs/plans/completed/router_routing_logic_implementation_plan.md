# Implementation Plan: Router Routing Logic (Day 11.5)

**Date:** 2025-12-31
**Status:** Proposed
**Timeline:** 2-3 days
**Dependencies:** Day 10-11 (E2E Flow) complete
**Related ADRs:** ADR-0003 (Model Stack), ADR-0008 (Model Course Correction)
**Research Basis:** `../research/router_prompt_patterns_best_practices_2025-12-31.md`

---

## 1. Overview

### 1.1 Current State (Problem)

**What works:**

- ✅ CLI → Orchestrator → LLM Client → Model → Response
- ✅ Telemetry, error handling, timeout configuration
- ✅ `/v1/responses` endpoint integration

**What doesn't work:**

- ❌ Router model **answers queries directly** instead of **routing**
- ❌ No model selection logic (hardcoded `channel → model_role`)
- ❌ Complex queries like "What is Python?" use small 4B router model
- ❌ No multi-model coordination

**Current Flow:**

```
INIT → LLM_CALL(router) → SYNTHESIS → COMPLETED
         ↓
    qwen-4b answers directly (1 model, 1 step)
```

### 1.2 Desired State (Solution)

**Router acts as intelligent dispatcher:**

- ✅ Simple queries ("Hello") → Router answers directly
- ✅ Complex queries ("What is Python?") → Router delegates to reasoning model
- ✅ Code queries ("Fix this bug") → Router delegates to coding model
- ✅ Full telemetry of routing decisions

**Desired Flow:**

```
INIT → LLM_CALL(router) → LLM_CALL(reasoning) → SYNTHESIS → COMPLETED
         ↓                      ↓
    "Use reasoning model"   deepseek-r1-14b generates answer
    (2 models, 2 steps)
```

---

## 2. Goals & Success Criteria

### 2.1 Goals

1. **Router makes intelligent routing decisions** based on query complexity
2. **Multi-model coordination** works end-to-end
3. **Full telemetry** captures routing decisions and model usage
4. **Deterministic and testable** routing logic

### 2.2 Success Criteria

| Criterion | Measurement | Target |
|-----------|-------------|--------|
| **Routing Accuracy** | Manual evaluation of 20 test queries | >90% correct model selection |
| **Simple Query Handling** | "Hello", "What time is it?" | Router answers directly |
| **Complex Query Delegation** | "What is Python?", "Explain X" | Delegated to reasoning model |
| **Code Query Delegation** | "Fix this bug", "Implement X" | Delegated to coding model |
| **Telemetry Completeness** | Routing decision logged | 100% of decisions traced |
| **E2E Test Pass** | Existing E2E tests still pass | 100% pass rate |

---

## 3. Design

### 3.1 Architecture

**Component:** Orchestrator (`src/personal_agent/orchestrator/executor.py`)

**New Flow:**

```python
async def step_llm_call(ctx, session_manager, trace_ctx):
    """Execute LLM call with intelligent model selection."""

    # Stage 1: Determine model role
    if ctx.selected_model_role is None:  # First call
        model_role = determine_initial_model_role(ctx)
    else:  # Routing decision made
        model_role = ctx.selected_model_role

    # Stage 2: Call LLM
    response = await llm_client.respond(role=model_role, messages=ctx.messages)

    # Stage 3: Handle routing decisions
    if model_role == ModelRole.ROUTER and response.contains_routing_decision():
        # Router delegated to another model
        ctx.selected_model_role = parse_routing_decision(response)
        ctx.messages.append({"role": "assistant", "content": response.content})
        return TaskState.LLM_CALL  # Loop back for delegated model

    # Stage 4: Final response
    ctx.final_reply = response.content
    return TaskState.SYNTHESIS
```

### 3.2 Routing Decision Format

**Router Output (JSON):**

```json
{
  "routing_decision": "DELEGATE",
  "target_model": "REASONING",
  "confidence": 0.95,
  "reasoning_depth": 8,
  "reason": "Complex explanation requiring deep analysis"
}
```

**Or (Simple Handling):**

```json
{
  "routing_decision": "HANDLE",
  "response": "Hello! How can I help you today?"
}
```

### 3.3 Router System Prompt

See `../research/router_prompt_patterns_best_practices_2025-12-31.md` Section 5 for full template.

**Key Elements:**

1. **Role definition** ("You are a task classifier...")
2. **Model capabilities** (ROUTER, REASONING, CODING specs)
3. **Decision criteria** (complexity thresholds)
4. **Output format** (JSON schema)
5. **Examples** (3-5 edge cases)

---

## 4. Implementation Tasks

### 4.1 Day 1: Router Prompt & Types

**Duration:** 3-4 hours

**Tasks:**

1. **Create router prompt templates** (`src/personal_agent/orchestrator/prompts.py`):

   ```python
   ROUTER_SYSTEM_PROMPT = """..."""  # From research doc
   ROUTER_USER_TEMPLATE = """..."""
   ```

2. **Define routing decision types** (`src/personal_agent/orchestrator/types.py`):

   ```python
   class RoutingDecision(str, Enum):
       HANDLE = "HANDLE"       # Router answers directly
       DELEGATE = "DELEGATE"   # Delegate to another model

   class RoutingResult(TypedDict):
       decision: RoutingDecision
       target_model: ModelRole | None  # If DELEGATE
       confidence: float
       reasoning_depth: int
       reason: str
       response: str | None  # If HANDLE
   ```

3. **Add routing context to ExecutionContext**:

   ```python
   @dataclass
   class ExecutionContext:
       ...
       selected_model_role: ModelRole | None = None  # NEW
       routing_history: list[RoutingResult] = field(default_factory=list)  # NEW
   ```

**Acceptance:**

- ✅ `prompts.py` created with router templates
- ✅ `RoutingDecision` and `RoutingResult` types defined
- ✅ `ExecutionContext` updated
- ✅ Type checking passes (`mypy src/personal_agent/orchestrator/types.py`)

---

### 4.2 Day 2: Routing Logic Implementation

**Duration:** 4-5 hours

**Tasks:**

1. **Implement `determine_initial_model_role()`**:

   ```python
   def determine_initial_model_role(ctx: ExecutionContext) -> ModelRole:
       """Determine initial model role based on channel and context."""
       if ctx.channel == Channel.CODE_TASK:
           return ModelRole.CODING  # Skip router for code tasks
       elif ctx.channel == Channel.SYSTEM_HEALTH:
           return ModelRole.ROUTER  # Let router decide if tools needed
       else:  # CHAT
           return ModelRole.ROUTER  # Always start with router
   ```

2. **Implement `parse_routing_decision()`**:

   ```python
   def parse_routing_decision(response: LLMResponse) -> ModelRole:
       """Parse routing decision from router model response."""
       try:
           # Extract JSON from response
           content = response["content"]
           routing_data = extract_json(content)

           decision = RoutingDecision(routing_data["routing_decision"])

           if decision == RoutingDecision.HANDLE:
               # Router handled it, no delegation
               return None
           else:  # DELEGATE
               target = routing_data["target_model"]
               return ModelRole(target)
       except (KeyError, ValueError, json.JSONDecodeError) as e:
           log.error("routing_parse_error", error=str(e))
           return None  # Fallback: no delegation
   ```

3. **Update `step_llm_call()` with routing logic**:

   ```python
   async def step_llm_call(ctx, session_manager, trace_ctx):
       """Execute LLM call with intelligent routing."""

       # Determine which model to call
       if ctx.selected_model_role is None:
           model_role = determine_initial_model_role(ctx)
       else:
           model_role = ctx.selected_model_role

       # For router, add routing prompt
       if model_role == ModelRole.ROUTER and not ctx.routing_history:
           system_prompt = format_router_prompt(ctx)
       else:
           system_prompt = None

       # Call LLM
       response = await llm_client.respond(
           role=model_role,
           messages=ctx.messages,
           system_prompt=system_prompt,
           trace_ctx=trace_ctx
       )

       # Parse routing decision if router
       if model_role == ModelRole.ROUTER:
           routing_result = parse_routing_decision(response)
           ctx.routing_history.append(routing_result)

           log.info(
               "routing_decision",
               decision=routing_result["decision"],
               target_model=routing_result.get("target_model"),
               confidence=routing_result["confidence"],
               trace_id=ctx.trace_id
           )

           if routing_result["decision"] == RoutingDecision.DELEGATE:
               # Delegate to another model
               ctx.selected_model_role = routing_result["target_model"]
               ctx.messages.append({
                   "role": "assistant",
                   "content": f"[Routing to {routing_result['target_model']}]"
               })
               return TaskState.LLM_CALL  # Loop back
           else:  # HANDLE
               # Router answered directly
               ctx.final_reply = routing_result["response"]
               return TaskState.SYNTHESIS

       # Non-router models always synthesize
       ctx.final_reply = response["content"]
       return TaskState.SYNTHESIS
   ```

4. **Add telemetry events**:

   ```python
   # src/personal_agent/telemetry/events.py
   ROUTING_DECISION = "routing_decision"
   ROUTING_DELEGATION = "routing_delegation"
   ROUTING_HANDLED = "routing_handled"
   ```

**Acceptance:**

- ✅ Routing logic implemented in `executor.py`
- ✅ Helper functions (`determine_initial_model_role`, `parse_routing_decision`) working
- ✅ Telemetry events added
- ✅ Type checking passes
- ✅ Ruff formatting clean

---

### 4.3 Day 3: Testing & Validation

**Duration:** 3-4 hours

**Tasks:**

1. **Create test suite** (`tests/test_orchestrator/test_routing.py`):

   ```python
   @pytest.mark.asyncio
   async def test_simple_query_handled_by_router():
       """Router should answer simple queries directly."""
       ctx = create_test_context(user_message="Hello")
       result = await execute_task_safe(ctx, session_manager)

       assert len(ctx.routing_history) == 1
       assert ctx.routing_history[0]["decision"] == RoutingDecision.HANDLE
       assert "Hello" in result["reply"]

   @pytest.mark.asyncio
   async def test_complex_query_delegated_to_reasoning():
       """Router should delegate complex queries to reasoning model."""
       ctx = create_test_context(user_message="What is Python?")
       result = await execute_task_safe(ctx, session_manager)

       assert len(ctx.routing_history) == 1
       assert ctx.routing_history[0]["decision"] == RoutingDecision.DELEGATE
       assert ctx.routing_history[0]["target_model"] == ModelRole.REASONING
       assert len(ctx.steps) == 2  # Router + Reasoning

   @pytest.mark.asyncio
   async def test_code_query_delegated_to_coding():
       """Router should delegate code queries to coding model."""
       ctx = create_test_context(user_message="Fix this Python bug: ...")
       result = await execute_task_safe(ctx, session_manager)

       assert ctx.routing_history[0]["target_model"] == ModelRole.CODING
   ```

2. **Manual E2E validation** (20 test queries):

   ```bash
   # Simple queries (should use router)
   python -m personal_agent.ui.cli "Hello"
   python -m personal_agent.ui.cli "What time is it?"
   python -m personal_agent.ui.cli "How are you?"

   # Complex queries (should use reasoning)
   python -m personal_agent.ui.cli "What is Python?"
   python -m personal_agent.ui.cli "Explain quantum mechanics"
   python -m personal_agent.ui.cli "Why is the sky blue?"

   # Code queries (should use coding)
   python -m personal_agent.ui.cli "Write a Python function to sort a list"
   python -m personal_agent.ui.cli "Debug this code: def foo(): return 1/0"
   ```

3. **Validate telemetry**:

   ```bash
   # Check routing decisions are logged
   grep "routing_decision" logs/personal_agent.log | jq .

   # Verify multi-model traces
   python -c "from personal_agent.telemetry import reconstruct_trace; \
              trace = reconstruct_trace('TRACE_ID'); \
              print(f'Models used: {trace.models_called}')"
   ```

4. **Performance benchmarking**:

   ```python
   # tests/evaluation/routing_performance.py

   async def benchmark_routing_overhead():
       """Measure routing decision overhead."""
       queries = ["Hello", "What is Python?", "Fix bug: ..."]

       for query in queries:
           start = time.time()
           result = await execute_query(query)
           elapsed = time.time() - start

           routing_time = result.routing_decision_ms
           total_time = elapsed * 1000
           overhead_pct = (routing_time / total_time) * 100

           print(f"{query[:30]}: {routing_time}ms routing ({overhead_pct:.1f}% overhead)")

   # Target: Routing overhead <200ms (per ADR-0008)
   ```

**Acceptance:**

- ✅ All unit tests pass (`pytest tests/test_orchestrator/test_routing.py`)
- ✅ Manual E2E tests show correct routing (>90% accuracy)
- ✅ Telemetry captures all routing decisions
- ✅ Routing overhead <200ms
- ✅ Existing E2E tests still pass

---

## 5. Success Metrics

| Metric | Target | Measurement Method |
|--------|--------|--------------------|
| **Routing Accuracy** | >90% | Manual evaluation of 20 test queries |
| **Simple Query Speed** | <1s | Router answers directly, no delegation |
| **Complex Query Quality** | >8/10 | Human rating of reasoning model responses |
| **Routing Overhead** | <200ms | Time to make routing decision |
| **Telemetry Coverage** | 100% | All routing decisions logged with trace_id |
| **Test Coverage** | >90% | pytest-cov on routing logic |

---

## 6. Rollout Plan

### 6.1 Phase 1: Implementation (Days 1-3)

- Day 1: Prompts, types, context updates
- Day 2: Routing logic implementation
- Day 3: Testing and validation

### 6.2 Phase 2: Monitoring (Week 1 after deploy)

- Monitor routing accuracy via telemetry
- Collect user feedback on response quality
- Adjust routing thresholds if needed

### 6.3 Phase 3: Optimization (Week 2-3)

- Fine-tune router prompt based on failures
- Add complexity heuristics (query length, keywords)
- Implement caching for common routing decisions

---

## 7. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Router makes wrong routing decisions** | Medium | High | A/B test routing logic, add fallback heuristics |
| **Routing adds too much latency** | Low | Medium | Optimize prompt, cache decisions |
| **Router fails to parse JSON** | Medium | Medium | Add robust JSON extraction, fallback to heuristics |
| **Complex queries still use router** | Low | High | Adjust complexity thresholds, add more examples |
| **Breaking existing E2E tests** | Low | High | Run full test suite before merge |

---

## 8. Open Questions

1. **Should we add caching for routing decisions?**
   - Answer: Phase 3 optimization, after collecting routing patterns

2. **How do we handle ambiguous queries?**
   - Answer: Use confidence scores, default to REASONING if confidence <0.7

3. **Should we support hybrid routing (multiple models)?**
   - Answer: Future enhancement (Phase 2), keep simple for MVP

4. **How do we validate routing accuracy continuously?**
   - Answer: Log routing decisions + user feedback, weekly review

5. **Can the agent improve its own routing configuration?**
   - Answer: **YES!** See `../architecture/ROUTER_SELF_TUNING_ARCHITECTURE_v0.1.md`
   - Phase 2-3: Agent analyzes telemetry, proposes improvements to prompts/thresholds/parameters
   - Metacognitive feedback loop: Observe → Analyze → Propose → Validate → Apply (with approval)
   - Expected outcome: Routing accuracy improves from 85% (baseline) → 94%+ (self-tuned)

---

## 9. Related Documents

**Research:**

- `../research/router_prompt_patterns_best_practices_2025-12-31.md` — Router prompt best practices

**Architecture:**

- `../architecture/ORCHESTRATOR_CORE_SPEC_v0.1.md` (Section 3: LLM Client Interaction)
- `../architecture/ROUTER_SELF_TUNING_ARCHITECTURE_v0.1.md` — **Self-improving routing (Phase 2-3)**
- `../architecture_decisions/ADR-0003-model-stack.md` (Model roles)
- `../architecture_decisions/ADR-0008-model-stack-course-correction.md` (Routing patterns)

**Implementation:**

- `src/personal_agent/orchestrator/executor.py` (Main implementation)
- `src/personal_agent/orchestrator/prompts.py` (Router prompts)
- `tests/test_orchestrator/test_routing.py` (Test suite)

**Roadmap:**

- `./IMPLEMENTATION_ROADMAP.md` (Day 11.5: Router Routing Logic)

---

## 10. Approval Criteria

This plan is approved when:

1. ✅ Research document reviewed (`router_prompt_patterns_best_practices_2025-12-31.md`)
2. ✅ Implementation plan reviewed (this document)
3. ✅ ADR-0003 updated with routing logic section
4. ✅ Roadmap updated with Day 11.5 task
5. ✅ Project owner approval obtained

---

**Status:** Proposed, Awaiting Approval
**Next Actions:**

1. Review research document and implementation plan
2. Update ADR-0003 with routing section
3. Update roadmap with Day 11.5
4. Begin implementation (Day 1)

**Estimated Total Time:** 2-3 days (10-13 hours)
