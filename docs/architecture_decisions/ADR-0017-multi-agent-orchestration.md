# ADR-0017: Three-Tier Multi-Agent Orchestration with MoE Orchestrator and SLM Workers

**Status**: Accepted
**Date**: 2026-02-22 (Revised: 2026-03-09)
**Deciders**: System Architect
**Related**: ADR-0016 (Service Architecture), ADR-0003 (Model Stack), ADR-0008 (Model Stack Course Correction), ADR-0018 (Seshat Memory Librarian)

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
| Context overload ("context rot") | Performance degrades as the context window fills with tool output, memory, task state | Prompt bloat on complex tasks; smaller models hit this ceiling faster |
| No delegation | Complex tasks can't be decomposed into subtasks for parallel execution | Single sequential pipeline |
| One model per request | Can't combine strengths (e.g., reasoning model plans, coding model implements) | Model selection is all-or-nothing |
| Router SLM underutilized | liquid/lfm2.5-1.2b exists on port 8500 but only does basic triage | Model provisioned but not integrated |
| No cost-aware escalation | Every request uses the same compute path regardless of difficulty | Simple queries burn the same resources as complex ones |

### Industry Direction

The SOTA pattern (Feb 2026) is **Orchestrator-Worker with context isolation and tiered models**:

- **Anthropic Sub-Agents** (Claude Code): Orchestrator-Worker pattern where sub-agents receive isolated context windows and return compressed results. Internal testing showed >90% performance gains on complex tasks vs. single-agent. The primary value is **context isolation** — preventing "context rot" — not domain specialization. (Boris Cherny / Anthropic, Jan 2026)
- **OpenAI Agents SDK**: delegation via handoffs (agents delegating to other agents as tools)
- **LangGraph**: workflows (deterministic) vs agents (dynamic tool use)
- **RouteLLM**: router sending simpler queries to cheaper models, harder to stronger models
- **MoA (Mixture of Agents)**: multi-agent deliberation showing benchmark gains

**Key insight from Anthropic's approach**: sub-agents are a **context management strategy first** and a specialization strategy second. Each worker gets a fresh, focused context window — which is especially valuable for SLMs where effective context is smaller than frontier models. Workers return compressed summaries to the orchestrator, keeping the orchestrator's context clean.

Our infrastructure already supports this — we have 4 model endpoints, MCP gateway with 41 tools, and a brainstem scheduler. We just haven't connected them as a multi-agent system.

### Cost Constraint and Research Goal

This is a local-SLM research project, not a funded production system. Unlike Anthropic's recommendation to use "the best model for everything" (which assumes API-priced frontier models), we must design for **cost-effective local inference** with selective remote escalation. The research question is:

> How effective can a three-tier local-local-remote architecture be, where an MoE orchestrator manages SLM workers and escalates to a remote foundation model only when necessary?

---

## 2. Decision

### Adopt three-tier multi-agent orchestration: SLM router for triage, MoE model for orchestration/evaluation, SLM worker pool for execution, with remote foundation model escalation for tasks beyond local capability.

### 2.1 Architecture

```
Tier 3 — Remote Escalation (rare, expensive)
┌─────────────────────────────────────────────────────────────┐
│              Remote Foundation Model                         │
│  (Sonnet/Opus-class via API — called only on escalation)    │
└──────────────────────────▲──────────────────────────────────┘
                           │ escalate (when workers fail
                           │  after rework budget exhausted)
                           │
Tier 1 — Orchestrator (local, mid-size MoE)
┌──────────────────────────┴──────────────────────────────────┐
│           Orchestrator (MoE model, candidate: Qwen3.5 35B-3B)│
│  Decomposes tasks, dispatches workers, evaluates results,   │
│  decides: accept / rework / escalate                        │
└───┬───────────┬────────────┬────────────┬───────────────────┘
    │           │            │            │
Tier 2 — Worker Pool (local SLMs, fast, isolated context)
┌───▼───┐  ┌───▼────┐  ┌────▼────┐  ┌───▼──────────┐
│Worker │  │Worker  │  │Worker   │  │   Seshat     │
│ (SLM) │  │ (SLM)  │  │ (SLM)  │  │  (Librarian) │
│       │  │        │  │         │  │  (ADR-0018)  │
│ task- │  │ task-  │  │ task-   │  │              │
│scoped │  │scoped  │  │scoped   │  │ autonomous + │
│context│  │context │  │context  │  │ on-demand    │
└───────┘  └────────┘  └─────────┘  └──────────────┘
    ▲
    │
Tier 0 — Router (local, smallest SLM)
┌───┴─────────────────────────────────────────────────────────┐
│              Router SLM (liquid/lfm2.5-1.2b)                │
│  Classifies: complexity (simple/moderate/complex)           │
│              risk (read-only / state-modifying)             │
└──────────────────────────▲──────────────────────────────────┘
                           │
┌──────────────────────────┴──────────────────────────────────┐
│                    Request Entry                             │
│  User → Gateway → Policy/Safety Prefilter                   │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 Tier Definitions

| Tier | Role | Model Class | When Used | Cost |
|------|------|-------------|-----------|------|
| **Tier 0: Router** | Fast triage — classify complexity and risk | Smallest SLM (liquid/lfm2.5-1.2b, :8500) | Every request | Negligible |
| **Tier 1: Orchestrator** | Decompose tasks, dispatch workers, evaluate results, decide accept/rework/escalate | Mid-size MoE (candidate: Qwen3.5 35B-3B active) | Every non-trivial request | Low (sparse activation) |
| **Tier 2: Workers** | Execute well-scoped subtasks with isolated context, return compressed results | Local SLMs (empirically selected per task type) | Bulk of compute | Low (local inference) |
| **Tier 3: Escalation** | Handle tasks that workers cannot, after rework budget exhausted | Remote foundation model (API call) | Rare — target <10% of requests | High (per-token API cost) |

**Seshat** (ADR-0018) is a specialized Tier 2 agent with autonomous scheduling capability. It operates within the worker tier for on-demand context assembly, and independently via brainstem scheduler for curation cycles.

### 2.3 Agent Base Class

All agents implement a standard interface:

```python
class AgentTier(str, Enum):
    """Capability tier determining model class and permissions."""
    ROUTER = "router"
    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"
    ESCALATION = "escalation"

class AgentSpec(BaseModel):
    """Specification for an agent at any tier."""
    name: str
    description: str
    system_prompt: str
    tier: AgentTier
    model_role: ModelRole
    allowed_tools: list[str]
    max_iterations: int = 10
    max_rework_attempts: int = 2
    autonomous: bool = False

class AgentResult(BaseModel):
    """Result from an agent execution."""
    success: bool
    output: str
    compressed_summary: str
    artifacts: list[dict[str, Any]] = []
    tool_calls: list[ToolCallRecord] = []
    cost: CostRecord | None = None
    trace_id: str
    rework_count: int = 0
    escalated: bool = False
```

### 2.4 Router Classification

The router SLM classifies along two axes (not five — the orchestrator handles fine-grained decomposition):

- **Complexity**: simple (direct answer, no orchestration needed), moderate (single worker task), complex (multi-step, requires task decomposition)
- **Risk level**: read-only (search, retrieval, analysis) vs. state-modifying (file writes, system changes, memory mutations)

Simple/read-only requests bypass the orchestrator entirely (fast path). All other requests flow to the orchestrator.

### 2.5 Orchestrator Behavior

The orchestrator is the central intelligence of the system. It receives classified requests and runs an **evaluate-rework-escalate loop**:

1. **Decompose**: Break the task into well-scoped subtasks, each with a focused context bundle
2. **Dispatch**: Assign each subtask to a worker with isolated context (system prompt + task-specific context only — no accumulated conversation history)
3. **Evaluate**: Assess each worker's compressed result against acceptance criteria
4. **Accept**: If the result meets quality threshold, aggregate into final response
5. **Rework**: If the result is close but insufficient, send back to the worker with specific feedback (up to `max_rework_attempts`)
6. **Escalate**: If the worker fails after rework budget is exhausted, send the subtask to the remote foundation model (Tier 3)
7. **Aggregate**: Combine all subtask results into a coherent response

```
                    ┌──────────────┐
                    │  Decompose   │
                    │  task into   │
                    │  subtasks    │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
              ┌────►│  Dispatch to │
              │     │  SLM worker  │
              │     └──────┬───────┘
              │            │
              │     ┌──────▼───────┐
              │     │  Evaluate    │
              │     │  result      │
              │     └──┬───┬───┬───┘
              │        │   │   │
         rework│  accept│   │   │escalate
         (≤N)  │       │   │   │
              │   ┌────▼┐  │  ┌▼──────────┐
              └───┤ Re- │  │  │ Remote    │
                  │ work│  │  │ foundation│
                  └─────┘  │  │ model     │
                           │  └─────┬─────┘
                    ┌──────▼────────▼──┐
                    │   Aggregate      │
                    │   final response │
                    └──────────────────┘
```

### 2.6 Context Isolation (First-Class Design Principle)

Context isolation is not a side effect of multi-agent architecture — it is the **primary motivation**. Each worker receives:

- A task-specific system prompt
- Only the context relevant to its subtask (assembled by the orchestrator, with Seshat's help for memory-intensive tasks)
- No accumulated conversation history, tool output from other subtasks, or orchestrator reasoning

Workers return a **compressed summary** (not their full context) to the orchestrator. This ensures:

- **SLMs operate within their effective context window** — a 4B model with 2K tokens of focused context outperforms the same model with 8K tokens of mixed context
- **Orchestrator context stays clean** — it sees summaries, not raw tool output from every worker
- **Parallel execution is safe** — workers can't interfere with each other's context

### 2.7 Worker Pool (Empirically Selected)

Rather than pre-assigning fixed models to fixed roles, worker model selection is an **empirical question** resolved through benchmarking:

| Task Type | Candidate SLMs | Selection Criteria |
|-----------|---------------|-------------------|
| Code generation/debugging | devstral-small-2, others TBD | Pass rate on code benchmarks with isolated context |
| Reasoning/analysis | qwen3.5-4b, others TBD | Quality on reasoning tasks with compressed output |
| Fast retrieval/simple QA | qwen3.5-4b, others TBD | Latency + accuracy on retrieval tasks |
| Memory curation (Seshat) | TBD (per ADR-0018) — needs strong entity extraction | Quality of entity extraction and curation decisions |

**Note**: Worker model selection is an open research question. The models listed above are current candidates based on available infrastructure. As new SLMs become available, they should be benchmarked against these baselines with isolated-context subtasks. The model-to-task mapping is configuration, not architecture — swapping a worker model requires no code changes.

---

## 3. Alternatives Considered

### Alternative A: Enhanced Single-Agent with Better Prompting

Keep one orchestrator, improve model selection and prompt engineering.

- **Pros**: Simpler, no new abstractions, lower latency for simple requests
- **Cons**: Doesn't solve context rot, can't parallelize, router SLM stays underutilized
- **Rejected because**: We already have 4 model endpoints — not using them as a multi-agent system is waste

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

### Alternative D: Role-Based Specialization (Same Model Tier for All)

Assign fixed domain roles (Coder Agent, Analyst Agent, Retrieval Agent) each with a dedicated model, without tiered orchestration or escalation. (This was the original proposal in ADR-0017 v1.)

- **Pros**: Simple to reason about, clear agent boundaries, each agent has a domain-optimized model
- **Cons**: Specialization axis is domain (coder/analyst) rather than cognitive demand (orchestrator/worker); router must classify into correct domain (fragile); no escalation path when all local models fail; conflates "what model to use" with "what role to play"
- **Rejected because**: Anthropic's sub-agent research (Jan 2026) demonstrates that context isolation matters more than domain specialization. Tiering by cognitive demand (orchestrator vs. worker) with empirical model selection produces better results than pre-assigning models to roles. The escalation path to a remote foundation model also addresses the ceiling that all-local architectures hit on complex tasks.

### Alternative E: Same Model for Everything (Cherny's Approach)

Use the single best available model for all agents, relying on context isolation alone for multi-agent value.

- **Pros**: No model selection complexity, no routing errors, quality compounds across sessions
- **Cons**: Assumes access to a frontier model (Opus-class) at scale; infeasible for local SLM project where the best available model is 8B parameters; ignores the cost advantage of tiering
- **Rejected because**: Cherny's advice is context-dependent — it applies when API-priced frontier models are available and per-token cost is the main expense. Our project runs local SLMs where tiering by cognitive demand (MoE orchestrator + smaller workers) is both cheaper and potentially more effective, since context isolation compensates for SLM limitations.

---

## 4. Consequences

### Positive

- **Context isolation**: Workers operate with clean, focused context — SLMs perform better with less noise
- **Cost efficiency**: Bulk of compute is local SLM inference; remote model called only on escalation (<10% target)
- **Composability**: Complex tasks decomposed into worker-appropriate subtasks
- **Observability**: Per-tier traces, cost tracking, escalation rate metrics, quality evaluation scores
- **Model flexibility**: Worker-to-model mapping is config, not architecture — new SLMs swapped in via benchmarking
- **Escalation safety net**: Remote foundation model handles what local models cannot, with measurable frequency
- **Research value**: Three-tier local-local-remote architecture with MoE orchestrator is a novel, under-explored pattern

### Negative

- **Complexity**: Three tiers + evaluate-rework-escalate loop is more complex than a simple dispatch model
- **Latency**: Orchestrator evaluation adds overhead for moderate requests (mitigate with fast-path bypass for simple requests)
- **MoE dependency**: Architecture assumes a capable MoE model is available locally for orchestration — if evaluation quality is poor, the entire system degrades
- **Testing**: Need integration tests for the full evaluate-rework-escalate loop across tiers
- **Remote API dependency**: Tier 3 escalation requires network access and API key management

### Risks

- **Orchestrator evaluation too weak**: MoE model can't reliably judge worker output quality → over- or under-escalation (mitigate: rework budget with auto-escalate after N failures; benchmark orchestrator evaluation accuracy early)
- **Escalation rate too high (>30%)**: Workers are being given tasks beyond their capability → cost savings eroded (mitigate: tune task decomposition granularity; benchmark worker capability boundaries)
- **Router SLM miscalibrates**: Wrong complexity classification → simple tasks get expensive orchestration or complex tasks get fast-pathed (mitigate: conservative defaults — when uncertain, route to orchestrator)
- **Agent proliferation**: Maintenance burden from too many worker types (mitigate: start with 3-4 workers, add only with demonstrated need)

---

## 5. Acceptance Criteria

- [ ] Agent base class implemented with `AgentTier`, `AgentSpec`, and `AgentResult` (including `compressed_summary` and `escalated` fields)
- [ ] Router SLM integrated with two-axis classification (complexity + risk)
- [ ] MoE orchestrator implements the evaluate-rework-escalate loop
- [ ] At least 3 SLM workers operational with isolated context
- [ ] Remote foundation model escalation path functional (API integration)
- [ ] Context isolation enforced: workers receive task-scoped context only, return compressed summaries
- [ ] Per-tier observability (traces, costs, quality metrics) in Elasticsearch
- [ ] **Escalation rate tracked** as a primary system metric (target: <10% of non-trivial requests)
- [ ] **Orchestrator evaluation accuracy** benchmarked (can the MoE model reliably judge worker output?)
- [ ] Simple requests bypass orchestrator (fast path) with <50ms overhead
- [ ] Integration tests cover dispatch, rework, escalation, and fast-path flows
- [ ] Governance enforced per-tier (tool permissions scoped by agent tier and risk level)

---

## 6. Implementation Notes

### Phase Placement

This is **Phase 2.4**, following Phase 2.3 (Homeostasis & Feedback). Phase 2.3 provides the observability infrastructure that multi-agent tracing depends on.

### Implementation Sequence

1. **Validate orchestrator evaluation** (1-2 days): Before building the full system, benchmark whether the MoE model can reliably evaluate SLM worker output. This is the critical assumption — if it fails, the architecture needs adjustment.
2. Agent base class with tier support + orchestrator framework: 3-4 days
3. Router SLM two-axis classification: 1-2 days
4. Worker pool with context isolation: 3-4 days
5. Evaluate-rework-escalate loop: 2-3 days
6. Remote escalation integration: 1-2 days
7. Testing + observability + escalation rate tracking: 2-3 days
8. **Total**: ~13-20 days (3-4 weeks)

### Key Metrics to Track

| Metric | Description | Target |
|--------|-------------|--------|
| **Escalation rate** | % of non-trivial requests requiring Tier 3 | <10% |
| **Rework rate** | % of worker results requiring rework before acceptance | <30% |
| **Orchestrator evaluation accuracy** | Does the orchestrator correctly accept good results and reject bad ones? | >85% |
| **Fast-path hit rate** | % of requests handled without orchestrator (simple/read-only) | >40% |
| **Per-tier cost breakdown** | Token/compute cost by tier | Tier 2 dominates |
| **End-to-end latency by complexity** | Time from request to response, bucketed by router classification | Simple <1s, moderate <5s, complex <30s |

### Dependencies

- Phase 2.3 observability (for per-tier traces)
- SLM Server operational (ports 8500-8503)
- MoE model available locally (requires evaluation of candidate models)
- Remote API access configured (for Tier 3 escalation)
- MCP Gateway tool access (already operational)

### Revision History

- **2026-02-22**: Original proposal — role-based specialization with fixed agent roster (Coder, Analyst, Retrieval, Seshat)
- **2026-03-09**: Revised after reviewing Anthropic's sub-agent architecture (Boris Cherny, Jan 2026). Reframed from domain-role specialization to cognitive-demand tiering. Added MoE orchestrator with evaluate-rework-escalate loop, context isolation as first-class principle, remote escalation tier, and empirical worker selection. Added Alternatives D and E to document the reasoning for this shift.
