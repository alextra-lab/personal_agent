# Seshat v2 Architecture — Implementation Plan

> **Linear**: [FRE-192](https://linear.app/frenchforest/issue/FRE-192)
> **ADRs**: 0043–0050
> **Date**: 2026-04-14
> **Status**: Planning (Phase A issues created — awaiting approval)

---

## Overview

Eight ADRs define the Seshat v2 architectural evolution. All are **Accepted**. This plan translates them into an ordered sequence of implementable issues, each scoped to 1–5 days of work.

The fundamental shift: Seshat moves from a laptop-only CLI tool to a platform with three explicit layers (Knowledge / Execution / Observation), cloud infrastructure, a streaming UI protocol, a mobile PWA, and bidirectional integration with external agent harnesses.

---

## Dependency Map

```
ADR-0049 (Modularity — Protocols)
    ├──► ADR-0043 (Three-Layer Separation)
    │       ├──► ADR-0044 (Provider Abstraction) ← also needs FRE-145 done
    │       ├──► ADR-0046 (AG-UI Transport)
    │       │       └──► ADR-0047 (Context Observability)
    │       └──► ADR-0045 (Cloud Infrastructure)
    │               ├──► ADR-0048 (Mobile PWA) ← also needs ADR-0046
    │               └──► ADR-0050 (Remote Agent Integration) ← also needs ADR-0043
    └──► ADR-0047 (partial — data models only, no infra)
         ADR-0050 D1 (SKILL.md docs — no deps, start now)
```

**Can start immediately (no infrastructure required)**:
- Phase A1: Protocol definitions (ADR-0049 Phase 1)
- Phase A2: Context observability data models (ADR-0047 D3–D5)
- Phase A3: SKILL.md integration docs (ADR-0050 D1)

**Needs AG-UI first**:
- Phase B1 → C1, C2, D1, E1, F1

---

## Phase A — Foundations

*No infrastructure required. Pure Python + documentation.*

### A1: Protocol Definitions (ADR-0049 Phase 1)

**Linear**: Child of FRE-192 | **Tier**: Tier-2:Sonnet | **Priority**: High

Define Python `Protocol` classes for all major module boundaries. This is additive — no existing code moves. Protocols are placed in new `protocols.py` files alongside existing modules.

**Files to create**:

```
src/personal_agent/
  memory/protocols.py         # KnowledgeGraphProtocol, SessionStoreProtocol, SearchIndexProtocol
  tools/protocols.py          # ToolExecutorProtocol
  request_gateway/protocols.py  # ContextAssemblerProtocol
  telemetry/protocols.py      # TraceSinkProtocol, MetricsCollectorProtocol
  transport/                  # NEW module
    __init__.py
    protocols.py              # UITransportProtocol, VisualizationProtocol
```

**Key Protocol definitions**:

```python
# memory/protocols.py
class KnowledgeGraphProtocol(Protocol):
    async def search(self, query: str, limit: int, ctx: TraceContext) -> Sequence[EntityResult]: ...
    async def get_entity(self, entity_id: str) -> Entity | None: ...
    async def store_fact(self, fact: Fact, ctx: TraceContext) -> str: ...
    async def get_relationships(self, entity_id: str) -> Sequence[Relationship]: ...

class SessionStoreProtocol(Protocol):
    async def get_session(self, session_id: str) -> Session | None: ...
    async def save_message(self, session_id: str, message: Message) -> None: ...
    async def get_messages(self, session_id: str, limit: int) -> Sequence[Message]: ...

class SearchIndexProtocol(Protocol):
    async def index(self, doc_id: str, document: Mapping[str, Any]) -> None: ...
    async def search(self, query: str, index: str, limit: int) -> Sequence[SearchResult]: ...

# tools/protocols.py
class ToolExecutorProtocol(Protocol):
    async def execute(self, tool_name: str, args: Mapping[str, Any], ctx: TraceContext) -> ToolResult: ...
    def list_available(self) -> Sequence[ToolDefinition]: ...

# telemetry/protocols.py
class TraceSinkProtocol(Protocol):
    def emit(self, event: TraceEvent) -> None: ...
    async def query(self, query: TraceQuery) -> Sequence[TraceEvent]: ...

# transport/protocols.py
class UITransportProtocol(Protocol):
    async def send_text_delta(self, text: str, session_id: str) -> None: ...
    async def send_tool_event(self, event: ToolEvent, session_id: str) -> None: ...
    async def send_state(self, state: Mapping[str, Any], session_id: str) -> None: ...
    async def send_interrupt(self, context: InterruptContext, session_id: str) -> InterruptResponse: ...
```

**Extend `config/bootstrap.py`** with a typed composition root:

```python
def bootstrap(profile: str = "local") -> Orchestrator:
    """Wire concrete implementations from protocol definitions."""
    knowledge: KnowledgeGraphProtocol = Neo4jMemoryService(...)
    sessions: SessionStoreProtocol = PostgresSessionRepository(...)
    search: SearchIndexProtocol = ElasticsearchClient(...)
    trace: TraceSinkProtocol = ElasticsearchTraceSink(...)
    tools: ToolExecutorProtocol = NativeToolExecutor(...)
    return Orchestrator(knowledge=knowledge, tools=tools, trace=trace)
```

**Tests**: Runtime `isinstance` checks are not used with Protocols — verify by running mypy against the existing implementations (they should implicitly satisfy the protocols without modification).

---

### A2: Context Observability (ADR-0047 D3–D5)

**Linear**: Child of FRE-192 | **Tier**: Tier-2:Sonnet | **Priority**: High

Three additive changes to the existing context management pipeline:

#### D3: Compaction logging

New file: `src/personal_agent/telemetry/compaction.py`

```python
@dataclass(frozen=True)
class CompactionRecord:
    trace_id: str
    session_id: str
    timestamp: datetime
    trigger: str              # "budget_exceeded" | "tier_rebalance" | "manual"
    tier_affected: str        # "near" | "episodic" | "long_term"
    tokens_before: int
    tokens_after: int
    tokens_removed: int
    strategy: str             # "summarize" | "truncate" | "drop_oldest"
    content_summary: str
    entities_preserved: list[str]
    entities_dropped: list[str]
```

Wire into `request_gateway/budget.py` and the context compressor: log a `CompactionRecord` to Elasticsearch (via structlog) whenever compaction is triggered.

**Compaction quality feedback**: In `request_gateway/recall_controller.py`, after a recall hit, check if the result matches any `entities_dropped` from recent `CompactionRecord`s. If so, emit a `compaction_quality: poor` event.

#### D4: Knowledge freshness surfacing

In `request_gateway/context.py` (recall relevance scoring), add a freshness modifier:
- Entities accessed in the last 7 days: +10% relevance score
- Entities not accessed in 90+ days: -15% relevance score
- Read-only — no writes on the hot path (freshness updates remain event-driven via ADR-0042)

#### D5: Knowledge confidence metadata

New Pydantic model in `memory/models.py` (or new `memory/weight.py`):

```python
class KnowledgeWeight(BaseModel):
    model_config = ConfigDict(frozen=True)
    confidence: float = 0.5           # 0.0–1.0
    source_type: Literal[
        "conversation", "tool_result",
        "web_search", "manual", "inferred"
    ] = "inferred"
    corroboration_count: int = 0
    last_confirmed: datetime | None = None
```

Add `weight: KnowledgeWeight` to the `Entity` model. Neo4j schema: store confidence, source_type, corroboration_count, last_confirmed on entity nodes. Backfill existing entities with default weight (confidence=0.5, source_type="inferred").

Modify relevance scoring in the recall controller to apply a confidence modifier: low-confidence facts (< 0.4) get a soft penalty of -10%.

**Tests**:
- Unit: `tests/personal_agent/telemetry/test_compaction.py` — CompactionRecord creation and ES logging
- Unit: `tests/personal_agent/memory/test_weight.py` — KnowledgeWeight model validation
- Integration: verify freshness modifier changes recall ordering for stale vs. recent entities

---

### A3: SKILL.md Integration Docs (ADR-0050 D1)

**Linear**: Child of FRE-192 | **Tier**: Tier-3:Haiku | **Priority**: Normal

Write four SKILL.md files in `docs/skills/` that teach external agents (Claude Code, Codex, Cursor) how to interact with Seshat's Knowledge Layer.

Each file follows `docs/skills/SKILL_TEMPLATE.md` and documents both:
- **Immediate**: existing CLI commands (`uv run agent memory search`, etc.)
- **Gateway-ready**: curl commands against the future Seshat API Gateway (described as "available after Phase C deployment")

**Files to create**:

| File | Content |
|------|---------|
| `docs/skills/seshat-knowledge.md` | Search entities, get entity details, store facts |
| `docs/skills/seshat-sessions.md` | Read conversation history and session context |
| `docs/skills/seshat-observations.md` | Query execution traces and performance data |
| `docs/skills/seshat-delegate.md` | Delegate tasks back to Seshat |

**Auth pattern** (consistent across all docs):
```bash
# Set in shell env or Claude Code settings.json:
export SESHAT_API_TOKEN="<token>"
export SESHAT_API_URL="https://seshat.example.com"  # or http://localhost:9000
```

---

## Phase B — Transport

*Requires: Phase A1 (transport protocol definition)*

### B1: AG-UI Transport Module (ADR-0046)

**Linear**: Child of FRE-192 | **Tier**: Tier-2:Sonnet | **Priority**: High

Implements the streaming transport layer. Zero impact on existing `/chat` endpoint — purely additive.

**Module structure**:

```
src/personal_agent/transport/
  __init__.py
  protocols.py          # UITransportProtocol (from Phase A1)
  events.py             # Internal event types (backend-defined, protocol-agnostic)
  agui/
    __init__.py
    adapter.py          # Converts internal events → AG-UI wire format
    endpoint.py         # FastAPI SSE endpoint (/stream)
  viz/
    __init__.py
    charts.py           # Vega-Lite spec generation
    diagrams.py         # Mermaid text generation
```

**Internal event types** (`transport/events.py`):

```python
@dataclass(frozen=True)
class TextDeltaEvent:
    text: str
    session_id: str

@dataclass(frozen=True)
class ToolStartEvent:
    tool_name: str
    args: Mapping[str, Any]
    session_id: str

@dataclass(frozen=True)
class ToolEndEvent:
    tool_name: str
    result_summary: str
    session_id: str

@dataclass(frozen=True)
class StateUpdateEvent:
    key: str
    value: Any
    session_id: str

@dataclass(frozen=True)
class InterruptEvent:
    context: str
    options: Sequence[str]
    session_id: str

InternalEvent = TextDeltaEvent | ToolStartEvent | ToolEndEvent | StateUpdateEvent | InterruptEvent
```

**SSE endpoint** (`transport/agui/endpoint.py`):

```python
@router.get("/stream/{session_id}")
async def stream_session(session_id: str, request: Request) -> EventSourceResponse:
    """AG-UI SSE endpoint. Clients connect and receive real-time agent events."""
    ...
```

**Integration points**:
- Wire `TextDeltaEvent` into the LLM client's streaming callback
- Wire `ToolStartEvent`/`ToolEndEvent` into the tool executor
- Wire `StateUpdateEvent` for context budget updates (ADR-0047 D2)
- The orchestrator receives a `UITransportProtocol` instance via DI (Phase A1 bootstrap)

**Tests**:
- Unit: event type creation and AG-UI wire format conversion
- Integration: SSE endpoint returns valid event stream for a mock request

---

## Phase C — Cloud Infrastructure

*Requires: ADR-0043 logical layer separation (conceptually satisfied), Phase B1 for gateway streaming*

### C1: Docker Compose Cloud Simulation (ADR-0045 Phase 0)

**Linear**: Child of FRE-192 | **Tier**: Tier-3:Haiku | **Priority**: Normal

Create `docker-compose.cloud.yml` that mirrors the target cloud deployment with local resource constraints (RAM limits, CPU quotas). Used to validate service topology before provisioning a real VPS.

**Acceptance criteria**:
1. All services start and pass health checks within 8 GB RAM
2. Seshat API Gateway serves `/health` over HTTPS (self-signed cert for local)
3. Neo4j, PostgreSQL, Elasticsearch, Redis all reachable from the gateway container
4. Basic data read/write round-trip works

---

### C2: Seshat API Gateway (ADR-0045 Phases 3–4)

**Linear**: Child of FRE-192 | **Tier**: Tier-1:Opus | **Priority**: High

The most significant infrastructure change: split the monolithic `service/app.py` into a VPS-side gateway and a local execution service.

**Split**:

| Endpoint group | Stays local (execution) | Moves to gateway (VPS) |
|---------------|------------------------|------------------------|
| `/chat` | ✅ | — |
| `/stream` | ✅ | — |
| `/knowledge/*` | — | ✅ |
| `/sessions/*` | — | ✅ |
| `/observations/*` | — | ✅ |
| `/health` | ✅ | ✅ (separate) |

**New gateway modules**:

```
src/personal_agent/gateway/      # The VPS-side Knowledge+Observation API
  __init__.py
  app.py                         # Separate FastAPI app (not service/app.py)
  knowledge_api.py               # /knowledge/* endpoints
  session_api.py                 # /sessions/* endpoints
  observation_api.py             # /observations/* endpoints
  auth.py                        # Token-based auth middleware
  rate_limiting.py               # Per-token rate limits
```

**Auth model**: Bearer token, validated against `config/mcp_server_access.yaml` scope definitions.

Requires: detailed design doc before implementation (Tier-1:Opus task).

---

## Phase D — Execution Profiles

*Requires: ADR-0033 (FRE-145) fully implemented (LiteLLM client wired), Phase C2 for cloud profile*

### D1: Execution Profile Config (ADR-0044 D1–D3)

**Linear**: Child of FRE-192 | **Tier**: Tier-2:Sonnet | **Priority**: High

Introduce profile-based execution configuration as first-class config objects.

**Files to create/modify**:

```
config/profiles/
  local.yaml       # Local inference (Qwen3.5-35B, no cost limit)
  cloud.yaml       # Cloud inference (Claude Sonnet, $2/session)

src/personal_agent/config/
  profile.py       # ExecutionProfile Pydantic model, profile loader
```

**Profile schema** (Pydantic):

```python
class DelegationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    allow_cloud_escalation: bool = False
    escalation_provider: str | None = None
    escalation_model: str | None = None

class ExecutionProfile(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    description: str
    primary_model: str
    sub_agent_model: str
    provider_type: Literal["local", "cloud"]
    cost_limit_per_session: float | None
    delegation: DelegationConfig
```

**CLI integration**: `agent --profile cloud "question"` passes profile to the execution path.

**TraceContext extension**: Add `profile: str` field so all telemetry is profile-tagged.

---

## Phase E — External Agent Integration

*Requires: Phase C2 (Seshat API Gateway)*

### E1: Seshat MCP Server + Delegation Adapters (ADR-0050 D2–D6)

**Linear**: Child of FRE-192 | **Tier**: Tier-2:Sonnet | **Priority**: Normal

Two sub-deliverables:

**E1a: Delegation adapter pattern**:

```
src/personal_agent/delegation/
  __init__.py
  protocols.py              # DelegationExecutorProtocol
  adapters/
    __init__.py
    claude_code.py          # CLI subprocess + MCP config injection
    codex.py                # REST API (stub — Codex API not yet GA)
    generic_mcp.py          # Any MCP-capable agent (fallback)
```

The `claude_code.py` adapter formalizes the existing delegation subprocess pattern from Slice 2 with:
- MCP server connection info injection (`--mcp-server` flag)
- Delegation depth limit (max 2)
- Outcome parsing

**E1b: Seshat MCP server**:

```
src/personal_agent/mcp/
  server/
    __init__.py
    server.py               # MCP server (Seshat as server, not just client)
    tools.py                # 6 tool definitions
    resources.py            # seshat:// URI scheme resources
    auth.py                 # Token scope validation
```

MCP server tools mirror the Seshat API Gateway endpoints. Access is token-scoped per `config/mcp_server_access.yaml`.

---

## Phase F — Mobile PWA

*Requires: Phase B1 (AG-UI), Phase C2 (API Gateway)*

### F1: PWA Scaffold (ADR-0048 Phase 1)

**Linear**: Child of FRE-192 | **Tier**: Tier-1:Opus | **Priority**: High

Separate repository or top-level directory: `seshat-pwa/`

**Technology stack**:
- Next.js 14+ (App Router)
- Tailwind CSS
- `@ag-ui/react` for streaming state
- `next-pwa` for service worker / install prompt

**Phase 1 scope** (chat interface only — no dashboards, no graph view):
- Profile selection (local vs. cloud conversation start)
- Chat message list with streaming text (AG-UI `TEXT_DELTA`)
- Tool call indicators (AG-UI `TOOL_CALL_START`/`TOOL_CALL_END`)
- HITL approval card (AG-UI `INTERRUPT`/`RESUME`)
- Context budget meter (from AG-UI `STATE_DELTA` context_window state)
- PWA manifest + service worker for Add to Home Screen

**Phase 2** (separate issue): Knowledge graph view, cost/performance dashboards.

---

## Issue Summary

| Issue | Title | ADR | Phase | Tier | Priority | Status |
|-------|-------|-----|-------|------|----------|--------|
| [FRE-201](https://linear.app/frenchforest/issue/FRE-201) | Protocol definitions | 0049 | A1 | Tier-2:Sonnet | High | Needs Approval |
| [FRE-202](https://linear.app/frenchforest/issue/FRE-202) | Context observability | 0047 | A2 | Tier-2:Sonnet | High | Needs Approval |
| [FRE-203](https://linear.app/frenchforest/issue/FRE-203) | SKILL.md integration docs | 0050 D1 | A3 | Tier-3:Haiku | Normal | Needs Approval |
| [FRE-204](https://linear.app/frenchforest/issue/FRE-204) | AG-UI transport module | 0046 | B1 | Tier-2:Sonnet | High | Needs Approval |
| [FRE-205](https://linear.app/frenchforest/issue/FRE-205) | Docker Compose cloud simulation | 0045 Ph.0 | C1 | Tier-3:Haiku | Normal | Needs Approval |
| [FRE-206](https://linear.app/frenchforest/issue/FRE-206) | Seshat API Gateway | 0045 Ph.3-4 | C2 | Tier-1:Opus | High | Needs Approval |
| [FRE-207](https://linear.app/frenchforest/issue/FRE-207) | Execution profile config | 0044 | D1 | Tier-2:Sonnet | High | Needs Approval |
| [FRE-208](https://linear.app/frenchforest/issue/FRE-208) | MCP server + delegation adapters | 0050 D2-D6 | E1 | Tier-2:Sonnet | Normal | Needs Approval |
| [FRE-209](https://linear.app/frenchforest/issue/FRE-209) | PWA scaffold | 0048 | F1 | Tier-1:Opus | High | Needs Approval |

---

## What's Not Planned Yet

These require dedicated planning sessions after earlier phases deliver data:

- **ADR-0043 Brainstem split**: ADR-0043 explicitly says "no immediate code reorganization mandate." The logical split is enforced by dependency rules. A physical split into `knowledge/`, `execution/`, `observation/` directories is deferred until modules are touched.
- **ADR-0044 D4–D5 (dual-harness simultaneous operation, profile-aware observation)**: Depends on D1 profile config and cloud gateway.
- **ADR-0045 VPS provisioning (Phases 1–2)**: Terraform + Vault setup. Planned after Docker Compose simulation validates the topology.
- **ADR-0047 D6 (self-monitoring loop)**: Weekly aggregations and trend data. Planned after D3–D5 are collecting data.
- **ADR-0048 Phase 2 (knowledge graph view, dashboards)**: Planned after Phase 1 (chat) delivers real usage.
- **ADR-0049 target directory reorganization**: Optional, deferred to when modules are refactored anyway.

---

## Verification Matrix

| Phase | Verification |
|-------|-------------|
| A1 Protocols | `uv run mypy src/` — existing implementations satisfy new protocols without modification |
| A2 Observability | `uv run pytest tests/personal_agent/telemetry/test_compaction.py` + compaction record appears in ES |
| A3 SKILL.md | Claude Code session reads doc and successfully queries `uv run agent memory search` |
| B1 AG-UI | `curl -N http://localhost:9000/stream/<session>` receives SSE events during a chat |
| C1 Docker Sim | All services pass health checks inside resource limits |
| C2 Gateway | `curl https://gateway/knowledge/search?q=test` returns results |
| D1 Profiles | `uv run agent --profile cloud "hello"` uses Claude Sonnet, telemetry shows `profile=cloud` |
| E1 MCP Server | External Claude Code session successfully calls `seshat_search_knowledge` |
| F1 PWA | iPhone Safari: Add to Home Screen, open, start a conversation, see streaming text |
