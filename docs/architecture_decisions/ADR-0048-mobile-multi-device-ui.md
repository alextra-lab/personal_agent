# ADR-0048: Mobile & Multi-Device UI

**Status**: Accepted
**Date**: 2026-04-13
**Deciders**: Project owner
**Depends on**: ADR-0043 (Three-Layer Separation), ADR-0045 (Infrastructure — Cloud Knowledge Layer), ADR-0046 (Agent-to-UI Protocol Stack)
**Related**: ADR-0040 (Linear Async Feedback Channel), ADR-0044 (Provider Abstraction & Dual-Harness), ADR-0047 (Context Management & Observability)
**Linear**: FRE-21 (Daily-use iPhone interface), 3.0 Daily-Use Interface project

---

## Context

### The UI is the missing piece

The Personal Agent has a working CLI (`agent "question"`), a REST API (`/chat`), and Swagger UI documentation. None of these are suitable for daily use from a phone, iPad, or even a laptop browser. The CLI requires a terminal. The REST API requires curl or a script. Swagger is for development, not conversation.

This matters because:

1. **Seshat needs real usage data to improve.** The evaluation phase (EVAL-01 through EVAL-08) proved that synthetic test conversations reveal different problems than real daily use. Building a UI is not a polish step — it's a prerequisite for generating the usage data that drives Slice 3 (self-improvement).

2. **The agent should be reachable from any device.** A phone waiting room check-in, an iPad research session, a laptop deep-dive — these are different interaction modes but the same agent, same knowledge, same conversation history. Without a mobile UI, the agent is laptop-only.

3. **HITL (Human-in-the-Loop) needs a surface.** The Linear feedback channel (ADR-0040) works for async approval but is clunky for in-conversation decisions. "Should I run this web search?" needs a UI button, not a Linear label change.

### Why PWA

| Option | Cross-device | Offline | Install | Development cost | Native feel |
|--------|-------------|---------|---------|-----------------|-------------|
| **PWA** | ✅ All browsers | ✅ Service worker | ✅ Add to home screen | Low (one codebase) | Good (responsive) |
| **React Native** | ✅ iOS + Android | ✅ Native | ✅ App Store | High (two platforms) | Excellent |
| **Native iOS + web** | ⚠️ iOS + web only | ✅ Native | ✅ App Store | Very high (two codebases) | Excellent (iOS) |
| **Electron + responsive web** | ✅ Desktop + web | ⚠️ Limited | ✅ Binary | Medium | OK |

For a single-developer, self-hosted personal agent, PWA is the clear choice:

- One codebase, all devices.
- No App Store review process.
- Install to home screen on iPhone and iPad for native-feeling access.
- Service worker enables offline reading of conversation history (not offline inference, obviously).
- Modern PWA capabilities (push notifications, background sync, media access) cover Seshat's needs.
- If native feel becomes insufficient later, a React Native shell wrapping the same web views is an incremental step, not a rewrite.

---

## Decision

### D1: PWA as the primary UI

Build a **Progressive Web App** as the primary interface for all non-CLI interaction with Seshat. The PWA targets:

- **iPhone** (primary mobile device): Safari/WebKit, Add to Home Screen.
- **iPad**: Larger canvas for knowledge graph exploration and side-by-side views.
- **Laptop browser**: Full desktop experience alongside the CLI.

**Technology stack**:

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Framework | **Next.js** (App Router) | React-based, SSR for initial load, excellent PWA support, built-in API routes for BFF pattern |
| Styling | **Tailwind CSS** | Utility-first, responsive design, dark mode support, fast iteration |
| State management | **AG-UI React SDK** (`@ag-ui/react`) | Handles streaming state from AG-UI events natively |
| Real-time transport | **AG-UI SSE client** | Consumes AG-UI event stream from Seshat backend (ADR-0046) |
| PWA manifest | **next-pwa** or manual config | Service worker, install prompt, offline caching |
| Charts/viz | **Vega-Lite** (lightweight) or **Plotly** (interactive) | Rendered via MCP Apps pattern (ADR-0046) |
| Graph viz | **D3.js** or **Cytoscape.js** | Knowledge graph exploration view |

### D2: Chat-first interface with embedded visualizations

The primary interaction model is **conversation** — the same paradigm as ChatGPT, Claude, or iMessage. Every other view is secondary to or embedded within the chat flow.

**Core chat interface**:
- Message list with streaming text display (AG-UI `TEXT_DELTA` events).
- Tool call indicators showing what the agent is doing ("Searching knowledge graph...", "Querying web...") via AG-UI `TOOL_CALL_START`/`END` events.
- Inline rich content: charts, diagrams, knowledge graph snippets rendered as MCP App embeds within the message flow.
- Message composition with optional attachments (text, images for multi-modal future).

**Embedded visualizations** (within chat):
- **Context window meter**: Horizontal bar showing near/episodic/long-term context usage (ADR-0047). Always visible during active conversation.
- **Agent trace timeline**: Expandable per-message view showing the execution steps (gateway stages, tool calls, sub-agent invocations, memory queries). Collapsed by default.
- **Inline charts**: Cost summaries, performance trends — rendered as Vega-Lite charts via MCP Apps, appearing as message attachments.
- **Knowledge graph snippets**: When the agent references entities/relationships, show a mini-graph view with the relevant subgraph. Tappable to expand to full exploration view.

### D3: Model/profile selection per conversation

Each conversation starts with a profile selection:

```
┌─────────────────────────────────────────────┐
│  New Conversation                            │
│                                              │
│  ┌──────────────┐  ┌──────────────────────┐ │
│  │ 🖥  Local     │  │  ☁️  Cloud            │ │
│  │ Qwen3.5-35B  │  │  Claude Sonnet       │ │
│  │ Free, slower  │  │  $, faster + smarter │ │
│  └──────────────┘  └──────────────────────┘ │
│                                              │
│  Profile details: model, cost estimate,      │
│  capabilities summary                        │
└─────────────────────────────────────────────┘
```

Profile selection is per-conversation, not per-message (ADR-0044). The selected profile is shown in the conversation header. Cross-profile delegation (local → cloud escalation) happens transparently, with a subtle indicator when it occurs.

### D4: Knowledge graph exploration view

A dedicated view (not inline chat) for exploring the knowledge graph:

- **Graph visualization**: Interactive force-directed graph (Cytoscape.js or D3) showing entities and relationships.
- **Search and filter**: Find entities by name, type, freshness, confidence.
- **Entity detail panel**: Tap an entity to see its properties, relationships, access history (ADR-0042), and confidence score (ADR-0047).
- **Freshness indicators**: Color-coded by access recency — recently used entities are bright, stale entities are dimmed.
- **Navigation**: Tap a relationship to traverse the graph. Breadcrumb trail for backtracking.

This view reads from the Knowledge Layer API (ADR-0045) and is available from all devices. On phone, it uses a simplified list+detail layout instead of a full graph canvas.

### D5: Context and cost dashboards

Accessible from a sidebar or tab:

- **Context dashboard**: Current session's context budget, tier allocation, compaction history (ADR-0047). Historical context utilization across sessions.
- **Cost dashboard**: Per-profile spending (cloud profiles only). Daily/weekly/monthly trends. Cost per conversation. Budget warnings.
- **Performance dashboard**: Response latency, tool call success rates, delegation outcomes. Profile comparison views.

Rendered as Vega-Lite charts via MCP Apps (ADR-0046). The agent can generate these on request ("show me my costs this week") or they can be accessed from a dedicated dashboard view.

### D6: HITL approval flows

Two complementary approval mechanisms:

**In-conversation approvals** (real-time):
- The agent sends an AG-UI `INTERRUPT` event when it needs approval.
- The UI renders an inline approval card: "I'd like to search the web for X. Allow?"
- The user taps Approve or Deny.
- The agent receives the decision via AG-UI `RESUME` event and continues.

**Linear-integrated approvals** (async):
- For actions that don't need immediate response (e.g., "I want to create a Linear issue for this"), the agent creates a Linear issue with state "Needs Approval" (per CLAUDE.md policy).
- The user can approve via the Seshat UI (which calls Linear's API) or directly in Linear from any device.
- Status updates flow back via the feedback channel (ADR-0040).

The UI shows pending approvals in a notification badge. Tapping shows a list of pending items with context and approve/deny buttons.

---

## Consequences

### Positive

- **Daily use becomes possible**: A phone-accessible chat interface means the agent can be used throughout the day — commute, meetings, errands — not just at the laptop.
- **Real usage data at last**: Actual daily conversations generate the data Seshat needs for evaluation and self-improvement. This is the primary motivator.
- **HITL is natural**: In-conversation approval cards feel like a natural extension of the chat interface, not a workflow tool.
- **Knowledge graph is explorable**: The graph visualization makes the agent's knowledge tangible. Users can see what the agent knows, verify it, and correct it.
- **Cross-device continuity**: Start a conversation on the phone, continue on the laptop. Same knowledge, same history, same agent.

### Negative

- **Significant frontend development effort**: A PWA with streaming chat, embedded visualizations, knowledge graph exploration, and dashboards is a substantial project. This is not a weekend build.
- **iPhone PWA limitations**: iOS PWA support is improving but still has gaps — no push notifications in all contexts, limited background execution, potential WebKit-specific bugs. These are Apple platform constraints, not architectural ones.
- **Two "apps" to maintain**: The CLI and the PWA are separate codebases in separate languages (Python CLI vs. TypeScript PWA). They share the same backend API and AG-UI protocol, but UI bugs must be fixed in two places if they share logic.
- **Responsive design complexity**: The same UI must work on a 6.1" phone screen and a 13" laptop. The knowledge graph exploration view, in particular, needs very different layouts for small vs. large screens.

### Neutral

- **CLI remains the power-user interface**: The PWA doesn't replace the CLI. Power users, scripts, and development workflows continue to use `agent "question"`. The PWA is for daily use; the CLI is for dev use.
- **No native app needed initially**: If iPhone PWA limitations become blocking, a thin React Native wrapper around the same web views is an incremental step. The architecture doesn't preclude this.
- **Frontend framework is swappable**: Next.js is a strong default, but the PWA consumes AG-UI events via a standard protocol. Swapping to SvelteKit or Nuxt later affects only the frontend, not the backend or protocol layer.
