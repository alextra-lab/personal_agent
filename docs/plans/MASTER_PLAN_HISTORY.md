# Master Plan — History (grepable narrative)

> **This file is the append-only, grepable history of the project's decisions and shipped work.**
> It is **NOT** auto-loaded on re-prime — `MASTER_PLAN.md` (the concise plan) is. Search here when you
> need "when did X ship / what was the reasoning for Y." `/prepare-reset` appends compacted narrative
> here as it trims `MASTER_PLAN.md`.
>
> **Split established 2026-07-08.** Pre-split narrative (through ~2026-07-04) lives in
> [`completed/2026-07-04-master-plan-archive.md`](completed/2026-07-04-master-plan-archive.md). The dense
> 2026-07-05→09 header narrative was migrated here **2026-07-10** (the flagged deep purify), leaving
> `MASTER_PLAN.md` as uniquely the plan.

---

## 2026-07-15/16 — vision docs/PDF SHIPPED live; config-UI epic stood up + blocked at step-0; config-audit miss

- **ADR-0102 vision docs/PDF SHIPPED + LIVE (2026-07-15) → Implemented.** Seam T9/FRE-689 AC-SEAM verified end-to-end live: gateway rebuilt from `a43dc817` + PWA v31 (owner-authorized); native PDF `document` block on Sonnet (correct read, ~$0.045), cost gate ($0.5244 held for confirm vs $0.50 threshold), vision path (local Qwen rasterized), joinability all green. Children T1–T8 Done. **Live gotcha:** the deployed local Qwen is vision-capable, so the AC-SEAM fail-closed leg is unreachable live (a Local override succeeds rather than failing closed) — proof stays the FRE-684 unit test. **Follow-up FRE-885 (Needs Approval):** cost-estimator token counter rejects the `document` content-block type → pre-flight token counts undercounted for document turns (telemetry-only; billed cost unaffected).
- **FRE-886 SHIPPED + LIVE (deployed 2026-07-15).** Attachments now default to cloud/Sonnet (Auto → escalation model) for images + PDFs; $0.50 gate unchanged; reversible via `attachment_default_processing_target=local` + rebuild. Owner-requested during the vision go-live.
- **FRE-884 merged (Awaiting Deploy) → ADR-0086 retired.** Removed the orphaned artifact-decomposition path. Deploy **batched, low-urgency** (near-zero observable change) — deploy on the next gateway rebuild.
- **NEXT EPIC stood up — Seshat config-management interface (ADR-0119, owner-directed 2026-07-15).** The capstone surface on ADR-0099's single-source role matrix + validator; model-selection (ADR-0118) folded in. ADR-0119 authored (PR #542, Proposed) + impl chain filed/approved (umbrella FRE-887 + L1–L5 = FRE-888→892). **BLOCKED AT STEP-0 (2026-07-16):** FRE-879/880 (the absorbed ADR-0118 backend) parked — code-review caught a real regression: `artifact_builder` as a matrix role resolves to its default and **ignores ExecutionProfile, so a local-profile artifact build silently hits cloud Haiku**. ADR-0119 doesn't resolve it → whole L1–L5 chain blocked. Being raised as an **ADR-0119 amendment in the adr session** (owner direction). Working impl uncommitted on `fre-879-artifact-builder-role-cost-lane` (do NOT discard). FRE-894 filed (config-UI Phase-2 placeholder, trigger-gated). FRE-883 canceled (superseded by ADR-0119 L5).
- **Config-parameter usage audit (FRE-893) — MERGED THEN INVALIDATED.** The audit report (PR #543) never read the deployed `/opt/seshat/.env`, so 64 of 73 real `AGENT_*` overrides were falsely flagged "hardcode candidate / never overridden." The report's own Limitations section admitted the gap — a self-admitted fatal flaw that master should have bounced at the gate, not merged. Owner caught it ("This alone invalidates the audit!!"). Fixed: reverted the report + CONFIG_INVENTORY §10 (PR #545), reopened FRE-893, re-dispatched the redo on build2 (must read the deployed `.env` this time). Lesson → memory `feedback_gate_read_stated_limitations_of_audits`.
- **cc-explore2 (ephemeral) — cost-gate provider-pool deliberation.** Verdict: NOT a flat per-provider pool; use `on_denial: raise` vs `nack` denial-semantics with a reserved floor (budget.yaml already encodes this); main_inference + captains_log + insights collide on Anthropic Sonnet. Note captured at `docs/research/2026-07-16-cost-gate-provider-pool-deliberation.md`. Owner correction → memory `feedback_explore_is_conversation_master_enables_not_operates` (explore is the owner's conversation; master enables RC access + steps back, never inject-run-harvest).
- **Ops housekeeping.** Dedup'd the GitHub MCP (removed the direct plaintext-PAT entry from `~/.claude.json`, kept the env-var plugin one — recommend rotating that PAT); updated claude (2.1.211) + codex (0.144.5) + removed the npm codex shadow.
- **ADR-0116 Phase-1 + ADR-0117 tooling (both LIVE, from the 2026-07-14 window).** Per-seat MCP-channel push replaced poll + send-keys idle-scrape (FRE-852/871/872 Done; FRE-875 parked). `scripts/pr_gate.py` — the ADR-0117 deterministic signal collector at master SKILL Step 4 (facts only, never judges/blocks) + ADR index guard (`scripts/next_adr.py`) shipped (FRE-877 Done). ADR-0118 model-selection = Proposed (folded into ADR-0119).

## 2026-07-13 — heavy delivery: corpus de-noise, owner-identity, vision T2/T3, ADR-0116 channel AC-2 proven live

- **ADR-0115 existing-corpus de-noise DONE (owner-authorized prod ops).** FRE-865 backfill classed all 7,992 `class=None` entities → 6,215 World + 410 Personal + 1,372 System-marked (fail-open 3.2%, ~$0.31, gpt-5.4-mini). FRE-868 eviction removed those 1,372 from Core (1,014 findings→`sysgraph.stat`, 358 ephemeral dropped; 126 MB snapshot retained). **Core 7,997→6,625, all World/Personal, 0 System** — ADR-0114 existing-corpus de-confound complete. Gotcha: sysgraph DSN uses docker host `postgres`; host-run scripts need an `@localhost` override (memory).
- **FRE-632 owner-identity unified + deployed.** The two split "Alex" nodes (`:Entity` holding the knowledge, `:Person` holding `is_owner`) folded into one `:Person:Entity` via apoc mergeNodes (degree 478→432 dedup, 0 self-loops, node backup retained). Re-tiered **Opus** at dispatch (design decision + prod migration). Forward-fix held across gateway bootstrap (no re-fork).
- **Gateway rebuilt to `7131c011`** (owner-authorized) — shipped FRE-632 forward-fix + **FRE-869** cost-attribution fix (entity_extraction/captains_log/insights now bill their own budget lanes, not `main_inference`).
- **Vision docs/PDF chain (ADR-0102):** FRE-682 (T2 capability flag) + FRE-683 (T3 doc-resolution module) merged; T4/684 building. Owner-directed after "integrate files."
- **FRE-739 (ADR-0107 user_id log propagation) merged** — bounced first for a mis-tier (Standard src, no codex plan-review, off the ticket's "mechanical" self-label); master ran codex itself (clean, no blocking) to close the gate rather than bounce-loop. Awaiting Deploy; ADR-0107 seam owner (needs FRE-740 + a live non-owner request).
- **ADR-0116 channel / event-driven dispatch:** FRE-852 (ADR, spike-proven) merged. **FRE-871 AC-2 PROVEN LIVE** — master ran the headless-channel test: a localhost POST fired a turn in an idle RC seat; the individual Claude Max account DOES honor a custom channel allowlist (**Risk row 1 resolved**); the missing enablement step was `claude plugin install`; headless auto-dismisses new-MCP dialogs (cutover needs `enableAllProjectMcpServers`). Chain **consolidated** — FRE-873/874 canceled into FRE-872 (decompose-when-risky, collapse-when-proven); **FRE-875** = cutover (ask-first, retires the idle-scrape).
- **Config enforcement (cc-explore handoff):** `Config guard` is now a **required** merge check (owner made the ruleset UI change — master's PAT lacks Administration scope). **FRE-876** (ADR-0099 D4 field-self-documentation check) filed Approved-parked.

## 2026-07-10 — multipath recall graduated (ADR-0104 Implemented)

- **FRE-724 Done.** Added a permanent `latency_ms` float to the `multipath_recall` telemetry event
  (PR #457, merge 3be36fd7) — the recall core carried no timing, so AC-6(c) (p50 ≤ 17s) was
  unmeasurable. Gateway rebuilt (owner-authorized always-ask). **AC-6(c) proven live:** 12 recall
  probes through `/memory/query` against the prod graph, `latency_ms` read back from ES — n=12, p50
  1332ms, p95 2241ms, max 3599ms, a 13× margin under the 17s ceiling. The ceiling came from FRE-679's
  ~15.7s VPS-CPU reranker; the Voyage rerank-2.5 cutover (FRE-851) collapsed it to sub-second. Both
  arms fused (multi_query + lexical), rerank fired, fused-set capped at 25. ADR-0104 → **Implemented**.

## 2026-07-09 — master/build/adr automation redesigned top-down + re-enabled

- Owner judged the automation over-built; redesigned it top-down and re-enabled it (PR #454 skills,
  #455 watcher). **New model (do NOT re-open):** watcher **triggers** master (ability-not-obligation —
  master leads "Gating PR #X"); CI-red → plain message to the worker; bounce → master `send-keys` the
  worker directly (no marker/monitor); workers self-complete to CI-green; `/prime-worker` **deleted**
  (folded into build/adr § respond-to-a-poke); deploy sentinel **removed** (build/adr deny kept);
  prime-master → 9-step current-state→target→process; adr gained an Explore mode. Watcher + dispatcher
  LIVE on the new code. This supersedes the 2026-07-08 "ADR-0113 reversed / gating-watcher stopped" text.

## 2026-07-08 — dispatch tooling + review-gate + ticketing process

- **FRE-847** `context_probe` shipped — headless per-session context% + idle from the transcript JSONL
  (`input+cache_read+cache_creation`, matches `/context`). Rejected the statusLine approach (would edit a
  CC-managed file); `idle_s`/`state` is a weak proxy, the pane (`session_is_idle`) is authoritative.
- **FRE-848** watcher context-pressure alert shipped + deployed — gating-watcher nudges cc-master to
  checkpoint at ≥70% idle; watcher restarted onto the new code. Dogfooded the new review gate (build
  found + folded 2 real bugs, improved on the spec).
- **Review gate → shift-left:** build self-reviews (`/code-review` + `/security-review`, effort-sized) once
  pre-PR, hands master a self-review summary; master validates as executive + decides, keeps veto over
  fold-ins. Encoded in build/master skills.
- **Anti-over-ticketing:** a ticket is an objective (user story), not a boxed single change; build folds
  non-ADR supporting changes + review findings into the PR — no paper-trail tickets. Encoded in build/master.
- **New skills:** `/prepare-reset` (safe wind-down + decision-distillation + MASTER_PLAN compaction, bookend
  to `/prime-master`). MASTER_PLAN split into concise-plan + this history file.

### 2026-07-08 (later) — dispatch fully live + Voyage direction

- **Dispatch automation fully live end-to-end.** Orchestrator installed + enabled as a systemd daemon
  (`seshat-dispatch-orchestrator.service`). Root cause of "never worked / was removed": the committed
  unit had no `PATH` → `claude` not found → fail-to-start (fixed PR #445 `Environment=PATH`). Launcher
  slot-collision fixed (PR #448 — kill+recreate the persistent tmux slot). Watcher always sends
  `/master <id>` (PR #447 — the idle screen-scrape kept a busy master uninformed; workers keep the idle
  guard). Proven: orchestrator auto-launched FRE-820. ⇒ **build dispatch = Linear labels** (send-keys
  `/build` retired). *(The 2026-07-09 redesign further evolved this; see above.)*
- **Shipped + deployed + Done:** FRE-710 (Captain's-Log reflection → per-session cadence) · FRE-718
  (Postgres RAM/CPU tuning: cpus 1.0, right-sized cache, pg_stat_statements + scheduled sysgraph vacuum).
- **Reranker → Voyage (FRE-851).** Voyage rerank-2.5 primary + Mac-tunnel 4B fallback. Latency
  in-pipeline ~250ms vs 4B ~2.5s (~10×); FRE-695 quality-equivalent (J 0.73≈0.747); OVH has no reranker.
  Reranker = latency/reliability fix, NOT a recall-ceiling fix.
- **Owner corrections (saved to memory):** a bad eval is discarded, not reframed as a "lower bound";
  check stored evals/research/config BEFORE running anything (esp. paid). `localhost:8000` dead SLM
  endpoint retired → tunnel (PR #451).

## 2026-07-07 — ADR-0105 loop shipped, board reconciliation, identity, ADR-0113 reversal

- **ADR-0105 self-improvement loop shipped + deployed** (720/714/715/716/721/719 Done); sysgraph live,
  generation-time dedup live (FRE-721 AC-9 read-before-emit fired `decided`×1), funnel dashboard live.
  FRE-717 held at Awaiting Deploy (AC-6 outcome-ingestion no organic input yet).
- **Embedder cutover:** OVH-managed Qwen3-Embedding-8B @ 1024 dims live (FRE-821, deployed ~07:18 UTC);
  6,109 KG entities re-embedded; local `cloud-sim-embeddings` container stopped (~2.8 GB freed). Dim 1024
  not native 4096 (owner caught a confound; FRE-694 sweet-spot confirmed; managed-embed dim fix FRE-826).
- **Board reconciliation (master):** FRE-817/809/630/721/795 flipped Done with evidence comments.
- **Multipath flipped ON** (flags + floor 0.60 live ~13:25 UTC); functionally working (2-arm), but the
  `multipath_recall` event emitted no latency field → p50 unmeasurable — graduation held pending a small
  telemetry add. *(Resolved 2026-07-10, above.)*
- **ADR-0113 self-driving delivery loop SUPERSEDED / REVERSED.** LLM-review harness (FRE-829–834 merged)
  falsified in use — the pr-gate reviewer hallucinated a false-positive security blocker on PR #433 twice.
  Harness removed (PR #435); FRE-835 + FRE-828 canceled; gating-watcher stopped+disabled. Survivor:
  FRE-832 (prime-master checkpoint). *(Automation re-designed + re-enabled 2026-07-09, above.)*
- **FRE-738 shipped + deployed** (~11:15 UTC, merge 124ddcc): `assert_claim` resolves by acting `user_id`,
  not the `is_owner` singleton (ADR-0107 T1). AC-4 5-claim re-attribution done — 5 mis-attributed HAS_FACT
  edges re-pointed off the owner (live graph Alex 12 / Laurent 4 / Susan 1). FRE-739 owns the assembled seam.

## 2026-07-06 — sysgraph chain, V2 taxonomy cutover, dispatch automation built, ADR-0112

- **sysgraph chain shipped + deployed** (coordinated live deploy ~07:04 UTC): FRE-720 (separation-probe →
  `fallback`), FRE-714 (isolated sysgraph store, AC-2 proven), FRE-808 (app dropped off the `agent`
  Postgres superuser — sysgraph denied at the permission layer for the live app credential), FRE-715
  (converge producers). Gateway runs as restricted `seshat_app`; migrations 0014+0015 applied; health +
  joinability green.
- **ADR-0109 V2 taxonomy cutover shipped end-to-end:** 10-type extractor (FRE-771) + recall-consumer remap
  (FRE-794) live in the gateway (rebuilt 09:54); FRE-772 KG migration ran against prod Neo4j — 0
  Technology/Topic/Concept remnants, joinability green — batched + cost-gated (FRE-800/801, ~$0.43, ~38×
  fewer calls). Also: FRE-796 web-tools fix · FRE-798 ruff-hook `--unfixable F401` · FRE-677/795
  test-hygiene · FRE-790 boundary probe (κ 0.858) · FRE-797 = no-change.
- **ADR-0099 config management Implemented** (FRE-648–652 Done).
- **ADR-0110 dispatch automation built + proven live end-to-end:** T1 resolver (785), T2 launcher (786),
  T3 orchestrator (787), `fetch_board` 400 fix (804), T4 ops (788), prime-worker refactor (806). Live
  assembled-seam demo FRE-472 (resolve→launch→build→PR→merge→advance). Settled autonomy posture:
  orchestrated sessions run ONLY kick-off skills; owner HITL via RC; `--dangerously-skip-permissions`
  never; master's gate unchanged.
- **ADR-0112 Accepted** (FRE-809, supersedes ADR-0111 "fortress"): storage owner-controlled · managed API
  endpoints under no-train/no-log terms · every substrate component config-selectable per ADR-0099 profile ·
  no bigger box / no 2nd box / no GPU tier. ADR-0111 → Superseded; #404 GPU addendum closed moot. Infra
  tickets FRE-810–815 need reconciliation. Linear keys rotated to `pass`. FRE-620 KGQ quality-monitor fix
  deployed (false daily high-sev anomaly killed).

---

## 2026-07-12 — ADR-0115 knowledge-class axis shipped + proven live; FRE-858 triage (moved from MASTER_PLAN)

**ADR-0115 (knowledge-class axis) — Accepted → Implemented + deployed live in one arc** (from the
cc-explore memory-ADR drift audit). Chain: FRE-863 emission (two-axis output_kind + World/Personal
class; Stance is a HAS_STANCE edge, not an entity class) → FRE-864 persistence (Literal-typed
Entity.class + index) → FRE-728 dispatch (isolation by absence-of-write; System → sysgraph.stat) →
FRE-865 backfill + FRE-868 eviction (test-substrate scripts; prod runs pending). Deployed SHA
`c51a7486` (migration 0019 as admin role + gateway rebuild); proven live on a sanctioned owner turn
(trace `2564b7c5`): 5 World entities classed vs 0/7992 pre-deploy, 4 System findings → sysgraph, 0
leaked. Supersedes ADR-0106 + ADR-0098-§D1; refines ADR-0097. Also shipped: FRE-860 session retention
(180d soft-prune), FRE-869 cost-attribution fix (entity_extraction was billing main_inference),
PR #492 build-seat test-DB permission allowlist. Residual for the ADR-0114 de-confound of the EXISTING
corpus = the FRE-865 + FRE-868 prod ops-runs (owner-gated).

**FRE-858 Memory-Recall triage — EXECUTED (2026-07-11).** Owner's 2 strategic calls: ADR-0098 impl
shelved (FRE-639/642 canceled, 640/641/713 parked); ADR-0106 W2 kept (dispatch after ADR-0114).
Verify-6 dispositioned (FRE-776 canceled; 633/764/605/761 parked). ~4 cancels + ~13 parks total.
FRE-768 shipped from the keep-set. The verified keep-bugs (632/733/751/762/760/805/850) remain parked
Approved-unlabeled — still tracked in MASTER_PLAN.

---

## 2026-07-13 (evening) — ADR-0116 channel delivery Done + cutover drift-guard (moved from MASTER_PLAN)

**FRE-872 (ADR-0116 T2–T4 consolidated channel delivery) — Done.** PR #514, merged **dormant**
(`topology.mode` stays send-keys for every seat → zero live behavior change; the AC-5 not-yet-cutover
test proves byte-for-byte identical dispatch). Ledger transport tagging + per-seat mode + structured
channel payload + send-keys fallback + dependabot boundary guard. Code-provable AC halves unit-proven
(215/215 module + 12/12 node); live AC-1/3/6 halves owned by the FRE-875 seam. No deploy.

**FRE-875 (ADR-0116 T5 cutover seam) — Phase A merged; master-driven.** PR #516: made
`StreamTopology.mode` the **single source of truth** for a seat's channel state — the launcher derives
channel-wiring from it (`--channels` iff `mode == "channel"`), and the independent `--channels` flag /
`LauncherCapabilities.channels` was removed, so the launch shape can't drift from the delivery mode the
watcher reads. Inert (all seats send-keys). Master authored + self-reviewed (workflow-backed high-effort
code-review) rather than dispatching to build2, because 875 rewires the dispatch fabric the build seats
run on (reflexive) — same posture as the FRE-871 AC-2 live proof. Self-review caught + fixed a docstring
overclaim ("atomically, by construction" — the flip→relaunch window is covered by the FRE-872 fallback,
not atomicity) and surfaced the Phase-B ordering (provision the channel secret before flipping a seat).
**FRE-875 stays In Progress (multi-phase); Phase B = the live per-seat cutover** (adr→build2→build1→
master, delete the idle-scrape last), ask-first deploy, deferred to a fresh master — runbook + prereqs
on the ticket. 873/874 canceled (consolidated into 872). Gateway unchanged this session (`7131c011`).
