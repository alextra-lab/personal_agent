# Cognitive Architecture Redesign v2

**Date**: 2026-03-16
**Status**: Draft
**Phase**: 2.4 — Cognitive Architecture Redesign
**Supersedes**: ADR-0017 (Three-Tier Multi-Agent Orchestration)
**Evolves**: ADR-0018 (Seshat Memory Librarian Agent)
**Related**: ADR-0024 (Session Graph), ADR-0025 (Memory Recall), ADR-0029 (Concurrency Control)

---

## 1. Vision and Principles

### 1.1 What This System Is

Personal Agent is an **always-running cognitive companion** -- an intraday life
collaborator that maintains coherent memory across conversations, understands
context deeply, and knows when and how to expand its capabilities by engaging
other agents and systems.

It is simultaneously:

- **A conversational partner** -- you ask questions, think out loud, plan work,
  reflect on decisions.
- **A research platform** -- the system itself is the subject of study; its
  architecture embodies hypotheses about memory, orchestration, and
  self-improvement that are tested by using it.
- **A self-improving system** -- it captures its own performance, proposes
  improvements, and helps you implement them.
- **A delegation hub** -- when a task requires capabilities beyond its own
  (coding, deep research, specialized tools), it composes instructions and
  delegates to external agents, tracking the results.

### 1.2 What Changed From ADR-0017

ADR-0017 proposed a three-tier hierarchy: small router, MoE orchestrator, and
specialist SLM workers. This was designed for a world where the agent does
everything locally, including coding and deep analysis through dedicated 4-9B
specialist models.

Three insights invalidate that design:

**Small models don't orchestrate well.** Research and direct experience show
that 4-9B models handle single-stream, well-scoped tasks but lack the reasoning
depth for complex orchestration, task decomposition, and quality evaluation. The
30B+ range is where usable orchestration begins.

**Specialist agents fragment capability without improving it.** A dedicated
"Coder Agent" running a 9B model will always underperform Claude Code or Codex.
The coordination overhead of managing specialist workers exceeds the benefit.
The ecosystem produces better specialists every week -- the agent should
delegate to them rather than compete with them.

**One solid agent with dynamic knowledge beats a swarm of small specialists.**
This is the Anthropic Skills model: one capable agent, skills loaded on demand,
sub-agents spawned for parallelism. Claude Code itself validates this -- one
model with skills that activate based on the task, not a team of specialist
models.

### 1.3 Architectural Principles

**1. Deterministic Before Probabilistic**

Security, governance, rate limiting, and intent classification happen in
deterministic code *before* the LLM sees the request. The LLM is powerful but
should never decide what it's allowed to do. This is both a security principle
and an efficiency principle -- don't spend inference tokens on decisions that
can be made in microseconds.

**2. One Brain, Many Hands**

A single capable model (Qwen3.5-35-A3B) is the reasoning center. It doesn't
share this role. When it needs to expand, it spawns ephemeral sub-agents for
parallel work or delegates to external agents. Sub-agents are task-scoped
processes, not persistent specialist identities. They expand and contract with
demand.

**3. Memory Is Infrastructure, Not Feature**

Memory isn't something bolted on after the agent works. It's the foundation
that makes the agent a *companion* rather than a chatbot. Seshat (named after
the Egyptian goddess of writing and record-keeping -- the memory steward) is a
core subsystem, not a deferred afterthought. The agent's value
compounds over time because it remembers.

**4. Observability Is The Research Instrument**

Every interaction, delegation, memory access, and self-improvement proposal is
traced and visible in Elasticsearch/Kibana. This isn't just debugging -- it's
how you study the system. The dashboards are your microscope.

**5. Expand and Contract**

The system breathes. In a calm state, it's a small footprint -- primary agent,
memory, basic tools. When a complex task arrives, it expands: spawning
sub-agents, loading skills, delegating externally, assembling rich context.
When the task completes, it contracts: consolidates what it learned, proposes
improvements, returns to calm. The brainstem homeostasis model provides the
biological foundation for this. (See Section 2.3 Flow 4 and Section 4.3 for
the concrete mechanism.)

**6. Infrastructure Can Evolve**

Postgres, Elasticsearch, Neo4j are the current stack. They serve the current
needs well. But as the system grows, any of these can be evaluated, replaced,
or augmented if the research demands it. The architecture couples to
*interfaces* (protocols, abstract services), not to specific databases.

**7. The Project Teaches The Builder**

This is a learning system in both directions. The agent learns from
interactions (memory, self-improvement). The builder learns from the agent
(architecture, cognitive patterns, delegation strategies). Every design
decision should be observable, explainable, and experimentable.

---

## 2. System Architecture

### 2.1 The Layers

The system has seven layers. Three exist today, two are partially built, two
are new.

```
+---------------------------------------------------------------------+
|                        INTERFACE LAYER                               |
|  CLI (exists) . API /chat (exists) . Future: mobile, voice, etc.    |
+--------------------------------+------------------------------------+
                                 |
+--------------------------------v------------------------------------+
|                      PRE-LLM GATEWAY                       [NEW]    |
|                                                                     |
|  Security -> Session -> Governance -> Intent -> Decomposition       |
|                                       Classification  Assessment    |
|                                            |         |              |
|                                            v         v              |
|                                       Context    Delegation         |
|                                       Assembly   Plan               |
|                                            |         |              |
|                                            v         |              |
|                                       Context        |              |
|                                       Budget         |              |
+--------------------------------+--------------+------+--------------+
                                 |              |
              +------------------v--------+     |
              |      PRIMARY AGENT        |     |
              |   Qwen3.5-35-A3B         |     |
              |                           |     |
              |   . Conversational core   |     |
              |   . Tool calling (MCP +   |     |
              |     native)               |     |
              |   . Dynamic skill loading |     |
              |   . Delegation composer   |     |
              +--+----------+--------+----+     |
                 |          |        |          |
    +------------v--+  +---v-----+  +v---------v------------------+
    |   TOOLS       |  |  SESHAT |  |   EXPANSION LAYER    [NEW]  |
    |               |  | MEMORY  |  |                             |
    |  MCP Gateway  |  |         |  |  Internal sub-agents        |
    |  Native tools |  | Protocol|  |  (ephemeral, task-scoped)   |
    |  search_memory|  |  |      |  |                             |
    |  system_health|  | Neo4j   |  |  External agents            |
    |  (extensible) |  | (+ future| |  (Claude Code, Codex, etc.) |
    |               |  |  stores)|  |                             |
    +---------------+  +----+----+  +-------------+---------------+
                            |                     |
              +-------------v---------------------v---------------+
              |              BRAINSTEM                    EXISTS   |
              |                                                   |
              |  Homeostasis loop . Sensors . Mode manager         |
              |  Consolidation scheduler . Quality monitor         |
              |  Lifecycle manager . Expand/contract signals       |
              +------------------------+--------------------------+
                                       |
              +------------------------v--------------------------+
              |           SELF-IMPROVEMENT LOOP          EXISTS   |
              |                                                   |
              |  Captain's Log captures . LLM reflection          |
              |  Insights engine . Promotion pipeline -> Linear   |
              +------------------------+--------------------------+
                                       |
              +------------------------v--------------------------+
              |              INFRASTRUCTURE              EXISTS   |
              |                                                   |
              |  PostgreSQL (sessions, metrics, cost)              |
              |  Elasticsearch (traces, logs, captures, insights) |
              |  Neo4j (knowledge graph, memory)                  |
              |  (All replaceable behind service interfaces)       |
              +---------------------------------------------------+
```

### 2.2 What Exists Today vs. What Changes

| Layer | Today | After Redesign |
|---|---|---|
| **Interface** | CLI + `/chat` API | No change needed yet |
| **Pre-LLM Gateway** | Heuristic routing (regex) + governance modes + memory recall detection | Full pipeline: security, session, governance, intent, decomposition assessment, context assembly, budget management |
| **Primary Agent** | Orchestrator state machine routes to model roles (standard/reasoning/coding) via heuristic | Single 35B model as primary reasoning engine. State machine simplified -- no more role-switching mid-request. Model selection happens in gateway, not executor |
| **Tools** | MCP Gateway + native tools (search_memory, system_health) | No structural change. Tool set grows organically |
| **Seshat / Memory** | Neo4j memory service with entity extraction, broad recall, session graph | Seshat protocol (abstract interface). Current Neo4j service becomes first implementation. Research agenda for memory types (episodic/semantic/procedural/working/derived) |
| **Expansion** | Does not exist | Sub-agent spawning + external agent delegation + telemetry for both |
| **Brainstem** | Homeostasis loop, sensors, scheduler, quality monitor | Adds expansion/contraction signals. Brainstem already monitors resource pressure -- now it also signals when expansion is safe or when to contract |
| **Self-Improvement** | Captain's Log, reflection, insights, Linear promotion | No structural change. Gains new data sources: delegation outcomes, memory quality, expansion patterns |
| **Infrastructure** | Postgres + ES + Neo4j | Same, but all accessed through service interfaces. Any can evolve |

### 2.3 Key Data Flows

**Flow 1: Conversational Request (Calm State)**

```
User message
  -> Pre-LLM Gateway: security, governance, intent: CONVERSATIONAL
  -> Decomposition: SINGLE (fits in one context window)
  -> Context Assembly: load session history + relevant memory from Seshat
  -> Budget: trim to practical context limit
  -> Primary Agent: respond using assembled context
  -> Response to user
  -> Background: TaskCapture -> ES, brainstem notified
```

**Flow 2: Complex Task Requiring Expansion**

```
User message: "Research how Graphiti handles temporal memory and
              draft a comparison with our current Neo4j approach"
  -> Pre-LLM Gateway: intent: ANALYSIS, complexity: COMPLEX
  -> Decomposition: DECOMPOSE
      - Sub-task A: Research Graphiti (web search, documentation)
      - Sub-task B: Summarize current Neo4j approach (memory + code access)
      - Sub-task C: Synthesize comparison (needs results from A + B)
  -> Primary Agent receives decomposition plan
  -> Spawns sub-agent A (background) and sub-agent B (background)
  -> Waits for results
  -> Synthesizes comparison (sub-task C, in primary context)
  -> Response to user
  -> Background: all sub-agent traces -> ES, delegation outcomes captured
```

**Flow 3: External Agent Delegation**

```
User message: "I need a new FastAPI endpoint for session export.
              Can you write the spec and have Claude Code implement it?"
  -> Pre-LLM Gateway: intent: DELEGATION
  -> Decomposition: DELEGATE (coding -> external agent)
  -> Primary Agent: composes instruction package
      - Spec written by primary agent (it understands the architecture)
      - Context package: relevant code paths, conventions, test patterns
      - Acceptance criteria
  -> Delegation to Claude Code (via MCP or API)
  -> Track delegation status
  -> Capture outcome + learnings when complete
  -> Background: delegation telemetry -> ES
```

**Flow 4: Expand -> Contract Cycle**

```
EXPAND (triggered by complex task):
  Brainstem: resource check -> expansion safe
  Gateway: decomposition -> spawn sub-agents
  Primary agent: orchestrating, holding delegation context
  Sub-agents: executing focused sub-tasks
  ES: capturing all traces in real-time

CONTRACT (triggered by task completion + idle):
  Sub-agents: return results, terminate
  Primary agent: synthesize, respond
  Brainstem: detect idle -> trigger consolidation
  Seshat: extract entities, update memory
  Captain's Log: reflect on task, propose improvements
  Insights: detect patterns across delegations
  System returns to calm state
```

**Flow 5: Self-Improvement**

```
Captain's Log captures (continuous)
  -> LLM reflection (brainstem-scheduled, idle time)
  -> Insights engine (daily pattern detection)
  -> Proposals with fingerprint dedup (weekly)
  -> Promotion to Linear (if seen_count >= 3, age >= 7d)
  -> You review and approve
  -> Agent helps implement the improvement
  -> Cycle continues
```

### 2.4 Orchestrator State Machine -- Simplified

The current executor has: `INIT -> PLANNING -> LLM_CALL -> TOOL_EXECUTION ->
SYNTHESIS -> COMPLETED | FAILED`.

This mostly survives, but the semantics shift:

| State | Today | After Redesign |
|---|---|---|
| **INIT** | Heuristic routing, memory recall detection | Pre-LLM Gateway handles all of this *before* the state machine starts. INIT just receives the assembled context |
| **PLANNING** | Minimal (mostly unused) | Primary agent plans decomposition if gateway flagged DECOMPOSE. Otherwise skipped |
| **LLM_CALL** | Selects model role, calls LLM | Always calls primary 35B model. No role selection -- model is predetermined |
| **TOOL_EXECUTION** | Execute tool calls from LLM | Same, plus: sub-agent spawning and external delegation are "tools" from the executor's perspective |
| **SYNTHESIS** | Finalize reply, update session | Same, plus: capture delegation outcomes |
| **COMPLETED** | Done | Same. Brainstem takes over for contraction phase |
| **FAILED** | Error during execution | Same. Additionally: if failure occurred during expansion, contraction is triggered to clean up sub-agents and release resources |

The state machine gets *simpler* because the gateway absorbs the routing
complexity that was previously tangled into the executor.

---

## 3. The Pre-LLM Gateway

The deterministic intelligence layer -- everything that happens before the 35B
model sees a single token. Today, pieces of this are scattered across
`routing.py`, `executor.py`'s `step_init()`, and `mode_manager.py`. The
redesign formalizes them into an explicit pipeline.

### 3.1 Stage 1: Security [NEW]

- Rate limiting (per-session, per-source)
- Input sanitization (prompt injection patterns)
- PII detection + redaction before LLM sees content
- Request size limits

Decision: ALLOW / REJECT (with reason).
Telemetry: `security_check` event -> ES.

### 3.2 Stage 2: Session [EXISTS]

- Load or create session (SessionManager -- exists)
- Hydrate conversation history from DB (exists)
- Track session metadata: turn count, duration, dominant topics, last activity

Output: `SessionContext` with history + metadata.
Telemetry: `session_hydration` span -> ES.

### 3.3 Stage 3: Governance [PARTIAL]

- Check brainstem operational mode (exists: NORMAL, ALERT, DEGRADED, LOCKDOWN,
  RECOVERY)
- Apply mode-specific restrictions (exists: tool filtering)
- NEW: Resource-aware gating -- if brainstem signals resource pressure,
  constrain expansion options
- NEW: Cost awareness -- track API spend, apply budget limits for external
  delegation

Output: `GovernanceContext {mode, allowed_tools, expansion_permitted,
cost_budget_remaining}`.
Telemetry: `governance_decision` event -> ES.

### 3.4 Stage 4: Intent Classification [PARTIAL -- evolves]

Today: `heuristic_routing()` classifies to model roles (STANDARD, REASONING,
CODING) and a separate `is_memory_recall_query()` detects memory intent as a
boolean flag. These are two disconnected mechanisms.

Redesign: unify into **task types**, not model roles. The model is always
the 35B -- what changes is how we prepare context and whether we expand.

Task types:

| Type | Description |
|---|---|
| `CONVERSATIONAL` | Chat, quick questions, reflection |
| `MEMORY_RECALL` | "What did I...", "do you remember..." |
| `ANALYSIS` | Research, comparison, deep thinking |
| `PLANNING` | Project planning, task breakdown |
| `DELEGATION` | Task suited for external agent |
| `SELF_IMPROVE` | Agent proposing/discussing changes to itself |
| `TOOL_USE` | Explicit tool request (search, read, list) |

Complexity estimate: `SIMPLE / MODERATE / COMPLEX` (based on message length,
detected sub-tasks, required context depth, keyword signals).

Still deterministic (regex + heuristics). The LLM is NOT used for
classification -- that is the entire point. But the heuristics get richer than
today's patterns.

Output: `IntentResult {task_type, complexity, confidence, signals: list[str]}`.
Telemetry: `intent_classification` event -> ES.

### 3.5 Stage 5: Decomposition Assessment [NEW]

Should this be handled by the primary agent alone, or decomposed/delegated?

Decision matrix:

| Task Type | Complexity | Decision |
|---|---|---|
| CONVERSATIONAL | any | SINGLE |
| MEMORY_RECALL | any | SINGLE (memory-enriched) |
| TOOL_USE | SIMPLE | SINGLE |
| ANALYSIS | SIMPLE | SINGLE |
| ANALYSIS | MODERATE | SINGLE or HYBRID |
| ANALYSIS | COMPLEX | DECOMPOSE |
| PLANNING | MODERATE+ | HYBRID (agent plans, sub-agents research) |
| DELEGATION | any | DELEGATE |
| SELF_IMPROVE | any | SINGLE (self-referential) |
| any | any (resource pressure) | Force SINGLE + compress |

For DECOMPOSE/HYBRID: the gateway does NOT plan the sub-tasks -- it just flags
"this needs decomposition." The primary agent creates the decomposition plan.
**Gateway decides IF to expand. Agent decides HOW.**

Output: `DecompositionResult {strategy: SINGLE|HYBRID|DECOMPOSE|DELEGATE,
reason, constraints}`.
Telemetry: `decomposition_decision` event -> ES.

### 3.6 Stage 6: Context Assembly [PARTIAL]

Today: session messages loaded (max 50) + optional `memory_context` from broad
recall query.

Redesign: Assemble context from multiple sources based on task type and
decomposition strategy.

Sources:

| Source | When Included |
|---|---|
| Session history | Always (recent turns) |
| Seshat memory | Relevant episodic/semantic recall (query shaped by intent) |
| Skills/knowledge | Loaded on demand by task type (e.g., architecture context for SELF_IMPROVE tasks) |
| Tool definitions | Filtered by governance mode + task relevance |
| Delegation context | For DELEGATE: relevant code, conventions, acceptance criteria |

Key principle: **task type shapes what memory to retrieve.** A MEMORY_RECALL
query gets broad entity recall. An ANALYSIS query gets relevant past analyses.
A SELF_IMPROVE query gets architecture docs + recent Captain's Log entries.

Output: `AssembledContext {messages, memory_context, skills, tool_definitions,
delegation_context}`.
Telemetry: `context_assembly` span -> ES (with source sizes).

### 3.7 Stage 7: Context Budget [NEW]

Ensure assembled context fits the practical window.

Practical budget (reference hardware: M4 Max 128GB; adjust for different
configurations) with 35B @ 8-bit:

- Model weights: ~35 GB
- KV cache headroom: ~60-80 GB available
- Comfortable context: 32K-64K tokens
- Maximum context: 128K tokens (degraded performance)
- Reserve for generation: ~4K-8K tokens

Budget strategy:

1. Measure total assembled tokens.
2. If under budget: pass through.
3. If over budget, trim in priority order:
   a. Compress older session history (summarize).
   b. Reduce memory context (raise relevance threshold).
   c. Trim tool definitions (keep most relevant).
   d. If still over: flag for DECOMPOSE override (the task is too big for one
      pass -- re-enter decomposition with DECOMPOSE forced).

Context budget IS a decomposition trigger. This is where the practical hardware
constraint meets the delegation architecture.

Output: `BudgetedContext {final_messages, token_count, trimmed: bool,
overflow_action: str | None}`.
Telemetry: `context_budget` event -> ES (budget vs actual, what was trimmed).

### 3.8 Gateway Failure Modes and Graceful Degradation

Each gateway stage can fail. The pipeline must degrade gracefully rather than
halt the request.

| Stage | Failure | Degradation |
|---|---|---|
| **Security** | Sanitization service down | Log warning, pass through with flag `security_degraded=true`. Never block on sanitization failure -- availability over perfection |
| **Session** | DB unreachable | Create ephemeral in-memory session. Mark `session_ephemeral=true`. Conversation history unavailable but request proceeds |
| **Governance** | Brainstem unresponsive | Default to NORMAL mode with expansion disabled. Conservative: treat resource state as unknown |
| **Intent** | Low confidence classification (below threshold) | Default to CONVERSATIONAL with complexity SIMPLE. Log the ambiguous signals for later analysis. Never block on classification uncertainty |
| **Decomposition** | Cannot assess (e.g., governance unavailable) | Default to SINGLE. The safest option -- no expansion, no resource risk |
| **Context Assembly** | Neo4j unreachable (memory unavailable) | Proceed without memory context. Mark `memory_degraded=true` in telemetry. The agent can still converse, just without memory enrichment |
| **Context Assembly** | Skill files missing or unreadable | Proceed without skills. Log warning. Agent operates with base knowledge only |
| **Budget** | Token count exceeds budget even after maximum trimming | Two paths: (1) If decomposition was SINGLE and intent is COMPLEX, re-enter Stage 5 with DECOMPOSE forced. (2) If already DECOMPOSE or intent is SIMPLE, proceed with truncated context and warn the agent via system message that context was trimmed |

**Re-entry behavior:** When Stage 7 (Budget) forces a DECOMPOSE override, the
pipeline re-enters at Stage 5 with a `budget_overflow=true` flag. Stage 5
sets strategy to DECOMPOSE unconditionally. Stage 6 then assembles a minimal
context for decomposition planning (just the user message + essential system
prompt), not the full enriched context. This avoids infinite loops -- the
re-entry path always produces a context that fits.

**Telemetry principle:** Every degradation emits a structured event to ES with
the stage name, failure type, and degradation path chosen. This makes failure
patterns visible in Kibana and feeds the insights engine.

### 3.9 What This Replaces in Current Code

| Current Code | What Happens |
|---|---|
| `routing.py: heuristic_routing()` | Evolves into Stage 4 (intent classification). Same regex approach, richer task types instead of model roles |
| `routing.py: resolve_role()` | Removed. No more role resolution -- the model is always the 35B primary |
| `routing.py: is_memory_recall_query()` | Moves into Stage 4 as one of the intent classifiers (memory recall is a task type, not a separate boolean check) |
| `routing.py: _MEMORY_RECALL_PATTERNS` | Absorbed into Stage 4 intent patterns |
| `executor.py: step_init()` | Split: memory/routing logic moves to Stages 4-6. `step_init()` becomes thin -- just receives the gateway output |
| `mode_manager.py` | Stays, feeds into Stage 3 (governance). Gains resource-pressure signaling |
| `config/models.yaml: router role` | Removed or repurposed. The `liquid/lfm2.5-1.2b` router entry is no longer used for request classification. See Section 3.10 for potential future roles |
| `orchestrator/types.py: ModelRole.ROUTER` | Removed. Intent classification returns task types, not model roles |

### 3.10 The Router SLM Question

The current `liquid/lfm2.5-1.2b` router model is unnecessary for intent
classification -- that's deterministic. However, there are possible future
roles:

- **Context summarization**: A small fast model could summarize old
  conversation history to compress context in Stage 7.
- **Decomposition hints**: For borderline cases, a small model could suggest
  whether a task needs decomposition.

These are optimizations, not foundations. For Slice 1, the gateway is purely
deterministic. The router SLM question becomes a research experiment for a
later slice.

---

## 4. Primary Agent and Expansion Model

### 4.1 The Primary Agent

The 35B model is the single reasoning center. Everything flows through it --
conversation, analysis, planning, delegation instructions, self-improvement
proposals.

What it does:

- Direct conversation (most requests)
- Tool calling via MCP + native tools
- Decomposition planning (when gateway flags DECOMPOSE)
- Instruction composition for external agents
- Self-reflection and improvement proposals
- Sub-agent task specification

What it does NOT do:

- Route to different models (gateway decides)
- Execute long-running background work itself (sub-agents handle that)
- Code like Claude Code (delegates instead)
- Make security/governance decisions (gateway decides)

### 4.2 Dynamic Skill Loading

Today, the system prompt is static -- the same instructions regardless of task
type. In the redesign, the gateway's Context Assembly stage loads task-relevant
knowledge into the context.

"Skills" means: **structured knowledge documents that the agent loads on
demand**, not permanent system prompt instructions. This is the same pattern
Claude Code uses.

```
Skills Library (on disk, versioned)
  architecture/          <- loaded for SELF_IMPROVE tasks
    system-overview.md
    coding-conventions.md
    memory-architecture.md
  delegation/            <- loaded for DELEGATION tasks
    claude-code-patterns.md
    instruction-composition.md
    acceptance-criteria-template.md
  memory/                <- loaded for MEMORY_RECALL tasks
    entity-types.md
    query-strategies.md
    seshat-capabilities.md
  analysis/              <- loaded for ANALYSIS tasks
    comparison-framework.md
    research-methodology.md
  tools/                 <- loaded when specific tools relevant
    mcp-gateway-usage.md
    search-patterns.md
```

How loading works:

1. Gateway classifies intent (Stage 4).
2. Context Assembly (Stage 6) maps task type to relevant skills.
3. Skill content loaded into context as part of system/user messages.
4. Budget stage (Stage 7) ensures total fits the window.
5. Skills that don't fit get summarized or dropped by priority.

This is a lookup table, not clever. Task type maps to skill set. But it means
a MEMORY_RECALL conversation gets memory-specific knowledge injected, while a
DELEGATION task gets instruction-composition patterns.

### 4.3 System Prompt Strategy

The primary agent's system prompt has two layers:

**Base prompt (always present):** Agent identity, behavioral constraints, tool
usage instructions, response formatting guidelines. This is the permanent
personality and operational contract. Compact -- target under 1K tokens.

**Task-type injection (variable):** Skills, memory context, delegation
templates, and tool definitions loaded by the gateway's Context Assembly
stage. This is what changes per request. Injected as additional system or
user messages after the base prompt.

The base prompt should be explicit about the agent's role as a life
collaborator, its memory capabilities, and its delegation model. It should
NOT contain task-specific instructions -- those come from skills. This
separation keeps the base prompt stable across all task types while allowing
rich contextual augmentation.

### 4.4 The Expansion Model

Three expansion modes:

**Mode 1: SINGLE (Calm State)**

```
User -> Gateway -> Primary Agent -> Response
                       |
                  (tools if needed)
```

Most requests. Quick answers, conversation, simple tool use. The agent handles
everything in a single inference pass (possibly with tool-call loops). Low
resource usage. This is the contracted state.

**Mode 2: HYBRID (Moderate Expansion)**

```
User -> Gateway -> Primary Agent -> plans decomposition
                       |                    |
                       |         +----------+----------+
                       |         v                     v
                       |    Sub-agent A            Sub-agent B
                       |    (focused task)         (focused task)
                       |         |                     |
                       |         v                     v
                       |<-- results ---------- results -+
                       |
                       v
                  Synthesizes -> Response
```

The primary agent stays in the loop. It plans the decomposition, spawns
sub-agents for parallel sub-tasks, receives their results, and synthesizes the
final answer.

**Mode 3: DELEGATE (External Expansion)**

```
User -> Gateway -> Primary Agent -> composes instructions
                       |                    |
                       |                    v
                       |         +---------------------+
                       |         |  Instruction Package |
                       |         |  . Task description  |
                       |         |  . Relevant context  |
                       |         |  . Constraints       |
                       |         |  . Acceptance criteria|
                       |         |  . Feedback channel  |
                       |         +----------+----------+
                       |                    |
                       |                    v
                       |            External Agent
                       |         (Claude Code, Codex,
                       |          Coding Hive, etc.)
                       |                    |
                       |                    v
                       |<--- outcome (async or sync)
                       |
                       v
                  Reports result -> Response
```

### 4.5 Delegation Evolution: A -> B -> C

| Stage | How Delegation Works | What You Learn |
|---|---|---|
| **A: Instruction Composition** (Slice 1) | Agent writes a well-structured task description. You copy it to Claude Code manually. Agent helps you evaluate the result. | What makes good instructions? What context does the external agent need? What acceptance criteria matter? |
| **B: Structured Handoff** (Slice 2) | Agent produces a formal instruction package (DelegationPackage). You paste it, but the format is consistent and machine-readable. | What's the minimal context for good delegation? How do different agents respond to different instruction styles? |
| **C: Programmatic Orchestration** (Slice 3+) | Agent calls external agents via API/MCP. Tracks status. Evaluates results against criteria. Can request revisions. | Full closed-loop delegation. The agent is the manager. |

Starting at A is deliberate -- you learn what good delegation looks like
before automating it.

### 4.6 Sub-Agent Architecture

Sub-agents are NOT separate services, NOT persistent processes, and NOT
specialist identities. They are **task-scoped inference calls** -- the primary
agent specifies a sub-task, the system runs a focused LLM call with a
constrained context, and returns the result.

```python
# Conceptual types -- defines the contract
@dataclass(frozen=True)
class SubAgentSpec:
    """What the primary agent provides to spawn a sub-agent."""
    task: str                          # What to do
    context: Sequence[Message]         # Focused context slice
    tools: Sequence[str] | None        # Which tools available
    output_format: str                 # What to return
    max_tokens: int                    # Token budget
    timeout_seconds: float             # Time limit
    background: bool                   # Can run async?

@dataclass(frozen=True)
class SubAgentResult:
    """What comes back from a sub-agent."""
    task_id: str
    summary: str                       # Compressed for primary agent
    full_output: str                   # Complete (goes to ES only)
    tools_used: Sequence[str]
    token_count: int
    duration_ms: int
    success: bool
    error: str | None
```

Sub-agent characteristics:

- Run as separate inference calls (not separate model instances).
- Each gets a focused context slice -- only what they need.
- Return a compressed result -- summary + key findings, not full trace.
- Full trace goes to ES for observability, but only the summary enters the
  primary agent's context.
- Brainstem monitors resource pressure during expansion -- if GPU/memory
  spikes, remaining sub-agents queue rather than running in parallel.

Which model runs sub-agents is a design variable:

- **Default**: Same 35B model.
- **Optimization**: For simple sub-tasks (summarization, data gathering), a
  smaller model (9B) could suffice with less resource pressure.
- **Research question**: Does sub-agent quality degrade meaningfully with a
  smaller model? Testable. Telemetry will answer this.

### 4.7 Sub-Agent Concurrency Model

Sub-agents execute as `asyncio.Task` instances within the existing service
process. They integrate with the ADR-0029 concurrency controller:

- Each sub-agent inference call acquires a concurrency slot before executing.
- The brainstem's `expansion_budget` determines maximum concurrent sub-agents,
  but the concurrency controller's `max_concurrency` per model is the hard
  limit. If the 35B model has `max_concurrency: 1`, sub-agents execute
  sequentially regardless of expansion_budget.
- Sub-agent tasks respect the same timeout and cancellation mechanisms as
  regular inference calls.
- If the user sends a new message while sub-agents are running, the new
  request is queued behind active inference. The user receives a response
  once their request reaches the front of the queue. Sub-agent results from
  the previous expansion are still collected and available for synthesis.
- Future optimization: route simple sub-agent tasks to the 9B model (which
  has its own concurrency slot), enabling true parallelism with the 35B
  primary. This is a Slice 2/3 experiment.

### 4.8 Brainstem Expansion Signals

The brainstem gains two new signals:

**Expansion Permission Signal:**

Brainstem evaluates current GPU utilization, available memory headroom, active
inference count (concurrency controller), and recent error rate. Produces
`expansion_budget: int` -- how many concurrent sub-agents are safe to run.

- 0: force SINGLE, system under pressure.
- 1: one sub-agent at a time (sequential expansion).
- 2-3: parallel sub-agents permitted.

**Contraction Trigger:**

After expansion completes, brainstem detects active sub-agents = 0, no pending
requests, idle timer starts. Triggers consolidation cycle: Seshat processes
new memories, Captain's Log captures expansion performance, system contracts
to calm state.

---

## 5. Seshat -- Memory Architecture and Research Agenda

### 5.1 What Seshat IS

Seshat is not a database. Seshat is the **memory stewardship intelligence** --
the layer that decides what to remember, how to organize it, when to forget,
and what to surface proactively.

The **protocol** defines what memory operations look like. The **backends**
implement storage and retrieval. The **intelligence** -- curation, lifecycle,
contradiction detection -- sits above both. This allows experimenting with
different storage technologies (Graphiti, AgentDB, custom Neo4j) without
rewriting the intelligence layer.

### 5.2 What Exists Today

| Capability | Status |
|---|---|
| Entity extraction from conversations | Works |
| Turn + Session + Entity nodes in Neo4j | Works |
| Session graph with NEXT chains (ADR-0024) | Works |
| Entity-name memory query | Works |
| Broad recall (ADR-0025) | Works |
| Multi-factor relevance scoring | Works |
| Implicit feedback detection (rephrase) | Works |
| `search_memory` native tool (ADR-0026) | Works |
| Brainstem-scheduled consolidation | Works |
| Quality monitoring | Works |
| **Curation intelligence** | Missing |
| **Contradiction detection** | Missing |
| **Lifecycle management (forgetting)** | Missing |
| **Confidence scoring on memories** | Missing |
| **Memory type distinction** | Missing |
| **Proactive surfacing** | Missing |
| **Abstract protocol** | Missing |

### 5.3 Memory Types

Six memory types, each with different lifecycles:

**Working (seconds-minutes):** Current session state, active conversation
context, in-flight sub-agent tasks. Storage: in-process (ExecutionContext).
Exists today.

**Episodic (interactions -- "what happened"):** Conversation turns with
timestamps, tool uses and outcomes, delegation events and results, decisions
and reasoning, lessons learned. Storage: Neo4j Turn/Session nodes. Partially
exists.

**Semantic (consolidated knowledge -- "what I know"):** Stable facts about user
and interests, project knowledge, entity relationships that have stabilized.
Curated from repeated episodic patterns. Storage: Neo4j Entity nodes.
Partially exists (but no episodic-to-semantic promotion pipeline yet).

**Procedural (how-to -- "what works"):** Effective tool-call patterns,
delegation templates that produce good results, query strategies that find
relevant memory. Storage: not yet implemented.

**Profile (user model -- "who you are"):** Preferences, interests and their
evolution, working patterns, feedback history. Storage: PostgreSQL
user_interests. Minimal today.

**Derived (insights -- "what I've figured out"):** Patterns detected by
insights engine, Captain's Log synthesis, cross-session correlations,
provenance chain back to source episodes. Storage: Captain's Log entries + ES.
Partially exists.

These types have different lifecycles. Episodic memories are fast to create,
slow to decay. Semantic memories are slow to create (consolidated from
episodes), but very stable. Procedural memories strengthen through repetition.
Working memory is ephemeral by design. **The lifecycle management IS Seshat's
core intelligence.**

### 5.4 The Seshat Protocol

The abstract interface that all memory operations go through:

```python
class MemoryProtocol(Protocol):
    """Abstract memory interface. All memory access goes through this."""

    # Store
    async def store_episode(
        self, episode: Episode, ctx: TraceContext
    ) -> str: ...
    async def store_fact(
        self, fact: Fact, confidence: float, ctx: TraceContext
    ) -> str: ...
    async def store_procedure(
        self, procedure: Procedure, ctx: TraceContext
    ) -> str: ...

    # Retrieve
    async def recall(
        self, query: MemoryQuery, ctx: TraceContext
    ) -> MemoryResult: ...
    async def recall_broad(
        self, scope: RecallScope, ctx: TraceContext
    ) -> BroadRecallResult: ...
    async def get_working_context(
        self, session_id: str, ctx: TraceContext
    ) -> WorkingContext: ...

    # Curate (Seshat intelligence)
    async def consolidate(
        self, window: TimeWindow, ctx: TraceContext
    ) -> ConsolidationResult: ...
    async def promote(
        self, episode_id: str, to_type: MemoryType, ctx: TraceContext
    ) -> str: ...
    async def demote(
        self, memory_id: str, reason: str, ctx: TraceContext
    ) -> None: ...
    async def forget(
        self, memory_id: str, reason: str, ctx: TraceContext
    ) -> None: ...

    # Quality
    async def check_contradiction(
        self, new_fact: Fact, ctx: TraceContext
    ) -> list[Contradiction]: ...
    async def assess_health(
        self, ctx: TraceContext
    ) -> MemoryHealthReport: ...
    async def get_provenance(
        self, memory_id: str, ctx: TraceContext
    ) -> ProvenanceChain: ...

    # Proactive
    async def suggest_relevant(
        self, current_context: str, ctx: TraceContext
    ) -> list[Memory]: ...
```

**Mapping to existing types:** The protocol types wrap existing models from
`memory/models.py`:

| Protocol Type | Maps To / Contains |
|---|---|
| `Episode` | Wraps `TurnNode` + user_message + assistant_response + tools_used |
| `Fact` | New: extracted stable assertion with confidence score |
| `Procedure` | New: reusable tool/delegation pattern |
| `MemoryQuery` | Evolves existing `MemoryQuery` (adds memory type filter) |
| `MemoryResult` | Evolves existing `MemoryQueryResult` |
| `BroadRecallResult` | Wraps current `query_memory_broad()` return shape |
| `WorkingContext` | New: current session messages + active sub-agent state |
| `RecallScope` | New: enum (ALL, EPISODIC, SEMANTIC, PROCEDURAL, DERIVED) |
| `TimeWindow` | New: start/end datetime for consolidation range |
| `ConsolidationResult` | Evolves current consolidation summary dict |
| `MemoryType` | New: enum matching the six memory types in Section 5.3 |
| `Contradiction` | New: conflicting fact pair with confidence comparison |
| `MemoryHealthReport` | Evolves current quality monitor report dicts |
| `ProvenanceChain` | New: list of source episode IDs leading to a derived fact |
| `Memory` | Union type: Episode or Fact or Procedure (for suggest_relevant) |

Detailed field definitions will be specified during Slice 1 implementation.
The table above establishes the relationship to existing code.

The first implementation wraps the existing `MemoryService` behind this
protocol. No new storage -- just a clean interface over what exists. Then, as
experiments with Graphiti or other backends proceed, the implementation can be
swapped or augmented without changing any consuming code.

### 5.5 Research Agenda: What To Study vs. What To Build

| Technology | What It Does | Relationship to Seshat | Action |
|---|---|---|---|
| **Graphiti** (by Zep) | Temporal knowledge graph on Neo4j. Episodic + entity memory with time-aware retrieval. Handles entity dedup, relationship extraction, temporal queries | Could be a storage backend beneath Seshat. Handles the graph layer currently built manually. Seshat's curation intelligence sits on top | Study + Experiment (Slice 2) |
| **AgentDB** | Agent-native database for persistent memory across sessions. Handles memory CRUD, semantic search, agent context management | Could replace or augment Neo4j + PostgreSQL for agent memory. More opinionated -- closer to what Seshat needs at the storage level | Study. Evaluate data model fit. If it handles episodic/semantic well, it simplifies implementation. If too opinionated or lacks lifecycle management, Seshat remains distinct |
| **mem0** | Memory layer for LLM apps. Vector-based with entity extraction and user profiles | Overlaps with entity extraction + user interest tracking. Less sophisticated graph model | Study briefly. Reference for profile memory patterns |
| **LangMem** | LangChain memory system with different memory types | Reference architecture for memory type taxonomy | Read documentation only |
| **neo4j-graphrag** | Neo4j's official graph RAG toolkit | Could improve retrieval quality within existing Neo4j | Experiment (Slice 2). Test against current relevance scoring |

The fundamental question for each: Does it provide the curation intelligence
(what Seshat IS), or does it provide better storage and retrieval (what Seshat
sits ON TOP OF)?

Most tools handle storage/retrieval. None do what Seshat envisions: active
curation, contradiction detection, lifecycle management, proactive surfacing.
That intelligence is what gets built. The storage layer underneath can be
whatever works best.

---

## 6. External Agent Integration

### 6.1 The Knowledge Broker

The personal agent remembers. Claude Code doesn't. Codex doesn't. Each external
agent is powerful but amnesiac -- it starts fresh every time.

The personal agent's unique value in a multi-agent world isn't that it can code
or analyze better. It's that it **holds the context** -- preferences, project
history, what worked, what failed, what matters. When it delegates to an
external agent, it's not just sending a task. It's sending a context package
distilled from memory.

This makes the personal agent a **knowledge broker** -- the persistent
intelligence that connects ephemeral specialists.

### 6.2 Stage A: Instruction Composition (Slice 1)

The agent helps write better instructions for external agents. The user still
copies and executes manually. But the agent uses its memory to enrich the
instructions.

Example output:

```
DELEGATION INSTRUCTION PACKAGE

Task: Add GET /sessions/{id}/export endpoint

Context:
  . Service: FastAPI app in src/personal_agent/service/
  . Pattern: follow existing /chat and /sessions endpoints
  . DB: PostgreSQL, session table schema: [included]
  . Tests: mirror in tests/personal_agent/service/

Conventions:
  . Google-style docstrings
  . Type hints on all public APIs
  . structlog logging with trace_id
  . Pydantic models for request/response

Acceptance Criteria:
  [ ] Endpoint returns session with all turns
  [ ] Includes entity summary from Neo4j
  [ ] Tests pass: uv run pytest tests/service/
  [ ] Type check: uv run mypy src/

Known pitfall (from memory):
  Last delegation needed DB schema that wasn't included.
  Schema attached above.
```

Telemetry captured: instruction package content, user edits before sending,
outcome (success/rounds), feedback on what was missing.

### 6.3 Stage B: Structured Handoff (Slice 2)

Same concept, but the instruction package becomes machine-readable and the
feedback loop tightens.

```python
@dataclass(frozen=True)
class DelegationPackage:
    """Structured instruction package for external agents."""
    task_id: str
    target_agent: str                    # "claude-code", "codex", etc.
    task_description: str
    context: DelegationContext
    memory_excerpt: Sequence[MemoryItem]
    acceptance_criteria: Sequence[str]
    known_pitfalls: Sequence[str]
    estimated_complexity: str
    created_at: datetime

@dataclass(frozen=True)
class DelegationOutcome:
    """What comes back after delegation completes."""
    task_id: str
    success: bool
    rounds_needed: int
    what_worked: str
    what_was_missing: str
    artifacts_produced: Sequence[str]
    duration_minutes: float
    user_satisfaction: int | None        # 1-5 rating
```

Changes from Stage A: packages have a consistent schema, outcomes are captured
structurally, the agent can analyze delegation patterns, and the agent starts
suggesting delegation proactively.

### 6.4 Stage C: Programmatic Orchestration (Slice 3+)

The agent calls external agents directly. The user is informed but doesn't
copy-paste.

| Agent | Interface | Feasibility |
|---|---|---|
| Claude Code | CLI invocation or Claude Agent SDK | Feasible now |
| Codex | OpenAI API | Feasible now |
| Coding hives | Varies -- typically API or MCP | As they emerge |
| Future agents | MCP is the likely universal protocol | MCP already in the stack |

### 6.5 Inter-Agent Knowledge Sharing

Beyond delegation, the agent can **brief** other agents:

The agent assembles from Seshat: active projects and status, recent decisions
and reasoning, coding conventions and preferences, common pitfalls, tools and
patterns preferred. It produces a context briefing -- a structured document
that Claude Code can consume as part of its CLAUDE.md or system context.

The agent could produce **CLAUDE.md files dynamically** -- generating project
instructions for Claude Code based on what it knows about the project,
preferences, and past delegation results.

### 6.6 Delegation Telemetry

Every delegation event produces rich telemetry in Elasticsearch:

```json
{
  "trace_id": "...",
  "delegation_id": "del-20260316-...",
  "target_agent": "claude-code",
  "task_type": "coding",
  "stage": "A|B|C",
  "package": {
    "context_items": 5,
    "memory_items": 3,
    "acceptance_criteria": 4,
    "known_pitfalls": 1,
    "estimated_complexity": "MODERATE",
    "total_tokens": 2400
  },
  "outcome": {
    "success": true,
    "rounds": 1,
    "duration_minutes": 12,
    "missing_context": null,
    "user_satisfaction": 4
  },
  "memory_impact": {
    "new_procedural_memory": "include DB schema for coding delegation",
    "delegation_pattern_updated": true
  }
}
```

Kibana dashboards show: delegation success rate by agent/type/complexity,
average rounds (trending down = learning), most common missing context, cost
per delegation, time-to-completion trends.

The agent analyzes its own delegation performance through the insights engine
and proposes improvements, which become procedural memory in Seshat.

---

## 7. Self-Improvement and Observability

### 7.1 The Current Loop

The existing self-improvement machinery (Captain's Log, insights engine,
promotion pipeline) works but has an open loop: the agent proposes improvements
and gets them into Linear, but implementation is manual.

### 7.2 The Enhanced Loop

The redesign closes the loop by adding:

**New data sources feeding the insights engine:**

- Delegation outcomes (success rates, missing context, round counts)
- Expansion/contraction metrics (when expansion helped vs. added overhead)
- Memory quality across experiments (backend comparisons)
- Sub-agent performance (quality at different model tiers)
- Context budget decisions (what gets trimmed, how often)
- External agent feedback (satisfaction ratings, revision requests)

**Closed-loop implementation:**

When the agent has an approved proposal in Linear, it composes a
DelegationPackage for the improvement itself -- linking to the Linear issue,
including relevant architecture context, the ProposedChange (what/why/how),
acceptance criteria, and memory of similar past changes. It delegates to Claude
Code, captures the outcome, and learns whether the original proposal was
accurate.

This feedback becomes a new TaskCapture, feeding back into the loop.

### 7.3 What The Agent Can Propose

With expanded data sources, the insights engine detects new pattern categories:

- **Delegation intelligence**: which context items correlate with delegation
  success; optimal task size for different agents.
- **Expansion patterns**: when sub-agents help vs. add overhead; optimal
  decomposition thresholds.
- **Memory quality**: how backend changes affect recall precision; entity
  extraction trends.
- **Architecture proposals**: patterns across Captain's Log entries suggesting
  structural changes.

### 7.4 The Agent As Its Own Architect

When the agent has enough self-improvement data, it can participate in
architecture discussions as a data-informed collaborator. It provides evidence
from its own experience -- performance data, quality metrics, experiment
results -- to inform design decisions. It does not decide. You decide. But it
grounds the discussion in evidence.

### 7.5 Observability: Dashboard Families

**Family 1: Conversation Intelligence**

- Request volume, latency, error rates (exists)
- Intent classification distribution (new)
- Complexity distribution over time (new)
- Context budget utilization (new)

**Family 2: Expansion and Delegation**

- Expansion frequency (new)
- Sub-agent spawn rate, success rate, duration (new)
- Delegation volume by external agent (new)
- Delegation success rate trending (new)
- Rounds-to-completion by agent and task type (new)
- Cost per delegation (new)

**Family 3: Memory and Seshat**

- Entity extraction quality, graph health (exists)
- Memory type distribution (new)
- Promotion rate -- episodes to stable knowledge (new)
- Recall precision (new)
- Contradiction detection frequency (new)
- Backend comparison metrics (new)

**Family 4: Self-Improvement**

- Captain's Log entry volume (exists)
- Proposal categories and scopes (exists)
- Closed-loop success rate (new)
- Proposal accuracy (new)
- Time from observation to implementation (new)
- Self-improvement ROI (new)

---

## 8. Vertical Slices -- Implementation Path

Each slice cuts through the full stack, produces a working system, and teaches
something specific. After each slice, the vision updates based on what was
learned.

### 8.1 Slice 1: Foundation

**Theme: "One brain, clean interface, start observing"**

**Duration estimate:** 2-3 weeks

| Layer | Deliverable |
|---|---|
| **Gateway** | Formalize intent classification (evolve routing.py). Task types replace model roles. Governance stage wraps existing mode_manager. Emit intent_classification events to ES |
| **Primary Agent** | 35B as sole entry point. Remove resolve_role() and role-switching. Remove router SLM call in executor. Simplify step_init(). System prompt varies by task type |
| **Seshat** | Define MemoryProtocol. Wrap existing MemoryService as first implementation. No new capabilities -- just the clean contract |
| **Delegation** | Stage A: instruction composition. Agent produces markdown delegation packages. Manual outcome capture |
| **Telemetry** | Intent classification dashboard. Task type distribution over time. Delegation events tracked |

Acceptance criteria:

- [ ] All requests route through the gateway pipeline (no direct executor entry)
- [ ] Intent classification events appear in ES with task type and confidence
- [ ] Role-switching removed: `resolve_role()` gone, all requests use 35B
- [ ] MemoryProtocol defined with at least `recall()` and `store_episode()`
- [ ] Existing MemoryService passes as MemoryProtocol implementation (tests)
- [ ] Agent can produce a markdown delegation instruction package for a sample task
- [ ] Gateway degradation: agent responds when Neo4j is down (without memory)
- [ ] Kibana dashboard shows intent classification distribution

What you learn:

- Does single-agent feel better or worse than role-switching?
- Are the task type classifications accurate? Which are ambiguous?
- Is the MemoryProtocol the right abstraction?
- What do delegation instructions need that the agent doesn't provide?
- What does the intent distribution look like in real usage?

What this unblocks: decomposition assessment, memory experiments, structured
delegation.

Key code changes:

| File | Change |
|---|---|
| `orchestrator/routing.py` | `heuristic_routing()` returns `IntentResult` (task types) instead of `HeuristicRoutingPlan` (model roles). `resolve_role()` removed. `is_memory_recall_query()` absorbed into intent classification |
| `orchestrator/executor.py` | `step_init()` simplified -- receives `GatewayOutput` instead of performing routing/memory detection inline. Router model dispatch path removed. LLM_CALL always uses primary 35B |
| `orchestrator/types.py` | New: `TaskType` enum, `IntentResult`, `GatewayOutput`, `DecompositionResult`. `ModelRole.ROUTER` removed |
| `memory/protocol.py` | New file: `MemoryProtocol` abstract interface |
| `memory/service.py` | Implements `MemoryProtocol` (wrapper over existing logic, no new capabilities) |
| `config/models.yaml` | Router role entry removed or commented out |
| New: `request_gateway/` | New module: gateway pipeline stages (named to avoid collision with existing `mcp/gateway.py`). Contains: `pipeline.py`, `security.py`, `intent.py`, `decomposition.py`, `context.py`, `budget.py` |

### 8.2 Slice 2: Expansion

**Theme: "Learn to breathe -- expand when needed, contract when done"**

**Duration estimate:** 3-4 weeks

**Prerequisite:** Slice 1 complete + at least 2 weeks of real usage data

| Layer | Deliverable |
|---|---|
| **Gateway** | Decomposition assessment stage. Context assembly from multiple sources. Context budget management with trim strategy |
| **Primary Agent** | Decomposition planning. Sub-agent spawning (SubAgentSpec -> SubAgentResult). Result synthesis from sub-agent outputs |
| **Seshat** | Episodic/semantic distinction. promote() pipeline. Graphiti experiment (compare against existing Neo4j) |
| **Delegation** | Stage B: structured handoff (DelegationPackage/DelegationOutcome). Agent suggests delegation proactively. Pattern analysis in insights engine |
| **Brainstem** | Expansion permission signal (expansion_budget). Contraction trigger. Expansion metrics to ES |
| **Telemetry** | Expansion dashboard. Context budget dashboard. Delegation outcomes dashboard. Memory comparison dashboard |

Acceptance criteria:

- [ ] Gateway decomposition stage operational (SINGLE/HYBRID/DECOMPOSE/DELEGATE decisions emitted to ES)
- [ ] Context budget management active: token counts logged, trimming occurs when over budget
- [ ] At least one successful HYBRID execution: sub-agent spawned, result synthesized
- [ ] SubAgentSpec/SubAgentResult types implemented with full ES tracing
- [ ] Brainstem expansion_budget signal operational and visible in telemetry
- [ ] Episodic and semantic memory types distinguished in Neo4j
- [ ] At least one promote() execution: episode consolidated to semantic fact
- [ ] Graphiti experiment completed with comparison report
- [ ] DelegationPackage/DelegationOutcome types in use for Stage B handoffs
- [ ] Kibana dashboards for expansion, context budget, and delegation outcomes

What you learn:

- When does expansion actually help vs. add overhead?
- What's the practical context budget on your hardware?
- Does Graphiti's temporal model fit your needs?
- How does episodic-to-semantic promotion work in practice?
- What makes structured delegation better than Stage A?

What this unblocks: programmatic delegation, proactive memory, dynamic skill
loading, self-improvement loop closure.

### 8.3 Slice 3: Intelligence

**Theme: "The agent gets smarter about itself and its world"**

**Duration estimate:** 4-5 weeks

**Prerequisite:** Slice 2 complete + delegation data + memory experiment results

| Layer | Deliverable |
|---|---|
| **Gateway** | Memory-informed context assembly (Seshat proactive recall shapes context). Dynamic skill loading from disk. Decomposition learning (adjust thresholds from data) |
| **Primary Agent** | Sub-agent model routing experiment (9B vs 35B for sub-tasks). Self-improvement delegation (agent packages its own approved proposals for Claude Code) |
| **Seshat** | Proactive surfacing: suggest_relevant(). Lifecycle management: demote() + forget(). Contradiction detection. Procedural memory. Backend decision based on Slice 2 experiments |
| **Delegation** | Stage C: programmatic orchestration via API/MCP/CLI. Context briefing generation. Delegation feedback becomes procedural memory |
| **Self-Improvement** | Closed loop operational. Agent as architecture collaborator. Improvement ROI tracking |
| **Telemetry** | Memory intelligence dashboard. Self-improvement dashboard. Sub-agent model comparison. Full system health overview |

Acceptance criteria:

- [ ] Proactive memory surfacing operational: suggest_relevant() called during context assembly
- [ ] Lifecycle management: at least one demote() or forget() execution with reason logged
- [ ] Contradiction detection: check_contradiction() runs on new facts
- [ ] At least one procedural memory stored (tool pattern or delegation template)
- [ ] Dynamic skill loading: different skills loaded for different task types (visible in ES)
- [ ] At least one programmatic delegation: agent calls external agent via API/CLI
- [ ] Self-improvement loop closed: approved proposal delegated to Claude Code, outcome captured
- [ ] Seshat backend decision documented based on Slice 2 experiment data
- [ ] Full system health dashboard operational in Kibana

What you learn:

- Does proactive memory improve conversations or add noise?
- Does forgetting improve memory quality?
- Can the agent reliably implement its own improvements?
- Does programmatic delegation work end-to-end?
- What's the ROI of the self-improvement loop?

### 8.4 After Slice 3

Slice 3 produces the research platform from which future work emerges
organically. Possible directions -- driven by data, not speculation:

| Direction | Triggered By |
|---|---|
| Voice/mobile interface | If ambient interaction beyond terminal is desired |
| Richer inter-agent protocols | If Stage C delegation reveals protocol gaps |
| Infrastructure migration | If ES/Neo4j/Postgres metrics show bottlenecks |
| Advanced memory models | If Seshat experiments reveal needs beyond graphs |
| Multi-user support | If the agent should collaborate with others |
| Agent marketplace integration | As new coding agents/hives emerge |

These are not planned. They are hypotheses the system will generate through
its own self-improvement loop.

### 8.5 Slice Summary

| | Slice 1: Foundation | Slice 2: Expansion | Slice 3: Intelligence |
|---|---|---|---|
| **Theme** | One brain, clean interface | Learn to breathe | Gets smarter about itself |
| **Gateway** | Intent classification | Decomposition + budget | Memory-informed + skills |
| **Agent** | 35B primary, simplified | Sub-agents, synthesis | Self-improvement delegation |
| **Seshat** | Protocol + wrapper | Episodic/semantic + Graphiti | Proactive + lifecycle |
| **Delegation** | Stage A: composition | Stage B: structured | Stage C: programmatic |
| **Telemetry** | Intent dashboards | Expansion + budget | Full system intelligence |
| **Duration** | 2-3 weeks | 3-4 weeks | 4-5 weeks |

---

## 9. What This Supersedes

### 9.1 Linear Project Cleanup

The 2.4 Multi-Agent Orchestration project has 6 open issues designed for the
old specialist-agent architecture:

| Issue | Title | Action | Reason |
|---|---|---|---|
| **FRE-5** | Architecture assessment and ADRs | Done | Already completed |
| **FRE-12** | Router SLM integration | Close | Router SLM replaced by deterministic gateway |
| **FRE-13** | Agent base class + standard interface | Close | Replaced by SubAgentSpec/Result and DelegationPackage/Outcome |
| **FRE-14** | Orchestrator-as-supervisor pattern | Close | Replaced by gateway decomposition + primary agent planning |
| **FRE-15** | Coder specialist agent (devstral) | Close | No internal coding specialist. Coding delegates externally |
| **FRE-16** | Analyst specialist agent (qwen3-8b) | Close | No internal analyst. 35B handles analysis; sub-agents for parallelism |
| **FRE-26** | LM Studio sequential processing | Review | Partly addressed by ADR-0029. Keep if LM Studio still used |

New Linear issues will be created for the three slices, each broken into
concrete tasks during implementation planning.

### 9.2 ADR Impact

| ADR | Action |
|---|---|
| **ADR-0017** (Three-Tier Multi-Agent) | Superseded. Write ADR-0033 referencing this spec |
| **ADR-0018** (Seshat) | Evolves. Core vision preserved. Implementation approach changes to protocol-first |
| **ADR-0024** (Session Graph) | Unchanged |
| **ADR-0025** (Memory Recall) | Unchanged. Mechanism moves into gateway Stage 4 |
| **ADR-0026** (search_memory tool) | Unchanged |
| **ADR-0029** (Concurrency Control) | Unchanged. Even more important with sub-agent spawning |

### 9.3 Phase Structure -- Revised

Old phases:

```
2.3 Homeostasis (in progress)
2.4 Multi-Agent Orchestration        <- obsolete as specified
2.5 Seshat Memory                    <- deferred too far
2.6 Conversational Agent MVP
3.0 Daily-Use Interface
```

New phases:

```
2.3  Homeostasis and Observability (in progress -- complete current work)

2.4  Cognitive Architecture Redesign  <- THIS SPEC
       Slice 1: Foundation (gateway + single agent + protocol + Stage A)
       Slice 2: Expansion (decomposition + sub-agents + memory types + Stage B)
       Slice 3: Intelligence (proactive memory + programmatic delegation +
                self-improvement)

3.0  Emergent -- driven by data from Slice 3
       Whatever the system's own insights engine tells you matters next
```

Phase 2.6 (Conversational Agent MVP) is absorbed into Slice 1 -- the
single-agent architecture with persistent sessions IS the conversational
agent. Phase 3.0 is not pre-planned -- it emerges from what the system learns
about itself.

### 9.4 Infrastructure Evolution Notes

The current stack is solid for the work ahead. Pressure points to watch:

| Service | Potential Pressure | When To Evaluate |
|---|---|---|
| **PostgreSQL** | Session table growth with multi-turn conversations | If query latency degrades or table exceeds ~10M rows |
| **Elasticsearch** | Index growth with new telemetry (delegation, expansion, sub-agents) | If cluster size exceeds disk budget. Current lifecycle policies should handle this |
| **Neo4j** | Graph query performance as entity count grows | After Slice 2 Graphiti experiment. Data-driven decision |
| **LM Studio** | Single-model bottleneck. Sub-agent spawning adds inference pressure | If sub-agent parallelism is limited by serving throughput. Consider vLLM, llama.cpp server, MLX serving |

Principle: don't migrate proactively. Migrate when telemetry shows a
bottleneck. The observability stack exists precisely to inform these decisions
with evidence.

---

## References

- ADR-0017: Three-Tier Multi-Agent Orchestration (superseded by this spec)
- ADR-0018: Seshat Memory Librarian Agent (evolves with this spec)
- ADR-0024: Session-Centric Graph Model for Behavioral Memory
- ADR-0025: Memory Recall Intent Detection
- ADR-0026: search_memory Native Tool
- ADR-0029: Inference Concurrency Control
- Cognitive Architecture Overview: `docs/architecture/COGNITIVE_AGENT_ARCHITECTURE_v0.1.md`
- Research: `docs/research/cognitive_architecture_principles.md`
- Research: `docs/research/world-modeling.md`
- Research: `docs/research/context-switching-task-segmentation.md`
- Captain's Log: ADR-0030 (dedup and self-improvement pipeline)
- Service Architecture: `docs/architecture/SERVICE_IMPLEMENTATION_SPEC_v0.1.md`
