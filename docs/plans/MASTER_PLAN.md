# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest) — per-ticket state.
> **Source of truth for priorities**: this file (sequencing only; do not re-enumerate Linear here) — keep it *concise*.
> **This file is UNIQUELY the plan**: current live-env / standing state + active priorities + sequencing + Needs-Approval. **Completed / superseded narrative does NOT live here** — it moves to [`MASTER_PLAN_HISTORY.md`](MASTER_PLAN_HISTORY.md) (grepable, NOT auto-loaded). `/prepare-reset` performs the move each reset; this session's *decisions* live in [`LAST_SESSION.md`](LAST_SESSION.md).
> **Last updated**: 2026-07-10

## Current live-env & standing state

- **Delivery automation LIVE** (systemd: gating-watcher + dispatch-orchestrator). Model: **watcher triggers master** (ability-not-obligation — master leads "Gating PR #X"); CI-red → plain message to the worker; bounce → master `send-keys` the worker directly; workers self-complete to CI-green; `/prime-worker` deleted; deploy sentinel removed; prime-master → 9-step; adr has an Explore mode. **Build dispatch = Linear labels** (label an Approved+unblocked ticket on a free stream → orchestrator auto-launches). Full contract: `.claude/skills/lifecycle-rules.md`.
- **Standing review model (do NOT re-open):** master reviews PRs directly; CI + CodeQL own deterministic security + correctness; a tool-equipped subagent offloads a large review when master's context is full; adversarial fresh-context subagent before a one-way door.
- **Embedder:** OVH-managed **Qwen3-Embedding-8B @ 1024 dims** (`AGENT_SUBSTRATE_PROFILE=managed_embedder`; managed-only, fails open if OVH down; local `cloud-sim-embeddings` container STOPPED). All 6,109 KG entities re-embedded to 8B/1024.
- **Reranker:** Voyage **rerank-2.5** primary + Mac-tunnel 4B fallback (FRE-851 live).
- **Recall:** multipath **LIVE + GRADUATED** — 2-arm (multi_query + lexical), similarity floor 0.60, permanent `latency_ms` telemetry (p50 ~1.3s ≪ 17s ceiling post-Voyage); **ADR-0104 Implemented**. Relevance-bounded recall (ADR-0100) live.
- **Substrate:** app runs as the restricted `seshat_app` Postgres role (off the superuser); sysgraph physically isolated (ADR-0105/0112). Neo4j / ES / Postgres healthy.
- **Taxonomy:** ADR-0109 **V2 10-type** live end-to-end (extractor + recall remap + prod KG migrated; 0 V1 remnants, joinability green).
- **Identity:** ADR-0107 — `assert_claim` resolves by acting `user_id`; `is_owner` = admin flag. FRE-738 deployed.
- **Config:** ADR-0099 config management **Implemented**. Linear keys rotated to `pass`.

## Open threads (current)

- **build2:** dry — ADR-0112 config chain complete (AC-9 / FRE-820 **Done** 2026-07-10; 816/819/821/817 Done, 818 canceled). Only Approved NEXT is **FRE-595** (Low, docs). ADR-0112 assembled-seam assertion (AC-2/3/4/6) is master-owned and still open (AC-3/FRE-818 was canceled — the seam definition needs a look).
- **build1:** dry — **FRE-739** (ADR-0107 assembled seam: T2 log-propagation + full-AC verification on a live non-owner request) Approved-but-unlabeled; **FRE-807** (behavioral tool-runway floor, Tier-2) parked-unlabeled.
- **adr:** idle (the automation redesign was the last adr work; ADR-0113 / FRE-828 canceled — see history).
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

**Sequencing rationale (current):** build2 = dry (ADR-0112 config chain complete; only FRE-595 Low docs remains). build1 = dry
(FRE-739 is the ADR-0107 assembled seam, Approved-but-unlabeled — dispatch when the owner sizes it).
The ADR-0105 sysgraph self-improvement chain has shipped; FRE-717 alone remains, held at Awaiting
Deploy for organic outcome input.

**Parked / held (rationale only — not dispatched, no stream label):** ADR-0102 vision-doc chain
FRE-682–689 (un-paused but LOW priority — sequenced behind Memory) · Inference ADR-0094/0095 trees
FRE-600–604 / 607–611 (held pending FRE-432/516 measurement) · FRE-713 (trigger-gated, Backlog) ·
FRE-760 (re-scoped Low, blocked by FRE-773).

---

## Priority order (owner, 2026-07-04) — all ladder up to Seshat Pedagogical Architecture (M3)

1. **Agentic Vision** — ADR-0101 **Implemented** (FRE-691/669 Done, AC-10b outlier-waived); ADR-0102 un-paused but LOW; ADR-0108 vision re-processing Proposed.
2. **Memory Recall** — hot path. Taxonomy/extraction: ADR-0109 **V2 live end-to-end**. Recall multipath: ADR-0103 Accepted / **ADR-0104 Implemented** (multipath live + graduated).
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
| **0113** | Self-driving delivery loop | **SUPERSEDED (2026-07-08).** LLM-review harness falsified in use (hallucinated a security blocker on PR #433); removed (PR #435); FRE-835 + FRE-828 canceled. Survivor: FRE-832. Then the master/build/adr automation was redesigned top-down + re-enabled (2026-07-09) — see Current live-env & MASTER_PLAN_HISTORY. |
| **0112** | Configurable substrate backends | **Accepted** (FRE-809, supersedes ADR-0111). Storage owner-controlled · managed API endpoints under no-train/no-log terms · every substrate component config-selectable per ADR-0099 profile. Impl: FRE-816/820 (config chain). |
| **0110** | External dispatch orchestrator | **Proposed** — dispatch/poll half superseded by the 2026-07-09 redesign; RC substrate + Linear-native dispatch retained. Impl FRE-785–788 shipped. |
| **0109** | Entity-Taxonomy V1→V2 (10-type) | **Accepted** + Amendment 1 (κ 0.900). **Shipped end-to-end** (extractor FRE-771 + recall remap FRE-794 + KG migration FRE-772). |
| **0108** | Stored-artifact vision re-processing | **Proposed** (PR #330). Impl FRE-743–748 + bug FRE-749 Needs-Approval. |
| **0107** | User identity for Claims + log/trace propagation | **Accepted** (PR #327). FRE-738 Done (deployed + AC-4 backfill); **FRE-739 = assembled seam** (Approved-but-unlabeled). |
| **0106** | System/User boundary by output_kind | **Accepted**. Children FRE-728–732 Approved (W2). |
| **0105** | Self-improvement pipeline + isolated System graph | **Accepted**, **shipped + deployed** (714/715/716/719/720/721 Done). FRE-717 held at Awaiting Deploy. |
| **0104** | Multi-path retrieval (RRF fuse) | **Implemented** (multipath live + graduated 2026-07-10; AC-6(c) proven live, FRE-724 Done). |
| **0103** | Recall principle (no clean floor; adaptive operating point) | **Accepted**. |
| **0102** | Vision doc/PDF ingestion | **Accepted** — un-paused but LOW priority. |
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
