# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-04-22 (ToolLoopGate: per-tool FSM loop detection implemented — ADR-0062; FRE-233: ADR-0053 gate monitoring + full feedback stream architecture; Hermes research integrated — FRE-251, FRE-252, FRE-226 updated)

---

## Current Focus

| # | Work Item | Linear | Spec / ADR | Status |
|---|-----------|--------|------------|--------|
| 1 | EVAL-10 run (Context Intelligence final verification) | [FRE-187](https://linear.app/frenchforest/issue/FRE-187/eval-10-context-intelligence-final-verification-run) | `specs/CONTEXT_INTELLIGENCE_SPEC.md` | Needs Approval |

## Upcoming — Needs Approval

Ordered by recommended implementation sequence. Dependency chains are encoded in Linear (`blockedBy` relations).

| # | Project | Linear | ADR / Spec | Depends On |
|---|---------|--------|------------|------------|
| 2 | Feedback Stream Bus Convention | [FRE-245](https://linear.app/frenchforest/issue/FRE-245) | ADR-0054 | ADR-0041 (Event Bus) — **must precede all Phase 2 ADRs** |
| 3 | Gate Feedback Monitoring — acceptance | [FRE-233](https://linear.app/frenchforest/issue/FRE-233) | ADR-0053 | — |
| 4 | System Health & Homeostasis — Mode Manager fix | [FRE-246](https://linear.app/frenchforest/issue/FRE-246) | ADR-0055 | FRE-245 |
| 5 | Error Pattern Monitoring — Level 3 observability | [FRE-244](https://linear.app/frenchforest/issue/FRE-244) | ADR-0056 | FRE-245 |
| 6 | Insights & Pattern Analysis — wire InsightsEngine | [FRE-247](https://linear.app/frenchforest/issue/FRE-247) | ADR-0057 | FRE-245 |
| 7 | Self-Improvement Pipeline — formalize Streams 1-3 | [FRE-248](https://linear.app/frenchforest/issue/FRE-248) | ADR-0058 | FRE-245 |
| 8 | Context Quality — compaction full loop | [FRE-249](https://linear.app/frenchforest/issue/FRE-249) | ADR-0059 | FRE-245, FRE-244 |
| 9 | Knowledge Graph Quality — consolidation + decay→reranking | [FRE-250](https://linear.app/frenchforest/issue/FRE-250) | ADR-0060 | FRE-245, FRE-247 |
| 10 | Within-Session Progressive Context Compression | [FRE-251](https://linear.app/frenchforest/issue/FRE-251) | ADR-0061 | FRE-249 |
| 11 | Agent self-updating skills (agentskills.io format) | [FRE-226](https://linear.app/frenchforest/issue/FRE-226) | ADR pending | FRE-248 |
| 12 | Linear Feedback Channel — Phase 3 meta-learning | [Project](https://linear.app/frenchforest/project/linear-async-feedback-channel-4517a7698be1) | ADR-0040 | Phases 1–2 done; FRE-183 needs feedback data |
| 13 | Context Intelligence — Stretch Goals | [Project](https://linear.app/frenchforest/project/context-intelligence-stretch-goals-315c8caa9cc9) | `specs/CONTEXT_INTELLIGENCE_SPEC.md` §4.7/4.S1/4.S2 | Proactive Memory MVP done (FRE-176) |

### Dependency graph (project-level)

```text
FEEDBACK STREAM ARCHITECTURE (FRE-233 — ADR-0053 accepted)
    ↓
ADR-0054: Bus Convention (FRE-245) ← FOUNDATION — must come first
    ├── ADR-0055: System Health & Homeostasis (FRE-246)  ← fixes app.py:176 hardcoded Mode.NORMAL
    ├── ADR-0056: Error Pattern Monitoring (FRE-244)     ← Level 3 observability
    ├── ADR-0057: Insights & Pattern Analysis (FRE-247)  ← wires InsightsEngine
    └── ADR-0058: Self-Improvement Pipeline (FRE-248)    ← adds captain_log.entry_created event
            ↓ (Phase 3 — depend on Phase 2)
        ADR-0059: Context Quality (FRE-249)              ← depends on 0054 + 0056
        ADR-0060: Knowledge Graph Quality (FRE-250)      ← depends on 0054 + 0057
            ↓ (Phase 4)
        ADR-0061: Within-Session Compression (FRE-251)   ← depends on ADR-0059
        FRE-226:  Agent skill files (agentskills.io)     ← depends on ADR-0058 (FRE-248)
        FRE-252:  Per-TaskType tool allowlist             ← independent (no blockers)

Architecture reference: docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md

Proactive Memory (ADR-0039)
    ↓
Context Intelligence Stretch Goals (4.7, 4.S1, 4.S2)

CLI Migration (ADR-0028)
    ↓
3.0 Daily-Use Interface (FRE-22 plugin arch)

Linear Feedback Channel Phase 3 (ADR-0040)  ← needs real feedback data (Phase 4 eval)
```

## Backlog

| Work Item | Linear | Spec / ADR |
|-----------|--------|------------|
| Per-TaskType tool allowlist in governance (Stage 3) | [FRE-252](https://linear.app/frenchforest/issue/FRE-252) | Enhancement to ADR-0028; no blocker |
| Captain's Log ES Backfill | — | `specs/CAPTAINS_LOG_ES_BACKFILL_SPEC.md` |

## Completed

| Phase | Completed | Summary |
|-------|-----------|---------|
| ToolLoopGate — per-tool FSM loop detection (ADR-0062) | 2026-04-22 | Replaced global `tool_call_signatures` dedup with `ToolLoopGate`: per-request registry of per-tool FSMs. Three detection signals: (1) call identity — block after N identical (tool, args) calls; (2) output identity — block when same args produce same output ≥2 times (skippable for polling tools via `loop_output_sensitive: true`); (3) consecutiveness — WARN at N consecutive calls, BLOCK at N+1 with WARNED→ACTIVE reset when a different tool runs in between. `loop_max_per_signature`, `loop_max_consecutive`, `loop_output_sensitive` fields added to `ToolPolicy`. Per-tool overrides in `tools.yaml` for `run_sysdiag`, `self_telemetry_query`, `infra_health` (output-sensitive polling), `create_linear_issue`, `write_file` (strict). All gate decisions emit structured `tool_loop_gate` log events (Level 2 observability). 24 unit tests. |
| Bug: event bus / second-brain pipeline not firing (FRE-239) | 2026-04-21 | Four fixes: (1) `seshat_captures_cloud` Docker volume added to `docker-compose.cloud.yml` — captures no longer wiped on container restart; (2) `BrainstemScheduler._trigger_consolidation` now only sets `last_consolidation` when `captures_processed > 0`, preventing an empty startup run from blocking consolidation for 1 hour; (3) `NoOpBus.publish` emits a `debug` log so silent discards are visible; (4) `app.py` lifespan logs `event_bus_ready` with registered consumer list on startup. |
| Bug: cross-provider tool_use_id orphan (FRE-237) | 2026-04-21 | New `llm_client/history_sanitiser.py` — two-pass strip of orphaned `tool_result` / `tool_calls` entries before every dispatch (both `LocalLLMClient` and `LiteLLMClient`). Fixes Anthropic 400 on Qwen→Sonnet failover. Telemetry: `history_sanitised` event. Also fixed `.env` `AGENT_MCP_GATEWAY_COMMAND` JSON format. |
| Seshat v2 Architecture (FRE-192: FRE-201–209) | 2026-04-14 | All 8 ADRs (0043–0050) implemented across 6 phases. FRE-201: Protocol definitions (KnowledgeGraphProtocol, SessionStoreProtocol, SearchIndexProtocol, etc.). FRE-202: Context observability (CompactionRecord, KnowledgeWeight, freshness scoring). FRE-203: SKILL.md docs (4 skill files). FRE-204: AG-UI transport (SSE streaming, 5 event types). FRE-205: Docker Compose cloud simulation (6-service topology). FRE-206: Seshat API Gateway (auth, rate limiting, knowledge/session/observation APIs, HTTP client). FRE-207: Execution profiles (local/cloud YAML, profile-aware TraceContext). FRE-208: MCP server + delegation adapters (ClaudeCode/Codex/GenericMCP adapters, 6 MCP tools). FRE-209: PWA scaffold (Next.js 14, AG-UI SSE streaming, HITL). 180+ new tests. |
| Proactive Memory (FRE-174–176; FRE-177 procedure) | 2026-04-04 | `suggest_relevant()` + `MemoryServiceAdapter`, `memory/proactive.py` scoring/budget, `AGENT_PROACTIVE_MEMORY_ENABLED`, `assemble_context` + `session_id` wiring. Tests: `test_proactive.py`, `test_context.py`. EVAL A/B: run harness + fill `telemetry/evaluation/EVAL-proactive-memory/README.md`. ADR-0039 Accepted (MVP). |
| LinearClient → native GraphQL (FRE-243) | 2026-04-22 | `captains_log/linear_client.py` rewritten to call Linear's GraphQL API directly via httpx + `AGENT_LINEAR_API_KEY` PAT. Removed `MCPGatewayAdapter` dependency — fixes silent `linear_client = None` on VPS (no Docker Desktop DCR socket). `service/app.py` now constructs `LinearClient()` whenever key is set, independent of MCP gateway state. 42-test suite added. Hook bug fixed (`pgrep -f "python.*-m pytest"` instead of `pgrep -f "pytest"`). ADR-0028 "LinearClient coupling" concern resolved; ADR-0040 references updated. |
| Linear Feedback Channel Phases 1–2 (ADR-0040) | 2026-04-04 | `FeedbackPoller`, all 6 handlers (Approved/Rejected/Deepen/Too Vague/Duplicate/Defer), `LinearClient` wrapper, promotion pipeline wired live, event bus integration (`feedback.received`, `promotion.issue_created`). Phase 3 meta-learning pending. |
| KG Freshness 6–7/7 (FRE-166, FRE-167) + relationship IDs | 2026-04-04 | FRE-166: `brainstem/jobs/freshness_review.py`, scheduler cron (`AGENT_FRESHNESS_REVIEW_SCHEDULE_CRON`), tier aggregation snapshot + deltas, Captain's Log dormant proposals when over threshold. FRE-167: `uv run agent memory freshness-backfill` (`freshness_backfill.py`), gated by `AGENT_FRESHNESS_BACKFILL_CONFIRM`. `MemoryAccessedEvent.relationship_ids` populated on query + consolidation paths; `FreshnessConsumer` UNWIND-updates relationships by `elementId`. Integration-style test: `tests/personal_agent/memory/test_freshness_pipeline.py`. |
| KG Freshness 5–6/7 (FRE-164, FRE-165) | 2026-04-04 | FRE-164: `FreshnessConsumer` batch writer — buffers `memory.accessed` events (5 s window / 50 max), deduplicates per entity, single Cypher UNWIND flush to Neo4j; wired into `app.py` lifespan. FRE-165: `compute_freshness` (exponential decay × frequency boost) + `classify_staleness` (WARM/COOLING/COLD/DORMANT tiers); freshness integrated as step 6 in `_calculate_relevance_scores()` with `w_scale` weight redistribution. |
| KG Freshness 3/7 (FRE-163) | 2026-04-04 | `memory.accessed` events published from all 6 active query paths (`query_memory`, `query_memory_broad`, `recall`, `recall_broad`, `memory_search` tool, consolidation traversal). Feature flag gates all publishing. `session_id` 422 fix in `/chat`. |
| KG Freshness 1–2/7 (FRE-161, FRE-162) | 2026-04-04 | FRE-161: Neo4j schema (`last_accessed_at`, `access_count`, `last_access_context`, `first_accessed_at`) + Cypher constraint. FRE-162: `AccessContext` enum, `MemoryAccessedEvent` (typed fields), `FreshnessSettings` in config, unit tests. |
| Event Bus Phase 4 foundation (FRE-160) | 2026-04-04 | `stream:memory.accessed` + `stream:memory.entities_updated`; `cg:freshness` group; `MemoryAccessedEvent` stub publish in `query_memory()`; `MemoryEntitiesUpdatedEvent` stub in consolidator. |
| Event Bus Phase 2 (FRE-158) | 2026-04-04 | `request.completed` → `cg:es-indexer` + `cg:session-writer`; `parse_stream_event`; consumer retries + dead-letter; `/chat` durable when bus enabled; FRE-51 session waiter. |
| Event Bus Phase 1 (FRE-157) | 2026-04-03 | Redis 7 infra, EventBus protocol, RedisStreamBus, ConsumerRunner, `request.captured` → consolidator migration. Feature flag off by default; polling fallback retained. |
| CLI-First Tool Migration (FRE-171/170/173/172/188) + ReAct loop | 2026-04-04 | ADR-0028 fully implemented. 5 native tools: `query_elasticsearch`, `perplexity_query`, `fetch_url`, `get_library_docs`, `run_sysdiag`. MCP ES/Perplexity/fetch/Context7/misc tools disabled in governance. `TOOL_INTEGRATION_GUIDE.md` + `SKILL_TEMPLATE.md` created. `is_synthesizing` gate removed — agent now chains tool calls (ReAct loop) until deciding to synthesize; bounded by `orchestrator_max_tool_iterations`. |
| ADR-0028 research (CLI vs MCP) | 2026-04-02 | FRE-99 complete. ADR accepted: hybrid three-tier model (native > CLI > MCP). Implementation project created. |
| Context Intelligence — Phase 4 ENHANCE | 2026-03-30 | Rolling LLM summarization (ADR-0038), async compression, structured context assembly, KV cache prefix stability, cross-session eval (CP-30/CP-31), proactive memory design (ADR-0039), recall classifier L2 design. EVAL-10 pending. |
| Context Intelligence — Phase 3 VERIFY (EVAL-09) | 2026-03-30 | 34/35 paths, 176/177 assertions (99.4%). All Phase 3 gates met. |
| Qwen3.5 Model Integration | 2026-03-16 | Think-tag stripping, per-model thinking control, sampling params. Project completed. |
| Redesign v2 — Slice 3: Intelligence | 2026-03-29 | Expansion controller, dual-mode sub-agents, Seshat hybrid search, recall controller (Stage 4b). |
| Redesign v2 — Slice 2: Expansion | 2026-03-20 | Decomposition, sub-agents, HYBRID execution, memory promotion, Stage B delegation, insights engine. |
| Redesign v2 — Slice 1: Foundation | 2026-03-19 | Pre-LLM Gateway (7 stages), single-brain architecture, MemoryProtocol, Stage A delegation. |
| 2.3 Homeostasis & Feedback | 2026-03-15 | ES indexing, Kibana dashboards, insights engine, Captain's Log dedup pipeline (ADR-0030), inference concurrency control (ADR-0029). |
| 2.2 Memory & Second Brain | 2026-01-23 | `plans/completed/PHASE_2.2_COMPLETE.md` |
| 2.1 Service Foundation | 2026-01-22 | `plans/completed/PHASE_2.1_COMPLETE.md` |
| 1.0 MVP (CLI Agent) | 2026-01 | 111 tests, MCP gateway (41 tools), telemetry |

---

## ADR Index (recent)

| ADR | Title | Status |
|-----|-------|--------|
| 0061 | Within-Session Progressive Context Compression | Needs Approval (FRE-251 — blocked by 0059) |
| 0060 | Knowledge Graph Quality Stream | Needs Approval (FRE-250 — blocked by 0054, 0057) |
| 0059 | Context Quality Monitoring Stream | Needs Approval (FRE-249 — blocked by 0054, 0056) |
| 0058 | Self-Improvement Pipeline Stream | Needs Approval (FRE-248 — blocked by 0054) |
| 0057 | Insights & Pattern Analysis Stream | Needs Approval (FRE-247 — blocked by 0054) |
| 0056 | Error Pattern Monitoring Stream | Needs Approval (FRE-244 — blocked by 0054) |
| 0055 | System Health & Homeostasis Stream | Needs Approval (FRE-246 — blocked by 0054) |
| 0054 | Feedback Stream Bus Convention | Needs Approval (FRE-245 — **draft next**) |
| 0062 | Tool Loop Gate — Per-Tool FSM-Based Loop Detection | Accepted (Implemented — 2026-04-22) |
| 0053 | Deterministic Gate Feedback-Loop Monitoring Framework | Proposed (FRE-233 — awaiting acceptance) |
| 0052 | Seshat Owner Identity Primitive | Proposed (Needs Approval) |
| 0051 | Cloud Profile Orchestrator Dispatch via ContextVar | Accepted |
| 0050 | Remote Agent Harness Integration | Accepted (implemented — FRE-208) |
| 0049 | Application Modularity | Accepted (implemented — FRE-201) |
| 0048 | Mobile & Multi-Device UI | Accepted (implemented — FRE-209) |
| 0047 | Context Management & Observability | Accepted (implemented — FRE-202) |
| 0046 | Agent-to-UI Protocol Stack | Accepted (implemented — FRE-204) |
| 0045 | Infrastructure — Cloud Knowledge Layer | Accepted (implemented — FRE-205, FRE-206) |
| 0044 | Provider Abstraction & Dual-Harness | Accepted (implemented — FRE-207) |
| 0043 | Three-Layer Architectural Separation | Accepted (implemented — foundational for ADRs 0044–0050) |
| 0042 | Knowledge Graph Freshness via Access Tracking | Accepted (implemented — 7/7 done) |
| 0041 | Event Bus — Redis Streams | Accepted (Phases 1–4 implemented) |
| 0040 | Linear as Async Feedback Channel | Accepted (Phases 1–2 implemented; Phase 3 pending) |
| 0039 | Proactive Memory via `suggest_relevant()` | Accepted (MVP implemented; EVAL numbers TBD) |
| 0038 | Context Compressor Model | Accepted (implemented) |
| 0037 | Recall Controller | Accepted (implemented) |
| 0036 | Expansion Controller | Accepted (implemented) |
| 0035 | Seshat Backend Decision | Accepted (implemented) |
| 0034 | SearXNG Self-Hosted Web Search | Accepted (implemented) |
| 0033 | Multi-Provider Model Taxonomy | Accepted (implemented) |
| 0032 | Robust Tool Calling Strategy | Accepted (implemented) |
| 0031 | Model Config Consolidation | Accepted (implemented) |
| 0030 | Captain's Log Dedup & Self-Improvement Pipeline | Accepted (implemented) |
| 0028 | External Tool CLI Migration (MCP → Native/CLI) | Accepted (implemented) |

---

## How This File Works

- **FIFO**: Completed items eventually drop off the bottom.
- **Linear is the task tracker**: This file tracks *priorities and sequencing*, not individual tasks.
- **Sub-plans** (e.g. `PHASE_2.3_PLAN.md`) contain implementation detail; this file links to them.
- **Specs** live in `docs/specs/`; **ADRs** in `docs/architecture_decisions/`.
- **Update cadence**: When priorities shift or phases complete.
