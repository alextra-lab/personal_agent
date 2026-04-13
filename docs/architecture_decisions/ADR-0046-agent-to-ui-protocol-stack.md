# ADR-0046: Agent-to-UI Protocol Stack

**Status**: Accepted
**Date**: 2026-04-13
**Deciders**: Project owner
**Related**: ADR-0028 (CLI-First Tool Migration — context overhead analysis), ADR-0043 (Three-Layer Separation)
**Enables**: ADR-0048 (Mobile & Multi-Device UI)
**Linear**: FRE-22 (Plugin/extension architecture)

---

## Context

### The agent has no real-time UI protocol

The current interface is a CLI (`agent "question"`) and a REST API (`POST /chat`). Both are request-response: the client sends a message, waits, gets a complete response. There is no streaming, no progress indication, no tool-call visibility, no HITL interrupts, and no shared state between the agent and a frontend.

This was fine when the agent was a CLI tool. It is not viable for a daily-use mobile UI (ADR-0048) that needs streaming text, tool-call progress, HITL interrupts, and state synchronization. But the terminal remains a first-class interface — the protocol stack must serve both graphical and terminal clients.

### Critical constraint: no context window regression

ADR-0028 migrated from MCP Gateway to CLI-first tools specifically to reclaim **3,000–6,500 tokens per request** of MCP schema overhead. That decision was correct and hard-won. Any UI protocol decision must be evaluated against this constraint:

**What costs context tokens** (LLM prompt injection):
- MCP tool schema discovery — 200–1,400 tokens per tool, injected into every request. This is what ADR-0028 eliminated.
- Tool definitions in the system prompt — still present for native tools, but lean (ADR-0028 Tier 1).
- SKILL.md / OpenClaw patterns — zero prompt tokens; the agent reads the skill doc from disk only when it needs the tool.

**What does NOT cost context tokens** (external to the LLM):
- AG-UI events — these flow from the backend to the frontend. The LLM never sees them. They are a rendering concern, not a reasoning concern.
- SSE/WebSocket transport — the LLM generates text; the transport layer streams it to clients. No token cost.
- Visualization payloads — charts, diagrams, and graph views are rendered by the frontend, not by the LLM. The LLM might generate a Vega-Lite spec (small JSON), but this is output, not input.

**The key distinction**: AG-UI is a **transport protocol** between the backend and the frontend. It does not inject anything into the LLM's context window. This is fundamentally different from MCP tool schemas, which are injected into every prompt.

### Protocol landscape as of April 2026

**AG-UI (Agent-User Interaction Protocol)** — Agent ↔ Frontend (runtime/transport)
- Open, event-based protocol with ~16 event types over SSE or WebSocket.
- Covers: streaming text, tool call lifecycle (start/progress/complete), state sync, HITL interrupts, frontend tool calls, shared state.
- Born from CopilotKit. Integrations with LangGraph, CrewAI, Pydantic AI, AG2 (AutoGen).
- AWS Bedrock AgentCore and Microsoft Agent Framework have AG-UI support as of March 2026.
- Key for Seshat: provides the bidirectional runtime connection between any backend and any frontend. Framework-agnostic.
- **Zero context overhead**: AG-UI events are transport, not prompt injection.

**MCP Apps** — Rich tool output rendering
- MCP extension (January 2026) where tools return interactive UI components.
- Important nuance for Seshat: MCP Apps as a **rendering format** (Vega-Lite JSON transported via AG-UI) is different from MCP as a **tool discovery protocol** (schema injection into prompts). ADR-0028 removed the latter. We can use the former's format conventions without running an MCP server for the frontend.
- Decision: use MCP Apps-compatible formats (Vega-Lite, Mermaid) for visualization payloads, but transport them via AG-UI events — **not** through a separate MCP server connection that would reintroduce schema overhead.

**A2UI (Agent-to-UI specification)** — Declarative UI specification
- From Google. JSON-based component descriptions that agents generate.
- Complementary to AG-UI: A2UI describes WHAT to render, AG-UI transports it.
- Less mature. Reserved for future evaluation.

### Compatibility with CLI-first architecture (ADR-0028)

The CLI-first tool migration established three tiers:

| Tier | Mechanism | Context cost | Status |
|------|-----------|-------------|--------|
| Tier 1: Native Python | In-process tools | Minimal (lean tool defs) | Active |
| Tier 2: CLI + SKILL.md | Agent reads skill doc, invokes CLI | Zero prompt tokens | Active |
| Tier 3: MCP | Full protocol server | 200–1,400 tokens/tool | Reserved for browser automation / bidirectional streaming |

AG-UI sits **outside this tier model entirely** — it is not a tool execution mechanism. It is a transport layer for getting the agent's output (text, tool progress, state) to the user's screen. Tools continue to execute via Tier 1/2/3 exactly as ADR-0028 defines. AG-UI just streams the results.

The SKILL.md / OpenClaw pattern and Claude Code Skills are complementary to AG-UI: skills define what the agent can do, AG-UI streams what the agent is doing. No conflict.

### Terminal compatibility

The terminal (tmux/screen sessions, SSH, Claude Code) is a primary interaction surface and must remain so. The protocol stack must support:

1. **Current CLI**: `agent "question"` → synchronous `/chat` endpoint → complete response. Unchanged.
2. **Streaming CLI**: A terminal client consuming AG-UI SSE events for real-time streaming output (like `curl -N` on an SSE endpoint). Incremental text appears as it's generated, tool calls show progress indicators. Think Claude Code's streaming output — the same experience, but for Seshat.
3. **Terminal multiplexer compatibility**: AG-UI SSE output must work inside tmux/screen sessions. SSE is just HTTP with `text/event-stream` content type — no special terminal requirements.
4. **Mobile as additive**: The PWA (ADR-0048) is an additional interface, not a replacement for the terminal. The same AG-UI endpoint serves both.

---

## Decision

### D1: AG-UI as the transport layer

Adopt **AG-UI** as the primary streaming protocol between the Seshat backend and all clients (PWA, streaming CLI, future native apps).

**Why AG-UI over raw SSE/WebSocket**:
- AG-UI defines a standard event vocabulary (~16 types). Building a custom event protocol would produce something functionally similar but incompatible with the growing AG-UI ecosystem.
- HITL interrupt support is built into the protocol (agent sends `INTERRUPT` event, frontend collects user input, sends `RESUME`). This maps directly to Linear approval flows (ADR-0040).
- Tool call lifecycle events (`TOOL_CALL_START`, `TOOL_CALL_PROGRESS`, `TOOL_CALL_END`) map directly to Seshat's existing tool execution model.
- State synchronization events enable the frontend to reflect agent state without polling.

**What AG-UI does NOT do**: It does not inject tool schemas into the LLM context. It does not replace ADR-0028's tool execution tiers. It does not require MCP servers. It is purely a delivery mechanism for output the LLM has already generated.

**Implementation approach**: AG-UI's Python server SDK provides the event emission interface. The Seshat execution layer emits AG-UI events at key points:

| Seshat event | AG-UI event type | Data |
|-------------|-----------------|------|
| Token generated | `TEXT_DELTA` | Incremental text |
| Tool call started | `TOOL_CALL_START` | Tool name, arguments |
| Tool result received | `TOOL_CALL_END` | Result summary |
| Sub-agent spawned | `STATE_DELTA` | Sub-agent status |
| Approval needed | `INTERRUPT` | Approval context |
| Context budget updated | `STATE_DELTA` | Budget usage |
| Response complete | `RUN_FINISHED` | Final state |

**Transport**: SSE for all implementations (simpler, HTTP-based, works through proxies/CDNs, works in tmux). WebSocket upgrade path for bidirectional real-time features if needed later.

### D2: Visualization via AG-UI payloads, not MCP servers

Rich visualizations (charts, diagrams, knowledge graphs) are transported as structured payloads within AG-UI events — **not** through a separate MCP server connection:

```
Agent generates Vega-Lite JSON → AG-UI STATE_DELTA event → Frontend renders chart
Agent generates Mermaid text   → AG-UI STATE_DELTA event → Frontend renders diagram
Agent references entity IDs    → AG-UI STATE_DELTA event → Frontend renders graph snippet
```

The visualization payloads use MCP Apps-compatible format conventions (so they _could_ be rendered by MCP-capable UIs if Seshat exposes an MCP server via ADR-0050), but they are delivered via AG-UI, not via an MCP protocol session. This avoids:

- MCP tool schema injection into the LLM context (the ADR-0028 problem)
- A separate MCP connection from the frontend to the backend
- MCP server lifecycle management in the frontend

For the terminal: rich visualizations degrade gracefully. A terminal client receiving a Vega-Lite payload either ignores it (text-only mode) or renders a text summary/ASCII representation. Charts and graphs are a PWA feature; the terminal gets the data in text form.

### D3: Three client tiers

| Client | Transport | Capabilities |
|--------|-----------|-------------|
| **Synchronous CLI** | `POST /chat` (existing) | Full response, no streaming. Current behavior. Unchanged. |
| **Streaming CLI** | AG-UI SSE | Streaming text, tool progress indicators, text-mode state. Works in tmux. |
| **PWA** | AG-UI SSE + visualization rendering | Streaming text, tool progress, rich charts/diagrams/graphs, HITL approval cards. |

All three clients connect to the same backend. The streaming endpoint is additive — the synchronous `/chat` endpoint remains for backward compatibility, scripts, and simple use cases.

### D4: Protocol modularity

The protocol layer is a module boundary (ADR-0049). The backend emits internal events; a transport adapter converts them to AG-UI events. If AG-UI is superseded, the adapter is replaced. The execution layer and the internal event model are unaffected.

```
src/personal_agent/
  transport/
    events.py       # Internal event types (backend-defined, protocol-agnostic)
    agui/
      adapter.py    # Converts internal events → AG-UI events
      endpoint.py   # SSE endpoint (serves both terminal and PWA clients)
    viz/
      charts.py     # Vega-Lite spec generation
      diagrams.py   # Mermaid generation
      graph.py      # Knowledge graph snippet generation
```

No `mcp_apps/` module — visualization payloads are generated by `viz/` and transported by `agui/`. MCP Apps format compatibility is a design convention, not a runtime dependency.

### D5: A2UI as future extension

**Do not implement A2UI now.** Reserve for evaluation when generative UI (agent-created forms, custom widgets) becomes a concrete need. A2UI components would be transported as AG-UI `STATE_DELTA` payloads — the architecture accommodates this without redesign.

---

## Consequences

### Positive

- **Zero context overhead**: AG-UI adds no tokens to the LLM's context window. The ADR-0028 gains (3,000–6,500 tokens reclaimed) are fully preserved.
- **CLI-first compatibility**: Tools continue to execute via ADR-0028's tier model (native Python, CLI + SKILL.md/OpenClaw). AG-UI streams the results, not the tool definitions.
- **Terminal works**: The streaming CLI works in tmux/screen sessions. SSE is plain HTTP — no special terminal requirements.
- **Real-time experience**: PWA users see streaming text and tool progress. Terminal users can opt into the streaming CLI for the same experience.
- **HITL is protocol-native**: Approval flows don't require polling. The agent sends an interrupt, the client (PWA or terminal) collects input, the agent resumes.
- **Incremental adoption**: AG-UI events can be added to existing execution paths one at a time. The synchronous endpoint continues to work throughout.

### Negative

- **Protocol dependency risk**: AG-UI is relatively new (born from CopilotKit, growing adoption via AWS/Microsoft but not yet universally established). If it stalls, we'd need to fork or replace the transport adapter. Risk is bounded by the modularity decision (D4) — the adapter is small.
- **Visualization graceful degradation**: Terminal clients must handle the case where the agent produces a visualization payload. The fallback (text summary or ignore) must be implemented and tested. This is UX complexity, not architectural complexity.
- **Two endpoints to maintain**: The synchronous `/chat` endpoint and the AG-UI SSE endpoint serve different clients but must produce equivalent results. Divergence is a testing concern.

### Neutral

- **No MCP regression**: This ADR does not re-introduce MCP tool schema injection. The visualization format is MCP Apps-compatible for portability, but the transport is AG-UI. No MCP server runs between the frontend and backend.
- **OpenClaw / SKILL.md / Claude Code Skills are orthogonal**: These define tool capabilities (what the agent can do). AG-UI defines output transport (how the agent shows what it's doing). They operate at different layers and compose naturally.
- **Existing telemetry unchanged**: AG-UI events are a UI concern. Internal telemetry (structlog → Elasticsearch) continues independently.
