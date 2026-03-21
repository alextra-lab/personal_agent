> **SUPERSEDED (2026-03-21)**: The hierarchical specialist routing patterns described here (MoMA, LLMRouter) were considered but ultimately rejected. Redesign v2 chose single-brain + delegation over specialist agent routing. See [Cognitive Architecture Redesign v2](../specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md) Section 1.2 for rationale. Retained for historical reference.

---

# Intelligent Routing Patterns — Inspiration from MoMA & LLMRouter

**Version:** 0.1
**Date:** 2025-12-31
**Status:** Inspirational / Future Roadmap
**Research Sources:** MoMA (arXiv:2509.07571), LLMRouter (UIUC), Multi-Agent RAG patterns

---

## Purpose

This document captures **architectural patterns and insights** from state-of-the-art routing research that can inform future evolution of the Personal Agent's orchestration layer. These patterns are **not for MVP** but provide a north star for Phases 3-5.

**Core Research Insight:**

> Modern AI systems achieve optimal performance through **hierarchical routing** where lightweight models make **fast, correct decisions** about which specialized models or agents should handle each task, rather than using one-size-fits-all approaches.

---

## 1. The MoMA (Mixture of Models and Agents) Pattern

### 1.1 Three-Stage Architecture

**Reference:** arXiv:2509.07571v1 — "Model and Agent Orchestration for Adaptive and Efficient Inference"

```
┌─────────────────────────────────────────────────────────┐
│                    User Query                            │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
         ┌───────────────────────┐
         │   Stage 1: CLASSIFY    │ ◄── Router Model (Qwen3-4B)
         │   Agent vs LLM?        │
         └───────┬───────────────┬┘
                 │               │
        ┌────────▼────────┐     │
        │  Deterministic   │     │
        │  Agent Path      │     │
        │                  │     │
        │ • Tool-only      │     │
        │ • Retrieval-only │     │
        │ • API calls      │     │
        └──────────────────┘     │
                                 │
                       ┌─────────▼──────────┐
                       │  Stage 2: SELECT   │ ◄── Router Model
                       │  Which LLM?        │
                       │                    │
                       │ • Complexity score │
                       │ • Cost/performance │
                       │ • Context needs    │
                       └─────────┬──────────┘
                                 │
                    ┌────────────┼────────────┐
                    │            │            │
              ┌─────▼─────┐ ┌───▼────┐ ┌────▼─────┐
              │  Router    │ │Reasoning│ │ Coding   │
              │  Model     │ │ Model  │ │  Model   │
              │  (Fast)    │ │(Deep)  │ │(Special.)│
              └─────┬──────┘ └───┬────┘ └────┬─────┘
                    │            │            │
                    └────────────┼────────────┘
                                 │
                       ┌─────────▼──────────┐
                       │ Stage 3: VALIDATE  │ ◄── Validation Model
                       │                    │
                       │ • Format check     │
                       │ • Grounding verify │
                       │ • Policy enforce   │
                       └─────────┬──────────┘
                                 │
                                 ▼
                           Final Response
```

### 1.2 Key Insights from MoMA

**Efficiency Through Routing:**
> "MoMA uses a trained router based on Qwen3 architecture with a mixture-of-experts (MoE) head to predict performance scores across candidate models."

**Translation for Our System:**

```python
# Current (MVP): Static role-based routing
model_role = determine_role_from_channel(channel)
response = await llm_client.respond(role=model_role, ...)

# Future (Phase 3): Dynamic performance-based routing
@dataclass
class RoutingDecision:
    selected_model: ModelRole
    confidence: float
    reasoning: str
    estimated_cost: float
    estimated_latency_ms: float

async def intelligent_route(query: str, context: ExecutionContext) -> RoutingDecision:
    """MoMA-inspired routing with performance prediction."""

    # Analyze query complexity
    complexity_features = await router_model.extract_features(query)

    # Predict performance for each candidate model
    predictions = {}
    for candidate in [ModelRole.ROUTER, ModelRole.REASONING, ModelRole.CODING]:
        predictions[candidate] = await router_model.predict_performance(
            query_features=complexity_features,
            model=candidate,
            metrics=["accuracy", "latency", "cost"]
        )

    # Select optimal model based on multi-objective optimization
    selected = optimize_selection(
        predictions=predictions,
        constraints=context.governance_constraints,
        preferences={"accuracy": 0.6, "latency": 0.3, "cost": 0.1}
    )

    return RoutingDecision(
        selected_model=selected.model,
        confidence=selected.confidence,
        reasoning=f"Predicted {selected.accuracy:.1%} accuracy with {selected.latency}ms latency",
        estimated_cost=selected.cost,
        estimated_latency_ms=selected.latency
    )
```

**Context-Aware Finite State Machine:**
> "MoMA employs a context-aware finite state machine with dynamic token masking for precise agent selection."

**Translation:** Our TaskState machine (ADR-0006) can be enhanced with:

```python
class ContextAwareTaskState(TaskState):
    """Enhanced state with context tracking."""

    def get_valid_transitions(self, context: ExecutionContext) -> list[TaskState]:
        """Only allow transitions that make sense given context."""

        if self == TaskState.ROUTING_DECISION:
            # If query is simple tool call, skip LLM entirely
            if context.query_complexity < 2:
                return [TaskState.TOOL_ONLY_EXECUTION]
            # If complex reasoning needed, require model selection
            elif context.query_complexity > 7:
                return [TaskState.MODEL_SELECTION]
            # Otherwise, either path valid
            else:
                return [TaskState.MODEL_SELECTION, TaskState.TOOL_ONLY_EXECUTION]

        # ... other state-specific logic
```

**Takeaway for Personal Agent:**

- ✅ **Adopt three-stage pattern** in Phase 3
- ✅ **Train router to predict model performance** on our tasks
- ✅ **Use multi-objective optimization** (accuracy + latency + cost)
- ✅ **Make state machine context-aware** based on query analysis

---

## 2. The LLMRouter Pattern (UIUC)

### 2.1 Four Families of Routing Algorithms

**Reference:** "LLMRouter: An Intelligent Routing System for Multi-LLM Inference" (2025)

```
┌──────────────────────────────────────────────────────────┐
│              LLMRouter Algorithm Families                │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  1. SINGLE-ROUND ROUTING                                │
│     • Matrix factorization routing                       │
│     • Similarity-based routing (query ↔ model profiles)│
│     • Causal inference-based routing                    │
│     ► Use when: Fast decision needed, no iteration      │
│                                                          │
│  2. MULTI-ROUND ROUTING                                 │
│     • Router R1: Sequential decision process + RL       │
│     • Iterative refinement based on intermediate results│
│     • Dynamic model switching mid-task                  │
│     ► Use when: Complex tasks, quality > speed          │
│                                                          │
│  3. PERSONALIZED ROUTING                                │
│     • User preference learning                          │
│     • Historical performance tracking per-user          │
│     • Adaptive model selection based on feedback        │
│     ► Use when: Multiple users, diverse preferences     │
│                                                          │
│  4. AGENTIC ROUTING                                     │
│     • Task decomposition into subtasks                  │
│     • Different models for different subtasks           │
│     • Synthesis across model outputs                    │
│     ► Use when: Complex workflows, specialization gains │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### 2.2 Router R1: Sequential Decision Framework

**Key Innovation:** Routing as a **Markov Decision Process (MDP)**

```python
# Conceptual implementation for Personal Agent (Phase 4+)

@dataclass
class RouterState:
    """State in routing MDP."""
    query: str
    query_embedding: np.ndarray
    previous_models_tried: list[ModelRole]
    previous_responses: list[str]
    previous_qualities: list[float]
    remaining_budget: CostBudget
    current_step: int

@dataclass
class RouterAction:
    """Action in routing MDP."""
    model_to_try: ModelRole
    confidence_threshold: float  # Retry if below this
    max_tokens: int
    temperature: float

class RouterR1Agent:
    """RL-trained routing agent inspired by LLMRouter."""

    def __init__(self):
        self.policy_network = None  # Trained with PPO/DQN
        self.value_network = None

    async def select_next_model(self, state: RouterState) -> RouterAction:
        """Select next model to try based on current state."""

        # Encode state
        state_encoding = self.encode_state(state)

        # Policy network outputs action distribution
        action_logits = self.policy_network(state_encoding)

        # Sample action (or take argmax for deterministic)
        action = self.sample_action(action_logits)

        return action

    def should_retry(self, response: LLMResponse, state: RouterState) -> bool:
        """Decide if we should try a different model."""

        # Estimate response quality
        quality_score = self.estimate_quality(response, state)

        # Check if below confidence threshold
        if quality_score < state.current_action.confidence_threshold:
            # Try different model if budget allows
            if state.remaining_budget.can_afford_retry():
                return True

        return False

    async def train_on_episode(self, episode: RoutingEpisode):
        """Update policy based on episode outcome."""

        # Compute rewards
        rewards = self.compute_rewards(
            episode.actions,
            episode.responses,
            episode.final_quality,
            episode.total_cost,
            episode.total_latency
        )

        # Update policy network (PPO/DQN)
        self.update_policy(episode, rewards)
```

**Reward Function Design:**

```python
def compute_routing_reward(
    final_quality: float,      # Did we get good response? (0-1)
    total_cost: float,          # How much did it cost? (normalized)
    total_latency: float,       # How long did it take? (normalized)
    num_steps: int,             # How many models tried?
) -> float:
    """Multi-objective reward for routing decisions."""

    # Weighted combination of objectives
    reward = (
        0.6 * final_quality           # Prioritize correctness
        - 0.2 * total_cost            # Penalize expensive routes
        - 0.15 * total_latency        # Penalize slow routes
        - 0.05 * (num_steps - 1)      # Penalize extra steps
    )

    return reward
```

**Takeaway for Personal Agent:**

- **Phase 3:** Implement **single-round routing** (fast, simple)
- **Phase 4:** Add **multi-round routing** for complex tasks
- **Phase 5+:** Experiment with **RL-trained router** (Router R1 style)
- **Skip personalization** (single user, but track preferences in Captain's Log)
- **Adopt agentic routing pattern** for multi-step workflows

---

## 3. Interleaved Retrieval and Reasoning Pattern

### 3.1 Multi-Agent RAG Architecture

**Reference:** "Multi Agent RAG with Interleaved Retrieval and Reasoning for Long-Context Tasks" (Pathway.com)

**Problem with Traditional RAG:**
```
Query → Retrieve All Docs → Reason Once → Answer
         ↑                      ↑
    May miss context      Single-shot, no iteration
```

**Interleaved Pattern:**
```
Query → Initial Retrieval → Reason → Need More Info? → Retrieve Again → Reason → Answer
                              ↓                            ↑
                         Check sufficiency ───────────────┘
```

**Implementation for Personal Agent (Phase 3 - when RAG added):**

```python
class InterleavedRAGOrchestrator:
    """Orchestrator that interleaves retrieval and reasoning."""

    async def execute_rag_task(self, query: str, max_iterations: int = 3) -> str:
        """Execute RAG with iterative refinement."""

        context_docs = []
        reasoning_trace = []

        for iteration in range(max_iterations):
            # Reasoning step: What do we know? What do we need?
            reasoning_result = await self.reasoning_model.analyze(
                query=query,
                current_context=context_docs,
                previous_reasoning=reasoning_trace
            )

            # Check if we have sufficient information
            if reasoning_result.is_sufficient:
                # Generate final answer
                return await self.reasoning_model.synthesize(
                    query=query,
                    context=context_docs,
                    reasoning=reasoning_trace
                )

            # Retrieval step: Get missing information
            missing_info = reasoning_result.missing_information
            new_docs = await self.retrieval_agent.retrieve(
                queries=missing_info,
                exclude=context_docs  # Don't retrieve same docs
            )

            context_docs.extend(new_docs)
            reasoning_trace.append(reasoning_result)

        # Max iterations reached, generate best-effort answer
        return await self.reasoning_model.synthesize(...)
```

**Example Execution:**

```
User: "How has my system's memory usage trended over the last week?"

Iteration 1:
  Retrieve: Current memory metrics
  Reason: "I have current state, but no historical data. Need time-series data."
  → Need more info

Iteration 2:
  Retrieve: Historical memory logs from telemetry
  Reason: "I have raw logs, but they're unprocessed. Need aggregated stats."
  → Need more info

Iteration 3:
  Retrieve: Pre-computed weekly memory statistics
  Reason: "Now I have current + historical + aggregated data. Sufficient."
  → Generate answer: "Memory usage increased 15% over last week, from avg 45GB to 52GB..."
```

**Benefits:**

1. ✅ **12.1% average improvement** over single-shot retrieval (research finding)
2. ✅ **Handles multi-hop reasoning** naturally
3. ✅ **Adapts to query complexity** (simple queries = 1 iteration, complex = 3+)
4. ✅ **Explicit reasoning trace** for observability

**Takeaway for Personal Agent:**

- **Phase 3:** Implement interleaved RAG when adding retrieval capabilities
- **Integration point:** New `TaskState.ITERATIVE_RAG` in orchestrator
- **Specialized agent:** Dedicated retrieval model (lightweight, fast)

---

## 4. Hierarchical Agent Coordination Patterns

### 4.1 The "Agents as Tools" Pattern

**Concept:** Instead of free-form multi-agent conversation, **coordinator agent invokes specialized agents as tools**.

```
┌─────────────────────────────────────────────────────────┐
│              COORDINATOR AGENT                          │
│                                                         │
│  • Receives user query                                 │
│  • Decomposes into subtasks                            │
│  • Invokes specialized agents as needed                │
│  • Synthesizes final response                          │
└─────────────────┬───────────────────────────────────────┘
                  │
    ┌─────────────┼─────────────┬─────────────┬──────────┐
    │             │             │             │          │
┌───▼───┐   ┌────▼────┐   ┌────▼────┐   ┌───▼────┐  ┌──▼──┐
│Retriev│   │Reasoning│   │  Coding │   │Summari-│  │Valid│
│ Agent │   │  Agent  │   │  Agent  │   │zation  │  │Agent│
└───────┘   └─────────┘   └─────────┘   └────────┘  └─────┘
    │             │             │             │          │
    └─────────────┴─────────────┴─────────────┴──────────┘
                              │
                    Specialized Capabilities
```

**Implementation in Personal Agent:**

```python
# This aligns with our current architecture!
# Our LocalLLMClient already treats models as "tools" for the orchestrator

class OrchestratorCoordinator:
    """Coordinator that treats specialized models as tools."""

    def __init__(self):
        self.llm_client = LocalLLMClient()
        self.tool_executor = ToolExecutionLayer()

    async def execute_complex_task(self, query: str) -> str:
        """Coordinate multiple agents/models to solve complex task."""

        # Decompose task (using reasoning model)
        subtasks = await self.llm_client.respond(
            role=ModelRole.REASONING,
            messages=[{
                "role": "system",
                "content": TASK_DECOMPOSITION_PROMPT
            }, {
                "role": "user",
                "content": f"Break down this task: {query}"
            }]
        )

        # Execute subtasks in parallel or sequence
        subtask_results = []
        for subtask in subtasks.tool_calls:
            if subtask.name == "code_generation":
                result = await self.llm_client.respond(
                    role=ModelRole.CODING,
                    messages=[{"role": "user", "content": subtask.arguments["prompt"]}]
                )
            elif subtask.name == "retrieval":
                result = await self.llm_client.respond(
                    role=ModelRole.RETRIEVAL,  # New in Phase 3
                    messages=[{"role": "user", "content": subtask.arguments["query"]}]
                )
            elif subtask.name == "tool_execution":
                result = await self.tool_executor.execute_tool(
                    subtask.arguments["tool_name"],
                    subtask.arguments
                )

            subtask_results.append(result)

        # Synthesize final response (using reasoning model)
        final_response = await self.llm_client.respond(
            role=ModelRole.REASONING,
            messages=[{
                "role": "system",
                "content": "Synthesize these subtask results into a coherent response."
            }, {
                "role": "user",
                "content": format_subtask_results(subtask_results)
            }]
        )

        return final_response.content
```

**This is exactly our current architecture!** We're already using the "Agents as Tools" pattern.

**Enhancement:** Make it **explicit** in the orchestrator:

```python
# Add to orchestrator/executor.py
class TaskState(str, Enum):
    # ... existing states
    TASK_DECOMPOSITION = "task_decomposition"  # NEW
    PARALLEL_EXECUTION = "parallel_execution"   # NEW
    SEQUENTIAL_EXECUTION = "sequential_execution"  # NEW
    RESULT_SYNTHESIS = "result_synthesis"       # NEW

async def step_task_decomposition(ctx: ExecutionContext) -> TaskState:
    """Decompose complex task into subtasks."""

    # Use reasoning model to create execution plan
    decomposition = await llm_client.respond(
        role=ModelRole.REASONING,
        messages=[
            {"role": "system", "content": TASK_PLANNER_PROMPT},
            {"role": "user", "content": ctx.user_message}
        ],
        tools=SUBTASK_TOOLS  # Available subtask types
    )

    # Parse subtasks
    ctx.subtasks = parse_subtasks(decomposition.tool_calls)

    # Determine execution strategy
    if can_parallelize(ctx.subtasks):
        return TaskState.PARALLEL_EXECUTION
    else:
        return TaskState.SEQUENTIAL_EXECUTION
```

**Takeaway for Personal Agent:**

- ✅ **Already using this pattern** implicitly
- **Phase 2:** Make it **explicit** with decomposition state
- **Phase 3:** Add **parallel subtask execution** with asyncio.gather
- **Phase 4:** Add **dependency-aware scheduling** for subtasks

---

## 5. Validation and Quality Assurance Patterns

### 5.1 Critic Agent Pattern

**Concept:** Before returning response to user, **validation agent** checks quality.

```python
class ValidationAgent:
    """Dedicated agent for output validation."""

    async def validate_response(
        self,
        query: str,
        response: str,
        tool_results: list[ToolResult],
        governance_mode: Mode
    ) -> ValidationResult:
        """Validate LLM response before user delivery."""

        checks = await asyncio.gather(
            self.check_grounding(response, tool_results),
            self.check_hallucination(response, query),
            self.check_policy_compliance(response, governance_mode),
            self.check_format(response),
            self.check_completeness(response, query)
        )

        return ValidationResult(
            passed=all(c.passed for c in checks),
            failures=[c for c in checks if not c.passed],
            confidence=sum(c.confidence for c in checks) / len(checks)
        )

    async def check_grounding(
        self,
        response: str,
        tool_results: list[ToolResult]
    ) -> ValidationCheck:
        """Verify response is grounded in tool outputs, not hallucinated."""

        # Use validation model to analyze
        validation_response = await llm_client.respond(
            role=ModelRole.VALIDATION,
            messages=[{
                "role": "system",
                "content": """You are a validation agent. Check if the response is grounded in the tool results.

Rules:
- Every claim must be traceable to tool results
- No fabricated data or speculation
- Flag any statements without evidence
"""
            }, {
                "role": "user",
                "content": f"""
Response to validate:
{response}

Tool results:
{format_tool_results(tool_results)}

Is this response grounded? Output JSON:
{{"grounded": true/false, "ungrounded_claims": [...], "confidence": 0.0-1.0}}
"""
            }]
        )

        result = json.loads(validation_response.content)
        return ValidationCheck(
            name="grounding",
            passed=result["grounded"],
            confidence=result["confidence"],
            details=result.get("ungrounded_claims", [])
        )
```

**Example Validation Flow:**

```
User Query: "How is my Mac's health?"
Tool Results: {"cpu_usage": 45, "memory_usage": 60, "disk_usage": 75}

LLM Response: "Your Mac is healthy. CPU at 45%, memory at 60%, disk at 75%.
               The SSD has 2TB free space and will last another 5 years."
               ↑ GROUNDED     ↑ GROUNDED    ↑ GROUNDED
               ↑ NOT IN TOOL RESULTS - HALLUCINATION!

Validation Agent: ❌ INVALID
  - Ungrounded claim: "SSD has 2TB free space" (not in tool results)
  - Ungrounded claim: "will last another 5 years" (speculation)

Action: Retry LLM call with stricter prompt, or flag for human review
```

**Takeaway for Personal Agent:**

- **Phase 2:** Implement basic validation (format, policy checks)
- **Phase 3:** Add validation model (reuse Qwen3-4B router)
- **Phase 4:** Advanced grounding verification with tool result cross-reference

---

## 6. Cost and Performance Optimization Patterns

### 6.1 Adaptive Model Selection Based on Budget

```python
class CostAwareusingRouter:
    """Router that balances quality and cost."""

    def __init__(self):
        self.model_costs = {
            ModelRole.ROUTER: 0.1,      # Relative cost units
            ModelRole.REASONING: 1.0,
            ModelRole.CODING: 0.8,
        }
        self.model_quality = {
            # Estimated from benchmarks/telemetry
            ModelRole.ROUTER: 0.75,
            ModelRole.REASONING: 0.95,
            ModelRole.CODING: 0.90,
        }

    async def select_model(
        self,
        query: str,
        remaining_budget: float,
        quality_threshold: float = 0.85
    ) -> ModelRole:
        """Select model that meets quality threshold within budget."""

        # Estimate required quality for this query
        estimated_difficulty = await self.estimate_difficulty(query)
        required_quality = quality_threshold * estimated_difficulty

        # Find cheapest model that meets quality requirement
        viable_models = [
            (model, cost)
            for model, cost in self.model_costs.items()
            if self.model_quality[model] >= required_quality
            and cost <= remaining_budget
        ]

        if not viable_models:
            raise InsufficientBudgetError(
                f"No model can meet quality {required_quality} within budget {remaining_budget}"
            )

        # Select cheapest viable model
        selected_model = min(viable_models, key=lambda x: x[1])[0]

        logger.info(
            "model_selected_by_cost",
            model=selected_model,
            required_quality=required_quality,
            cost=self.model_costs[selected_model],
            remaining_budget=remaining_budget
        )

        return selected_model
```

### 6.2 Caching Pattern for Repeated Queries

```python
class RouterWithCache:
    """Router that caches routing decisions for similar queries."""

    def __init__(self):
        self.decision_cache = {}  # Query embedding → RoutingDecision
        self.embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

    async def route_with_cache(self, query: str) -> RoutingDecision:
        """Check cache before making routing decision."""

        # Compute query embedding
        query_embedding = self.embedding_model.encode(query)

        # Check for similar cached query (cosine similarity > 0.9)
        for cached_embedding, cached_decision in self.decision_cache.items():
            similarity = cosine_similarity(query_embedding, cached_embedding)
            if similarity > 0.9:
                logger.info("routing_cache_hit", similarity=similarity)
                return cached_decision

        # Cache miss, make fresh routing decision
        decision = await self.make_routing_decision(query)

        # Cache for future queries
        self.decision_cache[query_embedding] = decision

        return decision
```

**Takeaway:**

- **Phase 3:** Implement cost tracking in telemetry
- **Phase 4:** Add cost-aware routing when budget constraints exist
- **Phase 4:** Experiment with routing decision caching

---

## 7. Recommended Implementation Phasing

### Phase 2 (Months 2-3): Basic Intelligent Routing

```python
# Add to orchestrator
- TaskState.ROUTING_DECISION  # Classify task type
- Basic complexity estimation
- Direct tool execution path (skip LLM for simple tasks)
- Basic validation checks (format, policy)
```

### Phase 3 (Months 4-5): Performance-Based Routing

```python
# Enhanced routing with learning
- Performance tracking per model per task type
- Historical success rate → routing decisions
- Interleaved RAG when retrieval added
- Validation agent (reuse router model)
```

### Phase 4 (Months 6-8): Advanced Optimization

```python
# Multi-objective optimization
- Cost-aware routing
- Multi-round routing for complex tasks
- Task decomposition with parallel execution
- Routing decision caching
```

### Phase 5+ (Months 9+): Research Features

```python
# Experimental / research-oriented
- RL-trained router (Router R1 style)
- Fine-tuned routing models
- Sophisticated MoE routing head
- Multi-agent debate patterns
```

---

## 8. Integration with Existing Architecture

### 8.1 Homeostasis Model Integration

Intelligent routing **enhances** homeostatic control:

```
Sensor: Query complexity, current load, model performance
Control Center: Intelligent router makes mode-aware selection
Effector: Selected model executes task
Feedback: Performance logged → improves future routing
```

### 8.2 Governance Integration

Routing respects governance constraints:

```python
async def governed_routing(
    query: str,
    mode: Mode,
    governance_config: GovernanceConfig
) -> ModelRole:
    """Route with governance constraints."""

    # Get routing recommendation
    recommendation = await intelligent_router.route(query)

    # Check mode constraints
    if mode == Mode.LOCKDOWN:
        # Only allow router model in lockdown
        return ModelRole.ROUTER
    elif mode == Mode.DEGRADED:
        # Prefer lightweight models
        if recommendation == ModelRole.REASONING:
            return ModelRole.ROUTER  # Downgrade to conserve resources

    # Check model constraints from governance config
    constraints = governance_config.models.get_constraints(mode)
    if not constraints.allows_model(recommendation):
        # Fall back to allowed model
        return constraints.default_model

    return recommendation
```

### 8.3 Telemetry Integration

All routing decisions logged:

```python
logger.info(
    "routing_decision",
    query_hash=hash(query),
    selected_model=decision.model,
    confidence=decision.confidence,
    reasoning=decision.reasoning,
    complexity_score=complexity,
    alternatives_considered=alternatives,
    mode=current_mode,
    trace_id=trace_id
)
```

---

## 9. Success Metrics for Intelligent Routing

| Metric | Baseline | Phase 2 Target | Phase 3 Target | Phase 4 Target |
|--------|----------|----------------|----------------|----------------|
| **Routing Accuracy** | N/A | >90% | >95% | >98% |
| **Task Success Rate** | 80% | 85% | 90% | 93% |
| **Avg Response Latency** | 5s | 4s | 3s | 2.5s |
| **Router Overhead** | N/A | <200ms | <150ms | <100ms |
| **Cost Efficiency** | Baseline | +20% | +35% | +50% |
| **Cache Hit Rate** | 0% | N/A | N/A | >40% |

**Cost Efficiency Calculation:**
```
efficiency = (task_success_rate × quality_score) / (total_inference_cost + routing_overhead)
```

---

## 10. Research Questions for Experimentation

1. **What percentage of queries can skip LLM entirely?**
   - Hypothesis: 30-40% are simple tool calls
   - Experiment: Classify 1000 real queries, measure direct-tool success rate

2. **How much does validation agent improve output quality?**
   - Hypothesis: 10-15% reduction in hallucinations
   - Experiment: A/B test with/without validation, measure user corrections

3. **What's the optimal cache similarity threshold?**
   - Hypothesis: 0.9 cosine similarity balances hits vs false positives
   - Experiment: Vary threshold 0.85-0.95, measure cache hits + query satisfaction

4. **How much does multi-round routing cost vs benefit?**
   - Hypothesis: Complex tasks see 20% quality boost, simple tasks waste 2x latency
   - Experiment: Track quality × latency for single vs multi-round

5. **Can we learn query → model mapping from telemetry?**
   - Hypothesis: After 1000 queries, learned routing matches expert routing
   - Experiment: Train classifier on telemetry, compare to rule-based routing

---

## 11. Key Takeaways

### What We're Already Doing Right ✅

- ✅ **Three-role model stack** (validated by research)
- ✅ **Deterministic orchestration** (single-agent + router pattern)
- ✅ **Role-based abstraction** (models as tools)
- ✅ **Explicit state machine** (ADR-0006)
- ✅ **Configuration-driven model selection**

### What We Should Add 🎯

- 🎯 **Three-stage routing** (classify → select → validate)
- 🎯 **Performance-based model selection** (learn from telemetry)
- 🎯 **Task decomposition with parallel execution**
- 🎯 **Validation agent** for quality assurance
- 🎯 **Cost-aware routing** when budget constraints exist

### What We Should Experiment With 🔬

- 🔬 **RL-trained router** (Router R1 approach)
- 🔬 **Routing decision caching**
- 🔬 **Multi-round routing for complex tasks**
- 🔬 **Fine-tuned routing models**

---

## Conclusion

The research into MoMA, LLMRouter, and modern multi-agent patterns **validates our core architectural choices** while revealing clear paths for enhancement. Our **single-agent + deterministic orchestration** approach is the recommended pattern for local, governed AI systems.

The key insight: **Intelligent routing is not about agent autonomy—it's about making smart, fast decisions about which specialized model should handle each task.**

We can evolve from:
- **MVP:** Static role-based routing
- **Phase 2-3:** Dynamic performance-based routing
- **Phase 4+:** Multi-objective optimized routing with learning

All while maintaining **determinism, observability, and governance**—the non-negotiables of the homeostasis model.

---

**Document Status:** Inspirational Roadmap Complete
**Next Actions:**
1. Incorporate Phase 2 enhancements into roadmap
2. Create experiment proposals for Phase 3-4 features
3. Track research developments in MoMA, LLMRouter, and related systems

**Related Documents:**
- `../research/model_orchestration_research_analysis_2025-12-31.md`
- `ADR-0002-orchestrator-style.md`
- `ADR-0006-orchestrator-runtime-structure.md`
- `./HOMEOSTASIS_MODEL.md`
