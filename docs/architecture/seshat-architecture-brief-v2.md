# Seshat Architecture Brief v2

## Status: Input for ADR authoring session (Claude Code)
## Date: 2026-04-13

---

## 1. Project Identity

**Name:** Seshat
**Nature:** Personal AI agent harness — knowledge-building, reasoning, tool use
**Current state:** Development paused at local-only, CLI-driven stage
**Owner:** Single developer (Alex), Paris-based, personal use
**Future:** Designed for eventual self-hosted setup by others (not public SaaS)
**Project management:** Linear (existing projects and issues)
**Core principle:** Application modularity is a critical architecture fundamental

---

## 2. Context & Motivation

The current agent system runs almost entirely on a laptop (MacBook Pro M4 max, 128GB).
Local LLM inference has hit practical limits for agentic reliability. Development
has paused while evaluating the path forward. Some components already use cloud models.

The decision: build a platform that supports both local and cloud model execution,
with a real UI for daily use, and proper infrastructure for always-on availability.

Key realization: the system needs a UI NOW — not as a polish step, but as a core
requirement for building real usage history and testing Seshat thoroughly through
actual daily interaction.

---

## 3. Three-Layer Architecture

### 3a. Knowledge Layer
- Facts, entities, relationships, conversation history, agent memory
- Persistent, growing, valuable — independent of how it was produced
- **Must be shared regardless of point of interaction** (phone, iPad, laptop, CLI)
- **Must be shared across all execution profiles** (local and cloud agents)
- Knowledge freshness tracking (when was a fact last confirmed/updated?)
- Knowledge weighting (confidence, source authority, corroboration)
- Knowledge/memory sovereignty: the user owns all data, can export, delete, migrate

### 3b. Execution Layer
- LLM inference, tool orchestration, agent loops, prompt construction
- Where local vs cloud vs hybrid happens — swappable, testable, disposable
- Dual-harness profiles: local models AND cloud models running simultaneously
- Local models can delegate to cloud models (not just one or the other)
- Must integrate with remote agent harnesses: Claude Code, Codex, Cursor, etc.
- Context management with monitoring:
  - Near context (current conversation window)
  - Episodic context (recent session memory)
  - Long-term context (knowledge layer retrieval)
  - Context size monitoring and alerting
  - Compaction logging: what was compacted, why, what was lost
  - Compaction decision feedback loops (was the compaction good?)

### 3c. Observation Layer
- Execution traces tied to specific infrastructure path and model config
- Latency, throughput, cost metrics per profile
- Tool-call success/failure rates, task completion evaluations
- Self-monitoring loop: agent observes its own performance patterns
- **Collaboration log is SHARED** — questions asked, knowledge produced,
  decisions made — connectable to any harness trace but not scoped to one
- The UI should allow the user to access and discuss agent observations
  through conversation with the agent itself

---

## 4. UX & Interaction Requirements

### 4a. Remote Access (Phone / iPad / Laptop)
- Secure interaction with the agent harness from any device
- Must work remotely — not tied to laptop being open (for cloud execution path)
- Build real usage history through daily interaction
- Test Seshat thoroughly through actual use, not just dev testing

### 4b. Model Selection
- Ability to choose Cloud or Local model as starting point for a conversation
- Local models can delegate to cloud models mid-task (hybrid execution)
- Simultaneous cloud agent + local agent operation

### 4c. Visualization Requirements
Two tiers of visualization capability:

**Standard visualizations:**
- Conversation/chat interface
- Knowledge graph exploration
- Context window usage meters
- Agent trace/timeline views
- Cost and performance dashboards

**Forward-thinking / "MCP UI" tier:**
- Rich interactive components returned by the agent as part of responses
- Support for: Vega/Vega-Lite, Plotly, Mermaid diagrams, custom widgets
- Agent-generated UI that goes beyond text walls
- Consider MCP Apps standard for interoperability

### 4d. HITL (Human-in-the-Loop)
- Linear task/issue approval integration
- Agent proposes actions → user approves via Linear or via UI
- Approval decisions feed back into agent learning

---

## 5. Protocol Landscape Analysis (April 2026)

The agent-to-UI space has consolidated around three complementary layers:

### MCP (Model Context Protocol)
- Agent ↔ Tools/Systems
- Mature, widely adopted, Seshat already uses MCP servers
- **MCP Apps extension (Jan 2026):** tools can now return interactive UI
  components (dashboards, forms, visualizations) that render in conversation
- Supported by Claude, ChatGPT, VS Code, Goose, and others
- Uses `ui://` URI scheme, HTML+JS sandboxed in iframes
- Launch partners: Amplitude, Asana, Box, Canva, Figma, Hex, Slack, Salesforce

### AG-UI (Agent-User Interaction Protocol)
- Agent ↔ User Interface (runtime/transport)
- Open, event-based protocol (~16 event types over SSE or WebSocket)
- Covers: streaming text, tool call progress, state sync, interrupts (HITL),
  frontend tool calls, shared state
- Born from CopilotKit, integrates with LangGraph, CrewAI, Pydantic AI
- AWS Bedrock AgentCore added AG-UI support (March 2026)
- Microsoft Agent Framework has AG-UI integration
- Key for Seshat: provides the bi-directional runtime connection between
  the agent backend and any frontend

### A2UI (Agent-to-UI specification)
- Declarative UI specification (from Google)
- JSON-based component descriptions that agents generate
- Complementary to AG-UI: A2UI describes WHAT to render, AG-UI transports it
- Extensible component model with trusted catalog pattern

### Recommendation for Seshat
- **AG-UI as the transport layer** between Seshat backend and UI frontend
  - Provides streaming, HITL interrupts, shared state, tool progress
  - Framework-agnostic (works with any backend, any frontend)
  - Event types map well to Seshat's needs (traces, tool calls, approvals)
- **MCP Apps for rich visualization** when the agent returns complex results
  - Vega/Plotly/Mermaid rendered as MCP App resources
  - Interoperable if Seshat ever needs to work within Claude/ChatGPT/VS Code
- **A2UI as optional future extension** for generative UI components
- All three are complementary, not competing

---

## 6. Decisions Needed (ADRs to Author)

### ADR-001: Three-Layer Separation
- Knowledge / Execution / Observation as distinct, decoupled layers
- Knowledge is shared across all clients and execution profiles
- Observation traces are profile-scoped but queryable across profiles
- Collaboration log (questions, knowledge, decisions) is shared

### ADR-002: Provider Abstraction & Dual-Harness Design
- Single codebase, profile-based configuration (do NOT clone repo)
- Provider interface: local (llama.cpp, mlx-lm, mistral.rs) and cloud
  (Anthropic, Google, OpenAI, Mistral) implement the same contract
- Local models can delegate to cloud models (hybrid execution)
- Simultaneous local + cloud agent operation
- Remote agent harness integration (Claude Code, Codex, Cursor)
- Support both parallel and sequential eval runs

### ADR-003: Infrastructure — Local, Cloud, or Hybrid
Options:
1. Tunnel-based (laptop must be running)
2. Cloud knowledge layer + flexible execution (recommended starting point)
3. Full cloud hosting

Security requirements:
- TLS everywhere, encryption at rest
- API authentication (token-based, rotatable)
- Cloud LLM data policies: verify no-training-on-input per provider
- Knowledge/memory sovereignty: user owns all data

### ADR-004: Agent-to-UI Protocol Stack
- AG-UI as transport layer (SSE event stream between backend and frontend)
- MCP Apps for rich interactive visualizations
- Standard visualization library: Vega-Lite/Plotly for charts, Mermaid for
  diagrams, custom components via MCP Apps pattern
- A2UI as future extension path
- Modularity: UI protocol layer must be swappable/extensible

### ADR-005: Context Management & Observability
- Three-tier context model: near (window), episodic (session), long-term (KB)
- Context size monitoring with visibility in UI
- Compaction logging: what, why, feedback on quality
- Knowledge freshness tracking
- Knowledge weighting (confidence, source, corroboration)
- Self-monitoring loop: agent observes own patterns
- User can discuss observations with agent through UI

### ADR-006: Mobile & Multi-Device UI
- PWA as starting point (lowest friction, cross-device)
- Chat-first interface with embedded rich visualizations
- Model/profile selection per conversation
- Knowledge graph exploration view
- Context and cost dashboards
- HITL approval flows (integrated with Linear)

### ADR-007: Application Modularity
- Core architectural principle: every major component is a replaceable module
- Module boundaries aligned with the three-layer architecture
- Provider modules (LLM backends), storage modules (DB, graph, search),
  protocol modules (AG-UI, MCP), UI modules (visualization renderers)
- Clear interfaces between modules, no tight coupling
- Enables: swapping inference backends, changing DBs, upgrading protocols,
  and eventually allowing others to self-host with their own module choices

### ADR-008: Remote Agent Harness Integration
- Seshat must integrate with external agent environments:
  Claude Code, Codex, Cursor, and future tools
- These are both tools Seshat can delegate to AND environments that can
  interact with Seshat's knowledge layer
- Bidirectional: Seshat delegates coding tasks to Claude Code;
  Claude Code sessions can query Seshat's knowledge
- MCP server pattern is the natural integration point
- Security: scoped access, audit logging

---

## 7. Existing Architecture Context

- Multi-model agent system with primary agent and subagent roles
- Model-agnostic explicit JSON prompting (not native tool-call tokens)
- Per-model config objects with unified parsing layer
- Hermes-format fine-tunes preferred where available
- Infrastructure: DB, event bus, Elastic, graph DB (all currently local)
- Caddy as local reverse proxy
- GitLab-based CI/CD
- Linear integration via MCP
- Existing Linear projects and issues to reference

## 8. Models Under Evaluation

**Local (GGUF on llama.cpp):**
- Qwen3.6-35B-A3B (current best for orchestration)
- Qwen3-Coder-30B-A3B (code tasks)
- GLM-4.7-Flash (alternative)
- Gemma 4 31B dense, 26B-A4B MoE (new candidates)

**Cloud (to be evaluated with SOTA harness):**
- Claude Sonnet / Haiku (Anthropic)
- Gemini 2.5 Flash / Pro (Google)
- GPT-4o / 4o-mini (OpenAI)
- Mistral Medium / Small (Mistral)

---

## 9. Instructions for Claude Code Session

1. Read this brief and the existing codebase structure
2. Check existing Linear projects/issues for alignment
3. Author formal ADRs (001-008) in `docs/adr/` directory
4. Follow standard ADR format: Title, Status (Proposed), Date, Context,
   Decision, Consequences, Alternatives Considered
5. Each ADR should stand alone but cross-reference related ADRs
6. Flag any conflicts with existing architecture or Linear issues
7. ADRs should be opinionated — make recommendations, not just list options
8. Where the brief says "recommended," encode that as the decision