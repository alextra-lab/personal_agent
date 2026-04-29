# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-04-29 (FRE-249 marked Done — ADR-0059 implemented 2026-04-27; FRE-264 marked Done — implemented 2026-04-26; ADR-0060 draft in review FRE-250)
> **Implementation sequence**: `docs/superpowers/specs/2026-04-22-implementation-sequence-wave-plan-design.md`

---

## ✅ FULL PIVOT-4 Verdict (2026-04-28) — supersedes PARTIAL PIVOT-4

Root cause of PARTIAL PIVOT-4 identified and fixed:
- **FRE-283**: bash primitive used `shlex.split + create_subprocess_exec(*argv)` — pipes and shell operators silently failed. Fixed to `/bin/bash -o pipefail -c`. Also wired `auto_approve_prefixes` into `_check_permissions`.
- **FRE-284**: Seshat skill docs had wrong API paths (missing `/api/v1`), wrong response shapes, wrong Kibana index pattern. Fixed.

**G3 eval result (run-g3-full-sonnet-2026-04-28T1930)**: quality 19/20 ✅, cost 1.39× ✅.

**All 8 tools: DEPRECATE** via `AGENT_LEGACY_TOOLS_ENABLED=false`:
`query_elasticsearch`, `fetch_url`, `list_directory`, `system_metrics_snapshot`, `self_telemetry_query`, `run_sysdiag`, `infra_health`, `read_file`

**FRE-263 scope expanded to all 8 tools.** See updated issue.
**FRE-277** (eval cleanup script) is open/approved — optional housekeeping.
**Normal sequencing resumes** at FRE-263 (PIVOT-4, full scope), then Wave 3 (FRE-249 ✅ done 2026-04-27, FRE-250 in review).

Eval: `telemetry/evaluation/EVAL-primitive-tools/run-g3-full-sonnet-2026-04-28T1930/EVAL_RESULT.md`
---

## Current Focus

> Next item is always the first incomplete entry in the wave plan. Update this row when an item ships.

| Wave | # | Work Item | Linear | Type | Status |
|------|---|-----------|--------|------|--------|
| ~~0~~ | ~~1~~ | ~~bug(captains_log): DSPy bypassed for cloud models~~ | ~~[FRE-253](https://linear.app/frenchforest/issue/FRE-253)~~ | ~~Bug fix~~ | ~~Done 2026-04-22~~ |
| ~~0~~ | ~~2~~ | ~~Governance: per-TaskType tool allowlist Stage 3~~ | ~~[FRE-252](https://linear.app/frenchforest/issue/FRE-252)~~ | ~~Feature~~ | ~~Done 2026-04-22~~ |
| ~~1~~ | ~~3a~~ | ~~Investigate step-count reduction (interaction latency)~~ | ~~[FRE-254](https://linear.app/frenchforest/issue/FRE-254)~~ | ~~Investigation~~ | ~~Done 2026-04-22~~ |
| ~~1~~ | ~~3b~~ | ~~Feedback Stream Bus Convention (ADR-0054)~~ | ~~[FRE-245](https://linear.app/frenchforest/issue/FRE-245)~~ | ~~ADR + implementation~~ | ~~Done 2026-04-23 — Accepted, flattened `EventBase`, 10 producer sites migrated~~ |
| ~~2~~ | ~~4~~ | ~~System Health & Homeostasis — Mode Manager fix (ADR-0055)~~ | ~~[FRE-246](https://linear.app/frenchforest/issue/FRE-246)~~ | ~~ADR + fix~~ | ~~Done 2026-04-24~~ |
| ~~2~~ | ~~3~~ | ~~Error Pattern Monitoring — Level 3 observability (ADR-0056)~~ | ~~[FRE-244](https://linear.app/frenchforest/issue/FRE-244)~~ | ~~ADR + implementation~~ | ~~Done 2026-04-24 — Phase 1 (cg:error-monitor + dual-write) + Phase 2 (GEPA failure-path reflection, flag off)~~ |
| ~~2~~ | ~~4~~ | ~~Insights & Pattern Analysis — wire InsightsEngine~~ | ~~[FRE-247](https://linear.app/frenchforest/issue/FRE-247)~~ | ~~ADR + implementation~~ | ~~Done 2026-04-24~~ |
| ~~2.5~~ | ~~—~~ | ~~ADR-0063 Primitive Tools & Action-Boundary Governance~~ | ~~[FRE-259](https://linear.app/frenchforest/issue/FRE-259)~~ | ~~ADR + 6-phase migration~~ | ~~FRE-260 done~~ · ~~FRE-261 done~~ · ~~FRE-262+FRE-283+FRE-284 done — FULL PIVOT-4~~ · ~~FRE-263 done 2026-04-28 — all 8 tools deprecated~~ · FRE-265 blocked (2-week window until 2026-05-12) |

## Upcoming — Approved

Ordered by recommended implementation sequence. All items Approved in Linear. Dependency chains encoded in Linear (`blockedBy` relations).

| # | Project | Linear | ADR / Spec | Depends On |
|---|---------|--------|------------|------------|
| ~~2~~ | ~~System Health & Homeostasis — Mode Manager fix~~ | ~~[FRE-246](https://linear.app/frenchforest/issue/FRE-246)~~ | ~~ADR-0055~~ | ~~FRE-245~~ |
| ~~3~~ | ~~Error Pattern Monitoring — Level 3 observability~~ | ~~[FRE-244](https://linear.app/frenchforest/issue/FRE-244)~~ | ~~ADR-0056~~ | ~~FRE-245~~ |
| ~~5~~ | ~~Self-Improvement Pipeline — formalize Streams 1-3~~ | ~~[FRE-248](https://linear.app/frenchforest/issue/FRE-248)~~ | ~~ADR-0058~~ | ~~Done 2026-04-25~~ |
| ~~2.5-P1~~ | ~~ADR-0063 — Sever TaskType→tool-filter wire~~ | ~~[FRE-260](https://linear.app/frenchforest/issue/FRE-260)~~ | ~~ADR-0063 §D1~~ | ~~Done 2026-04-25 — verified in prod; 48h gate active~~ |
| ~~2.5-P2~~ | ~~ADR-0063 — Four primitives + sandbox + action-boundary~~ | ~~[FRE-261](https://linear.app/frenchforest/issue/FRE-261)~~ | ~~ADR-0063 §D2-D3~~ | ~~Done 2026-04-27~~ |
| ~~2.5-P5~~ | ~~ADR-0063 — Loop gate signal split + model_config fix *(parallel to P2/P3/P4)*~~ | ~~[FRE-264](https://linear.app/frenchforest/issue/FRE-264)~~ | ~~ADR-0063 §D5-D6~~ | ~~Done 2026-04-26~~ |
| ~~6~~ | ~~Context Quality — compaction full loop~~ | ~~[FRE-249](https://linear.app/frenchforest/issue/FRE-249)~~ | ~~ADR-0059~~ | ~~Done 2026-04-27 — ADR accepted + implemented~~ |
| **7** | **Knowledge Graph Quality — consolidation + decay→reranking** | [FRE-250](https://linear.app/frenchforest/issue/FRE-250) | ADR-0060 | ~~FRE-245~~, ~~FRE-247~~ |
| ~~2.5-P3~~ | ~~ADR-0063 — Skill docs + model evaluation~~ | ~~[FRE-262](https://linear.app/frenchforest/issue/FRE-262)~~ | ~~ADR-0063 §D7~~ | ~~Done 2026-04-28 — FULL PIVOT-4 after FRE-283/FRE-284 fixes~~ |
| 8 | Within-Session Progressive Context Compression | [FRE-251](https://linear.app/frenchforest/issue/FRE-251) | ADR-0061 | FRE-249 |
| ~~2.5-P4~~ | ~~ADR-0063 — Flag-gated deprecation of all 8 legacy tools~~ | ~~[FRE-263](https://linear.app/frenchforest/issue/FRE-263)~~ | ~~ADR-0063 §D4~~ | ~~Done 2026-04-28 — 2-week stability window until 2026-05-12~~ |
| 9 | Agent self-updating skills — phase 2 *(phase 1 absorbed into FRE-262)* | [FRE-226](https://linear.app/frenchforest/issue/FRE-226) | ADR-0058 | FRE-248 |
| 2.5-P6 | ADR-0063 — Delete legacy tool code | [FRE-265](https://linear.app/frenchforest/issue/FRE-265) | ADR-0063 | FRE-263 (2-week window) |
| 10 | Linear Feedback Channel — Phase 3 meta-learning | [Project](https://linear.app/frenchforest/project/linear-async-feedback-channel-4517a7698be1) | ADR-0040 | Phases 1–2 done; FRE-183 needs feedback data |
| 11 | Context Intelligence — Stretch Goals | [Project](https://linear.app/frenchforest/project/context-intelligence-stretch-goals-315c8caa9cc9) | `specs/CONTEXT_INTELLIGENCE_SPEC.md` §4.7/4.S1/4.S2 | Proactive Memory MVP done (FRE-176) |

## Needs Approval

| Work Item | ADR / Plan | Notes |
|-----------|------------|-------|
| PWA: per-session thumbs feedback → Captain's Log + Insights consumer | — | [FRE-267](https://linear.app/frenchforest/issue/FRE-267) — deferred from FRE-235; design groundwork complete in `plans/let-s-analyze-and-wisely-stateless-tome.md` |

### Dependency graph (project-level)

```text
ADR-0053: Gate Feedback Monitoring (FRE-233) ✅ Done 2026-04-22 — spawned FRE-244–251
    ↓
ADR-0054: Bus Convention (FRE-245) ✅ Done 2026-04-23 — Wave 2 unblocked
    ├── ADR-0055: System Health & Homeostasis (FRE-246)  ← fixes app.py:176 hardcoded Mode.NORMAL
    │                                                      ← PREREQ for ADR-0063 phase 2
    ├── ADR-0056: Error Pattern Monitoring (FRE-244)     ← Level 3 observability
    ├── ADR-0057: Insights & Pattern Analysis (FRE-247)  ← wires InsightsEngine
    └── ADR-0058: Self-Improvement Pipeline (FRE-248)    ← adds captain_log.entry_created event
            ↓ (Phase 3 — depend on Phase 2)
        ADR-0059: Context Quality (FRE-249)              ← depends on 0054 + 0056
        ADR-0060: Knowledge Graph Quality (FRE-250)      ← depends on 0054 + 0057
            ↓ (Phase 4)
        ADR-0061: Within-Session Compression (FRE-251)   ← depends on ADR-0059
        FRE-226:  Agent skill files (agentskills.io)     ← SPLIT by ADR-0063:
                                                            phase 1 (hand-authored) → PIVOT-3
                                                            phase 2 (self-updating) → depends on ADR-0058
        FRE-252:  Per-TaskType tool allowlist             ← superseded by ADR-0063 phase 1

ADR-0063: Primitive Tools & Action-Boundary Governance (2026-04-24) ← APPROVED
    Parallel to Wave 2 (no code intersection); P2 depends on FRE-246.
    Phases P1 → P2 → P3 → P4 → P5 → P6 (see migration plan).
    P1 severs TaskType→tool-filter wire; P2-P4 adds primitives; P5-P6 removes legacy.

Architecture reference: docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md

Proactive Memory (ADR-0039)
    ↓
Context Intelligence Stretch Goals (4.7, 4.S1, 4.S2)

CLI Migration (ADR-0028)
    ↓ (natural continuation)
ADR-0063 Primitive Tools (bash + read + write + run_python)
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
| FRE-261: PIVOT-2 — Four primitive tools + sandbox + action-boundary governance | 2026-04-27 | `bash` primitive (hard-deny regex, shlex parse, auto-approve allowlist, 50 KB cap with scratch overflow), `read`/`write` primitives (path allowlist + unattended scratch paths), `run_python` Docker sandbox (`python:3.12-slim`, non-root uid 1000, `--read-only`, `--network=none`, `--memory-swap` pinned, per-trace scratch bind-mount), AG-UI approval round-trip (`ToolApprovalRequestEvent` + `approval_waiter.py` asyncio.Future registry + `POST /agui/approval/{id}` endpoint), PWA `ApprovalModal` (countdown, risk chip, auto-deny on timeout). Feature-flagged off (`AGENT_PRIMITIVE_TOOLS_ENABLED`, `AGENT_APPROVAL_UI_ENABLED`). 15-case pentest suite (11 unit + 6 integration). Workspace volume `seshat_workspace_cloud` at `/app/agent_workspace/` (FRE-227 forward-compat). pgvector `notes_search` dropped from FRE-227 scope — `bash grep` covers retrieval. Gate: pentest integration run + E2E approval flow on cloud-sim before enabling flags. |
| FRE-229: Memory visibility layer — public / group / private (ADR-0064 §D6/D7) | 2026-04-26 | Three-level Neo4j visibility scoping. `Visibility` enum + `visibility` string property on `:Turn`, `:Entity`, `:Relationship`, `:Session` nodes. Single chokepoint `_build_visibility_filter()` in `memory/service.py` — injected into all 7 read methods. `create_conversation` / `create_entity` (ON CREATE SET semantics) / `create_relationship` accept `visibility=` param. `user_id` + `authenticated` flow from `/chat` endpoint → `run_gateway_pipeline` → `assemble_context` → `MemoryRecallQuery` → adapter → service. `TaskCapture.user_id` + `ExecutionContext.user_id` plumbed for consolidator write path — produces `"group"` nodes from authenticated sessions, `"public"` from CLI/unauthenticated. Backfill: `scripts/migrate_fre229_visibility_backfill.py`. 23 new unit tests. ADR-0064 status updated to Accepted. |
| FRE-268: Session ownership scoping via CF Access identity (ADR-0064) | 2026-04-26 | Reads `Cf-Access-Authenticated-User-Email` on inbound requests; maps to stable `user_id` UUID in new Postgres `users` table. `GET /sessions`, `GET/PATCH /sessions/{id}`, `POST /chat`, `POST /chat/stream`, SSE `/stream/{session_id}` all scoped to the authenticated user — mismatch returns 404. Dev-mode fallback via `AGENT_OWNER_EMAIL`. Migration script in `scripts/migrate_fre268_add_user_identity.py`. Memory graph remains global (shared knowledge for trusted group). ADR-0064 redirects FRE-229 to simplified three-level `public`/`group`/`private` model (single unnamed group = CF Access policy). 15 new unit tests. |
| FRE-235: PWA persistent sessions + cross-device resume | 2026-04-26 | Cloud gateway `chat_api.py` now session-persistent: loads prior messages, persists every turn with `(trace_id, timestamp, metadata.source)`, emits `RequestCompletedEvent` to bus. PWA: permanent `/c/{sessionId}` URLs, cold-start hydration from `GET /api/v1/sessions/{id}/messages`, session list drawer (hamburger), Escape to close. `GET /api/v1/sessions` now returns `title` field. All message appends on all paths now carry full observability payloads. Sibling issue FRE-267 filed for thumbs feedback. |
| ADR-0058: Self-Improvement Pipeline Stream (FRE-248) | 2026-04-25 | Closes the Stream 1 bus gap (ADR-0054). `CaptainLogEntryCreatedEvent` published on `stream:captain_log.entry_created` from both `CaptainLogManager.save_entry()` (new writes) and `_merge_into_existing()` (dedup merges, `is_merge=True`). Suppression path correctly skips. Bus publish uses fire-and-forget `asyncio.create_task` pattern (durable-first per ADR-0054 D4; bus failure logged+swallowed per D6). No new consumer group — producer-only ADR. Stream 1 `Bus?` flipped to ✅ in FEEDBACK_STREAM_ARCHITECTURE.md. 9 new unit tests. Unblocks FRE-226 phase 2 (agent self-updating skills). |
| ADR-0055: System Health & Homeostasis — Mode Manager fix (FRE-246) | 2026-04-24 | Closed the critical Mode Manager disconnect: 4 × `Mode.NORMAL` hardcodes in `service/app.py` replaced by `get_current_mode()`. `MetricsDaemon` dual-writes `MetricsSampledEvent` to `stream:metrics.sampled` every 5 s (MAXLEN 720). `ModeManager.transition_to()` dual-writes `ModeTransitionEvent` to `stream:mode.transition`. `cg:mode-controller` consumer drives the FSM: rolling 60 s window → 30 s evaluation cadence → `ModeManager.evaluate_transitions()`. Anomalous transition cadence (≥3 per 10 min per edge) → `CaptainLogEntry(RELIABILITY, scope=mode_calibration)` with SHA-256 fingerprint. `mode_controller_enabled` defaults True. 56 new tests. ADR-0055 and FEEDBACK_STREAM_ARCHITECTURE.md updated. |
| ADR-0057: Insights & Pattern Analysis (FRE-247) | 2026-04-24 | Closes Streams 4 & 9. `build_consolidation_insights_handler` extended: publishes `InsightsPatternDetectedEvent` per insight + `InsightsCostAnomalyEvent` per anomaly on the bus; calls `create_captain_log_proposals` → `CaptainLogManager.save_entry` (ADR-0030 fingerprint dedup applies). New `Insight.pattern_kind` field. `_pattern_fingerprint` / `_cost_fingerprint` / `_severity_for_cost_ratio` / `_category_for_insight_type` / `_scope_for_insight_type` helpers extracted to `insights/fingerprints.py`. `InsightsEngine.detect_delegation_patterns()` stub replaced with 3 real ES aggregations (success rate, rounds p75, missing-context themes). Config flag `insights_wiring_enabled=True`. |
| Investigation: Step-count latency reduction (FRE-254) | 2026-04-22 | Root cause: Qwen3-35B-A3B emits one tool call per turn regardless of batching instructions — orchestrator already supports N calls/turn. Top findings: (1) `get_tool_definitions_for_llm()` ignores TaskType `allowed_categories` — wiring it eliminates ~3,000–4,000 tokens on conversational turns; (2) no step-budget hint in system prompt (`_TOOL_RULES` prompts.py:39) — add `"≤ 6 tool calls"` guidance; (3) total tool description cost ~4,200–4,600 tokens with redundant/stale references. Full report: `docs/research/FRE-254-step-count-investigation.md`. |
| ADR-0054: Feedback Stream Bus Convention accepted + implemented (FRE-245) | 2026-04-23 | ADR rewritten from the "two-base" draft to a single flattened `EventBase` carrying `trace_id` / `session_id` / `source_component` / `schema_version`. D3 now describes the flat design; Alternatives table flips the verdict (A "flatten" adopted; C "FeedbackEventBase as second root" rejected with explicit post-hoc rationale). Implementation in the same change: `EventBase` fields added in `events/models.py`; 10 production `xadd` sites migrated with `source_component=<dotted-module-path>`; ~40 test event constructions updated; 5 new tests cover `source_component` required, `schema_version` default, nullable trace on scheduled events, forward-compat v1-consumer-reads-v2-payload. Verified: 118 tests pass across `events/`, `memory/test_memory_access_events`, `second_brain/`, `brainstem/`; mypy surfaces no new errors. Still reserves 8 Phase 2 stream names + 6 consumer group names. Wave 2 unblocked. |
| ADR-0054 draft (FRE-245) | 2026-04-22 | Initial draft written at `docs/architecture_decisions/ADR-0054-feedback-stream-bus-convention.md` (Status: Proposed — In Review). Seven decisions D1–D7 drafted with a `FeedbackEventBase` as a secondary root; superseded in place on 2026-04-23 by the flatten decision (see row above). |
| Bug: DSPy bypassed for cloud models (FRE-253) | 2026-04-22 | Extended `configure_dspy_lm()` to accept `ModelRole \| str` and build `"{provider}/{model_id}"` LiteLLM strings for cloud models (Anthropic/OpenAI API key from settings, no api_base). Removed 70-line cloud bypass from `reflection.py`; both local and cloud now route through DSPy ChainOfThought. `generate_reflection_dspy()` gains `captains_log_role` param. 5 new unit tests. |
| ADR-0053: Gate Feedback Monitoring framework (FRE-233) | 2026-04-22 | Drafted ADR-0053 at `docs/architecture_decisions/ADR-0053-gate-feedback-monitoring.md`. Established Feedback Stream ADR Template; four-level observability framework documented. Spawned implementation issues FRE-244–251 (all Approved). |
| Bug: create_linear_issue teamId type mismatch (FRE-255) | 2026-04-22 | Fixed `IDComparator.eq` type error in `create_linear_issue` native tool — was passing raw UUID where API expects `ID!` scalar. |
| FRE-242: Bug agent loops on web searches — Canceled | 2026-04-22 | Superseded by ADR-0062/ToolLoopGate which handles all tool loop detection generically. |
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
| EVAL-10: Context Intelligence final verification (FRE-187) | 2026-04-14 | 175/181 assertions, 33/37 paths (96.7%). CP-30/CP-31 cross-session pass. Four paths failed (CP-05 timeouts, CP-07 ES format, CP-11 decomp telemetry, CP-22 tool lifecycle) — documented with root causes; re-baseline to 99.4% deferred to follow-up issues. |
| Context Intelligence — Phase 4 ENHANCE | 2026-03-30 | Rolling LLM summarization (ADR-0038), async compression, structured context assembly, KV cache prefix stability, cross-session eval (CP-30/CP-31), proactive memory design (ADR-0039), recall classifier L2 design. |
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
| 0061 | Within-Session Progressive Context Compression | Approved (FRE-251 — blocked by 0059) |
| 0060 | Knowledge Graph Quality Stream | Proposed — In Review (FRE-250 — ADR drafted 2026-04-29) |
| 0059 | Context Quality Monitoring Stream | Accepted (Implemented — FRE-249 done 2026-04-27) |
| 0058 | Self-Improvement Pipeline Stream | Accepted (Implemented — FRE-248 done 2026-04-25) |
| 0057 | Insights & Pattern Analysis Stream | Accepted (Implemented — FRE-247 2026-04-24) |
| 0056 | Error Pattern Monitoring Stream | Approved (FRE-244 — blocked by 0054) |
| 0055 | System Health & Homeostasis Stream | Approved (FRE-246 — blocked by 0054) |
| 0054 | Feedback Stream Bus Convention | Accepted (Implemented — FRE-245 done 2026-04-23) |
| 0063 | Primitive Tools & Action-Boundary Governance | Approved (2026-04-24 — Wave 2.5; epic [FRE-259](https://linear.app/frenchforest/issue/FRE-259), PIVOT-1..6 = FRE-260..265) |
| 0062 | Tool Loop Gate — Per-Tool FSM-Based Loop Detection | Accepted (Implemented — 2026-04-22) |
| 0053 | Deterministic Gate Feedback-Loop Monitoring Framework | Accepted (Implemented — FRE-233 done 2026-04-22) |
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
