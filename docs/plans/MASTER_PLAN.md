# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest) — per-ticket state.
> **Source of truth for priorities**: this file (sequencing only; do not re-enumerate Linear here) — keep it *concise*.
> **This file is UNIQUELY the plan**: current live-env / standing state + active priorities + sequencing + Needs-Approval. **Completed / superseded narrative does NOT live here** — it moves to [`MASTER_PLAN_HISTORY.md`](MASTER_PLAN_HISTORY.md) (grepable, NOT auto-loaded). `/prepare-reset` performs the move each reset; this session's *decisions* live in [`LAST_SESSION.md`](LAST_SESSION.md).
> **Last updated**: 2026-07-17 (FRE-896 config-cleanup chain SHIPPED end-to-end — 876/896/897/906/907; FRE-906 event-bus self-reclaim DEPLOYED + live-verified; ADR-0120 cost-gov Proposed; backlog reel-in done; **tmux 3.7a cutover COMPLETE + verified**, all seats re-created + watcher-reachable. Next: owner gates — accept ADR-0120, approve FRE-895)

## Current live-env & standing state

- **Delivery automation — watcher LIVE, dispatch-orchestrator RESUMED (2026-07-16, owner-directed for the backlog reel-in).** Both `seshat-gating-watcher` and `seshat-dispatch-orchestrator` active; kill-switch sentinel clear. Build tickets auto-dispatch again (stream label + Approved + unblocked → launched to the owning seat). **All 3 worker seats deliver via MCP channel** (ADR-0116 Phase B done + verified live); **master gates with `scripts/pr_gate.py`** — the ADR-0117 signal collector (determinable CI/mergeability/dependabot facts only, never judges/blocks; master SKILL Step 4). Model: watcher triggers master on a CI-green PR ("Gating PR #X"); CI-red → channel push (send-keys fallback) to the owning worker; build dispatch = Linear labels; NEXT via `next_resolver --eligible` (FRE-846). **Master stays send-keys** (no launcher topology entry — by design). Full contract: `.claude/skills/lifecycle-rules.md`.
- **Standing review model (do NOT re-open):** master reviews PRs directly; CI + CodeQL own deterministic security + correctness; a tool-equipped subagent offloads a large review when master's context is full; adversarial fresh-context subagent before a one-way door.
- **Embedder:** OVH-managed **Qwen3-Embedding-8B @ 1024 dims** (`AGENT_SUBSTRATE_PROFILE=managed_embedder`; managed-only, fails open if OVH down; local `cloud-sim-embeddings` container STOPPED). All 6,109 KG entities re-embedded to 8B/1024.
- **Reranker:** Voyage **rerank-2.5** primary + Mac-tunnel 4B fallback (FRE-851 live).
- **Recall:** multipath **LIVE but PARTIAL** — **2 of 3 arms live** (multi_query + lexical), similarity floor 0.60, permanent `latency_ms` telemetry (p50 ~1.3s ≪ 17s ceiling post-Voyage). **ADR-0104 still Partial, NOT Implemented**: the structural/type arm is now **WIRED into fusion** (FRE-866, 2026-07-13 — `structural_recall_arm_ranked` appended to `arm_coros`) but stays **flag-dark** (`structural_arm_enabled` defaults False), plus a flag-gated class predicate (`structural_class_predicate_enabled`, default False — mechanism-only, no class-bias policy). ADR-0104 reaches **3-arms-live only when the flag is rolled on + verified in a live A/B** (future rollout; owner owns the class-bias policy decision). ADR **file status still `Proposed`**. Relevance-bounded recall (ADR-0100) live.
- **Substrate:** app runs as the restricted `seshat_app` Postgres role (off the superuser); sysgraph physically isolated (ADR-0105/0112). Neo4j / ES / Postgres healthy. **Running gateway: `cfffe3dc`** (2026-07-17 — rebuilt to ship FRE-906 event-bus self-reclaim; `cloud-sim-embeddings` re-stopped post-rebuild).
- **Taxonomy — entity *type* + knowledge *class* both LIVE.** ADR-0109 **V2 10-type** + ADR-0115 **knowledge-class axis** both Implemented + deployed (SHA `c51a7486`; supersedes ADR-0106 + ADR-0098-§D1, refines ADR-0097). New entities persist `class` (World/Personal); System-natured → `sysgraph.stat`; 180d retention live. **Existing corpus de-noised 2026-07-13** — Core is **6,625 entities, all World/Personal, 0 System** (FRE-865 backfill + FRE-868 eviction ran + verified; **ADR-0114 de-confound complete**). **FRE-632 owner-identity unified + deployed** — the two split `Alex` nodes folded into one `:Person:Entity` ("who am I" anchor now whole). **FRE-869** cost-attribution fix deployed. FRE-866 structural arm wired-but-flag-dark (see Recall). _(Full ops detail → MASTER_PLAN_HISTORY 2026-07-13.)_
- **Identity:** ADR-0107 — `assert_claim` resolves by acting `user_id`; `is_owner` = admin flag. FRE-738 deployed.
- **Config:** ADR-0099 config management **Implemented**; **D4 field-doc guard live** (FRE-876). **FRE-893 audit + FRE-896 cleanup chain SHIPPED** (2026-07-17): alias-aware read scanner (`scripts/audit/`), 3 confirmed-outgrown fields removed, `managed_*` secret redaction fixed, `event_bus_ack_timeout_seconds` wired to an XAUTOCLAIM self-reclaim sweep (FRE-906, deployed). 186 read-but-never-overridden "hardcode candidates" HELD (trade against config-UI tunability). Linear keys rotated to `pass`.
- **tmux:** server cut over to **3.7a** — **COMPLETE + verified 2026-07-17** (`tmux display-message -p '#{version}'` reports the *server* as 3.7a; `tmux -V` only reports the client binary). Built from source, `/usr/local/bin`; apt 3.5a fallback in `/usr/bin` (revert = `rm /usr/local/bin/tmux`). Fixes the control-client crash class that killed `cc-build2` mid-build 2026-07-16. Recovery runbook: memory `reference_worker_seat_crash_recovery`.
- **Seats:** all re-created post-cutover via `cc-sessions restart <seat>` (per-seat — **never `recover`**, which can spawn a duplicate seat on stale context). **Master MUST run inside a `cc-master` tmux seat**: `gating_watcher.py` resolves it by `tmux has-session -t cc-master` and `send_to_session()` returns `"absent"` — a **silent drop, no alert** — if it is missing, so a master started outside tmux looks up but is unreachable (hit live 2026-07-17; `claude -n` sets only a display name). Verify: `tmux has-session -t cc-master; echo $?` → `0`. Trap + relaunch runbook: memory `reference_cc_sessions_tool`.

## Open threads (current)

- **ADR-0114 associative-memory study — v0 chain BUILT; VERDICT held (priority-#2 decoupled side-study).** **FRE-838→842 (v0) all Done** (2026-07-10/11): isolated corpus (10,290/34,301, reproducible hash); schema+categorizer+writer with the owner-approved **paid ingest → AC-1 PASSED** (median MEMBER_OF degree 4, 100% provenance-distinct); baseline harness; pre-registered eval artifacts (**cross-family LLM gold, owner-accepted for v0**, ADR updated); consolidator. **RECAST 2026-07-11 — ADR-0114 is BLOCKED-ON-SUBSTRATE**, not merely corpus-limited: it needs the knowledge-*class* axis to filter the ~⅓–½ System-class noise in the corpus, but **0/7992 entities carry a class** (persistence unbuilt, above) → the study is confounded, sequenced over a dependency that was never built. **FRE-843 (v0 synthesis + AC-4 verdict) unblocked but HELD** — the class-noise reason is now **RESOLVED** (ADR-0115 live + corpus de-noised 2026-07-13, Core class-clean); the sole remaining hold is **corpus adequacy**: the consolidator **plateau (AC-3a) does not form** on the 46-episode corpus (density, not pure size), so a clean pre-registered τ_merge* may not be selectable. Master's rec: **don't densify the corpus** (it's the owner's real memory — densifying invalidates the eval); accept the honest v0 null + an *exploratory* recall probe. Full analysis + caveats (ac_proof spot-check bug, AC-7 judges, scoring mechanics) on FRE-843 comments + LAST_SESSION. v1 arms 853→856 gated behind 843. **Study Neo4j sandbox holds the paid ingest data (818 Concepts, 1,667 memberships) — preserve; 842/843 consume it.**
- **FRE-858 keep-bugs (parked Approved-unlabeled, parked-until-sized):** FRE-632 (owner-identity dup-merge), 733 (Neo4j test-pw), 751 (recency-0 coercion), 762 (temp fallback), 760 (V2 rel-gate), 805/850 (trivial). _(Triage execution 2026-07-11 → MASTER_PLAN_HISTORY.)_
- **ADR-0102 vision docs/PDF — SHIPPED + LIVE (2026-07-15), Implemented; FRE-886 attachments→cloud SHIPPED + LIVE; FRE-884 ADR-0086 retirement merged (Awaiting Deploy, batched).** Full ship narrative → MASTER_PLAN_HISTORY 2026-07-15/16. Open follow-up: **FRE-885** (Needs Approval — cost-estimator token counter rejects the `document` block type → document-turn token counts undercounted; telemetry-only, billed cost unaffected).

- **NEXT EPIC — Seshat config-management interface (owner-directed 2026-07-15).** The capstone SURFACE on ADR-0099's single-source role matrix + validator. **ADR-0119 authored (PR #542) + impl chain filed and approved** (umbrella FRE-887 + L1–L5 = FRE-888→892), model-selection (ADR-0118) folded in. **STEP-0 UNBLOCKED (2026-07-16): the ADR-0119 ExecutionProfile amendment LANDED (PR #550).** The regression (`artifact_builder` as a flat matrix row silently routing a local build to cloud Haiku) is resolved: all open roles (`primary`/`sub_agent`/`artifact_builder`) resolve via the ExecutionProfile, matrix resolves only pinned writers; role-tier taxonomy + AC-8/9/10 guards added; vision stays pinned (scope tightened). **FRE-879/880 re-approvable** — the working impl on `fre-879-artifact-builder-role-cost-lane` is mostly reusable (only the resolution seam changes: `artifact_builder` binding into `config/profiles/{local,cloud}.yaml`, resolved by `resolve_model_key`, not the matrix). ADR-0119 stays **Proposed** (sign-off = owner gate after the chain delivers). Constraint still binds: observe-first, model-selection + FRE-886 attachment-default only, routing (0082/0094/0095) deferred. **build2 idle** — ready for FRE-895 (frenchforet scrub, pending approval) then the config-UI chain. **Co-design the shared seams with the cost-gov project (below): T5 splits artifact_builder+vision out of `main_inference`; T6 renders the cost surface inside this config UI.** **cc-explore deliberating** the multi-parent associative-memory question — read-only, owner-gated. Parked-Approved: FRE-858 keep-bugs · **FRE-876** · **FRE-867** · **FRE-807**.
- **COST-GOVERNANCE REWORK — ADR-0120 AUTHORED + merged (Proposed, 2026-07-16; supersedes ADR-0065, now Superseded).** Kill hard dollar caps → forward-by-default + visibility + owner-consent; soft **pause reflex** + out-of-band **Signal** alert, **owner sole resume/kill**; ADR-0065's reserve/commit/refund primitive retained. Codex round-1 (7 blocking) fixed in the doc; owner rulings folded (Section-A per-vendor thresholds + per-role visibility, Perplexity onboarded, anomaly-adaptive threshold w/ fixed bootstrap). **Foundation-first sequence T0→T6** (AC-1..8, each runnable): **T0** = instrument OVH/Voyage/Perplexity into `api_costs` (three off-books vendors) — the hard prerequisite + first buildable; T5/T6 co-design with config-UI. **Impl tickets NOT filed yet** — await owner acceptance of the ADR (Proposed→Accepted) before dispatch. Decision note: `docs/research/2026-07-16-cost-governance-rework-adr-0065.md`; memory `project_cost_governance_rework`. **All cost/budget = ask-first** — nothing actuates without explicit OK.
- **Deliberation seats (standing).** `cc-explore` (worktree-isolated at `.claude/worktrees/explore`; owner-hubbed, all of master's vision none of its hands) + ephemeral `cc-exploreN` as needed. Owner-hubbed injection only — explore is the owner's conversation (lifecycle-rules § Explore; memory `feedback_explore_is_conversation…`). cc-explore currently on the multi-parent associative-memory question; ephemeral cc-explore2 completed the cost-gate provider-pool deliberation (note on main).
- **ADR-0116 event-driven dispatch — Phase-1 LIVE; ADR-0117 pr_gate + ADR-index-guard LIVE** (2026-07-14). Both in the ADR table + Current live-env; narrative → HISTORY. Standing residual: **FRE-875 PARKED** (channel seam hardening — seat-secret durability + scrape deletion; non-blocking, degrades to send-keys). ADR-0118 model-selection = Proposed, folded into ADR-0119.
- **FRE-717 held** at Awaiting Deploy — ADR-0105 T4 code+schema live, but AC-6 outcome-ingestion has zero organic input yet; live proof pends a promoted proposal reaching a terminal state.
- **Infra ticket reconciliation PENDING** — FRE-810–815 (filed under the superseded ADR-0111) need cancel/reshape + new ADR-0112 impl tickets.
- **Pending hygiene (to file):** (a) `load_linear_key` → prefer `pass show CC_LINEAR_API_KEY`; (b) switch `/build`+`/adr` Linear to direct `tools/linear.py`, drop `mcpServers.linear`.
- **Open owner items:** FRE-805 (sibling stale `:Conversation`, Needs Approval, Low) · FRE-621 (graph-hygiene, Needs Approval, Medium) · **MedicalCondition** entity-type seed (parked) · config-UI capstone.
- **Deferred:** watcher idle-scrape (`capture-pane`) removal.

---

## Dispatch

Dispatch lives in **Linear** (process v2 — contract in `.claude/skills/lifecycle-rules.md`
§ Dispatch): a stream's NEXT = `Approved` + `stream:<name>` label + no open blocked-by relation,
priority-ordered. This file carries **why the sequence is what it is** — priorities, waves,
dependency rationale — never per-ticket board state (it drifts).

**Go-forward sequence (owner, 2026-07-16 — the plan for the next several sessions):**
1. **Route the cost-gov note to the adr seat** (new ADR superseding ADR-0065, authored with the owner) — the FIRST action post-reset. See LAST_SESSION ▶ BEGIN HERE.
2. **Converge/revise, then build the config-UI (ADR-0119)** with the cost-gov project waiting in the wings (co-design T5/T6). Config-UI prerequisites: approve **FRE-895 (scrub) first**, then **FRE-879→880→888→892** (dispatcher is paused — resume or launch manually).
3. **Attack the Seshat inference backlog.**

**Backlog reel-in (2026-07-16 — reconcile = reschedule, not cancel).** The ~81 Approved tickets were *all* unlabeled (approved-but-not-dispatched) and **28 were orphaned (no project)** — the pivot-era tickets. Reconciled: **27 orphans homed** into their projects (security cluster → Security; inference/routing → Seshat Inference; artifact → Turn Cost & Latency; consolidator/schema → Memory Recall; learning/insights → Pedagogical / Linear Async Feedback; infra-audit → VPS/Cloud). Two stale **Urgent** board-lies de-drifted to Low: **FRE-822** (loop cost incident — fixed + event-driven part shipped as ADR-0116; residual = post-mortem + TTL doc) and **FRE-792** (entity_extraction cap reset — verified still $5.00; folds into the cost-gov ADR, do not execute standalone). **Reel-in is a bounded flow** (one batch per free stream, drain, pull next — never bulk-label). **Batch-1 flowing: FRE-876** (config D4 guard) on `cc-build2`.
- **Reel-in wave order** (by priority ladder; dispatch a batch as a stream frees): Config hygiene (FRE-896/897 pending approval, 876 in flight) → Memory Recall ready half → Security cluster (FRE-359/361/362 — verify vs shipped identity first) → Telemetry residuals (704/599/574) → then Pedagogical / Inference longer-horizon.
- **Do NOT dispatch (gated/superseded — verify/reshape first):** ADR-0111 trio FRE-810/811/814 (reshape to ADR-0112) · ADR-0114 five FRE-843/853–856 (held on corpus adequacy) · config-UI five FRE-888–892 (wait on cost-gov) · FRE-729–732 (ADR-0106 superseded by 0109/0115 — verify still-valid) · FRE-240 (reranker — verify vs Voyage migration) · **FRE-259** ADR-0063 epic (dormant; owner decision: revive / decompose / cancel).

**Backing state:** ADR-0102 vision + FRE-886/884 SHIPPED (2026-07-15); FRE-893 config-audit redo DONE (PR #548, reads deployed `.env`); ADR-0119 ExecutionProfile amendment landed (PR #550) → config-UI step-0 unblocked, FRE-879/880 re-approvable. Owner constraint: the frenchforet cleanup (FRE-895) lands before ADR-0119 sign-off. Parked-Approved when a stream frees: FRE-858 recall keep-bugs + FRE-876. FRE-739 (Awaiting Deploy, ADR-0107 seam — needs FRE-740 + a live non-owner request). FRE-717 held at Awaiting Deploy for organic outcome input.

**Parked / held (rationale only — not dispatched, no stream label):** Inference ADR-0094/0095 trees
FRE-600–604 / 607–611 (held pending FRE-432/516 measurement) · FRE-713 (trigger-gated, Backlog) ·
FRE-760 (re-scoped Low, blocked by FRE-773).

---

## Priority order (owner, 2026-07-04) — all ladder up to Seshat Pedagogical Architecture (M3)

1. **Agentic Vision** — ADR-0101 **Implemented** (images live). **ADR-0102 documents/PDF — SHIPPED + LIVE (2026-07-15), Implemented**: all children + seam FRE-689 Done, AC-SEAM verified live (native PDF block on Sonnet, cost gate, vision path, joinability green). Follow-ups: **FRE-885** (Needs Approval — cost-estimator token-counter document-type gap, telemetry-only) · **FRE-886** SHIPPED + LIVE (default Auto attachments → cloud/Sonnet). **ADR-0108 stored-image reprocess — lower priority, HELD** (ADR still Proposed; owner accepts via `/adr` before dispatch).
2. **Memory Recall** — hot path. entity-*type* ADR-0109 **V2 live**; knowledge-*class* **ADR-0115 live**, **existing corpus de-noised** (6,625 class-clean). Recall multipath: ADR-0103 Accepted / **ADR-0104 Partial** — structural arm now **wired into fusion but flag-dark** (FRE-866 Done); 3-arms-live pends flag-on rollout + owner class-bias policy.
3. **Telemetry Surface Audit** — core complete (FRE-703 dashboard wave shipped); residual FRE-574/599/704 Approved.
4. **Configuration Management** — ADR-0099 **Implemented**; config-UI capstone + lifecycle audit next.
5. **Linear async feedback channel** — unblocked now the sysgraph is built (ADR-0105/0106); convergence + surfacing is the remaining build (W2 FRE-728–732).
6. **Seshat Inference** — ADR-0082; FRE-432 phases (Ph0 done).

---

## Program Architecture (L0–L3) — `docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md`

Portfolio organized as **substrate pillars vs feature consumers** (FRE-504). Live Linear projects map to layers:

| Layer | Role | Status |
|-------|------|--------|
| **L0 — Observability substrate** | Makes actual traversal observable; gates reconciliation + shipping-to-default. Governs Telemetry Surface Audit (ADR-0090) + Observability Foundation (ADR-0088). | Core spine complete. Telemetry dashboards shipped (FRE-703). Residual: FRE-574/599/704/585/739/740 Approved. |
| **L1 — Intended-traversal matrix** | Normative spec (folded into Observability Foundation). | Co-located with L0. |
| **L2 — Substrate pillars** | Memory Recall Quality (ADR-0087/0098/0100/0103/0104) · Seshat Inference (ADR-0082) · ADR-0081 Extended · Artifact Execution Security (ADR-0089 Implemented). | All Approved/live. |
| **L3 — Consumers** | Seshat Pedagogical Architecture · Turn Cost & Latency · Turn Reliability Hardening. | Features on the substrate. |

**Reconciliation loop (L0↔L1):** intended matrix vs actual ledger; every gap resolved loudly ("loud or it rots"). A principle, not yet a running control system.

---

## Standing pillars & threads

- **Memory Recall Quality** (parent FRE-435) — the north-star measurement instrument. Extraction ceiling root-caused to the *taxonomy* (FRE-766) → ADR-0109 V2 (live). Recall = multi-path retrieval over a typed living KG (ADR-0103 principle + ADR-0104 arch, live). Memory substrate ADR-0098 (Claims write-path live, idle until consolidation).
- **639 ↔ ADR-0105 reconciliation** (adr thread) — gate-vs-isolate for the System class. FRE-639 (ADR-0098 T3) *gates* System-subject extracted entities (~46% operational noise) via a query-time class filter; ADR-0105/0708 argues *physically isolate* into a separate sysgraph. Reconciliation = fresh adr design: does isolate-don't-gate extend to 639's conversation-extracted System entities, or are they distinct scopes? Feeds the Linear-async channel (priority #5).
- **Agent Vision & Attachments** (ADR-0101 Implemented / ADR-0102 doc chain LOW / ADR-0108 Proposed) — vision core live (local Qwen); cloud spine + cost controls shipped (FRE-691/692/693).
- **Self-improvement loop / System-KG isolation** (ADR-0105 Accepted / ADR-0106 Accepted) — isolated System graph (sysgraph Postgres) shipped + deployed; FRE-717 held for organic outcome input. Unblocks the Linear-async feedback channel.
- **Config Management** (ADR-0099) — single-source role matrix + validator; Implemented. Capstone: config-UI.
- **Seshat Inference** (ADR-0082 / FRE-432) — tier-aware routing measurement; Ph0 confirmed ~75% thinking on trivial SINGLE turns.

---

## Active ADRs

Recent / active (older ADRs → `docs/architecture_decisions/`):

| ADR | Title | Status |
|-----|-------|--------|
| **0119** | Config-management interface (capstone) | **Proposed (PR #542)** — active epic. Impl chain FRE-887 + 888→892 approved but **BLOCKED at step-0**: FRE-879/880 parked on the `artifact_builder`/ExecutionProfile regression; amendment being raised in adr. |
| **0118** | Model-selection layer / user-selectable artifact builder | **Proposed** — folded into ADR-0119 (its L-tier). |
| **0117** | Master signal-collector (`pr_gate`) + ADR index guard | **Implemented (FRE-877 Done, LIVE).** Facts-only collector at master Step 4; never judges/blocks. |
| **0116** | Event-driven dispatch actuation (MCP channels) | **Accepted, Phase-1 LIVE (2026-07-14).** FRE-852/871/872 Done; channel cutover live; FRE-875 (scrape-deletion + secret-durability) parked. Supersedes ADR-0110's transport half. |
| **0113** | Self-driving delivery loop | **SUPERSEDED (2026-07-08).** LLM-review harness falsified in use (hallucinated a security blocker on PR #433); removed (PR #435); FRE-835 + FRE-828 canceled. Survivor: FRE-832. Then the master/build/adr automation was redesigned top-down + re-enabled (2026-07-09) — see Current live-env & MASTER_PLAN_HISTORY. |
| **0112** | Configurable substrate backends | **Accepted** (FRE-809, supersedes ADR-0111). Storage owner-controlled · managed API endpoints under no-train/no-log terms · every substrate component config-selectable per ADR-0099 profile. Impl: FRE-816/820 (config chain). |
| **0110** | External dispatch orchestrator | **Proposed** — dispatch/poll half superseded by the 2026-07-09 redesign; RC substrate + Linear-native dispatch retained. Impl FRE-785–788 shipped. |
| **0109** | Entity-Taxonomy V1→V2 (10-type) | **Accepted** + Amendment 1 (κ 0.900). **Shipped end-to-end** (extractor FRE-771 + recall remap FRE-794 + KG migration FRE-772). |
| **0108** | Stored-artifact vision re-processing | **Proposed** (PR #330). Impl FRE-743–748 + bug FRE-749 Needs-Approval. |
| **0107** | User identity for Claims + log/trace propagation | **Accepted** (PR #327). FRE-738 Done; **FRE-739 merged** (Awaiting Deploy, seam owner) — assembled AC-1/2/3a/3b/4/5 verify needs FRE-740 + a live non-owner request. |
| **0106** | System/User boundary by output_kind | **Accepted**. Children FRE-728–732 Approved (W2). |
| **0105** | Self-improvement pipeline + isolated System graph | **Accepted**, **shipped + deployed** (714/715/716/719/720/721 Done). FRE-717 held at Awaiting Deploy. |
| **0104** | Multi-path retrieval (RRF fuse) | **Implemented** (multipath live + graduated 2026-07-10; AC-6(c) proven live, FRE-724 Done). |
| **0103** | Recall principle (no clean floor; adaptive operating point) | **Accepted**. |
| **0102** | Vision doc/PDF ingestion | **Implemented + LIVE (2026-07-15).** Seam FRE-689 verified; FRE-886 attachments→cloud shipped. Follow-up FRE-885 (token-counter, Needs Approval). |
| **0101** | Agent vision ingestion of attachments | **Accepted** (functionally implemented — FRE-691/669 Done). |
| **0100** | Relevance-bounded recall | **Accepted** (live). |
| **0099** | Config management & validation | **Implemented** (648–652 merged). Next: config-UI capstone + lifecycle audit. Residual: FRE-789 benchmark-YAML drift. |
| **0098** | Memory substrate & lifecycle | **Accepted** (implements 0097). Claims write-path live. |
| **0092** | Context-Compaction Observability | **Implemented**. |
| **0090** | Telemetry Surface Contract | **Accepted** (governs Telemetry Surface Audit). |
| **0089** | Artifact Execution Security | **Implemented** + Addendum A curated `/lib/`. |
| **0088** | Execution Topology Observability | **Accepted**; spine + read surfaces shipped. |
| **0087** | Memory Recall Quality | **Accepted 2026-06-27** (parent FRE-435). |
| **0084** | Pedagogical Architecture (Socratic Tutor) | **Accepted**; M1 Done; M3 gated on M2. |
| **0082** | Tier-Aware Model Selection | Partially superseded by 0084; D1 plumbing may ship (FRE-432). |
| **0081** | Cache-Aware Context Layout | **Core chain COMPLETE + live**; follow-ups in ADR-0081 Extended. |

Earlier ADRs (0040–0080) Implemented/Accepted — see `docs/architecture_decisions/`.

---

## Historical waves (complete — retained as index)

Waves **A/B/C/E/I/J** ✅ complete. **D** planned, impl deferred (FRE-214 §8.7). **F/G** partial. **H** (memory/context) partial. Detail → `git log` + `docs/plans/completed/`. ADR-0081 cache chain (D1/D4/D2/D3) shipped + live.

---

## How This File Works

- **Linear is the task tracker** — this file tracks priorities and sequencing only. Do not re-enumerate per-ticket tables here (they drift).
- **Two-file split (established 2026-07-08):** `MASTER_PLAN.md` = uniquely the plan (current state + priorities + sequencing + Needs-Approval). `MASTER_PLAN_HISTORY.md` = append-only grepable narrative of shipped work + decisions, NOT auto-loaded. `/prepare-reset` moves completed narrative from here → history each reset; a header longer than ~1 screen means the move is overdue.
- **Dispatch** = Linear (state + `stream:*` label + priority + blocked-by relations; contract in `.claude/skills/lifecycle-rules.md` § Dispatch). Priority order = the numbered list above. Cross-project sequencing = [`sessions/2026-07-02-priority-sequencing.md`](sessions/2026-07-02-priority-sequencing.md).
- **Update after every ship**: apply any dispatch mutations in Linear + bump "Last updated".
- **Specs** → `docs/specs/` · **ADRs** → `docs/architecture_decisions/` · **Session plans** → `docs/superpowers/plans/` · **Archive** → `docs/plans/completed/`.
