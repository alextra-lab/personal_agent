# ADR-0043: Three-Layer Architectural Separation

**Status**: Accepted
**Date**: 2026-04-13
**Deciders**: Project owner
**Related**: ADR-0016 (Service-Based Cognitive Architecture), ADR-0035 (Seshat Backend — Neo4j), ADR-0041 (Event Bus — Redis Streams), ADR-0042 (Knowledge Graph Freshness)
**Enables**: ADR-0044 (Provider Abstraction), ADR-0045 (Infrastructure), ADR-0049 (Application Modularity)

---

## Context

### Current architecture: functionally layered but not explicitly separated

The Personal Agent has grown organically through Phases 2.1–2.6 and the Cognitive Architecture Redesign (Slices 1 & 2). The result is a working system with clear module boundaries (`request_gateway/`, `orchestrator/`, `memory/`, `brainstem/`, `telemetry/`, `llm_client/`), but no explicit architectural layering that separates **what the agent knows** from **how it reasons** from **what it observes about itself**.

This matters now because:

1. **Multi-device access is imminent** (ADR-0048). A phone client and a laptop CLI must see the same knowledge — conversation history, entities, relationships, user preferences. If knowledge is entangled with execution, every client needs a full execution stack just to read memory.

2. **Dual-harness operation** (ADR-0044). Local models and cloud models will run simultaneously, each producing different execution traces but operating on the same knowledge base. Without layer separation, each harness drags its own knowledge store — a recipe for divergence.

3. **Remote agent integration** (ADR-0050). Claude Code, Codex, and Cursor sessions need read (and potentially write) access to Seshat's knowledge graph. If knowledge is only accessible through the execution layer's internal APIs, external agents must go through the full orchestrator pipeline to answer simple questions.

4. **Self-improvement** (Slice 3). The agent's ability to reason about its own performance requires clean separation between "what happened" (observations) and "what I know" (knowledge). Mixing these makes it impossible to ask "did my context assembly improve after I changed my retrieval strategy?" — you can't compare execution traces if they're interleaved with the knowledge they operate on.

### What "layer" means here

A layer is not a deployment boundary (though layers _can_ be deployed separately). It is:

- A **data ownership boundary**: each layer owns specific data, with clear APIs for other layers to access it.
- A **lifecycle boundary**: knowledge persists indefinitely, execution state lives for a request or session, observations are retained per policy.
- A **coupling constraint**: layers depend downward (Execution → Knowledge, Observation → Execution), never upward.

### Current module mapping

| Module | Primary concern | Layer affinity |
|--------|----------------|---------------|
| `memory/` | Entity/relationship storage, recall, promotion | Knowledge |
| `request_gateway/` | Intent classification, context assembly, decomposition | Execution |
| `orchestrator/` | LLM invocation, tool calling, sub-agent orchestration | Execution |
| `llm_client/` | Model dispatch, inference, response parsing | Execution |
| `tools/` | External tool integration | Execution |
| `mcp/` | MCP gateway and governance | Execution |
| `brainstem/` | Scheduling, sensors, homeostasis | Observation + Execution (mixed) |
| `telemetry/` | Structured logging, ES indexing | Observation |
| `captains_log/` | Self-improvement capture, dedup | Observation |
| `insights/` | Delegation pattern analysis | Observation |
| `events/` | Event bus (Redis Streams) | Infrastructure (cross-cutting) |
| `config/` | Settings, model config | Infrastructure (cross-cutting) |
| `service/` | FastAPI endpoints | Interface (cross-cutting) |

The `brainstem/` is the clearest case of layer mixing: it owns both observation logic (sensors, quality monitors) and execution-side scheduling (consolidation triggers, lifecycle loops). Separating these concerns will simplify both testing and future deployment flexibility.

---

## Decision

Adopt a **three-layer architecture** with explicit boundaries:

### Layer 1: Knowledge Layer

**Owns**: Facts, entities, relationships, conversation history, agent memory, user profile data.

**Properties**:
- **Persistent and growing** — knowledge accumulates; deletion is deliberate, not incidental.
- **Shared across all clients** — phone, iPad, laptop CLI, web UI all read/write the same knowledge store.
- **Shared across all execution profiles** — local and cloud agents operate on one knowledge base, never fork it.
- **Sovereignty-preserving** — the user owns all data. Export, deletion, and migration are first-class operations.

**Current backing stores**:
- Neo4j (knowledge graph: entities, relationships, episodes, semantic memories)
- PostgreSQL (session history, conversation messages, structured metrics)
- Elasticsearch (indexed logs and traces — queryable knowledge about past behavior)

**API surface**: The Knowledge Layer exposes a protocol-based interface (extending `MemoryProtocol` from Slice 1) that any client or execution profile can call. This is not a REST API today — it's an internal Python protocol — but it must be designed so that a network-accessible API can be placed in front of it (ADR-0045).

**What it does NOT contain**: LLM inference, prompt construction, tool orchestration, model configuration. Knowledge doesn't know how it was produced.

### Layer 2: Execution Layer

**Owns**: LLM inference, tool orchestration, agent loops, prompt construction, decomposition, sub-agent management, delegation.

**Properties**:
- **Swappable** — local models, cloud models, or hybrid; the knowledge layer doesn't care which produced a result.
- **Profile-based** — execution configuration is captured in profiles (ADR-0044). A "local" profile uses Qwen3.5 on llama.cpp; a "cloud" profile uses Claude/Gemini via LiteLLM. Both read from and write to the same Knowledge Layer.
- **Testable in isolation** — execution can be tested with mock knowledge (fixture data) and mock observations (no telemetry sink).
- **Disposable** — an execution profile can be torn down and replaced without losing knowledge or observation history.

**Current modules**: `request_gateway/`, `orchestrator/`, `llm_client/`, `tools/`, `mcp/`.

**Delegation boundary**: When the execution layer delegates to an external agent (Claude Code, Codex), it composes a `DelegationPackage` (ADR-0033, Slice 2). The external agent may query the Knowledge Layer directly (via MCP server, ADR-0050) or receive context in the delegation package. Either way, knowledge produced by the external agent flows back through the execution layer and into the Knowledge Layer.

### Layer 3: Observation Layer

**Owns**: Execution traces, performance metrics, cost tracking, self-monitoring, insights, quality assessments.

**Properties**:
- **Profile-scoped traces** — each execution trace is tagged with the profile that produced it (local, cloud, hybrid). Traces from different profiles are queryable together but attributable.
- **Shared collaboration log** — the log of what questions were asked, what knowledge was produced, and what decisions were made is shared across all profiles. This is distinct from execution traces (which are profile-specific).
- **Self-monitoring** — the observation layer feeds signals back to the execution layer (via brainstem sensors and the event bus) to enable adaptive behavior.
- **User-accessible** — the user can discuss observations with the agent through the UI (ADR-0048). The agent can answer "how did you perform on that task?" because observations are structured and queryable.

**Current modules**: `telemetry/`, `captains_log/`, `insights/`.

**Brainstem split**: The `brainstem/` module currently mixes observation (sensors, quality monitors) with execution-side scheduling. Under this ADR:
- **Observation-side brainstem** (sensors, quality metrics, homeostasis state) stays in the Observation Layer.
- **Scheduling and lifecycle triggers** (consolidation, promotion, cleanup) move to a cross-cutting scheduling concern that the Execution Layer owns, informed by Observation Layer signals via the event bus (ADR-0041).

### Dependency direction

```
┌──────────────────────────┐
│   Interface Layer        │  (CLI, API, PWA — ADR-0048)
│   Depends on: all three  │
└──────────┬───────────────┘
           │
┌──────────▼───────────────┐
│   Execution Layer        │  (request gateway, orchestrator, LLM clients, tools)
│   Depends on: Knowledge  │
│   Reads from: Observation│
└──────────┬───────────────┘
           │
┌──────────▼───────────────┐     ┌─────────────────────────┐
│   Knowledge Layer        │◄────│   Observation Layer     │
│   Depends on: nothing    │     │   Depends on: Knowledge │
│   (foundation)           │     │   Writes to: own stores │
└──────────────────────────┘     └─────────────────────────┘
```

- Knowledge depends on nothing — it is the foundation.
- Execution depends on Knowledge (reads context, writes results).
- Observation depends on Knowledge (reads knowledge to contextualize traces) and receives events from Execution (via event bus), but Execution does not depend on Observation.
- The Interface Layer (CLI, API, PWA) depends on all three — it's the composition point.

### Security and authentication at layer boundaries

Once layers can be separated by a network (ADR-0045), every inter-layer boundary becomes an authentication and authorization surface:

**Knowledge Layer API access**:
- All clients (execution profiles, external agents, mobile UI) authenticate via API tokens with explicit scopes (read, write, admin).
- Tokens are rotatable and revocable. No default credentials.
- Write access is audited: who wrote what, when, from which client.
- Sensitive data (conversation content, user profile) is encrypted at rest. PII redaction applies before Observation Layer indexing.

**Execution Layer ↔ Knowledge Layer**:
- Execution profiles authenticate to the Knowledge Layer with profile-scoped tokens. A local profile and a cloud profile get separate tokens with potentially different write scopes.
- External agents (ADR-0050) get separate, more restricted tokens — typically read-only by default, with explicit write grants.

**Observation Layer access**:
- Observation data (traces, metrics, costs) may contain sensitive execution details. Access requires authentication.
- The agent's self-monitoring reads are internal (same trust boundary). User-facing observation queries (via UI) go through the same auth as Knowledge Layer access.

**Event bus**:
- Redis Streams (ADR-0041) currently runs on localhost. When deployed on a cloud VPS (ADR-0045), Redis requires AUTH and TLS.
- Event payloads carry identifiers, not large data (ADR-0041 design principle), limiting exposure if the bus is compromised.

**Principle**: Security is not a separate layer — it is enforced at every boundary where trust changes. In-process boundaries (same service) can trust without auth. Network boundaries (VPS API, MCP server) always authenticate.

### Cross-cutting concerns

Some components don't belong to a single layer:
- **Event bus** (`events/`): Transport between layers. Infrastructure, not a layer.
- **Configuration** (`config/`): Shared settings. Infrastructure.
- **Service** (`service/`): FastAPI endpoints — Interface Layer, composing all three.
- **Security**: Applied at every inter-layer boundary where trust changes. Not a single module — authentication is enforced at the Knowledge Layer API, the Observation API, the event bus, and the MCP server (ADR-0050).

---

## Consequences

### Positive

- **Multi-device becomes straightforward**: Any client that can reach the Knowledge Layer API gets full access to the agent's knowledge. No need to replicate execution machinery on every device.
- **Dual-harness is safe**: Local and cloud execution profiles share knowledge without risk of divergence, because knowledge ownership is explicit and centralized.
- **External agents get clean access**: Claude Code can query the Knowledge Layer via an MCP server (ADR-0050) without going through the orchestrator pipeline.
- **Self-improvement is tractable**: Observation data is cleanly separated from knowledge, making it possible to ask comparative questions about execution performance.
- **Independent evolution**: Each layer can be modified, tested, and (eventually) deployed independently. Replacing Neo4j with a different graph database affects only the Knowledge Layer.

### Negative

- **Brainstem refactoring required**: The current `brainstem/` module must be split. Sensors and quality monitors stay in Observation; scheduling moves to Execution. This is mechanical but touches a lot of code.
- **More explicit data flow**: Operations that currently "just work" because modules share an in-process boundary (e.g., orchestrator directly calling `MemoryService.query()`) will need to go through defined layer interfaces. This adds indirection but was already partially solved by `MemoryProtocol`.
- **Not a deployment boundary yet**: This ADR establishes logical separation, not physical. The system will continue to run as a single process for now. Physical separation (Knowledge Layer on a cloud VM, Execution on laptop) is addressed in ADR-0045.

### Neutral

- **No immediate code reorganization mandate**: Modules don't need to be physically moved into `knowledge/`, `execution/`, `observation/` directories. The layering is a design constraint on dependencies and data ownership, enforced by convention and review, not by directory structure. Directory reorganization may follow later if the project grows.
- **Collaboration log is a Knowledge Layer concept**: Although collaboration logs are produced during execution and contain observation-like data, they represent _shared knowledge about what happened_ — not execution state. They live in the Knowledge Layer.
