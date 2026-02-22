# ADR-0017: Multi-Agent Orchestration with Specialized Sub-Agents

**Status**: Proposed
**Date**: 2026-02-22
**Deciders**: System Architect
**Related**: ADR-0016 (Service Architecture), ADR-0003 (Model Stack), ADR-0008 (Model Stack Course Correction)

---

## 1. Context

### Current Architecture: Single-Agent Pipeline

The Personal Agent currently operates as a **single-agent system**. Every user request flows through one orchestrator that selects one model role (router, standard, reasoning, or coding) and executes linearly:

```
User → Orchestrator → LLM (one model) → Tools → Response
```

This was the correct architecture for Phase 1-2, where the priority was building foundational infrastructure (service, memory, telemetry). However, industry-wide evidence and our own research (see `docs/research/ARCHITECTURE_ASSESSMENT_2026-02-22.md`) indicate this is no longer sufficient.

### Problems with Single-Agent

| Problem | Impact | Evidence |
|---------|--------|----------|
| No task-type specialization | Coding tasks use the same flow as analysis tasks | All requests share one execution path |
| Router SLM underutilized | liquid/lfm2.5-1.2b exists on port 8500 but only does basic triage | Model provisioned but not integrated |
| No delegation | Complex tasks can't be decomposed into subtasks for parallel execution | Single sequential pipeline |
| One model per request | Can't combine strengths (e.g., reasoning model plans, coding model implements) | Model selection is all-or-nothing |
| Context overload | One agent accumulates all context (tools, memory, task state) | Prompt bloat on complex tasks |

### Industry Direction

The SOTA pattern (Feb 2026) is **one orchestrator/supervisor + specialized sub-agents** with deterministic workflow controls:

- OpenAI Agents SDK: delegation via handoffs (agents delegating to other agents as tools)
- LangGraph: workflows (deterministic) vs agents (dynamic tool use)
- RouteLLM: router sending simpler queries to cheaper models, harder to stronger models
- MoA (Mixture of Agents): multi-agent deliberation showing benchmark gains

Our infrastructure already supports this — we have 4 model endpoints, MCP gateway with 41 tools, and a brainstem scheduler. We just haven't connected them as a multi-agent system.

---

## 2. Decision

### Adopt hierarchical multi-agent orchestration with router SLM triage and specialized sub-agents.

### 2.1 Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Request Entry                          │
│  User → Gateway → Policy/Safety Prefilter                │
└──────────────────────┬───────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────┐
│              Router SLM (liquid/lfm2.5-1.2b)             │
│  Classifies: task type, complexity, modality, tool need  │
└──────────────────────┬───────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────┐
│           Orchestrator (Supervisor Agent)                  │
│  Manages workflow, delegates, aggregates, retries         │
└───┬──────────┬───────────┬──────────┬────────────────────┘
    │          │           │          │
┌───▼───┐ ┌───▼───┐ ┌────▼────┐ ┌───▼──────────┐
│Coder  │ │Analyst│ │Retrieval│ │   Seshat     │
│Agent  │ │Agent  │ │ Agent   │ │  (Librarian) │
│       │ │       │ │         │ │  (ADR-0018)  │
│devstr-│ │qwen3- │ │qwen3-  │ │              │
│al     │ │ 8b    │ │ 4b     │ │              │
│:8503  │ │:8502  │ │:8501   │ │              │
└───────┘ └───────┘ └────────┘ └──────────────┘
```

### 2.2 Agent Base Class

All agents implement a standard interface:

```python
class AgentSpec(BaseModel):
    """Specification for a specialized agent."""
    name: str
    description: str
    system_prompt: str
    model_role: ModelRole
    allowed_tools: list[str]
    max_iterations: int = 10
    autonomous: bool = False

class AgentResult(BaseModel):
    """Result from an agent execution."""
    success: bool
    output: str
    artifacts: list[dict[str, Any]] = []
    tool_calls: list[ToolCallRecord] = []
    cost: CostRecord | None = None
    trace_id: str
```

### 2.3 Router Classification

The router SLM classifies along multiple axes (not just "difficulty"):

- **Task type**: QA, coding, planning, analysis, retrieval, system health
- **Complexity**: simple (direct answer), moderate (tool use), complex (multi-step reasoning)
- **Tool need**: none, single tool, multi-tool orchestration
- **Memory need**: none, session context, persistent memory lookup
- **Risk level**: standard, elevated (file writes, system changes)

### 2.4 Orchestrator Behavior

The orchestrator (supervisor) receives the classified request and:

1. Selects agent(s) based on classification
2. Provides task-specific context (from Seshat if needed)
3. Delegates execution
4. Monitors progress (iteration limits, cost budgets)
5. Aggregates results
6. Handles failures (retry with different agent, escalate, reformulate)

### 2.5 Initial Agent Roster

| Agent | Model | Purpose | Tools |
|-------|-------|---------|-------|
| **Coder** | devstral-small-2 (:8503) | Code generation, debugging, refactoring | filesystem, code execution |
| **Analyst** | qwen3-8b (:8502) | Reasoning, planning, analysis, synthesis | search, memory, calculation |
| **Retrieval** | qwen3-4b (:8501) | Fast fact lookup, simple QA, context retrieval | memory, search, knowledge |
| **Seshat** | qwen3-8b (:8502) | Memory curation, context assembly (see ADR-0018) | memory (full access), knowledge graph |

---

## 3. Alternatives Considered

### Alternative A: Enhanced Single-Agent with Better Prompting

Keep one orchestrator, improve model selection and prompt engineering.

- **Pros**: Simpler, no new abstractions, lower latency for simple requests
- **Cons**: Doesn't solve context overload, can't parallelize, router SLM stays underutilized
- **Rejected because**: We already have 4 model endpoints — not using them as specialized agents is waste

### Alternative B: Full Framework Adoption (LangGraph, CrewAI, AutoGen)

Adopt an existing multi-agent framework wholesale.

- **Pros**: Battle-tested, community support, faster to prototype
- **Cons**: Heavy dependency, framework lock-in, may conflict with our biologically-inspired architecture, adds abstraction layers that obscure the research
- **Rejected because**: This is a research project — we learn more by building the orchestration ourselves, and we avoid framework coupling

### Alternative C: Microservices (Separate Processes per Agent)

Each agent runs as its own service, communicating via HTTP/gRPC.

- **Pros**: True isolation, independent scaling, language-agnostic
- **Cons**: Massive operational overhead for a single-developer research project, network latency between agents, complex deployment
- **Rejected because**: Premature for current scale; in-process agent dispatch has millisecond overhead vs. network calls

---

## 4. Consequences

### Positive

- **Specialization**: Each agent optimized for its domain (prompt, model, tools)
- **Composability**: Complex tasks decomposed into agent-appropriate subtasks
- **Observability**: Per-agent traces, cost tracking, quality metrics
- **Model utilization**: All 4 SLM endpoints actively used
- **Extensibility**: New agents added by defining an `AgentSpec`, not modifying the orchestrator
- **Research value**: Multi-agent patterns are a rich research area we can now explore

### Negative

- **Complexity**: More moving parts, harder to debug cross-agent interactions
- **Latency**: Router classification adds overhead for simple requests (mitigate with fast-path bypass)
- **Testing**: Need integration tests for agent delegation flows
- **Context transfer**: Passing context between agents requires careful design

### Risks

- Router SLM miscalibrates → wrong agent selected → quality regression (mitigate: fallback to analyst for uncertain classifications)
- Agent proliferation → maintenance burden (mitigate: start with 3-4, add only with demonstrated need)

---

## 5. Acceptance Criteria

- [ ] Agent base class implemented with standard interface
- [ ] Router SLM integrated into request flow with multi-axis classification
- [ ] Orchestrator delegates to at least 3 specialist agents
- [ ] Per-agent observability (traces, costs, quality metrics) in Elasticsearch
- [ ] Simple requests bypass router (fast path) with <50ms overhead
- [ ] Integration tests cover delegation, failure, and fallback paths
- [ ] Governance enforced per-agent (tool permissions scoped by agent role)

---

## 6. Implementation Notes

### Phase Placement

This is **Phase 2.4**, following Phase 2.3 (Homeostasis & Feedback). Phase 2.3 provides the observability infrastructure that multi-agent tracing depends on.

### Estimated Effort

- Agent base class + orchestrator refactor: 3-4 days
- Router SLM integration: 2-3 days
- Initial 3 specialist agents: 3-4 days
- Testing + observability: 2-3 days
- **Total**: ~10-14 days (2-3 weeks)

### Dependencies

- Phase 2.3 observability (for per-agent traces)
- SLM Server operational (ports 8500-8503)
- MCP Gateway tool access (already operational)
