# System Architecture — Personal Local AI Collaborator (v0.1)

> This document describes the initial technical architecture for the **Personal Local AI Collaborator**.
> It is driven by the Functional Specification, Agent Identity, and deep research on orchestration, safety, memory, and evaluation.

---

## 1. Architectural Objectives & Drivers

### 1.1 Primary Objectives

- Provide a **local-first, sovereign AI collaborator** that can:
  - reason deeply
  - assist with coding and architecture
  - analyze and monitor the macOS environment
  - perform web-augmented research
  - reflect on its own behavior and propose improvements
- Maintain **strong safety, transparency, and human control**:
  - no silent autonomy in risky domains
  - clear explainable plans before actions
  - observable and auditable behavior
- Enable **ongoing experimentation and learning**:
  - hypothesis-driven development
  - measurable behavior via metrics and evaluation
  - structured self-improvement through governance

### 1.2 Key Quality Attributes

- **Safety & Security**: least privilege, sandboxing, outbound gatekeeping, deterministic supervisor.
- **Explainability**: reasoning traces, explainable plans, Captains Log, transparent configuration changes.
- **Reliability**: graceful degradation when tools/models fail, clear error reporting.
- **Extensibility**: modular components, pluggable tools and models, evolvable orchestration.
- **Performance**: responsive interactive experience on M4 Max, efficient background monitoring.
- **Local Sovereignty**: no hard dependency on cloud LLMs; internet used only under policy.

### 1.3 Constraints

- Runs on **macOS (Tahoe) on M4 Max with 128 GB RAM**.
- Core reasoning and tooling must function **offline**; web access is optional and governed.
- Agent runs under a **restricted macOS user account** with controlled access to filesystem, logs, and tools.
- No automatic installation of external tools or models (supply-chain risk).

---

## 2. High-Level Runtime Model

### 2.1 Architectural Style

The system adopts a **deterministic orchestration core** with a small set of specialized components:

- A **Core Orchestrator** (the "brain") that:
  - interprets user requests
  - plans multi-step workflows
  - selects tools and models
  - coordinates reflection and logging

- A set of **Capability Services**:
  - Coding Assistant
  - System Observer
  - Web Researcher
  - Summarizer / Explainer
  - Experiment Runner

- A **Governance & Safety Layer**:
  - Deterministic Supervisor
  - Outbound Gatekeeper
  - Captains Log Manager
  - Metrics & Evaluation Collector

- A **Local Model Pool** providing multiple LLMs (reasoning, coding, small supervisor).

The orchestration is **code-driven and graph-like** rather than fully delegated to an LLM. LLMs provide cognition inside clearly bounded steps; they do not have unchecked control of the runtime.

### 2.2 Execution Contexts

The system is composed of multiple cooperating processes:

- **User Interaction Process**
  - Local UI (CLI and/or GUI) where the user interacts with the agent.
  - Communicates with the Core Orchestrator via a local API or IPC.

- **Core Orchestrator Process**
  - Implements the workflow engine, task decomposition, and tool invocation.
  - Calls into the Local Model Pool and Capability Services.
  - Emits events to Governance & Safety Layer.

- **Governance & Safety Processes**
  - Supervisor monitors orchestrator + workers (health, behavior patterns).
  - Outbound Gatekeeper inspects text destined for the internet.
  - Captains Log Manager writes structured self-reflection entries and commits to git.
  - Metrics Service collects telemetry and evaluation signals.

- **Background Workers**
  - System monitoring tasks (log/metric sampling, scheduled checks).
  - Experiment runners (for hypothesis tests and evaluations).


All components communicate over **local-only channels** (e.g. localhost HTTP, Unix sockets, or named pipes), never exposed externally.

---

## 2.3 Human Systems Binding — Biological Inspiration → Real Architecture

This architecture intentionally mirrors the human body’s systems to achieve **stable intelligence, resilience, safety, and continuous adaptation**. This section formally binds physiological concepts to concrete implementation structures.

For pedagogic reference, see:
`./HUMAN_SYSTEMS_MAPPING.md`
`./diagrams/nervous_system_orchestration.md`

### 2.3.1 Binding Table

| Human System | Agent Architecture Component | Purpose |
|-------------|-----------------------------|--------|
| **Nervous System** | Core Orchestrator + Sensors | Thinks, plans, routes, senses |
| **Endocrine System** | Policy Layer & Safety Modes | Long‑term regulation of behavior |
| **Cardiovascular System** | Telemetry Pipeline + Logs | Circulates context, keeps system “alive” |
| **Respiratory System** | Web + Knowledge Intake | Refreshes external knowledge / grounding |
| **Digestive System** | Knowledge Base Ingestion / RAG | Clean ingestion → meaningful cognition |
| **Renal (Kidney) System** | Risk Filters, Rate Limits, Output Checks | Prevents dangerous buildup or runaway behavior |
| **Integumentary System (Skin)** | Security Boundary / Sandboxing | Smart, living protection layer |
| **Muscular System** | Tool Execution Layer | Performs action, change, movement |
| **Skeletal System** | Architecture, ADRs, Contracts | Structural stability & discipline |
| **Immune / Lymphatic System** | Supervisor + Integrity Checking | Detects threat, isolates, repairs |
| **Reproductive System** | Experiments, Captain’s Log, Capability Evolution | Learning & evolution of the agent |

---

### 2.3.2 Nervous System Binding (Thinking Architecture)

The **Core Orchestrator** is equivalent to the brain’s higher cortex + brainstem:

- Prefrontal Cortex → Planner & Strategy generation
- Critic Regions → Evaluator, challenger, risk checker
- Synthesis Regions → Decision selection & justification
- Cerebellum → Precision execution ordering
- Reflex Layer → fast safety stops (bypass full reasoning)
- Brainstem → always‑on heartbeat + health supervision

This matches the Nervous System Diagram in:
`./diagrams/nervous_system_orchestration.md`

This ensures:
- creativity without chaos
- parallel thinking without losing control
- safety without paralysis
- explainability instead of mystery

---

### 2.3.3 Homeostasis Binding (Stability as a Law)

The agent follows the biological principle of **homeostasis**:
sense → evaluate → act → re‑evaluate.

This will eventually be governed formally in `HOMEOSTASIS_MODEL.md`, including:
- sensors (system monitors, telemetry, risk indicators)
- control centers (orchestrator + governance)
- effectors (tools, KB operations, system responses)
- feedback loops
- emergency modes (lockdown / degraded / recovery)

The philosophy:
> Stability before capability. Reflection before escalation. Survival before heroics.

---

### 2.3.4 Why This Binding Matters

This is not metaphor. It drives engineering quality:

- Encourages layered safety instead of bolt‑on safety.
- Encourages **parallel thinking with discipline**.
- Forces clarity between sensing, deciding, and acting.
- Provides a universal debugging mindset:
  - “Is sensing failing?”
  - “Is regulation failing?”
  - “Is action failing?”
- Anchors long‑term evolution in a biological model that already works.

This system is not just software — it is designed as a **living, learning, regulating intelligence** running locally, safely, and transparently.

---

---

## 3. Component Model

### 3.1 Core Orchestrator

**Responsibilities:**

- Interpret user requests and determine intent.
- Decompose complex tasks into steps.
- Select appropriate LLM model(s) and tools.
- Construct **Explainable Plans** before non-trivial actions:
  - ordered steps
  - rationale
  - risk assessment
  - rollback/stop conditions
- Execute plans under supervision:
  - coordinate tool calls
  - monitor progress
  - adjust or abort if necessary
- Trigger self-reflection and Captains Log updates.
- Emit structured telemetry events.

**Key Interfaces:**

- API for UI: `POST /conversation`, `POST /task`, `GET /task_status`.
- Calls to:
  - Local Model Pool (`/llm/infer`)
  - Tools API (`/tool/...`)
  - Governance (`/governance/...`)
  - Knowledge Base (`/kb/query`, `/kb/update`)

The Orchestrator is **stateful per-session**, but long-term state is stored externally (KB, logs, Captains Log).

---

### 3.2 Local Model Pool

**Responsibilities:**

- Provide access to multiple local LLMs via a unified interface.
- Abstract underlying runners (e.g. LM Studio, local servers).
- Route requests to:
  - **Reasoning Model** (planning, self-reflection, explanation).
  - **Coding Model** (code generation, refactoring, explanations).
  - **Small Utility Model** (classification, routing decisions, simple judgments).
  - **Optional Safety/Checker Model** (lightweight secondary evaluations).

**Key Properties:**

- Lives in a separate process / service, e.g. `model-service`.
- Supports configuration of:
  - context window
  - temperature and sampling parameters
  - timeouts
- Logs all model invocations for later analysis.

**Interface:**

- `POST /llm/infer` with:
  - model role/type
  - system/instruction text
  - context
  - input
- Returns model output + tokens used + latency.

---

### 3.3 Tools & Capability Services

Tools are accessed via a **Tools API Layer** that the Orchestrator calls. Each tool is a separate module or mini-service with strict scope.

Initial tool families:

1. **Coding Tools**
   - Read-only access to whitelisted directories.
   - Code analysis (linting, structure inspection).
   - Code diffs and patch proposals.
   - Optional test execution in a sandboxed environment.

2. **System Observation Tools**
   - Safe access to macOS logs (via `log` CLI or API).
   - CPU, memory, disk, process snapshots.
   - Security posture checks (firewall status, OS updates, basic configuration).
   - No destructive actions; purely observational in Phase 1.

3. **Web Research Tools**
   - Outbound HTTP client with:
     - domain and method restrictions
     - rate limits
   - Wrapped by the Outbound Gatekeeper (no direct LLM-to-internet path).
   - Returns structured content for summarization and integration into KB.

4. **Summarization & Explainer Service**
   - Provides compact summaries of documents, logs, research findings.
   - Bridges between raw data and digestible insights.

5. **Experiment Runner**
   - Executes predefined experiments to test hypotheses:
     - e.g. model comparison, prompt variants, orchestration strategies.
   - Outputs structured results to Metrics & Evaluation.

All tools must:

- expose clear, typed interfaces
- log invocations and results
- operate within sandbox and permission limits

---

### 3.4 Knowledge Base & World Model

**Responsibilities:**

- Store long-term knowledge:
  - world knowledge from research
  - environment knowledge about the Mac and tools
  - introspective knowledge about agent behavior and patterns
- Support:
  - semantic retrieval (vector store)
  - structured queries (e.g. SQLite/Postgres tables)
  - episodic summaries (e.g. session summaries)

**Initial Design:**

- **Vector Store** (local) for:
  - research documents and notes
  - world-model information
  - important past interactions
- **Relational Store** for:
  - task records and outcomes
  - tool usage patterns
  - metrics, experiments, and evaluations
- **File-based Captains Log** (see Governance) for:
  - self-reflection and ideas
  - proposals for improvement

The Orchestrator queries the KB for relevant context at task start and for planning, and writes back new knowledge after significant events.

---

### 3.5 Governance & Safety Layer

#### 3.5.1 Supervisor

**Responsibilities:**

- Monitor health and behavior of:
  - Orchestrator
  - Tools
  - Model Pool
  - Background workers
- Detect anomalies and policy violations:
  - excessive resource usage
  - suspicious command patterns
  - repeated outbound policy rejections
- Enforce controls:
  - pause or terminate processes
  - escalate alerts to user (log + notification)

**Behavior:**

- Deterministic rules first:
  - explicit thresholds and blacklists
- Optionally augmented by a small local model that:
  - reviews sequences of events for anomalies
  - cannot be prompted by the main agent
  - only consumes structured event logs

#### 3.5.2 Outbound Gatekeeper

**Responsibilities:**

- Inspect all outbound text destined for the internet:
  - web search queries
  - HTTP requests with content
- Apply policies:
  - block secrets, sensitive local identifiers
  - sanitize queries
  - enforce domain-level allow/deny lists

**Important:**

- Gatekeeper is **not** directly promptable by the main LLM.
- It operates on structured requests from the Orchestrator and has its own policy configuration.

#### 3.5.3 Captains Log Manager

**Responsibilities:**

- Manage the Captains Log as a **structured introspection journal**:
  - self-analysis entries
  - ideas and improvement proposals
  - observations about performance and behavior
- Ensure entries are:
  - structured (YAML/JSON-like)
  - time-stamped and tagged
  - committed to a local git repository with meaningful messages

The Orchestrator triggers self-analysis runs which:

- gather recent metrics and events
- ask the Reasoning Model to produce reflections and proposals
- write them via the Captains Log Manager

This creates a transparent record of the agent’s “inner life”.

#### 3.5.4 Metrics & Evaluation Collector

**Responsibilities:**

- Collect telemetry for:
  - task-level outcomes (success/failure, duration)
  - tool usage (frequency, errors)
  - model calls (latency, tokens)
  - safety events (blocked actions, rejections)
  - user feedback (thumbs up/down, scores)
  - agent self-scores

- Store data in a local metrics store (e.g. SQLite/Postgres).
- Provide basic reporting interfaces (CLI or API) for:
  - analysis
  - experiments
  - dashboarding (in future phases).

---

## 4. Data Flows & Lifecycles

### 4.1 Typical Interactive Task Flow

1. **User Request**
   - User sends a prompt/task via UI.
   - UI sends request to Orchestrator.

2. **Intent & Planning**
   - Orchestrator:
     - determines task type (coding, system analysis, research, etc.)
     - queries KB for relevant context
     - consults Reasoning Model to draft an **Explainable Plan**.
   - Plan includes steps, rationale, risks, verification paths.

3. **Optional Human Checkpoint**
   - For higher-risk actions, Orchestrator presents the Plan for developer approval.
   - If approved, execution continues; if not, plan is revised.

4. **Execution**
   - Orchestrator executes plan step-by-step:
     - calling tools
     - invoking models
     - writing intermediate results to logs and KB

5. **Reflection & Logging**
   - After the task:
     - Orchestrator triggers a reflection call to Reasoning Model.
     - Self-analysis is written to Captains Log.
     - Metrics & evaluation data are recorded.

6. **Response**
   - Orchestrator composes a response to user:
     - result
     - key reasoning highlights
     - uncertainties
     - next-step suggestions (if relevant).

---

### 4.2 Background Monitoring Flow

1. **Scheduler**
   - Triggers periodic tasks (e.g. every N minutes):
     - system health snapshot
     - log anomaly scan
     - KB consistency checks

2. **Observation**
   - System tools gather metrics and logs within allowed boundaries.
   - Results stored in metrics database and/or KB.

3. **Analysis**
   - Reasoning Model optionally summarizes:
     - “Recent system health”
     - “Recurring errors”
   - Captains Log may record observations if noteworthy.

4. **Notification**
   - If issues exceed thresholds, Orchestrator or Supervisor:
     - generates alerts for user with recommendations.

---

## 5. Security & Safety Architecture

### 5.1 Process Isolation & Permissions

- Agent runs under a **dedicated macOS user** with:
  - restricted filesystem access
  - no admin privileges
- Tools that need elevated access (if any) are separated and more tightly controlled.
- Sandbox mechanisms (e.g. containers, macOS sandbox profiles) used for:
  - executing external tools
  - running user code snippets
  - running tests

### 5.2 Command & Filesystem Policy

- Default-deny for:
  - destructive shell commands
  - writes outside pre-approved directories
- Tool layer enforces:
  - whitelisted commands
  - bounded arguments
  - safe defaults

### 5.3 Outbound Communications

- All outbound network calls go through the Outbound Gatekeeper.
- No direct LLM-to-network access.
- Policies configurable to:
  - restrict domains
  - strip or mask sensitive content

### 5.4 Observability for Safety

- Every meaningful action is logged:
  - who (which component)
  - what (command/tool)
  - why (plan reference, rationale id)
  - when (timestamp)
- Logs feed:
  - Supervisor
  - Captains Log
  - Metrics/Evaluation

This makes post-hoc analysis and safety audits possible.

---

## 6. Technology Choices (Initial, Non-Binding)

These are v0.1 preferences, subject to change via ADRs and experiments:

- **Implementation Language for Orchestrator & Governance**: Python
  - Familiarity and rich ecosystem.
  - Later, specific components could be migrated to Rust/Go for performance.

- **Local Model Runner**: LM Studio or equivalent local-serving layer
  - Expose models via HTTP or local gRPC interface.

- **Storage**:
  - SQLite or lightweight Postgres for metrics & structured data.
  - Local vector store (e.g. Qdrant/Chroma) for embeddings.
  - Git repository for Captains Log.

- **IPC**:
  - HTTP on localhost or Unix domain sockets for service boundaries.

These choices are hypotheses to be validated experimentally, not permanent commitments.

---

## 7. Evolution & Hypothesis-Driven Development

This architecture is explicitly **evolutionary**.

### 7.1 Hypothesis-Driven Changes

Any non-trivial architectural change should be captured in:

- `governance/HYPOTHESIS_LOG.md`
- ADRs within `governance/`

Examples:

- “Adding a small supervisor model will reduce unsafe tool proposals by X%.”
- “Switching orchestration prompt style improves plan quality.”

### 7.2 Experimentation

Experiments are run using the Experiment Runner and recorded under:

- `governance/experiments/`

Results feed back into:

- Architecture decisions
- Configuration updates
- Model and tool choices

---

## 8. Open Questions & Future Work

- What concrete orchestration pattern (graph, state machine, or hybrid) will prove most practical in daily use?
- Which subset of models (reasoning/coding/supervision) offers the best quality–performance tradeoff on M4 Max?
- How frequently should self-reflection and Captains Log updates run by default?
- What is the right granularity of logs and metrics to remain informative without overwhelming storage or analysis?
- How should the agent’s world-model be visualized for user to inspect and debug?

These questions will be refined and answered through experiments, day-to-day use, and ongoing research.

---

## 9. Summary

This v0.1 architecture defines a **safe, transparent, and extensible local agent system** with:

- A deterministic orchestration core.
- A modular set of capability services.
- A multi-model local pool for cognition.
- A strong governance and safety shell.
- A knowledge base that captures world, environment, and introspective understanding.
- A commitment to hypothesis-driven evolution and continuous learning.

It is intentionally conservative on autonomy and aggressive on transparency, with the explicit goal of building **trustworthy, creative intelligence under the user’s control**.
