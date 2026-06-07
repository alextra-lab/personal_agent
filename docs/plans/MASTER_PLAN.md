# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-06-07 (master) — **✅ FRE-520 DONE+VERIFIED · 📊 FRE-453 BASELINE LANDED · 🔓 FRE-511 MERGED (deploy pending).** FRE-520 (PR #185, `3fd57f4`, deployed 17:04Z): waiter ownership inverted + 10s bounded wait; live proof = rerun `fre453-baseline-02` **18/18 cases, 0 timeouts, 0 hangs** incl. `closing_ritual`. **Baseline findings (report in telemetry/, summary on FRE-453):** 10/18 all-match; mismatch classes = skills-not-loaded (8), tools_any_of (4), model_path (1: `delegation_handoff` stayed single/primary — FRE-432 signature measured); expansion alive (2 hybrid cases, 8 sub-agents); thinking 0/18; tools used 15/18 → owner's "no tool use in PWA" = **rendering gap → FRE-522**; +FRE-521 (turn-count stat). Owner rubric pass = remaining FRE-453 close item. **FRE-511 (PR #184, `4d2e6f9`) merged: sanitizer retired (ADR-0089 D1/D7), gate label collapses to committed/not_applicable (forensics via commit_path+counts), prompt reframed JS-affirmative — DEPLOY PENDING (was held for the eval run); deploying unblocks FRE-510 script-artifact E2E; then FRE-496/500 supersession cleanup + FRE-512.** ADR-0089: 509✅ 510✅ 511 merged → 512. Previously: 2026-06-07 (master) — **🧪 FRE-453 harness MERGED (PR #183, `4295836`) + baseline run BLOCKED by new bug FRE-520.** Eval harness/dataset/tests shipped (code+security reviews clean; no src/ changes, no deploy). Baseline `fre453-baseline-01` (local profile: primary=local Qwen3.6-35B via slm.frenchforet.com, Haiku=skill-routing only) died 3/18: case `closing_ritual` scored turn hung the full 1200s **server-side pre-LLM**. Root cause = **pop/get race in `events/session_write_waiter.py`** (FRE-51/158): next-turn `await_previous_session_write` pops the Future before the consumer's `release_session_write_wait` can resolve it → permanent deadlock, no timeout. Prod-reachable (fast double-send on a session). Evidence: trace `7abc4a00` request_received→silence while the awaited append completed (session `eb92a21d` has all 4 msgs); SLM probes up. **FRE-520 filed (High, Needs Approval) — blocks the FRE-453 baseline rerun (15/18 cases unrun); FRE-453 stays In Progress.** Post-mortem: `docs/postmortems/2026-06-07-session-write-waiter-deadlock.md`. Also noted: EVAL channel suppresses side effects → may blank the harness background-surface layer (validate on rerun). **ADR-0089: 509✅ 510✅ → 511 (build, in flight) → 512. L0: 453 harness✅(run blocked), 515/517 Approved, 505 E2E pending.**

---

## Current State

Waves A ✅ B ✅ C ✅ E ✅ J ✅ complete. Wave H: FRE-375/374/376 ✅ — FRE-377 next, FRE-381 pending approval. Wave I (FRE-403 EPIC) ✅ COMPLETE — P0–P5 (FRE-404–409) all shipped+verified 2026-06-02; P6 (DSPy opt) optional. **ADR-0081 cache chain COMPLETE:** D1 ✅ (FRE-422) → D4 ✅ (FRE-431) → D2/D3 ✅ (FRE-434, PRs #129/#130) — frozen append-only layout + cache-aware scheduler shipped, A/B-verified (local cross-turn reuse 0 → 8,110+; cloud 13,916 → 19,542; quality flat), **enabled in prod**. FRE-433 spike root-caused it to gateway head-layout. Follow-ups (Needs Approval): FRE-435 (memory research), FRE-436/437/438/439/440. ADR-0074 fully Accepted. ADR-0075/0076/0077/0079/0080 Implemented; ADR-0082 (tier-routing) Proposed → FRE-432 Approved.

Wave D implementation (endpoint abstraction, compose unification, test parity) remains deferred per FRE-214 audit §8.7.

---

## Program Architecture (L0–L3) — `docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md`

As of 2026-06-06 (FRE-504) the portfolio is organized as **substrate pillars vs feature consumers** across four layers. This is the forward-looking organizing layer over the legacy Wave A–J sequence (below, retained as historical record). Live Linear projects map to layers:

| Layer | Linear project(s) | Role |
|-------|-------------------|------|
| **L0 — Observability substrate** | **Observability Foundation** — FRE-451 taxonomy ✅ · FRE-452 route-trace ledger ✅ (deployed+verified) · FRE-453 matrix seed · FRE-505 sub-agent auditability · FRE-506 gate-decision telemetry · **FRE-513** ADR-0088 spine (observe_topology seam + projector + report_degradation + FRE-501 removal + shield fix) · **FRE-514** route-trace REST read surface · **FRE-515** hybrid delegate_called→used/discarded (dep. FRE-453) | Makes *actual* traversal observable; gates reconciliation + shipping-to-default. Governed by **ADR-0088** (Accepted). |
| **L1 — Intended-traversal matrix** | *(folded into Observability Foundation)* — FRE-453 + knowledge-access column + decomposed build/teach case | Normative spec; authored *in parallel* with L0 (declaring intent needs no telemetry). |
| **L2 — Substrate pillars** | **Memory Recall Quality** (ADR-0087) · **Seshat Inference Architecture** (ADR-0082 — plumbing + planner reliability, incl. **FRE-502**) · **ADR-0081 Extended — Context & Memory Injection Quality** · **Artifact Execution Security** (ADR-0089 — *project deferred to ADR authoring*) | Cross-cutting capabilities with many consumers. All three live pillars **Approved**. |
| **L3 — Consumers** | **Seshat Pedagogical Architecture** · **Turn Cost & Latency Optimization** · **Turn Reliability Hardening** | Features standing on the substrate. |

**Reconciliation loop (L0↔L1):** intended matrix vs actual ledger; every gap resolved loudly in one of two explicit directions ("loud or it rots"). Currently a *principle*, not yet a running control system (operationalizing it is itself a future L0/L1 deliverable).

**Active sequence — visibility-first (decomposition first-run fix queue):** Wave 0 (SEE) = **FRE-501 ✅** (live cost+status meter, PR #171, deployed) · FRE-505 · FRE-506 — *build/adr*. Wave 1 = FRE-502 (planner reliability). Wave 2 = FRE-503 (proactive depth for build/teach). Parallel = FRE-500 (sandbox flag bridge). adr = **FRE-504** (spec ✅ landed PR #172; **ADR-0088/0089 pending**, ticket stays In Progress).

**Reconciliation — RESOLVED by adr session (2026-06-06; do not re-resolve):**
1. **FRE-502** (planner reliability) — ✅ **MOVED** to **Seshat Inference Architecture** (spec §4 mechanism-robustness routing); was in Turn Cost.
2. **Artifact Execution Security** (L2 pillar, ADR-0089) — **deferred by design**: creating the project + re-homing FRE-497/498/499/500 (currently Turn Cost) happens at **ADR-0089 authoring** (adr session), since the ADR defines the pillar's shape — creating it now would approve a boundary before its ADR. No master action; tracked interim.
3. **FRE-453** — **resolved, no change**: filed in **Observability Foundation**, which owns the L1 matrix. L0 and L1 are **co-located** (the two halves of the reconciliation loop; there is no separate L1 project) — so it *is* "with L1." Optional future polish (owner's call): two milestones ("L0 — Observation" / "L1 — Intended matrix") to make the split visible.
4. **3 pillars approve** — ✅ **DONE**: Memory Recall Quality, Seshat Inference Architecture, ADR-0081 Extended all **Approved** (restructure pass, owner-authorized).

*Restructure provenance:* Observability Foundation created (Approved); lifted FRE-451/452/453 (from Pedagogical M2), FRE-505 (from Turn Cost), FRE-506 (no prior project) into it.

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
| [FRE-470](https://linear.app/frenchforest/issue/FRE-470) ✅ | Low | Sonnet | **DONE** (PR #156, `696e5e6`) — exit 141 treated as success only on a top-level pipe (`_has_top_level_pipe`); standalone 141 still fails; `note` field added. 7 unit + 5 real-bash integration tests. Deployed + verified live (code in container, health green). |
| [FRE-471](https://linear.app/frenchforest/issue/FRE-471) ✅ | Low | Sonnet | **DONE** (PR #157, `a259503`) — `_truncate_plan` boundary-aware trim + anti-fabrication notice (never raises on oversize); cap 8000→16000; `plan_truncated`/`plan_original_length` flags; empty plan still raises. Deployed + verified live (`_MAX_PLAN_CHARS=16000` in container, health green). |
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
