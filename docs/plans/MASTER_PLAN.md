# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-06-01 (master) — **ADR-0081 §D2/D3 settled — implementation-grade, merged (PR #128).** Frozen append-only layout (volatile rides + persists with its user turn → strict forward extension) + cache-aware compaction *scheduler* (cost-optimal sawtooth reset, EOQ run-length `L*=√(2R/c)`, backend-asymmetric). All six FRE-434 decisions settled (freeze both recall+skill-bodies; cost/quality trigger replaces 0.65/0.85; `within_session_compression` *becomes* the scheduled persisted reset; Codex tail-arm **subsumed**, branch closed unmerged). Gated behind new `cache_frozen_layout_enabled` (default off). Codex 2-round review fixed the `frozen_narrative` role (assistant, not system — role-fix drops non-leading system msgs), the EOQ math, and the `sanitise_messages` byte-fragility hazard. **Next: build implements [FRE-434](https://linear.app/frenchforest/issue/FRE-434) (Approved) from the ADR** — byte-identity invariant is the #1 risk; verify with the FRE-433 A/B harness. Previously: **FRE-433 spike DONE — cross-turn KV reuse root-caused to gateway HEAD-LAYOUT** (mmproj / slot-eviction / TTL / telemetry / spec-decode all **refuted**). End-to-end A/B (harness `scripts/eval/fre433_cache_ab/`, both backends): the volatility-gradient relayout (move volatile out of the system head) fixes **cloud** (Sonnet reuse 13.9k→17–20k, *improves* it, does not break it) but **NOT local** (stays 0) — local additionally needs **frozen append-only history** (each turn a strict forward extension). Filed **[FRE-434](https://linear.app/frenchforest/issue/FRE-434)** (ADR-0081 **D2/D3**: frozen append-only layout + cache-aware **compaction scheduler** — compress at a computed cost/quality optimum; the "and compaction" half of ADR-0081) — **Needs Approval, Tier-1** — with a design brief. Findings + A/B + brief in **PR #127**; conclusion on FRE-433. Codex's `codex/fre-433-layout-tail-arm` = validated **cloud-only** partial, **HELD** pending the D2/D3 ADR. **Next: adr session writes ADR-0081 D2/D3 → build implements FRE-434.** Previously: **ADR-0081 §D4 (skill-index split) decided + ADR-0082 (tier-aware model selection) Proposed — both merged (PRs #121/#123, #122); impl tickets FRE-431 (D4) + FRE-432 (tier-routing) are Needs Approval, awaiting owner sign-off. Recommended build sequence once approved: D4 first (cache-GREEN gate, further along), then tier-routing — they share the hot executor path.** Earlier 2026-06-01: **FRE-422 ADR-0081 D1 (volatility-gradient prompt layout) shipped + deployed (PR #120).** Pure reorder of the system-prompt assembly: STATIC tool rules → SEMI-STATIC (tool awareness, base, decomposition) → VOLATILE memory tail; a layout-invariant unit test pins `tool_prompt` before `memory_section`; gateway rebuilt + healthy. **Post-deploy eval (build worktree) finding: the `orchestrator.primary` cache gate is still RED — D1 alone cannot flip it; skill-index injection (ADR-0081 D4, skill-index split) is the remaining erosion source.** This is exactly the residual the ticket predicted. FRE-422 was auto-closed to Done by the merge automation and **reopened to In Progress** — its cache-GREEN gate transfers to a forthcoming **ADR-0081 D4** ticket (adr worktree drafting the ADR + ticket). **Next: ADR-0081 D4 (skill-index split), then FRE-427 (dead-SSE cleanup).** Previously (2026-05-31 eod): **FRE-407 rating feature fully shipped + debugged; FRE-426 ✅ complete (PRs #113–#119, all deployed).** On-device testing + a systematic-debugging session resolved **six** real issues, each root-caused not guessed: (1) widget never rendered — client never received the turn `trace_id`; now carried on `turn_status`, stamped on DONE (PR #113); (2) cost meter always 0.00 incl. Sonnet — `cost_usd` never put on the `LLMResponse`; added + populated, **live-verified $0.0345 on a real cloud turn** (PR #113); (3) widget only on the live turn — hydrated history not marked `complete` (PR #114); (4) widget invisible until hover — hover-reveal CSS (`opacity-0 group-hover`); made persistent (PR #115); (5) no rated-vs-default indicator — unrated now faint, rated solid (PR #116); (6) rated state lost on reload — **FRE-426 rating hydration**: messages endpoint joins `user-turn-ratings-*` by `trace_id` (assistant-only, role-guarded), PWA seeds `TurnRating` (PRs #117/#118). **FRE-426 completed** with context+cost hydration: `GET /sessions/{id}` returns `context_tokens`/`context_max`/`cost_usd`, PWA `seedTurnStatus` populates the bar on mount/switch — verified real session hydrates context≈3193, cost $0.335 (PR #119, SW→v19). **Key debug finding:** the 70–120s "cycling" is **NOT** an over-cycling bug — simple turns make 1 call/0 tools; slow turns are *legitimate* multi-`bash` tasks (e.g. "give me metrics" → 12 ES queries) where each iteration re-prefills the growing context ~15s. That's **FRE-422** (cache-aware layout), now strongly justified. **Next: FRE-422** (the real latency fix), then FRE-427 (dead-SSE cleanup). FRE-426 device-glance (switch sessions → meters+ratings persist) pending. Previously: **FRE-407 acute fixes ✅ (PR #113, deployed + live-verified)** — on-device test surfaced two real bugs, both fixed: (1) **rating widget never rendered** — the client never received the turn's `trace_id` (TEXT_DELTA/DONE carry none); now carried on the `turn_status` STATE_DELTA, stashed client-side, stamped on DONE with `complete` set unconditionally. (2) **cost meter always 0.00 incl. Sonnet** — `cost_usd` was computed + written to Postgres but never put on the returned response; added to the `LLMResponse` TypedDict + populated on the cloud path. **Verified with a real cloud turn**: `turn_status` now carries `trace_id` + `turn_cost_usd=0.0345`. SW→v14. Two follow-up tickets filed (Approved): **FRE-426** (status surfaces server-authoritative — hydrate context+cost on session mount, fixes the switch-visibility loss) + **FRE-427** (remove dead pre-WS SSE code + extend joinability hook to transport envelopes — the `DONE`-missing-`trace_id` bug bypassed the log/bus/Cypher-only hook). **Next: device-confirm the widget renders → implement FRE-426 → FRE-427 → FRE-422.** Lesson logged: no PWA feature is "done" until driven through a real turn (FRE-407 was called done after backend-only verification). Previously: **FRE-407 P3 ✅ DONE (deployed, PR #112 + `7c28ea7`)** — per-turn 0–3 value rating, joined to PromptIdentity on `trace_id` → the human-eval instrument enchained ahead of FRE-422/ADR-0081 D1. Backend (endpoint + ownership scoping + identity join + dual-write + `user.turn_rated` bus event + `user-turn-ratings-*` template/90d ILM + Insights `detect_low_rating_sessions`) + PWA (4-segment `TurnRating` meter, trace/`complete`/`sessionId` threading, SW→v13). **Default = 2 ("ok"), no auto-submit; metric imputes un-rated completed turns as 2** (cardinality-of-trace_id denominator per callsite). codex pre-impl review caught 2 blockers + 7 edges; architect live-ES review caught a prod-only `event.keyword`→`event_type` bug invisible to mocked tests (regression-pinned). Vitest added as the PWA test framework. Process note: a Sonnet impl subagent pushed `7c28ea7` to main against instructions — code was already reviewed-good + gates green, so kept; subagent git scope tightened going forward. **Next: device-verify the rating control, then FRE-422 (D1 layout reorder) behind the baseline.** Previously: **FRE-406 P2 ✅ DONE** — ES template prompt_* explicit mapping, cache-erosion monitor (Jaccard ≥ 0.9, `make cache-erosion-status`), Kibana saved objects (per-callsite cost breakdown + hash stability view). Live result: orchestrator.primary jaccard=0.200 [ERODED] — confirms future-gate already crossed (cache-hit ≈ 0% on local SLM due to prefix churn). Composer-redesign follow-up ticket needed. FRE-403 EPIC stays In Progress (P3–P5 remain). Previously: **FRE-374 purge→replay ✅** — 14,213 entities + 11,984 relationships, 0 errors. Probe results: recall 0/15 empty/misleading; empty-desc 24.6% (was 42%); redundant edge pairs 4.4% (was 9.3%). **FRE-376 ✅** — ADR-0074 → Accepted; retroactive 6/6 audit green; three probe-tool bugs fixed (`712222e`+`f0478cc`). **FRE-412 ✅** — dedup threshold 0.92 + ALL_CAPS guard. **Next: FRE-406 P2 (prompt cost/cache attribution).**

---

## Current State

Waves A ✅ B ✅ C ✅ E ✅ J ✅ complete. Wave H: FRE-375/374/376 ✅ — FRE-377 next, FRE-381 pending approval. Wave I (FRE-403 EPIC): FRE-404/405/406/407 ✅ — FRE-408 (P4) next, FRE-409 (P5) after. ADR-0081 D1 ✅ (FRE-422) + D4 ✅ (FRE-431, PR #125, deployed). **FRE-433 spike ✅** root-caused the residual cross-turn re-prefill to gateway **head-layout** (A/B: relayout fixes cloud, not local). **ADR-0081 §D2/D3 settled ✅ (PR #128)** → **[FRE-434](https://linear.app/frenchforest/issue/FRE-434) Approved (Tier-1) — build implements** (frozen append-only layout + cache-aware compaction scheduler, gated behind `cache_frozen_layout_enabled`); the local cross-turn latency win lands here. ADR-0074 fully Accepted. ADR-0075/0076/0077/0079/0080 Implemented.

Wave D implementation (endpoint abstraction, compose unification, test parity) remains deferred per FRE-214 audit §8.7.

---

## Active Design Threads

Four threads carved from the FRE-389 on-device review (2026-05-28). All **Approved**. FRE-398 (bubble-up errors) ✅ Done.

| Thread | Issue | Scope |
|--------|-------|-------|
| **Dynamic artifacts** | [FRE-397](https://linear.app/frenchforest/issue/FRE-397) | Diagrams now → interactive later (Tier 1 SVG → Tier 2 sandboxed JS → Tier 3 JSX). |
| **Adaptive limits & error recovery** | [FRE-399](https://linear.app/frenchforest/issue/FRE-399) | Retry/fallback on transient 5xx/524; local inference telemetry; dynamic thresholds. |
| **E2E testing (transport/UI/error)** | [FRE-400](https://linear.app/frenchforest/issue/FRE-400) | Automated WS round-trip + PWA + failure-path coverage. |
| **Planner-executor split** | [FRE-401](https://linear.app/frenchforest/issue/FRE-401) | Reasoning model plans; subagents execute in isolated context. ADR required before implementation. |

**Recommended order**: [FRE-434](https://linear.app/frenchforest/issue/FRE-434) (ADR-0081 D2/D3 impl — ADR settled, build now; the local cross-turn latency win) → FRE-377 (quick canonicalization) → FRE-408 (P4) → FRE-399 Layer 2 → FRE-400 → FRE-397 Tier 2.

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
| **H** | Memory / context value | Partial | [FRE-375](https://linear.app/frenchforest/issue/FRE-375) ✅ → [FRE-374](https://linear.app/frenchforest/issue/FRE-374) ✅ → [FRE-376](https://linear.app/frenchforest/issue/FRE-376) ✅ → [FRE-377](https://linear.app/frenchforest/issue/FRE-377) (Approved) → [FRE-381](https://linear.app/frenchforest/issue/FRE-381) (Needs Approval) → FRE-178 → FRE-179 → FRE-180 · FRE-230 | FRE-376 ✅ 2026-05-30 — gate closed; ADR-0074 Accepted. FRE-377 next. |
| **I** | Prompt observability | In Progress | [FRE-403](https://linear.app/frenchforest/issue/FRE-403) EPIC (In Progress) · FRE-404 ✅ · FRE-405 ✅ · FRE-406 ✅ · FRE-407 ✅ · [FRE-408](https://linear.app/frenchforest/issue/FRE-408) (**P4, next**) · FRE-409 (P5) · FRE-183 · FRE-184 | FRE-404/405/406/407 shipped. ADR-0081 D1 (FRE-422) shipped behind the FRE-407 baseline; D4 next for the cache gate. EPIC stays In Progress until P5 ships. |
| **J** ✅ | Eval methodology hardening | Done | FRE-329–335 all shipped | |

---

## Pending Verification

*(nothing pending)*

---

## Immediately Actionable (approved, no gate)

| Ticket | Priority | Tier | What |
|--------|----------|------|------|
| [FRE-384](https://linear.app/frenchforest/issue/FRE-384) | **High** | Sonnet | `notes_search` tool returns error — notes not queryable by agent. |
| [FRE-383](https://linear.app/frenchforest/issue/FRE-383) | **High** | Sonnet | Agent hallucinated Neo4j write + fabricated JSON payload output. |
| [FRE-377](https://linear.app/frenchforest/issue/FRE-377) | Medium | Sonnet | Canonicalize TaskCapture token fields to `input_tokens`/`output_tokens` (ADR-0074 §I5). |
| [FRE-369](https://linear.app/frenchforest/issue/FRE-369) | Medium | Sonnet | User-upload UX in PWA with presigned PUT to R2 (images/files in chat). Spec: ADR-0069 + ADR-0070. |
| [FRE-314](https://linear.app/frenchforest/issue/FRE-314) | Medium | Sonnet | `feedback_history/` retention policy in DataLifecycleManager. |
| [FRE-350](https://linear.app/frenchforest/issue/FRE-350) | Medium | Opus | Post-deploy reflection-surfacing eval. Gate opened 2026-05-24 — startable now. |
| [FRE-349](https://linear.app/frenchforest/issue/FRE-349) | Medium | Opus | Surface actionable Insights in agent context (G3 from FRE-346). |
| [FRE-391](https://linear.app/frenchforest/issue/FRE-391) | Medium | Opus | Dynamic `max_tokens` based on tool/task context — addresses artifact truncation root cause. |

**Calendar-gated (approved but not yet startable):**
- **FRE-328** — naming-stability data review. Gate ≥ 2026-06-09 (2 weeks clean production data with agent-noun taxonomy).
- **FRE-381** (Needs Approval) — Stage 2 consolidator decoupling. Requires ADR-0074 §I5 amendment + post-FRE-380 soak data.

---

## Needs Approval

| Ticket | Priority | Tier | What |
|--------|----------|------|------|
| [FRE-431](https://linear.app/frenchforest/issue/FRE-431) | — | Sonnet | **ADR-0081 D4 impl** — skill-index split (cached index + volatile bodies); owns the `orchestrator.primary` cache-GREEN gate. ADR decided (PRs #121/#123). |
| [FRE-432](https://linear.app/frenchforest/issue/FRE-432) | — | — | **ADR-0082 impl** — tier-aware model selection for SINGLE tasks; deterministic `model_tier` axis. ADR Proposed (PR #122). |
| [FRE-381](https://linear.app/frenchforest/issue/FRE-381) | Medium | Sonnet | **Stage 2** consolidator decoupling — invert Turn-creation vs entity-extraction dependency; add `extractor_model` to `TurnNode`. Blocked-by FRE-380 ✅ + post-soak data. |
| [FRE-390](https://linear.app/frenchforest/issue/FRE-390) | Low | Sonnet | Eval harness skips transport layer — no automated WS delivery coverage. |

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
FRE-403 EPIC: FRE-404 ✅ → FRE-405 ✅ → FRE-406 ✅ → FRE-407 ✅ → FRE-408 (P4, next) → FRE-409 (P5)
ADR-0081: D1 ✅ (FRE-422, PR #120) → D4 skill-index split (drafting, owns the cache-GREEN gate D1 couldn't meet)
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
| **0082** | **Tier-Aware Model Selection for SINGLE Tasks** | **Proposed 2026-06-01 (PR #122). Adds `model_tier` axis so ~83% SINGLE traffic can use the non-thinking `sub_agent` tier; impl ticket FRE-432 (Needs Approval). Net-new design — not yet approved.** |
| **0081** | **Cache-Aware Context Layout & Compaction** | **D1 Implemented 2026-06-01 (FRE-422, PR #120, deployed); D4 (skill-index split) **decided** 2026-06-01 (PRs #121/#123) — owns the cache-GREEN gate D1 couldn't meet; impl ticket FRE-431 (Needs Approval). D2/D3/D5/D6 pending.** |
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
