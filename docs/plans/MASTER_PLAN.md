# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-05-10 (Wave J ✅; Wave A ✅; Wave B ✅ except FRE-326; Wave C ✅; Wave D planning ✅; Wave E FRE-213 ✅; FRE-227 paused → Backlog; FRE-346 audit ✅; FRE-347 G1 ✅; FRE-348 G2 ✅ PR #29; FRE-258 ADR-0068 ✅; FRE-351/352/354 ✅ PR #30; FRE-353 ✅ PR #31; FRE-350 post-deploy eval Needs Approval; FRE-349 G3 pending)

---

## Current State

Wave J complete. All 7 items shipped: FRE-329, FRE-331, FRE-330, FRE-334, FRE-332, FRE-333, FRE-335. Key findings: keyword/hybrid `es_first_call_correct_rate` drops 100%→45% with realistic prompts; model_decided maintains 100% ES routing + recall=0.95; ADR-0066 D2 threshold monitor now live — will auto-file a Linear ticket when skill index p95 exceeds 6,000 tokens for 2 consecutive days.

**Wave D planning complete (2026-05-08 / 2026-05-09).** FRE-214 verdict received: **ratify full-harness-on-VPS** (driven by ADR-0048, not ADR-0044). Deliverables on branch `fre-214-vps-topology-audit`:
* Audit: `docs/architecture/2026-05-08-fre-214-vps-topology-audit.md` (30-row parity matrix, 7 deviations, peer-deployment + endpoint-abstraction + opt-in-tunnel-mode design)
* ADR-0045 amendment landed in-place
* Implementation plans (Sonnet-ready, execution deferred per §8.7): Track 2a endpoint abstraction, Track 2b compose unification, Track 3 test parity (closes FRE-336)
* Follow-up tickets filed Needs Approval: FRE-338 (D-1 MCP env), FRE-339 (D-3 PWA runtime config), FRE-340 (D-5 transfer-models portability), FRE-341 (D-6 gateway token prune)
* FRE-217 closed as duplicate of FRE-214

**Wave D implementation pending** — Tracks 2a/2b/3 + the four FRE-338..341 + the original FRE-238 / FRE-240 / FRE-241 / FRE-236 are unblocked but not started. Implementation begins after backlog reduction (owner direction). FRE-326 calendar-gated ≥ 2026-05-13.

---

## Upcoming — 10-Wave Sequence

*Full triage rationale: `plans/complete-next-task-in-iterative-leaf.md`*

| Wave | Theme | Work Items | Key Issues | Notes |
|------|-------|-----------|------------|-------|
| **A** ✅ | Dev loop & hygiene | Fix Linear label lookup; mcp import error; flaky Neo4j test; skill-injection tests; primitive_tools default drift; stale 74-failure sweep; consolidate plan storage | [FRE-309](https://linear.app/frenchforest/issue/FRE-309) · [FRE-185](https://linear.app/frenchforest/issue/FRE-185) · [FRE-189](https://linear.app/frenchforest/issue/FRE-189) · [FRE-320](https://linear.app/frenchforest/issue/FRE-320) · [FRE-321](https://linear.app/frenchforest/issue/FRE-321) · [FRE-312](https://linear.app/frenchforest/issue/FRE-312) · [FRE-308](https://linear.app/frenchforest/issue/FRE-308) | **FRE-309 first** — broken label lookup poisons agent self-filing |
| **B** | Self-observation | `hit_iteration_limit` in reflection; error monitor scans warnings; model_config audit; env.example audit; consolidation gate re-eval | [FRE-301](https://linear.app/frenchforest/issue/FRE-301) · [FRE-300](https://linear.app/frenchforest/issue/FRE-300) · [FRE-319](https://linear.app/frenchforest/issue/FRE-319) · [FRE-269](https://linear.app/frenchforest/issue/FRE-269) · [FRE-326](https://linear.app/frenchforest/issue/FRE-326) | FRE-326 scheduled ≥ 2026-05-13 |
| **C** | Security | Domain guard — block known malicious sites | [FRE-225](https://linear.app/frenchforest/issue/FRE-225) | — |
| **D** | Architecture | Planning ✅ — VPS topology ratified (FRE-214). Implementation pending: endpoint abstraction (Track 2a), compose unification + tunnel mode (Track 2b), test parity (Track 3 / FRE-336), small follow-ups (FRE-338/339/340/341), SLM circuit breaker (FRE-238), reranker fallback (FRE-240), slm_server supervisor (FRE-241), PWA iOS SSE (FRE-236) | [FRE-214](https://linear.app/frenchforest/issue/FRE-214) ✅ verdict · ~~FRE-217~~ closed dup · [FRE-238](https://linear.app/frenchforest/issue/FRE-238) · [FRE-240](https://linear.app/frenchforest/issue/FRE-240) · [FRE-241](https://linear.app/frenchforest/issue/FRE-241) · [FRE-236](https://linear.app/frenchforest/issue/FRE-236) · [FRE-336](https://linear.app/frenchforest/issue/FRE-336) · [FRE-338](https://linear.app/frenchforest/issue/FRE-338) · [FRE-339](https://linear.app/frenchforest/issue/FRE-339) · [FRE-340](https://linear.app/frenchforest/issue/FRE-340) · [FRE-341](https://linear.app/frenchforest/issue/FRE-341) | FRE-214 ratified 2026-05-08; implementation deferred per audit §8.7 |
| **E** ✅ FRE-213 | Identity & write surface | Seshat owner identity (ADR-0052 ✅ amended); protected agent write dir; Wave E follow-ups | [FRE-213](https://linear.app/frenchforest/issue/FRE-213) ✅ · [FRE-227](https://linear.app/frenchforest/issue/FRE-227) ⏸ · [FRE-342](https://linear.app/frenchforest/issue/FRE-342) · [FRE-343](https://linear.app/frenchforest/issue/FRE-343) · [FRE-344](https://linear.app/frenchforest/issue/FRE-344) · [FRE-345](https://linear.app/frenchforest/issue/FRE-345) | FRE-227 paused → Backlog 2026-05-09 (conflates Need 1 wiki + Need 2 continuity; see research note + FRE-346 audit); FRE-342 HIGH needs approval |
| **F** | Self-improvement | Self-updating skills phase 2 (ADR + impl); adaptive self-query arch; trigger effectiveness analysis; **missing-skill feedback loop (FRE-328)** | [FRE-226](https://linear.app/frenchforest/issue/FRE-226) · [FRE-258](https://linear.app/frenchforest/issue/FRE-258) · [FRE-234](https://linear.app/frenchforest/issue/FRE-234) · [FRE-328](https://linear.app/frenchforest/issue/FRE-328) | FRE-226 needs FRE-227; FRE-258 Tier-1 Opus; **FRE-328 Phase 1 unblocked, Phase 3 needs FRE-227** |
| **G** | Cleanups & gates | Delete legacy tool code; flip graph_quality gate; feedback_history retention; budget auto-tuning (parked) | [FRE-265](https://linear.app/frenchforest/issue/FRE-265) · [FRE-299](https://linear.app/frenchforest/issue/FRE-299) · [FRE-314](https://linear.app/frenchforest/issue/FRE-314) · [FRE-311](https://linear.app/frenchforest/issue/FRE-311) | FRE-265 gate ≥ 2026-05-12; FRE-311 parked on FRE-302 |
| **H** | Memory / context value | Recall L2; Recall L3 LLM-judge; Context Gap Score; geolocation memory | [FRE-178](https://linear.app/frenchforest/issue/FRE-178) · [FRE-179](https://linear.app/frenchforest/issue/FRE-179) · [FRE-180](https://linear.app/frenchforest/issue/FRE-180) · [FRE-230](https://linear.app/frenchforest/issue/FRE-230) | FRE-178 → 179 → 180 chain |
| **I** | User feedback + meta-learning | PWA thumbs feedback; Feedback Channel Phase 3; Phase 4 eval | [FRE-267](https://linear.app/frenchforest/issue/FRE-267) · [FRE-183](https://linear.app/frenchforest/issue/FRE-183) · [FRE-184](https://linear.app/frenchforest/issue/FRE-184) | — |
| **J** *(new)* | Eval methodology hardening | Fix `es_first_call_correct_rate` `or`/`and` bug; re-run model_decided cells post-router-fix; split router-only vs end-to-end metrics; ES polling vs sleep; ES pagination past 500; expand prompt set (ambiguous/negative-control/adversarial); ADR-0066 D2 threshold monitor | [FRE-329](https://linear.app/frenchforest/issue/FRE-329) · [FRE-331](https://linear.app/frenchforest/issue/FRE-331) · [FRE-330](https://linear.app/frenchforest/issue/FRE-330) · [FRE-332](https://linear.app/frenchforest/issue/FRE-332) · [FRE-333](https://linear.app/frenchforest/issue/FRE-333) · [FRE-334](https://linear.app/frenchforest/issue/FRE-334) · [FRE-335](https://linear.app/frenchforest/issue/FRE-335) | **Sequence: 329 → 331 → 330 → 334 → 332/333 (parallel) → 335.** Blocking: ADR-0066's D2 threshold trigger cannot fire until 335 lands; any future routing-mode decision needs 329-331 data. |

---

## Wave J — Execution Order (skill-routing eval methodology)

```
FRE-329  (analysis OR/AND bug fix + re-analyse 2026-05-07 data)        ← start here, no deps
   │
   ▼
FRE-331  (router-only vs end-to-end metric split + ground-truth labels)  ← needs 329's analysis script
   │
   ▼
FRE-330  (re-run cloud-/local-model-decided cells post-router-fix)       ← needs 331's metrics + 329's bug fix
   │
   ▼
FRE-334  (expand prompt set: ambiguous + negative-control + adversarial) ← needs 331's ground-truth schema
   │      can run in parallel with:
   ├──► FRE-332  (ES polling instead of 5s sleep)         ← independent
   ├──► FRE-333  (ES pagination past size=500)            ← parallel with 332, same file
   │
   ▼
FRE-335  (Captain's Log p95 monitor — ADR-0066 D2 trigger)               ← needs 329 + 331 producing trustworthy p95
```

**Open items not yet ticketed (low priority, plan-level notes):**
- *Add fault-injection cells* (empty router output, wrong injected skill, missing skill doc, stale skill doc) — file when ADR-0066 D4 is reopened or a regression slips through Wave J
- *Multi-turn + adversarial prompt families as a separate eval* — out of scope for routing; belongs in a session-memory eval (likely Wave H or after FRE-180)
- *Profile confound reduction* (local.yaml vs cloud.yaml differ in cost limits + delegation, not just primary model) — note in any future cross-profile claim; orthogonal change

---

## Needs Approval

| Work Item | Notes |
|-----------|-------|
| **FRE-350: Post-deploy reflection-surfacing eval (FRE-348 follow-up)** | [FRE-350](https://linear.app/frenchforest/issue/FRE-350) — Tier-1:Opus. Earliest start ≥ 2026-05-24 (2 weeks of usage data). Decides keep/tune/kill on reflection surfacing |
| **FRE-349: Surface actionable Insights to agent (G3)** | [FRE-349](https://linear.app/frenchforest/issue/FRE-349) — Tier-1:Opus. Likely shares ADR + infra with FRE-348 |
| **FRE-355: read primitive log-file tailing** | [FRE-355](https://linear.app/frenchforest/issue/FRE-355) — Tier-2:Sonnet. `current.jsonl` (19 MB) exceeds 10 MB cap; design decision needed (→ ADR-0068 D6) |
| **FRE-356: Write self-telemetry.md skill doc** | [FRE-356](https://linear.app/frenchforest/issue/FRE-356) — Tier-3:Haiku. Unblocked (FRE-354 ✅ PR #30, FRE-353 ✅ PR #31) |
| Mermaid chart rendering in chat UI | [FRE-315](https://linear.app/frenchforest/issue/FRE-315) canonical — FRE-316/317/318 closed as duplicates 2026-05-06 |

---

## Key Dependencies

```
FRE-213 (owner identity) → FRE-227 (write dir, paused) ← FRE-346 (continuity audit unblocks re-scope) → FRE-226 (self-updating skills) + FRE-328-Phase-3 (auto-author skill)
FRE-178 (Recall L2) → FRE-179 (L3 judge) → FRE-180 (context gap score)
FRE-214 (arch review) ✅ ratified 2026-05-08 → FRE-238 / FRE-240 / FRE-241 / FRE-236 + FRE-336 + FRE-338-341 unblocked (FRE-217 closed dup)
  Track 2a (endpoint abstraction) → Track 2b (compose unification + tunnel mode) → Track 3 (test parity / FRE-336)
FRE-265 (legacy delete) — calendar gate ≥ 2026-05-12
FRE-351 ✅ + FRE-352 ✅ (PR #30) → FRE-353 ✅ (PR #31) → FRE-354 ✅ (PR #30) → FRE-356 (self-telemetry.md, unblocked); FRE-355 (read primitive) independent
FRE-326 (consolidation gates) — telemetry gate ≥ 2026-05-13
FRE-311 (budget auto-tuning) — parked until FRE-302 (ADR-0065) lands
FRE-329 → FRE-331 → FRE-330 → FRE-334 → FRE-335   (Wave J chain — see diagram above)
ADR-0066 D2 trigger (auto switch hybrid → model_decided) blocked on FRE-335
```

---

## Recently Completed

| Item | Date | Summary |
|------|------|---------|
| **FRE-353: ES agent-logs-* index template reconciliation (ADR-0068 D4)** | 2026-05-10 | Deleted 4 dead field declarations (`model_name`, `tokens_used`, `input_tokens`, `output_tokens`). Added 8 explicit typed mappings for actually-emitted fields (`model`, `model_id`, `endpoint`, `api_type`, `prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cache_creation_input_tokens`). Fixed 3 type mismatches (`total_tokens` int→long, `latency_ms` float→long, `cost_usd` float→double). PR #31. |
| **FRE-354: Fix query-elasticsearch.md skill doc** | 2026-05-10 | Replaced impossible `from personal_agent.telemetry.metrics import ...` (ImportError in sandbox) with `bash curl` snippets for captures and reflections. In PR #30. |
| **FRE-352: Step-level emit → llm_step_completed** | 2026-05-10 | Added `LLM_STEP_COMPLETED = "llm_step_completed"` to `telemetry/events.py`; executor step emit renamed from `model_call_completed` to prevent ES payload collision. 3 unit tests. In PR #30. |
| **FRE-351: Cloud emit field parity** | 2026-05-10 | `litellm_request_complete` gains `completion_tokens`, `latency_ms` (int ms), `total_tokens`, `endpoint`; `cache_creation_input_tokens` added. Backward-compat double-write for `tokens`/`elapsed_s`/`cache_write_tokens`. 6 unit tests. In PR #30. |
| **FRE-258: Adaptive self-telemetry ADR (ADR-0068)** | 2026-05-10 | Read-only audit (3 Explore agents) reframed the original diagnosis. Root cause of 24-/50-call loops was tool-design stasis, already resolved by FRE-261 primitives + skill docs. ADR-0068 ratifies primitives+skills as canonical, retires `self_telemetry_query` / `query_elasticsearch` via FRE-265 with no replacement. 6 follow-up tickets filed (FRE-351..356). |
| **FRE-348: Reflection surfacing in context assembly (FRE-346 G2)** | 2026-05-10 | PR #29 merged + ADR-0067. New `captains_log/recall.py` queries past reflections and `request_gateway/context.py` injects up to 3 relevant ones as a system message between memory and recall_controller. Selection: 14-day recency × `seen_count >= 2` (or has `failure_path.fix_what`) × capitalized-hint relevance × skip approved entries. Anti-thrash via prose framing ("signals, not directives") + `→ tracked as FRE-XXX` marker. Failure-mode: every error path returns `[]` silently. Four `AGENT_REFLECTION_RECALL_*` settings; kill-switch defaults true. 12 unit tests with stubbed ES; 30/30 captains_log+second_brain pass; no context_assembly regressions. Eval deferred to FRE-350 (post-deploy 2-week window). |
| **FRE-347: session_summary generation (FRE-346 G1)** | 2026-05-09 | New `second_brain/session_summary.py` populates `SessionNode.session_summary` on every consolidation pass. Uses captains_log model role + budget cap; returns None on any failure (never blocks consolidation). New settings flag `AGENT_SESSION_SUMMARY_ENABLED` (default true). 10 unit tests with mocked LLM client (cloud + local + budget-denied + timeout + edge cases). Unblocks UC-1/2/5 from FRE-346. Drive-by: removed pre-existing dead `entity_counts` block in `_consolidate_sessions`. |
| **FRE-346: Cross-session continuity audit** | 2026-05-09 | Read-only audit of memory + Captain's Log + Insights + context assembly + brainstem (3 parallel Explore agents). Verdict: capture is rich; gaps are in **synthesis** (`SessionNode.session_summary` always None — G1) and **surfacing** (Captain's Log + Insights never re-read by agent — G2/G3). Filed 3 followups: FRE-347 HIGH/Sonnet (G1), FRE-348 MEDIUM/Opus (G2), FRE-349 MEDIUM/Opus (G3). G4/G5 noted as observations only. Recommendation: keep FRE-227 paused until FRE-347 ships. Document: `docs/research/2026-05-09-cross-session-continuity-audit.md`. |
| **FRE-213: Seshat owner identity primitive (ADR-0052 amended)** | 2026-05-09 | Owner identity primitive for single-owner / multi-user CF Access deployment. Deliverables: `AGENT_OWNER_NAME`/`AGENT_AGENT_ID` config; `MemoryService.bootstrap_owner_identity()` (user_id anchor, never name-match); `get_or_provision_user_person()` lazy provisioning for non-owner users; `get_owner_stanza()` per-turn stanza injected into system prompt; `display_name` threaded from `RequestUser` → `ctx`; extraction prompt rule #1 clarified. ADR-0052 amended in place (single-owner-multi-user, drops name-match adoption, harness-vs-extracted disambiguation invariant). Research note `docs/research/2026-05-09-graph-identity-multi-user-patterns.md`. 26 new tests. Wave E follow-ups filed: FRE-342 (dedup hardening, HIGH), FRE-343 (personal time-window retrieval), FRE-344 (display_name seeding), FRE-345 (admin/non-admin ADR placeholder). |
| **FRE-214: VPS topology audit + ADR-0045 amendment + Wave D plans** | 2026-05-09 | Architecture review of full-harness-on-VPS deployment. Verdict: **ratify** (driver = ADR-0048 mobile UI, not ADR-0044 profiles). Deliverables on branch `fre-214-vps-topology-audit`: 30-row parity matrix, 7 deviations logged, ADR-0045 amended in-place, three Sonnet-ready implementation plans (Track 2a endpoint abstraction; Track 2b compose unification + opt-in tunnel mode + sync runbook; Track 3 test parity / FRE-336 closure with fixture-based MLX↔llama.cpp parity). Filed 4 follow-up tickets (FRE-338/339/340/341). FRE-217 closed as duplicate. **Implementation deferred** until backlog reduction per audit §8.7. |
| **FRE-225: Egress domain guard** | 2026-05-08 | DomainGuard in security.py: URLhaus feed, disk cache (TTL 1h), bundled fallback. GuardMode off/blocklist/allowlist. Wired into fetch_url_executor; blocked_url WARNING event emitted. AGENT_URL_GUARD_MODE + AGENT_URL_GUARD_ALLOWLIST settings. 19 unit tests. |
| **Wave B (FRE-300, FRE-301, FRE-319, FRE-269)** | 2026-05-08 | FRE-300: warning allowlist (tool_iteration_limit_reached) wired into error monitor ES query. FRE-301: hit_iteration_limit signal in Captain's Log reflection + DSPy nudge for cap-raise proposal. FRE-319: cloud model config drift fixed (sub_agent context_length 16384→32768, reasoning_heavy ID). FRE-269: 19 wrong-prefix vars fixed in .env.example; SKILL ROUTING section added. |
| **Wave A complete (FRE-309, FRE-185, FRE-189, FRE-320, FRE-321, FRE-312, FRE-308)** | 2026-05-08 | FRE-309: workspace-scope label fallback in LinearClient. FRE-185/189/320/321/312/308: all no-ops — already resolved by prior work. |
| **FRE-309: Linear label lookup workspace fallback** | 2026-05-08 | `_label_id()` now falls back to workspace-scope `issueLabels` query when team-scoped filter misses the label (e.g. workspace-level "PersonalAgent"). One extra round-trip per process lifetime, cached. Clear error message includes "workspace scope". 4 new tests. |
| **FRE-335: Skill routing threshold monitor (ADR-0066 D2)** | 2026-05-08 | `insights/skill_routing_threshold_monitor.py` — daily ES p95 query on `skill_index_assembled.injected_chars`, rolling state file, idempotent Linear `Needs Approval` ticket after 2 consecutive days over 6,000-token threshold. `AGENT_SKILL_INDEX_P95_TOKEN_THRESHOLD` env var. 14 unit tests. Wired into `BrainstemScheduler` lifecycle loop. |
| **FRE-332 + FRE-333: ES polling + pagination in eval harness** | 2026-05-08 | Replaced fixed `asyncio.sleep(5)` with `_wait_for_trace_complete()` terminal-event poller (30s hard-timeout, 0.5s interval). Replaced `size=500` single-shot fetch with `search_after` pagination (hard cap 10,000). `--es-wait-seconds` deprecated (honoured as hard-timeout). 12 unit tests. |
| **ADR-0066: Skill Routing Defaults + Threshold + Feedback Loop** | 2026-05-07 | Locks `hybrid` as default for both profiles. Defines p95 threshold (6,000 tokens) for switching to `model_decided`. Documents `missing_skill_requested` feedback loop (→ FRE-328). 5 decisions D1–D5. Eval data: 6 cells × 10 prompts; 100% es_first_call_correct, 0% iter_limit (caveat: see FRE-329 — analysis bug means numbers may be partly inflated). |
| **fix(skills): skill_routing budget role + narrow exception** | 2026-05-07 | Phase D eval revealed every routing call returned `[]` silently — root cause: `factory.get_llm_client_for_key()` defaulted `budget_role="skill_routing"` but `budget.yaml` never declared it. Cost gate raised KeyError on every reservation, swallowed by `route_skills()`. Fix: declared `skill_routing` role + caps ($0.10/d, $0.50/w user-confirmed), narrowed `except` to re-raise misconfiguration. Verified: routing returns 3 skills in 1.2s post-fix. Commit `178f664`. |
| Skill routing Phase D: eval harness + per-request override | 2026-05-06 | 6-cell matrix runnable end-to-end; per-request `skill_routing_mode` override on `/chat` so cells run without gateway restarts. Run cmd: `ENV=cloud make eval-skill-routing-cloud RUN=<id>`. |
| Skill routing Phase C: separate routing model | 2026-05-06 | `skill_routing_model_key` (default: claude_haiku); `get_llm_client_for_key()`; `route_skills()` pre-flight call; independent of primary agent path; `ctx.skill_routing_done` prevents re-fire. PR #23. |
| Skill routing Phase B: skill index + read_skill + hybrid | 2026-05-06 | `read_skill` tool (model pulls full doc on demand); compact 280-tok index; hybrid routing mode; dedup via `ctx.loaded_skills`; sub-agent inheritance; post-exec hint. PR #22 (also includes B.5 guards). |
| Skill routing Phase A: frontmatter auto-discovery | 2026-05-06 | Replaced hardcoded `_SKILL_FILES`/`_KEYWORD_ROUTES` with glob + YAML frontmatter. 14 skill docs self-describing. Natural-language ES keywords ("show me logs", "check your logs"). Contract tests. PR #20. |
| FRE-327: Neo4j direct Cypher skill doc | 2026-05-06 | `docs/skills/neo4j-direct.md` + keyword route in `skills.py`. Agent self-diagnoses Neo4j in ≤6 calls. PR #19. |
| FRE-325: Remove brainstem polling loop | 2026-05-06 | Deleted `_monitoring_loop` / `system.idle` path; consolidation now purely event-driven. PR #18. |
| FRE-323 + FRE-324: Memory recovery | 2026-05-05 | Cypher `ON CREATE SET` fix (PR #16); synthesis nudge after tool results (PR #17). Memory round-trip passing. |
| FRE-251: Within-session compression (ADR-0061) | 2026-05-01 | Head-middle-tail invariant; tool-output pre-pass; triggered by token budget threshold. PR #10. |
| FRE-250: Knowledge Graph Quality (ADR-0060) | 2026-04-30 | Tier reranking, decay, Streams 6+8 closed, Phase 2 governance flag-gated. |
| FRE-261: Primitive tools + sandbox (ADR-0063 P2) | 2026-04-27 | `bash`, `read`, `write`, `run_python` sandbox, AG-UI approval round-trip, PWA ApprovalModal. |
| FRE-249: Context Quality (ADR-0059) | 2026-04-27 | Compaction quality detection, full feedback loop. |
| FRE-235 + FRE-268 + FRE-229: PWA + identity + visibility | 2026-04-26 | Session persistence, CF Access scoping, Neo4j visibility (public/group/private). |
| FRE-263: Deprecate 8 legacy tools (ADR-0063 P4) | 2026-04-28 | `AGENT_LEGACY_TOOLS_ENABLED=false`. 2-week window ends 2026-05-12 → FRE-265. |

*Pre-April 2026 foundation (Redesign v2 Slices 1-3, Seshat v2, Event Bus, KG Freshness, Context Intelligence, Proactive Memory) — see `docs/plans/completed/` and ADRs 0028–0060.*

---

## Active ADRs

| ADR | Title | Status |
|-----|-------|--------|
| **0066** | **Skill Routing Defaults + Threshold + Feedback Loop** | **Accepted 2026-05-07; D1+D3 implemented; D2 monitor → FRE-335; D4 fixed at `178f664`; D5 noted** |
| 0065 | Cost Check Gate — Atomic Reservation | Accepted + Implemented (FRE-302/303/304/305/306/307); follow-up FRE-311 parked |
| 0063 | Primitive Tools & Action-Boundary Governance | In progress — FRE-265 (delete) pending gate ≥ 2026-05-12 |
| 0061 | Within-Session Progressive Context Compression | Accepted + Implemented (FRE-251) |
| 0060 | Knowledge Graph Quality Stream | Accepted + Implemented; Phase 2 flag-gated |
| 0052 | Seshat Owner Identity Primitive | **Accepted (amended 2026-05-09) + Implemented (FRE-213)** — single-owner multi-user; user_id anchor; operator stanza; Wave E follow-ups FRE-342/343/344/345 filed |
| 0041 | Event Bus — Redis Streams | Accepted; Phases 1–4 live |
| 0040 | Linear as Async Feedback Channel | Accepted; Phases 1–2 live; Phase 3 → Wave I |
| 0039 | Proactive Memory | Accepted (MVP); EVAL A/B numbers pending |

*Full ADR list: `docs/architecture_decisions/`*

---

## How This File Works

- **Linear is the task tracker** — this file tracks priorities and sequencing only.
- **Next task is always Wave A row 1** until Wave A closes, then Wave B row 1, etc. (Wave J runs in parallel, not after I.)
- **Update after every ship**: mark item done, move to Recently Completed, bump Last updated.
- **Specs** → `docs/specs/` · **ADRs** → `docs/architecture_decisions/` · **Session plans** → `docs/superpowers/plans/`
