# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-04-04 (Proactive Memory FRE-174–176 implemented; FRE-177 harness + table TBD)

---

## Current Focus

| # | Work Item | Linear | Spec / ADR | Status |
|---|-----------|--------|------------|--------|
| 1 | EVAL-10 run (Context Intelligence final verification) | [FRE-187](https://linear.app/frenchforest/issue/FRE-187/eval-10-context-intelligence-final-verification-run) | `specs/CONTEXT_INTELLIGENCE_SPEC.md` | Needs Approval |
| 2 | Fix test import failure (missing `mcp` module) | FRE-185 | — | Approved |

## Upcoming — Needs Approval

Ordered by recommended implementation sequence. Dependency chains are encoded in Linear (`blockedBy` relations).

| # | Project | Linear | ADR / Spec | Depends On |
|---|---------|--------|------------|------------|
| 4 | Linear Feedback Channel — Phase 3 meta-learning | [Project](https://linear.app/frenchforest/project/linear-async-feedback-channel-4517a7698be1) | ADR-0040 | Phases 1–2 done; FRE-183 needs feedback data |
| 5 | CLI-First Tool Migration | [Project](https://linear.app/frenchforest/project/cli-first-tool-migration-5b948aeb13bb) | ADR-0028 | FRE-99 (Done) |
| 6 | Context Intelligence — Stretch Goals | [Project](https://linear.app/frenchforest/project/context-intelligence-stretch-goals-315c8caa9cc9) | `specs/CONTEXT_INTELLIGENCE_SPEC.md` §4.7/4.S1/4.S2, `specs/RECALL_CLASSIFIER_L2_DESIGN.md` | Proactive Memory MVP done (FRE-176) |
| 7 | Phase 3.0 Daily-Use Interface | [Project](https://linear.app/frenchforest/project/30-daily-use-interface-60a517bd90f6) | — | CLI Migration (FRE-172) |

### Dependency graph (project-level)

```text
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
| Captain's Log ES Backfill | — | `specs/CAPTAINS_LOG_ES_BACKFILL_SPEC.md` |

## Completed

| Phase | Completed | Summary |
|-------|-----------|---------|
| Proactive Memory (FRE-174–176; FRE-177 procedure) | 2026-04-04 | `suggest_relevant()` + `MemoryServiceAdapter`, `memory/proactive.py` scoring/budget, `AGENT_PROACTIVE_MEMORY_ENABLED`, `assemble_context` + `session_id` wiring. Tests: `test_proactive.py`, `test_context.py`. EVAL A/B: run harness + fill `telemetry/evaluation/EVAL-proactive-memory/README.md`. ADR-0039 Accepted (MVP). |
| Linear Feedback Channel Phases 1–2 (ADR-0040) | 2026-04-04 | `FeedbackPoller`, all 6 handlers (Approved/Rejected/Deepen/Too Vague/Duplicate/Defer), `LinearClient` wrapper, promotion pipeline wired live, event bus integration (`feedback.received`, `promotion.issue_created`). Phase 3 meta-learning pending. |
| KG Freshness 6–7/7 (FRE-166, FRE-167) + relationship IDs | 2026-04-04 | FRE-166: `brainstem/jobs/freshness_review.py`, scheduler cron (`AGENT_FRESHNESS_REVIEW_SCHEDULE_CRON`), tier aggregation snapshot + deltas, Captain's Log dormant proposals when over threshold. FRE-167: `uv run agent memory freshness-backfill` (`freshness_backfill.py`), gated by `AGENT_FRESHNESS_BACKFILL_CONFIRM`. `MemoryAccessedEvent.relationship_ids` populated on query + consolidation paths; `FreshnessConsumer` UNWIND-updates relationships by `elementId`. Integration-style test: `tests/personal_agent/memory/test_freshness_pipeline.py`. |
| KG Freshness 5–6/7 (FRE-164, FRE-165) | 2026-04-04 | FRE-164: `FreshnessConsumer` batch writer — buffers `memory.accessed` events (5 s window / 50 max), deduplicates per entity, single Cypher UNWIND flush to Neo4j; wired into `app.py` lifespan. FRE-165: `compute_freshness` (exponential decay × frequency boost) + `classify_staleness` (WARM/COOLING/COLD/DORMANT tiers); freshness integrated as step 6 in `_calculate_relevance_scores()` with `w_scale` weight redistribution. |
| KG Freshness 3/7 (FRE-163) | 2026-04-04 | `memory.accessed` events published from all 6 active query paths (`query_memory`, `query_memory_broad`, `recall`, `recall_broad`, `memory_search` tool, consolidation traversal). Feature flag gates all publishing. `session_id` 422 fix in `/chat`. |
| KG Freshness 1–2/7 (FRE-161, FRE-162) | 2026-04-04 | FRE-161: Neo4j schema (`last_accessed_at`, `access_count`, `last_access_context`, `first_accessed_at`) + Cypher constraint. FRE-162: `AccessContext` enum, `MemoryAccessedEvent` (typed fields), `FreshnessSettings` in config, unit tests. |
| Event Bus Phase 4 foundation (FRE-160) | 2026-04-04 | `stream:memory.accessed` + `stream:memory.entities_updated`; `cg:freshness` group; `MemoryAccessedEvent` stub publish in `query_memory()`; `MemoryEntitiesUpdatedEvent` stub in consolidator. |
| Event Bus Phase 2 (FRE-158) | 2026-04-04 | `request.completed` → `cg:es-indexer` + `cg:session-writer`; `parse_stream_event`; consumer retries + dead-letter; `/chat` durable when bus enabled; FRE-51 session waiter. |
| Event Bus Phase 1 (FRE-157) | 2026-04-03 | Redis 7 infra, EventBus protocol, RedisStreamBus, ConsumerRunner, `request.captured` → consolidator migration. Feature flag off by default; polling fallback retained. |
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
| 0028 | External Tool CLI Migration (MCP → Native/CLI) | Accepted |

---

## How This File Works

- **FIFO**: Completed items eventually drop off the bottom.
- **Linear is the task tracker**: This file tracks *priorities and sequencing*, not individual tasks.
- **Sub-plans** (e.g. `PHASE_2.3_PLAN.md`) contain implementation detail; this file links to them.
- **Specs** live in `docs/specs/`; **ADRs** in `docs/architecture_decisions/`.
- **Update cadence**: When priorities shift or phases complete.
