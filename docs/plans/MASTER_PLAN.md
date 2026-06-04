# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-06-04 (master) — **✅ FRE-469 SHIPPED + VERIFIED (PR #154, `424c27b`). `_TOOL_INTENT_PATTERNS` extended with build/artifact noun alternation; priority-6 (tool_use), cannot override higher types. 3115 tests pass. Post-deploy: `intent_classified` → `task_type=tool_use, signals=['tool_intent_pattern']` confirmed live. Drive-by: `get_location_tool` added to skill contract fixture (FRE-230 omission). FRE-469 → Done. Turn Reliability Hardening: 468 ✅ + 469 ✅ + 473 ✅.** Previously: **✅ FRE-473 SHIPPED + VERIFIED (PR #153, `72910ea`). `_decorated_anthropic_copy()` deep-copies messages+tools before decoration; `respond()` sends wire copy, persisted session history stays provider-neutral. 14 tests pass incl. LiteLLM transform contract test. Post-deploy: `cache_read_tokens=17,772` unchanged vs FRE-468 baseline — no §D2 regression. FRE-473 → Done.** ✅ FRE-468 SHIPPED + VERIFIED (PRs #151+#152). Turn Reliability Hardening cluster: 468 ✅ + 473 ✅ complete. Remaining: FRE-469/470/471/472 (Needs Approval).** Previously: **🎉 FRE-230 COMPLETE ✅ (PRs #147+#148+#149, `aedebe4`). Two-gate location: operator (`AGENT_LOCATION_ENABLED`, default off) + per-user (`:Person.location_consent_enabled`). Backend: `tools/location.py`, `MemoryService` consent/location methods, `GET`/`PATCH /api/v1/preferences/location`, WGS84 bounds, precision validator. PWA: `LocationConsent` drawer toggle (hidden when operator gate off), `useLocation` hook with generation-counter consent-withdrawal guard (coordinates never transmitted after toggle-off), iOS `enableHighAccuracy` + IANA timezone. Path fixed `/api/preferences/location` → `/api/v1/preferences/location`. #149 fixed pre-existing `trace_id` stray in TurnStatusBar test fixture (tsc clean). Deployed gateway + PWA. Verified: `/api/v1/preferences/location` → `feature_enabled:false`. FRE-230 → Done.** ADR-0084 + Seshat Pedagogical Architecture M1 COMPLETE ✅ (PR #146, `af3e86e`). ADR-0084 accepted (Socratic tutor layer, result-type taxonomy, delegation policy, 5-layer architecture, D6 supersedes ADR-0082 routing question). PEDAGOGICAL_NORTH_STAR.md + research origin doc committed. FRE-447/448/449 → Done. ADR number drift fixed in Linear (FRE-447/432/project/M1 milestone all corrected ADR-0083→0084). ADR-0082 status updated to Partially Superseded. FRE-432 reconceived → Needs Approval; FRE-450 tracks scope revision.** FRE-384 ✅ + FRE-383 ✅ SHIPPED + DEPLOYED (PRs #143/#144, `a7f1903`). FRE-384: `notes_search` 100% broken on every call — root cause `asyncpg.AmbiguousParameterError` on bare `:tag_filter IS NULL`; fix = `CAST(:tag_filter AS text[]) IS NULL` (1 line). Also fixed `init.sql` FK ordering (`users` table missing before `artifacts`). Integration test added (real asyncpg driver). Verified live: `notes_search` executes without error. FRE-383: anti-fabrication rule added to `_TOOL_RULES` (prepended every tool-enabled turn); 4 regression tests. CI fix: baseline updated 1857→2176 (+319 chars, intentional). Both → Done.** 🎉 FRE-400 EPIC COMPLETE ✅ — all 3 PRs shipped: PR1 (WS1 backend harness + CI), PR2 (59 Vitest component+hook tests), PR3 (4 Playwright e2e browser tests). CI now covers WS round-trips, constraint pause, Send→Stop, turn_status, RUN_ERROR→Retry headless in Chromium. CodeQL permissions finding fixed (`permissions: contents: read`). FRE-400 → Done. Next per sequence: FRE-384/383 (High bugs) → FRE-432 → FRE-397 Tier 2.** FRE-377 AC-5 soak PASSED ✅ — `agent-captains-captures-2026-06-02` maps `input_tokens/output_tokens` (integer), no legacy collision, 44 captures clean. FRE-377 closed Done.** FRE-400 PR1 SHIPPED ✅ (PR #140, merged `99b533b`): WS1 backend harness + GitHub Actions CI. `ws_harness.py` + 16 unit tests cover: event delivery/seq/DONE, constraint round-trip (+invalid/timeout), USER_CANCEL, turn_status STATE_DELTA, RUN_ERROR (+budget_denied), reconnect replay, REPLAY_GAP, eviction 4001, rate-limit/oversize/CONNECT 1008. Tier-2 real-Postgres transport e2e in `tests/integration/`. `.github/workflows/ci.yml` wired (4 jobs: backend-unit, backend-integration, lint, pwa-unit). Key finding: `asyncio.create_task` required instead of `BackgroundTasks` in TestClient (blocks synchronously). FRE-390 closed as subsumed. FRE-400 reopened to In Progress (multi-phase: PR2=PWA Vitest, PR3=Playwright pending). No deploy needed.** FRE-399 Layer 3 SHIPPED ✅ (PR #139, merge `bfe184e`): cross-tunnel SLM health monitor (ADR-0083). `observability/slm_health/` module mirrors joinability probe — probe/snapshot/cache/sink/scheduler. Brainstem scheduler wired at 5 min. `/api/inference/status` enriched with `gpu_util_pct/queue_depth/model_loaded/degrade_reason` (all null today, graceful degradation confirmed). Executor error-reason hint reads cached snapshot to convert "an error occurred" → "GPU pinned (98%)" / "model not loaded". 3031✅. Verified live: status=up, latency_ms=276, null rich fields. Layer 2 (cloud fallback) + Mac-side enrichment filed as children FRE-443/444 (Needs Approval). FRE-399 stays In Progress (multi-phase). ADR-0083 Accepted.** Next per sequence: **FRE-400** (E2E testing) → FRE-397 Tier 2. **Also today: Brand refresh (side project, PR #138→fe0feb1) — lifted navy floor, violet agent identity, vivid blue, calmer state cards; deployed to seshat-pwa.** 🎉 FRE-403 Prompt-Observability EPIC COMPLETE. FRE-409 SHIPPED ✅ (PR #137, merge `afc2c38`): P5 agent self-reflection on prompt composition. Integration gate caught a prod-blocking bug (manifest filtered on `event_type` but log source uses `event` → always "unavailable"); sent back, build fixed (commit `3782240`, dual-key), re-gated, deployed. Verified LIVE: a real post-deploy reflection named taxonomy component `skill_index` and proposed a composition change in the ADR-0058 shape. Full suite 2982✅.** EPIC P0–P5 all shipped (FRE-404/405/406/407/408/409); P6 (DSPy opt) optional/future-gate. Next per sequence: **FRE-399 Layer 2** (retry/fallback). Earlier today: **FRE-408 SHIPPED ✅ (PR #136, merge `017debd`): P4 eval attribution — per-turn prompt_identity + per-`static_prefix_hash` report section. Eval-harness-only (no deploy). Post-deploy AB-bucket AC verified-by-equivalent on 3 real ES traces (3 buckets, stats); canonical harness smoke is env-gated on a live local SLM (Mac) → see Pending Verification.** Next per sequence: **FRE-409 (P5, closes FRE-403 EPIC)**. Earlier today: **FRE-377 SHIPPED ✅ (PR #135, merge `cf0f70f`): canonicalized `TaskCapture` token fields to `input_tokens`/`output_tokens` with back-compat aliases; deployed + ES template applied; AC-1/2/3/4 verified live (fresh capture canonical, legacy replay round-trips). AC-5 1-day soak pending ~2026-06-03 → see Pending Verification.** Also swept status drift: closed FRE-433 (spike, shipped) + FRE-422 (D1, delivered+verified by the ADR-0081 chain) — In Progress now clean (only FRE-403 EPIC). **Next per sequence: FRE-408 (P4, build).** Earlier today: **🎉 Protect-live-rollout cluster COMPLETE (3/3 shipped + deployed + verified). FRE-436 SHIPPED ✅ (PR #134, merge `f4ff9ee`): `/chat` now persists + honours `execution_profile` server-authoritatively (ADR-0079) — fixes cloud sessions displaying as local. Verified on real turns: new session persists `cloud`; follow-up `profile=local` correctly ignored. Full suite 2912✅.** Cluster: FRE-440 (config pin, PR #132) · FRE-437 (cache-tier cost persistence + migration 0008, PR #133) · FRE-436 (profile attribution, PR #134) — all merged, gateway rebuilt + verified end-to-end. Known limitation: pre-fix sessions persisted as `local` can't be backfilled. Needs-Approval table drift fixed (added FRE-441/442). **Still Needs Approval (owner's call): FRE-435 (memory research — next big initiative), 438 (PWA notes), 439 (rating UX), 441 (A/B eval view), 442 (always-references).** Earlier today: **🎉 ADR-0081 cache chain COMPLETE + rolled out. FRE-434 (D2/D3 frozen append-only layout + cache-aware scheduler) shipped (PRs #129/#130), deployed, A/B-verified, and enabled in prod.** Local cross-turn KV reuse **0 → ~8,110+ on 12/13 turns** (prefill ~10–12× faster) — the headline win; cloud reuse **13,916 → median 19,542** (19/20). FRE-407 quality flat (head 2.08 vs frozen 1.95 — noise on small n, no regression) + owner-confirmed improved. Full chain: D1 ✅ → D4 ✅ → D2/D3 ✅. **Owner debrief filed 6 follow-ups (all Needs Approval): FRE-435 (memory-recall research — next big FRE-433-style initiative), FRE-436 (cloud-shows-as-local bug), FRE-437 (cache pricing in cost), FRE-438 (PWA notes access/render/promote-delete), FRE-439 (rating UX: 0=red + technical-error category), FRE-440 (pin frozen-layout default — `.env` fragility).** `no_think` suffix retired (default off). Previously: **ADR-0081 §D2/D3 settled — implementation-grade, merged (PR #128).** Frozen append-only layout (volatile rides + persists with its user turn → strict forward extension) + cache-aware compaction *scheduler* (cost-optimal sawtooth reset, EOQ run-length `L*=√(2R/c)`, backend-asymmetric). All six FRE-434 decisions settled (freeze both recall+skill-bodies; cost/quality trigger replaces 0.65/0.85; `within_session_compression` *becomes* the scheduled persisted reset; Codex tail-arm **subsumed**, branch closed unmerged). Gated behind new `cache_frozen_layout_enabled` (default off). Codex 2-round review fixed the `frozen_narrative` role (assistant, not system — role-fix drops non-leading system msgs), the EOQ math, and the `sanitise_messages` byte-fragility hazard. **Next: build implements [FRE-434](https://linear.app/frenchforest/issue/FRE-434) (Approved) from the ADR** — byte-identity invariant is the #1 risk; verify with the FRE-433 A/B harness. Previously: **FRE-433 spike DONE — cross-turn KV reuse root-caused to gateway HEAD-LAYOUT** (mmproj / slot-eviction / TTL / telemetry / spec-decode all **refuted**). End-to-end A/B (harness `scripts/eval/fre433_cache_ab/`, both backends): the volatility-gradient relayout (move volatile out of the system head) fixes **cloud** (Sonnet reuse 13.9k→17–20k, *improves* it, does not break it) but **NOT local** (stays 0) — local additionally needs **frozen append-only history** (each turn a strict forward extension). Filed **[FRE-434](https://linear.app/frenchforest/issue/FRE-434)** (ADR-0081 **D2/D3**: frozen append-only layout + cache-aware **compaction scheduler** — compress at a computed cost/quality optimum; the "and compaction" half of ADR-0081) — **Needs Approval, Tier-1** — with a design brief. Findings + A/B + brief in **PR #127**; conclusion on FRE-433. Codex's `codex/fre-433-layout-tail-arm` = validated **cloud-only** partial, **HELD** pending the D2/D3 ADR. **Next: adr session writes ADR-0081 D2/D3 → build implements FRE-434.** Previously: **ADR-0081 §D4 (skill-index split) decided + ADR-0082 (tier-aware model selection) Proposed — both merged (PRs #121/#123, #122); impl tickets FRE-431 (D4) + FRE-432 (tier-routing) are Needs Approval, awaiting owner sign-off. Recommended build sequence once approved: D4 first (cache-GREEN gate, further along), then tier-routing — they share the hot executor path.** Earlier 2026-06-01: **FRE-422 ADR-0081 D1 (volatility-gradient prompt layout) shipped + deployed (PR #120).** Pure reorder of the system-prompt assembly: STATIC tool rules → SEMI-STATIC (tool awareness, base, decomposition) → VOLATILE memory tail; a layout-invariant unit test pins `tool_prompt` before `memory_section`; gateway rebuilt + healthy. **Post-deploy eval (build worktree) finding: the `orchestrator.primary` cache gate is still RED — D1 alone cannot flip it; skill-index injection (ADR-0081 D4, skill-index split) is the remaining erosion source.** This is exactly the residual the ticket predicted. FRE-422 was auto-closed to Done by the merge automation and **reopened to In Progress** — its cache-GREEN gate transfers to a forthcoming **ADR-0081 D4** ticket (adr worktree drafting the ADR + ticket). **Next: ADR-0081 D4 (skill-index split), then FRE-427 (dead-SSE cleanup).** Previously (2026-05-31 eod): **FRE-407 rating feature fully shipped + debugged; FRE-426 ✅ complete (PRs #113–#119, all deployed).** On-device testing + a systematic-debugging session resolved **six** real issues, each root-caused not guessed: (1) widget never rendered — client never received the turn `trace_id`; now carried on `turn_status`, stamped on DONE (PR #113); (2) cost meter always 0.00 incl. Sonnet — `cost_usd` never put on the `LLMResponse`; added + populated, **live-verified $0.0345 on a real cloud turn** (PR #113); (3) widget only on the live turn — hydrated history not marked `complete` (PR #114); (4) widget invisible until hover — hover-reveal CSS (`opacity-0 group-hover`); made persistent (PR #115); (5) no rated-vs-default indicator — unrated now faint, rated solid (PR #116); (6) rated state lost on reload — **FRE-426 rating hydration**: messages endpoint joins `user-turn-ratings-*` by `trace_id` (assistant-only, role-guarded), PWA seeds `TurnRating` (PRs #117/#118). **FRE-426 completed** with context+cost hydration: `GET /sessions/{id}` returns `context_tokens`/`context_max`/`cost_usd`, PWA `seedTurnStatus` populates the bar on mount/switch — verified real session hydrates context≈3193, cost $0.335 (PR #119, SW→v19). **Key debug finding:** the 70–120s "cycling" is **NOT** an over-cycling bug — simple turns make 1 call/0 tools; slow turns are *legitimate* multi-`bash` tasks (e.g. "give me metrics" → 12 ES queries) where each iteration re-prefills the growing context ~15s. That's **FRE-422** (cache-aware layout), now strongly justified. **Next: FRE-422** (the real latency fix), then FRE-427 (dead-SSE cleanup). FRE-426 device-glance (switch sessions → meters+ratings persist) pending. Previously: **FRE-407 acute fixes ✅ (PR #113, deployed + live-verified)** — on-device test surfaced two real bugs, both fixed: (1) **rating widget never rendered** — the client never received the turn's `trace_id` (TEXT_DELTA/DONE carry none); now carried on the `turn_status` STATE_DELTA, stashed client-side, stamped on DONE with `complete` set unconditionally. (2) **cost meter always 0.00 incl. Sonnet** — `cost_usd` was computed + written to Postgres but never put on the returned response; added to the `LLMResponse` TypedDict + populated on the cloud path. **Verified with a real cloud turn**: `turn_status` now carries `trace_id` + `turn_cost_usd=0.0345`. SW→v14. Two follow-up tickets filed (Approved): **FRE-426** (status surfaces server-authoritative — hydrate context+cost on session mount, fixes the switch-visibility loss) + **FRE-427** (remove dead pre-WS SSE code + extend joinability hook to transport envelopes — the `DONE`-missing-`trace_id` bug bypassed the log/bus/Cypher-only hook). **Next: device-confirm the widget renders → implement FRE-426 → FRE-427 → FRE-422.** Lesson logged: no PWA feature is "done" until driven through a real turn (FRE-407 was called done after backend-only verification). Previously: **FRE-407 P3 ✅ DONE (deployed, PR #112 + `7c28ea7`)** — per-turn 0–3 value rating, joined to PromptIdentity on `trace_id` → the human-eval instrument enchained ahead of FRE-422/ADR-0081 D1. Backend (endpoint + ownership scoping + identity join + dual-write + `user.turn_rated` bus event + `user-turn-ratings-*` template/90d ILM + Insights `detect_low_rating_sessions`) + PWA (4-segment `TurnRating` meter, trace/`complete`/`sessionId` threading, SW→v13). **Default = 2 ("ok"), no auto-submit; metric imputes un-rated completed turns as 2** (cardinality-of-trace_id denominator per callsite). codex pre-impl review caught 2 blockers + 7 edges; architect live-ES review caught a prod-only `event.keyword`→`event_type` bug invisible to mocked tests (regression-pinned). Vitest added as the PWA test framework. Process note: a Sonnet impl subagent pushed `7c28ea7` to main against instructions — code was already reviewed-good + gates green, so kept; subagent git scope tightened going forward. **Next: device-verify the rating control, then FRE-422 (D1 layout reorder) behind the baseline.** Previously: **FRE-406 P2 ✅ DONE** — ES template prompt_* explicit mapping, cache-erosion monitor (Jaccard ≥ 0.9, `make cache-erosion-status`), Kibana saved objects (per-callsite cost breakdown + hash stability view). Live result: orchestrator.primary jaccard=0.200 [ERODED] — confirms future-gate already crossed (cache-hit ≈ 0% on local SLM due to prefix churn). Composer-redesign follow-up ticket needed. FRE-403 EPIC stays In Progress (P3–P5 remain). Previously: **FRE-374 purge→replay ✅** — 14,213 entities + 11,984 relationships, 0 errors. Probe results: recall 0/15 empty/misleading; empty-desc 24.6% (was 42%); redundant edge pairs 4.4% (was 9.3%). **FRE-376 ✅** — ADR-0074 → Accepted; retroactive 6/6 audit green; three probe-tool bugs fixed (`712222e`+`f0478cc`). **FRE-412 ✅** — dedup threshold 0.92 + ALL_CAPS guard. **Next: FRE-406 P2 (prompt cost/cache attribution).**

---

## Current State

Waves A ✅ B ✅ C ✅ E ✅ J ✅ complete. Wave H: FRE-375/374/376 ✅ — FRE-377 next, FRE-381 pending approval. Wave I (FRE-403 EPIC) ✅ COMPLETE — P0–P5 (FRE-404–409) all shipped+verified 2026-06-02; P6 (DSPy opt) optional. **ADR-0081 cache chain COMPLETE:** D1 ✅ (FRE-422) → D4 ✅ (FRE-431) → D2/D3 ✅ (FRE-434, PRs #129/#130) — frozen append-only layout + cache-aware scheduler shipped, A/B-verified (local cross-turn reuse 0 → 8,110+; cloud 13,916 → 19,542; quality flat), **enabled in prod**. FRE-433 spike root-caused it to gateway head-layout. Follow-ups (Needs Approval): FRE-435 (memory research), FRE-436/437/438/439/440. ADR-0074 fully Accepted. ADR-0075/0076/0077/0079/0080 Implemented; ADR-0082 (tier-routing) Proposed → FRE-432 Approved.

Wave D implementation (endpoint abstraction, compose unification, test parity) remains deferred per FRE-214 audit §8.7.

---

## Active Design Threads

Four threads carved from the FRE-389 on-device review (2026-05-28). All **Approved**. FRE-398 (bubble-up errors) ✅ Done.

| Thread | Issue | Scope |
|--------|-------|-------|
| **Dynamic artifacts** | [FRE-397](https://linear.app/frenchforest/issue/FRE-397) | Diagrams now → interactive later (Tier 1 SVG → Tier 2 sandboxed JS → Tier 3 JSX). |
| **Adaptive limits & error recovery** | [FRE-399](https://linear.app/frenchforest/issue/FRE-399) | ~~524 root cause fixed (`cbd6f45`).~~ Layer 3 ✅ (ADR-0083, PR #139): cross-tunnel SLM health monitor, enriched `/api/inference/status`, executor error-reason hint. Children: FRE-444 (Mac-side enrichment), FRE-443 (L2 cloud fallback — gate: genuine failure observed), FRE-445 (dynamic thresholds, coordinate FRE-391). |
| **E2E testing (transport/UI/error)** | [FRE-400](https://linear.app/frenchforest/issue/FRE-400) ✅ Done | PR1 ✅ (PR #140): WS harness + 16 tests + CI. PR2 ✅ (PR #141): 59 Vitest component+hook tests. PR3 ✅ (PR #142): 4 Playwright e2e browser tests. FRE-390 closed (subsumed). |
| **Planner-executor split** | [FRE-401](https://linear.app/frenchforest/issue/FRE-401) | Reasoning model plans; subagents execute in isolated context. ADR required before implementation. |

**Recommended order**: FRE-434 ✅ → FRE-377 ✅ → FRE-408 ✅ → FRE-409 ✅ → FRE-399 L3 ✅ → FRE-400 ✅ (3/3 PRs) → **[FRE-384](https://linear.app/frenchforest/issue/FRE-384) / [FRE-383](https://linear.app/frenchforest/issue/FRE-383) (next — High bugs)** → FRE-432 → FRE-397 Tier 2.

**Standalone (Approved)**: FRE-394 (PWA SW dead code), FRE-395 (PWA ESLint).

---

## Upcoming — Wave Sequence

| Wave | Theme | Status | Key Issues | Notes |
|------|-------|--------|------------|-------|
| **A** ✅ | Dev loop & hygiene | Done | FRE-309 · FRE-185/189/320/321/312/308 | Shipped 2026-05-08 |
| **B** ✅ | Self-observation | Done | FRE-301 ✅ · FRE-300 ✅ · FRE-319 ✅ · FRE-269 ✅ · FRE-326 ✅ | |
| **C** ✅ | Security | Done | FRE-225 ✅ | |
| **D** | Architecture | Planning ✅, impl deferred | FRE-214 ✅ · FRE-238 · FRE-240 · FRE-241 · FRE-236 · FRE-338–340 | Deferred per audit §8.7 |
| **E** ✅ | Identity & write surface | Done | FRE-213 ✅ · FRE-227 ✅ · FRE-371 ✅ · FRE-368 ✅ · FRE-342 ✅ · FRE-343 ✅ · FRE-344 ✅ · [FRE-369](https://linear.app/frenchforest/issue/FRE-369) (Approved) | FRE-369 uploads next. |
| **F** | Self-improvement | Partial | [FRE-328](https://linear.app/frenchforest/issue/FRE-328) 🅿️ · FRE-385 ✅ · FRE-387 ✅ · FRE-226 · FRE-234 | Gate reset 2026-05-26 → review ≥ 2026-06-09. CL 2-week promotion gate ~2026-06-09. |
| **G** | Cleanups & gates | Partial | FRE-265 ✅ · FRE-299 ✅ · FRE-337 ✅ · [FRE-314](https://linear.app/frenchforest/issue/FRE-314) · FRE-311 | FRE-311 parked on FRE-302 |
| **H** | Memory / context value | Partial | [FRE-375](https://linear.app/frenchforest/issue/FRE-375) ✅ → [FRE-374](https://linear.app/frenchforest/issue/FRE-374) ✅ → [FRE-376](https://linear.app/frenchforest/issue/FRE-376) ✅ → [FRE-377](https://linear.app/frenchforest/issue/FRE-377) ✅ (soak ~06-03) → [FRE-381](https://linear.app/frenchforest/issue/FRE-381) (Needs Approval) → FRE-178 → FRE-179 → FRE-180 · FRE-230 | FRE-377 shipped (PR #135); AC-5 1-day soak pending. |
| **I** ✅ | Prompt observability | EPIC Done | [FRE-403](https://linear.app/frenchforest/issue/FRE-403) EPIC ✅ · FRE-404–409 ✅ (P0–P5) · P6 (DSPy opt, optional) · FRE-183 · FRE-184 | **EPIC complete 2026-06-02** (P0–P5 shipped+verified). P6 optional/future-gate (≥200 rated eval turns). FRE-183/184 separate Wave I items. |
| **J** ✅ | Eval methodology hardening | Done | FRE-329–335 all shipped | |

---

## Pending Verification

- **FRE-468** ✅ DONE — post-deploy verified 2026-06-04: no Anthropic 400, `cache_read_tokens=17,772` on round 2, `cache_control_cap_enforced` never fired. Fix confirmed live.
- **FRE-473** ✅ DONE — post-deploy verified 2026-06-04: `cache_read_tokens=17,772` unchanged vs FRE-468 baseline; no §D2 regression; persisted history now provider-neutral.
- **FRE-408** ✅ DONE (owner accepted real-telemetry equivalent — 3 buckets on real ES traces). Optional Mac harness smoke remains belt-and-suspenders, not blocking.

---

## Turn Reliability Hardening (2026-06-04 incident) — Needs Approval

All five from the `cache_control 5>4` post-mortem (PR #150). FRE-468 is Urgent and first.

| Ticket | Priority | Tier | What |
|--------|----------|------|------|
| [FRE-468](https://linear.app/frenchforest/issue/FRE-468) ✅ | **Urgent** | Sonnet | **DONE** (PRs #151+#152, `6fb0d2c`) — `_strip_cache_control` + `_enforce_cache_control_cap`; 11 tests; post-mortem amended. **Deploy + verify pending.** |
| [FRE-469](https://linear.app/frenchforest/issue/FRE-469) ✅ | **High** | Sonnet | **DONE** (PR #154, `424c27b`) — `_TOOL_INTENT_PATTERNS` artifact/build extension; verified live: `task_type=tool_use, signals=['tool_intent_pattern']`. |
| [FRE-470](https://linear.app/frenchforest/issue/FRE-470) | Low | Sonnet | `bash` tool: treat exit 141 (SIGPIPE from `head`/`grep -q`) as success, not failure |
| [FRE-471](https://linear.app/frenchforest/issue/FRE-471) | Low | Sonnet | `artifact_draft`: truncate-with-warning instead of terminal hard-fail; raise plan cap toward `_DRAFT_MAX_TOKENS` |
| [FRE-472](https://linear.app/frenchforest/issue/FRE-472) | Research | Opus | `conversational` capability trap: tool-runway floor, validation-retry budget, thinking/budget interaction |

---

## Immediately Actionable (approved, no gate)

| Ticket | Priority | Tier | What |
|--------|----------|------|------|
| [FRE-384](https://linear.app/frenchforest/issue/FRE-384) ✅ | **High** | Sonnet | **DONE** (PR #143) — `CAST(:tag_filter AS text[]) IS NULL` fixes AmbiguousParameterError. `init.sql` FK order fixed. Verified live. |
| [FRE-383](https://linear.app/frenchforest/issue/FRE-383) ✅ | **High** | Sonnet | **DONE** (PR #144) — anti-fabrication rule in `_TOOL_RULES`; 4 regression tests; baseline 1857→2176. |
| [FRE-377](https://linear.app/frenchforest/issue/FRE-377) ✅ | Medium | Sonnet | **DONE** (PR #135) — canonicalize TaskCapture token fields. AC-5 soak passed 2026-06-03. |
| [FRE-369](https://linear.app/frenchforest/issue/FRE-369) | Medium | Sonnet | User-upload UX in PWA with presigned PUT to R2 (images/files in chat). Spec: ADR-0069 + ADR-0070. |
| [FRE-314](https://linear.app/frenchforest/issue/FRE-314) | Medium | Sonnet | `feedback_history/` retention policy in DataLifecycleManager. |
| [FRE-350](https://linear.app/frenchforest/issue/FRE-350) | Medium | Opus | Post-deploy reflection-surfacing eval. Gate opened 2026-05-24 — startable now. |
| [FRE-349](https://linear.app/frenchforest/issue/FRE-349) | Medium | Opus | Surface actionable Insights in agent context (G3 from FRE-346). |
| [FRE-391](https://linear.app/frenchforest/issue/FRE-391) | Medium | Opus | Dynamic `max_tokens` based on tool/task context — addresses artifact truncation root cause. |

**Protect-live-rollout cluster (approved 2026-06-02) — ✅ COMPLETE (3/3 shipped + deployed + verified):**

| Ticket | Priority | Tier | What |
|--------|----------|------|------|
| [FRE-440](https://linear.app/frenchforest/issue/FRE-440) ✅ | — | Sonnet | **DONE** (PR #132, merge `e428e6e`) — pinned frozen-layout default in tracked config; full suite 2900✅; gateway reads `True`. |
| [FRE-437](https://linear.app/frenchforest/issue/FRE-437) ✅ | — | Sonnet | **DONE** (PR #133, merge `f8de7c3`, migrated `0008`) — persists per-tier cache token counts in `api_costs`. Audit reframe: `cost_usd` was never wrong (litellm already cache-aware); gap was discarded tier counts. Verified on real cloud turn (cache_creation 8,665). |
| [FRE-436](https://linear.app/frenchforest/issue/FRE-436) ✅ | — | Sonnet | **DONE** (PR #134, merge `f4ff9ee`) — `/chat` now persists + honours `execution_profile` (server-authoritative, ADR-0079). Verified: new session persists `cloud`; follow-up `profile=local` ignored, stays cloud. |

**Calendar-gated (approved but not yet startable):**
- **FRE-328** — naming-stability data review. Gate ≥ 2026-06-09 (2 weeks clean production data with agent-noun taxonomy).
- **FRE-381** (Needs Approval) — Stage 2 consolidator decoupling. Requires ADR-0074 §I5 amendment + post-FRE-380 soak data.

---

## Needs Approval

| Ticket | Priority | Tier | What |
|--------|----------|------|------|
| [FRE-435](https://linear.app/frenchforest/issue/FRE-435) | — | Opus | **Memory-recall research initiative** — quantify KG write + retrieval quality with a deep A/B harness (FRE-433 method); explore markdown/LLM-wiki retrieval. Owner's next big research item. |
| [FRE-438](https://linear.app/frenchforest/issue/FRE-438) | — | Sonnet | **PWA notes**: access + rendered-markdown (raw toggle) + promote/delete for notes & artifacts. |
| [FRE-439](https://linear.app/frenchforest/issue/FRE-439) | — | Sonnet | **Rating UX**: 0 → red pill + distinct "technical error" rating (un-conflate from quality-0). |
| [FRE-441](https://linear.app/frenchforest/issue/FRE-441) | — | Sonnet | **Eval tooling**: side-by-side pre/post (A/B) response comparison for human quality rating. Prereq for FRE-435. |
| [FRE-442](https://linear.app/frenchforest/issue/FRE-442) | — | Sonnet | **Behavior**: agent responses always include references/citations + make references a quality signal. |
| [FRE-464](https://linear.app/frenchforest/issue/FRE-464) | — | Opus | **ADR-0081 D4-trim**: skill-index format/size minimization (Pareto routing-accuracy vs tokens; DSPy candidate). Cost-trim on the now-cached index; does *not* gate cache-GREEN. Project: *ADR-0081 Extended*. |
| [FRE-465](https://linear.app/frenchforest/issue/FRE-465) | — | Opus | **ADR-0081 D5**: tiered virtual context — cold-tier on-demand `recall_session_history` (reinject context compression dropped). Open retrieval design Qs; likely needs ADR addendum. Project: *ADR-0081 Extended*. |
| [FRE-466](https://linear.app/frenchforest/issue/FRE-466) | — | Sonnet | **ADR-0081 D6**: optional message pin — never-compress + attention-aware placement; must respect the FRE-434 byte-identity invariant. Project: *ADR-0081 Extended*. |
| [FRE-381](https://linear.app/frenchforest/issue/FRE-381) | Medium | Sonnet | **Stage 2** consolidator decoupling — invert Turn-creation vs entity-extraction dependency; add `extractor_model` to `TurnNode`. Blocked-by FRE-380 ✅ + post-soak data. |
| [FRE-390](https://linear.app/frenchforest/issue/FRE-390) ✅ | Low | Sonnet | ~~Eval harness skips transport layer~~ — **Done** (closed as subsumed by FRE-400 PR1, 2026-06-03). |
| [FRE-467](https://linear.app/frenchforest/issue/FRE-467) | — | Opus | **Spatio-temporal memory** — location as episode dimension (from FRE-230; ADR required). |
| [FRE-432](https://linear.app/frenchforest/issue/FRE-432) | — | Opus | **Tier-aware model routing** — reconceived under ADR-0084 pedagogical north star; scope revision tracked FRE-450. |
| [FRE-468](https://linear.app/frenchforest/issue/FRE-468) | **Urgent** | Sonnet | `cache_control` ≤4 clamp (see Turn Reliability Hardening section above). |
| [FRE-469](https://linear.app/frenchforest/issue/FRE-469) | **High** | Sonnet | Classifier: artifact intent routing (see Turn Reliability Hardening section above). |
| [FRE-470](https://linear.app/frenchforest/issue/FRE-470) | Low | Sonnet | SIGPIPE false-fail (see Turn Reliability Hardening section above). |
| [FRE-471](https://linear.app/frenchforest/issue/FRE-471) | Low | Sonnet | `artifact_draft` truncate-with-warning (see Turn Reliability Hardening section above). |
| [FRE-472](https://linear.app/frenchforest/issue/FRE-472) | Research | Opus | Conversational capability trap research (see Turn Reliability Hardening section above). |
| [FRE-473](https://linear.app/frenchforest/issue/FRE-473) ✅ | **High** | Sonnet | **DONE** (PR #153, `72910ea`) — `_decorated_anthropic_copy()` deep-copies before decoration; 14 tests + LiteLLM contract test; `cache_read=17,772` unchanged post-deploy. |

---

## Key Dependencies

```
FRE-375 ✅ → FRE-374 ✅ → FRE-376 ✅ → FRE-377 (Approved, unblocked)
FRE-380 ✅ → FRE-381 (Stage 2, Needs Approval; post-soak data available)
FRE-178 → FRE-179 → FRE-180  (recall L2/L3/gap chain)
FRE-214 ✅ → FRE-238/240/241/236 + FRE-338–340 (unblocked, deferred §8.7)
FRE-302 ✅ → FRE-311 (budget auto-tuning, parked pending data)
FRE-346 ✅ → FRE-347 ✅ → FRE-348 ✅ → FRE-349 (G3, unblocked)
FRE-328 capture ✅ → naming-stability gate ≥ 2026-06-09 (agent-noun taxonomy deployed)
FRE-348 ✅ → FRE-350 (reflection eval, gate opened 2026-05-24, Approved)
FRE-403 EPIC ✅: FRE-404 ✅ → FRE-405 ✅ → FRE-406 ✅ → FRE-407 ✅ → FRE-408 ✅ → FRE-409 ✅ (all Done 2026-06-02)
ADR-0081 core chain ✅: D1 (FRE-422) → D4 split (FRE-431) → D2/D3 (FRE-434, live) — followups: FRE-464 (D4-trim) · FRE-465 (D5 cold-tier) · FRE-466 (D6 pin), all Needs Approval (project: ADR-0081 Extended)
FRE-227 ✅ → FRE-226 (self-updating skills)
FRE-391 (dynamic max_tokens) — independent; addresses artifact-truncation root cause
```

---

## Recently Completed

| Item | Date | Summary |
|------|------|---------|
| **ADR-0081 D1: volatility-gradient layout ✅ (D4-gated)** | 2026-06-01 | FRE-422, PR #120, deployed. System-prompt reorder STATIC→SEMI-STATIC→VOLATILE; layout-invariant test pins `tool_prompt` before `memory_section`. **Post-deploy eval: `orchestrator.primary` cache gate still RED — blocked on D4 (skill-index split).** Ticket reopened In Progress; cache-GREEN gate transfers to a forthcoming ADR-0081 D4 ticket. |
| **FRE-426: status + cost hydration ✅** | 2026-05-31 | PRs #113–#119, deployed (SW→v19). `GET /sessions/{id}` returns `context_tokens`/`context_max`/`cost_usd`; messages endpoint joins `user-turn-ratings-*` by `trace_id` (assistant-only); PWA `seedTurnStatus` hydrates meters + ratings on mount/switch. Verified context≈3193, cost $0.335. |
| **FRE-407 P3: per-turn 0–3 rating ✅** | 2026-05-31 | PR #112 + #113. Human-eval instrument joined to PromptIdentity on `trace_id`; default=2, un-rated imputed as 2; `user-turn-ratings-*` template + 90d ILM; PWA 4-segment `TurnRating`. The quality baseline enchained ahead of FRE-422/ADR-0081 D1. |
| **FRE-406 P2: cost/cache attribution + erosion alarm ✅** | 2026-05-31 | ES template `prompt_*` explicit mapping; `make cache-erosion-status` (Jaccard ≥ 0.9); Kibana saved objects. Live result: `orchestrator.primary` jaccard=0.200 [ERODED] — confirmed cross-turn KV reuse ≈ 0, justifying ADR-0081. |
| **FRE-374 purge→replay ✅** | 2026-05-30 | 14,213 entities + 11,984 relationships, 0 errors. Probe 1/2/5/6 green. Empty-desc 24.6% (was 42%); redundant edges 4.4% (was 9.3%). CostGate init + LiteLLMClient pool leak fixed en route. |
| **FRE-376 joinability gate ✅** | 2026-05-30 | ADR-0074 → Accepted. Retroactive 6/6 audit green; three probe-tool bugs fixed (legacy SSE exclusion, ws_ticket logger, three_way_mismatch escalation). |
| **FRE-412 entity dedup ✅** | 2026-05-30 | Threshold 0.85→0.92 + ALL_CAPS name-pattern guard. Prevents concept over-merging (e.g. `LLM_CALL`/`TOOL_EXECUTION` collapsing). |
| **FRE-405 P1: Prompt Identity ✅** | 2026-05-29 | PR #109. Every `model_call_completed` carries prompt callsite + component IDs + static/dynamic hash. Cache telemetry fixed (PR #110); `slm-requests-*` keyword index (PR #111). FRE-411 join 8/8 validated. |
| **FRE-421/417/415/414: cloud-path bugs ✅** | 2026-05-29 | PRs #105-108. Context meter uses active model's window; error card path-aware; `/no_think` Qwen-only (ADR-0080); input always writable; availability banner on down path. |
| **FRE-416/419: session profile desync ✅** | 2026-05-29 | PRs #102-104. Server-authoritative profile (ADR-0079); PATCH toggle; mount hydration; new-session hotfix (1056 rows backfilled). |
| **FRE-393/389: identity gate + constraint governance ✅** | 2026-05-28 | PRs #86-91. Scope-aware deny-by-default AST checker (70+→8 allowlist); constraint pause + DecisionCard + TurnStatusBar + Send→Stop; verified on-device. |
| **FRE-411: SLM telemetry joinable ✅** | 2026-05-28 | PR #101 + slm_server PRs. Trace headers + ES keyword index; 8/8 SLM calls join by span_id. |
| **FRE-404/P0 + FRE-402/398/410 ✅** | 2026-05-28 | PRs #92-100. Prompt corpus renderer (107 KB, 13 prompts); terminal tool short-circuit; classified error cards; `read` 200-line head cap + ranged paging (31K-token reduction on executor.py). |
| **FRE-396: Mermaid→SVG artifacts ✅** | 2026-05-28 | mmdc server-side render in `artifact_draft`; inline SVG; ADR-0070 D7 amended. |
| **FRE-392: WS duplicate guard ✅** | 2026-05-27 | PR #85. `MessageDeduplicator` (client_msg_id + SHA-256, 120s TTL). |
| **FRE-388: WebSocket transport ✅** | 2026-05-27 | PR #83 + 8 hotfixes. ADR-0075. SSE→WS; Postgres `session_events` replay; WS ticket auth. Verified live on iPad. |
| **FRE-387/385: eval isolation + Captain's Log ✅** | 2026-05-26 | PRs #81-82. eval_mode gate blocks consolidation→Neo4j; 3 CL files confirmed; 2-week promotion gate ~2026-06-09. |
| **FRE-375/374/376 (Phases 1-5): traceability ✅** | 2026-05-22–23 | PRs #69-80. Test substrate isolation (7688/9201/5433); cross-fact constraints (ADR-0073); 370+ identity-threaded log sites. |

*Older items → `docs/plans/completed/2026-05-22-completed-archive.md` · `docs/plans/completed/2026-05-10-completed-archive.md`*

---

## Active ADRs

| ADR | Title | Status |
|-----|-------|--------|
| **0084** | **Pedagogical Architecture: Socratic Tutor Layer** | **Accepted 2026-06-03 (PR #146). Primary = pedagogical continuity layer; delegation = bounded cognition only; 5-layer architecture; result-type taxonomy. Supersedes ADR-0082 D2–D5 for routing question. FRE-447/448/449 Done (M1). FRE-432 reconceived → Needs Approval; FRE-450 tracks scope revision.** |
| **0082** | **Tier-Aware Model Selection for SINGLE Tasks** | **Partially Superseded by ADR-0084 2026-06-03 — D2–D5 superseded for pedagogical routing; D1 plumbing may still ship in M4. FRE-432 scope invalidated → reconceived.** |
| **0081** | **Cache-Aware Context Layout & Compaction** | **Core chain COMPLETE + live: D1 ✅ (FRE-422) · D4 skill-index split ✅ (FRE-431) · D2/D3 frozen layout + scheduler ✅ (FRE-434, PRs #129/#130, enabled in prod). Deferred follow-ups tracked in project _ADR-0081 Extended — Context & Memory Injection Quality_: D4-index-trim (FRE-464), D5 cold-tier retrieval (FRE-465), D6 pin (FRE-466) — all Needs Approval.** |
| **0080** | **Thinking Control — server-side vs `/no_think` suffix** | **Implemented 2026-05-29 (FRE-417, PR #107)** |
| **0079** | **Server-Authoritative Session Profile** | **Implemented 2026-05-29 (FRE-416/419, PRs #102-104)** |
| **0078** | **Prompt Management & Observability** | **In Progress — P0 ✅ P1 ✅ P2 ✅ (FRE-406) P3 ✅ (FRE-407); P4 (FRE-408) next, P5 (FRE-409) after** |
| **0077** | **Artifact Draft — Sub-Agent HTML Generation** | **Implemented 2026-05-27 (PR #84)** |
| **0076** | **Adaptive Constraint Governance Protocol** | **Implemented 2026-05-28 (FRE-389, PRs #86-91)** |
| **0075** | **WebSocket Transport + Durable Channel** | **Implemented 2026-05-27 (FRE-388, PR #83 + 8 hotfixes)** |
| **0074** | **End-to-End Traceability + Identity Threading** | **Accepted 2026-05-30 (FRE-376 all phases ✅)** |
| **0073** | **Cross-Fact Constraint Layer** | **Proposed — FRE-374 replay ✅; D4 provenance pending perf probe** |
| **0070** | **Output Channel Model** | **Implemented 2026-05-21; D8 review gate ≥ 2026-06-04** |
| **0069** | **R2-Backed Artifact Substrate** | **Implemented 2026-05-17; FRE-369 next consumer** |
| **0067** | **Reflection Surfacing in Context Assembly** | **Accepted; eval → FRE-350** |
| **0066** | **Skill Routing Defaults + Threshold** | **Accepted; Wave J eval complete** |
| 0065 | Cost Check Gate — Atomic Reservation | Accepted + Implemented; FRE-311 parked |
| 0061 | Within-Session Progressive Context Compression | Accepted + Implemented (FRE-251) |
| 0060 | Knowledge Graph Quality Stream | Accepted + Implemented; gate live |
| 0052 | Seshat Owner Identity Primitive | Accepted + Implemented |
| 0041 | Event Bus — Redis Streams | Accepted; Phases 1–4 live |
| 0040 | Linear as Async Feedback Channel | Accepted; Phases 1–2 live; Phase 3 → FRE-183 |

*Full ADR list: `docs/architecture_decisions/`*

---

## How This File Works

- **Linear is the task tracker** — this file tracks priorities and sequencing only.
- **Next task**: pick from "Immediately Actionable" table above, highest priority first.
- **Update after every ship**: add to Recently Completed, bump Last updated.
- **Specs** → `docs/specs/` · **ADRs** → `docs/architecture_decisions/` · **Session plans** → `docs/superpowers/plans/`
- **Archive** → `docs/plans/completed/` (items older than ~1 week)
