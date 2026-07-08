# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest) — per-ticket state.
> **Source of truth for priorities**: this file (sequencing only; do not re-enumerate Linear here).
> **Last updated**: 2026-07-08 — this session's *decisions* are in the **Recent decisions** block just below the header (read it first on re-prime); the live-env facts in this paragraph are unchanged since 07-07. **LIVE-ENV CHANGE (critical for re-prime):** the embedder is now the **OVH-managed Qwen3-Embedding-8B @ 1024 dims** (`AGENT_SUBSTRATE_PROFILE=managed_embedder` in `.env`; managed-only, no live local fallback, fails open if OVH down) — FRE-821 Done, deployed ~07:18 UTC; all 6,109 KG entities re-embedded to 8B/1024; the local `cloud-sim-embeddings` 0.6b container is **STOPPED** (~2.8 GB freed, AC-5). Dimension is **1024 not native 4096** (owner caught a confound; FRE-694 sweet-spot confirmed on cloud via a re-run, docs/research/2026-07-07-fre-817-dimension-confound-rerun.md; managed-embed dimension fix = FRE-826 Done). **ADR-0105 self-improvement loop SHIPPED + deployed** (720/714/715/716/721/719 Done; **FRE-717 correctly held at Awaiting Deploy** — code+schema live, but AC-6 outcome-ingestion has zero organic input yet [outcome/produced/signal all empty], so its live proof is pending a promoted proposal reaching a terminal state; sysgraph live, generation-time dedup live [FRE-721 AC-9 read-before-emit fired `decided`×1 / `degraded`×0 on the running stack], funnel dashboard live). **Board reconciliation (2026-07-07, master):** the Awaiting-Deploy queue was reconciled — FRE-817/809/630/721/795 flipped to Done with evidence comments (721 AC-9 verified live; 795 test-PG volume recreated + auth verified on :5433); FRE-717 held (above); **FRE-724 held** — the master-owned, deploy-gated multipath-recall flag-flip live proof (gateway rebuild = always-ask + owner-coordinated A/B) is genuinely pending, not bookkeeping. **[later 2026-07-07]** Multipath recall **flipped ON** (FRE-724 flags + floor 0.60 live on the gateway, recreated ~13:25 UTC); functionally working (2-arm: multi_query+lexical), but `multipath_recall` **emits no latency field** so **p50 is unmeasurable from telemetry** — graduation/verification blocked pending a small telemetry add; auto-rollback stands if p50 breaches 17s once measurable. **ADR-0113 self-driving delivery loop SUPERSEDED / REVERSED (2026-07-08).** The LLM-review harness was built (FRE-829–834 merged) then **falsified in use** — the pr-gate reviewer hallucinated a false-positive security blocker on PR #433 twice. Master already uses tool-equipped, verify-first, advisory subagents correctly (#427/#429); the harness duplicated CI (CodeQL + pytest/mypy/ruff) and subtracted from the working behavior. **Harness removed (PR #435); FRE-835 + FRE-828 CANCELED; gating-watcher service STOPPED + disabled.** No replacement. **Survivor: FRE-832** (prime-master checkpoint-to-durable-state, merged). **STANDING MODEL NOW (do not re-open): master reviews PRs directly (owner-triggered `/master`); CI + CodeQL own deterministic security + correctness; a plain tool-equipped subagent offloads a large review when master's context is full; adversarial fresh-context subagent before a one-way door.** **Dispatch gating watcher LIVE + fixed** (FRE-823 + FRE-825 idle-detection fix): event-driven `tmux send-keys` — watcher pokes `/master <PR#>` into cc-master + `/prime-worker` into workers; polling loops removed (FRE-822 cost incident). **FRE-738 SHIPPED + DEPLOYED (2026-07-07, ~11:15 UTC, merge 124ddcc):** assert_claim resolves by acting user_id, not the is_owner singleton (ADR-0107 T1); gateway rebuilt, health green. **AC-4 5-claim re-attribution DONE** — master re-derived per session_id and re-pointed the 5 mis-attributed HAS_FACT edges (4 Laurent, 1 Susan) off Alex; live graph now Alex 12 / Laurent 4 / Susan 1, each claim on exactly its true user, none on the owner. ADR-0107 does NOT close here — **FRE-739 owns the assembled seam** (T2 log-propagation + full-AC verification against one live non-owner request); FRE-739 is Approved-but-unlabeled (build1 is now dry). **In DISCUSSION (adr session):** **FRE-828** — capstone ADR "autonomous actuation + distributed, human-gated judgment; master as a bounded coordinator" (watcher→master-only sensor→brain→hands model; context-alerting watcher; specialist subagents; deploy stays human-gated). `/adr` skill hardened to a discussion-first HARD gate. **Open config chain:** FRE-820 (dev/test isolation, build2). Identity model settled by ADR-0107 (user=`user_id IS NOT NULL`; `is_owner`=admin flag; no rename — FRE-827 canceled). Prior-session detail below. **[2026-07-06]** sysgraph chain live: FRE-720/714/808/715 shipped + **deployed** — app off the Postgres superuser, sysgraph isolation live; **ADR-0112 Accepted** supersedes ADR-0111; Linear keys rotated to `pass`. **ADR-0109 V2 taxonomy cutover SHIPPED end-to-end this session:** the 10-type extractor (FRE-771) + recall-consumer remap (FRE-794) are live in the gateway (rebuilt 09:54 from main), and the FRE-772 KG migration ran against prod Neo4j — **0 Technology/Topic/Concept remnants, joinability probe green** — batched + cost-gated via FRE-800/FRE-801 (~$0.43, ~38× fewer calls). **Correction:** the prior `0f46a84`-deployed note was inaccurate (baked from a stale tree still serving V1); the real V2 deploy is this session's rebuild. Also shipped this session: **FRE-796** web-tools fix (Perplexity→`sonar-reasoning-pro` + searxng chefkoch category-leak) live · **FRE-798** ruff-hook `--unfixable F401` · **FRE-677/795** test-hygiene · **FRE-790** boundary probe (κ 0.858) · **FRE-797 = no-change** (boundary clears; residual is a cross-provider artifact a definition can't fix). ADR-0099 config management **Implemented** (FRE-648–652 Done). **ADR-0110 dispatch automation — BUILT + PROVEN LIVE END-TO-END this session:** T1 resolver (FRE-785), T2 launcher (786), T3 orchestrator (787), `fetch_board` 400 fix (804), T4 ops (788), prime-worker refactor (806) all Done. **Live assembled-seam demo shipped FRE-472:** the orchestrator resolved build1's NEXT → launched a per-session RC tmux session seeded `/build FRE-472` → owner answered a prompt off-box (AC-3 live) → PR #393 → master merge → advance (`--once` tick cleared the record on the terminal merge state). **Production cutover is phased + documented** (`docs/runbooks/dispatch-orchestrator.md` § Production cutover): worktrees sync main (allowlist PR #395 live on main) → **Phase A** supervised `--once` + prove AC-5 (two-worker pytest-lock) & adr → **Phase B** supervised `--loop` in tmux → **Phase C** systemd enable. **SETTLED autonomy posture (do NOT re-open):** orchestrated sessions run ONLY kick-off skills (`prime-worker`/`build`/`adr`/`loop`, allowlisted in `.claude/settings.json`); owner is HITL via RC for everything else; `--dangerously-skip-permissions` never; master's gate unchanged. **FRE-620** KGQ quality-monitor fix merged **and deployed** (false daily high-sev anomaly killed, freshness revived). FRE-699 recall/rerank + FRE-472 conversational-runway research shipped. **Phase-A cutover EXECUTED (2026-07-06):** the ADR-0105 sysgraph chain is being delivered live via the automation — resolve→launch→build→PR→merge→advance proven on real work across all three streams. **SHIPPED + DEPLOYED this session (coordinated live deploy ~07:04 UTC 2026-07-06):** FRE-720 (separation-probe → `fallback`), FRE-714 (isolated sysgraph store, AC-2 proven), **FRE-808** (app dropped off the `agent` Postgres superuser — sysgraph now denied at the permission layer for the *live* app credential), FRE-715 (converge producers, source discriminator) — all **Done**. The gateway runs as the restricted `seshat_app` role; migrations 0014+0015 applied; health green, joinability green. **ADR-0112 Accepted** (FRE-809, **supersedes ADR-0111** "fortress"): storage owner-controlled by default · API endpoints managed under no-train/no-log terms · every substrate component config-selectable per ADR-0099 profile · embedder = OVH `Qwen3-Embedding-8B` + self-hosted 8B fallback (corpus A/B decides) · no bigger box / no 2nd box / no GPU tier. ADR-0111 → Superseded; #404 GPU addendum closed moot. **Infra ticket reconciliation PENDING** — FRE-810–815 (filed under 0111) need cancel/reshape/rename + new 0112 impl tickets. FRE-718 parked. **ADR-0105 DAG (current):**
- **build1:** 714 ✅ · 808 ✅ → **FRE-716 (next head, unblocked)** → 717 (needs 716) → 719 (needs 716).
- **build2:** 720 ✅ · 715 ✅ → 721 (needs 716+717). FRE-718 parked off-stream.

Dispatch runbook: `docs/runbooks/dispatch-orchestrator.md` § Production cutover. Phase-A `--once` ticks are run manually (master) with owner RC monitoring; Phase B (`--loop`) / C (systemd) not yet enabled.

**Approved/parked:** FRE-807 (behavioral tool-runway floor — FRE-472 R1, Tier-2, unlabeled). **Linear keys ROTATED to `pass`** (owner revoked the old shared key): `CC_LINEAR_API_KEY` = master/dispatch + workers (workers via `mcpServers.linear`, rewired to it); `seshat/AGENT_LINEAR_API_KEY` = the gateway app (`.env` updated + gateway rebuilt, health green); plaintext allow-rules purged; old key never committed. **Pending hygiene tickets (to file):** (a) `load_linear_key`→prefer `pass show CC_LINEAR_API_KEY` so dispatch uses CC not the seshat key from `.env`; (b) switch `/build`+`/adr` Linear to the direct `tools/linear.py` and drop `mcpServers.linear` (retire the config key-sprawl). **Open owner items:** FRE-805 (sibling stale `:Conversation`, Needs Approval, Low) · FRE-621 (graph-hygiene cleanup, Needs Approval, Medium) · **MedicalCondition** entity-type seed (health-domain, parked) · config-UI capstone. Process-v2 baseline: Linear-native dispatch `stream:*`+priority+blocked-by; PR-merge→Awaiting Deploy. Prior narrative → [`completed/2026-07-04-master-plan-archive.md`](completed/2026-07-04-master-plan-archive.md).

---

## Recent decisions (last session — 2026-07-08)
*Rolling bridge for the next session: decisions + why, not a work log (Linear has that). `/prepare-reset` replaces this each reset.*

- **Shipped + live:** **FRE-847** `context_probe` (on-demand per-session context% + idle, read from the transcript JSONL) + **FRE-848** watcher **context-pressure alert** (nudges cc-master to checkpoint at ≥70% idle; gating-watcher restarted onto the new code, PID live). Board clear — no Approved `stream:build1` queued.
- **Review gate → SHIFT-LEFT (build+master skills edited, live; do NOT re-open):** the **build** session runs `/code-review` + `/security-review` **once** at the pre-PR gate (effort-sized: `low` small / `high` src·schema·security·cost·memory), fixes its own findings, and hands master a **self-review summary**. **Master is the executive** — validates the summary and decides (merge/bounce), keeps full veto over fold-ins, does NOT re-run the review. Proven on FRE-848 (build found + folded 2 real bugs, improved on the spec).
- **Anti-over-ticketing (build+master skills edited, live):** a ticket is an **objective / user story** (usually ADR-spawned but *not uniquely*), NOT a boxed single change. Build **folds** non-ADR supporting changes + code-review findings into the current PR — **no separate tickets, no paper trail**. New ticket only for genuinely separate/sequenceable or ADR-requiring work. Single-developer project: tickets = sequencing, not a change log.
- **Context signal decided (don't re-litigate):** read a session's context% from its **transcript JSONL** (`input+cache_read+cache_creation`, matches `/context`), NOT Claude Code's statusLine (would require editing a CC-managed file — rejected). `context_probe`'s `idle_s`/`state` is a **weak** proxy (mtime → false-BUSY just-finished, false-IDLE long-turn); the **authoritative idle signal is the pane** (`session_is_idle`), so the watcher idle guard was left untouched (works post-FRE-845).
- **New `/prepare-reset` skill** (this session): owner-invoked safe wind-down before `/clear` — safety gate + distill fresh decisions to durable memory (this block) + checkpoint MASTER_PLAN + go/no-go. Bookend to `/prime-master`.
- **Standing correction reinforced all session:** verify before asserting; don't over-reach / re-fix what works; read the actual thing (bytes/code/pane), not a summary. Owner caught several (statusLine over-engineering, `session_is_idle` over-reach, trusting `context_probe`'s weak state over the pane).

---

## Dispatch

Dispatch lives in **Linear** (process v2, 2026-07-04 — contract in `.claude/skills/lifecycle-rules.md`
§ Dispatch): a stream's NEXT = `Approved` + `stream:<name>` label + no open blocked-by relation,
priority-ordered. This file carries **why the sequence is what it is** — priorities, waves,
dependency rationale — never per-ticket board state (it drifts).

**Sequencing rationale (current):** build1 + build2 = the ADR-0105 sysgraph self-improvement chain
(DAG in the header), delivered via the dispatch automation; FRE-808 (superuser-hardening, independent)
rides build1 ahead of the blocked FRE-716. adr = FRE-809 (infra topology + data-custody ADR, discussion
mode). **Recall flag-flip is master-owned**: multipath on + floor 0.60 + FRE-489/670 + FRE-658 window
probe.

**Parked / held (rationale only — not dispatched, no stream label):** ADR-0102 vision-doc chain
FRE-682–689 (un-paused 2026-07-04 but LOW priority — sequenced behind Memory) · Inference
ADR-0094/0095 trees FRE-600–604 / 607–611 (held pending FRE-432/516 measurement) · FRE-713
(trigger-gated, Backlog) · FRE-760 (re-scoped Low, blocked by FRE-773).

---

## Priority order (owner, 2026-07-04) — all ladder up to Seshat Pedagogical Architecture (M3)

1. **Agentic Vision** — ADR-0101 **Implemented** (FRE-691/669 Done, AC-10b outlier-waived); ADR-0102 un-paused but LOW; ADR-0108 vision re-processing Proposed.
2. **Memory Recall** — hot path. Taxonomy/extraction thread: **V2 chain FRE-769→773 Approved** (build1). Recall multipath: ADR-0103 Accepted / ADR-0104 Proposed (code merged flag-dark; flag-flip master-owned).
3. **Telemetry Surface Audit** — core complete (FRE-703 dashboard wave shipped); residual FRE-574/599/704 Approved.
4. **Configuration Management** — ADR-0099; build2 on FRE-649 (stages 650→652 queued).
5. **Linear async feedback channel** — **blocked until the sysgraph is built** (self-improvement loop ADR-0105/0106 needs the isolated System graph; impl in W1 FRE-714–716/720 + W2 728–732). Raw insights/reflections are producing; convergence+isolation+surfacing awaits the sysgraph.
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

- **Memory Recall Quality** (parent FRE-435) — the north-star measurement instrument. Extraction ceiling root-caused to the *taxonomy* (FRE-766) → ADR-0109 V2 redesign (build1 FRE-769→773). Recall = multi-path retrieval over a typed living KG (ADR-0103 principle + ADR-0104 arch); flag-flip master-owned. Memory substrate ADR-0098 (Claims write-path live, idle until consolidation).
- **639 ↔ ADR-0105 reconciliation** (adr thread) — gate-vs-isolate for the System class. FRE-639 (ADR-0098 T3) *gates* System-subject extracted entities (~46% operational noise) via a query-time class filter; ADR-0105/0708 argues *physically isolate* into a separate sysgraph. Reconciliation = fresh adr design work: does isolate-don't-gate extend to 639's conversation-extracted System entities, or are they distinct scopes? Feeds the Linear-async channel (priority #5).
- **Agent Vision & Attachments** (ADR-0101 Implemented / ADR-0102 doc chain LOW / ADR-0108 Proposed) — vision core live (local Qwen); cloud spine + cost controls shipped (FRE-691/692/693).
- **Self-improvement loop / System-KG isolation** (ADR-0105 Accepted / ADR-0106 Accepted) — isolated System graph (sysgraph Postgres); children W1/W2. Unblocks the Linear-async feedback channel.
- **Config Management** (ADR-0099) — single-source role matrix + validator; build2 chain 649→652.
- **Seshat Inference** (ADR-0082 / FRE-432) — tier-aware routing measurement; Ph0 confirmed ~75% thinking on trivial SINGLE turns.

---

## Active ADRs

Recent / active (older ADRs → `docs/architecture_decisions/`):

| ADR | Title | Status |
|-----|-------|--------|
| **0113** | Self-driving delivery loop — autonomous actuation, distributed & human-gated judgment | **SUPERSEDED (2026-07-08).** The LLM-review harness half was built (FRE-830/833/834) then **falsified in use** — the pr-gate reviewer hallucinated a false-positive security blocker on PR #433 (twice) despite holding the correct bytes. Master already uses tool-equipped, verify-first, advisory subagents correctly (the #427/#429 pattern); the harness only duplicated CI (CodeQL + pytest/mypy/ruff) and subtracted from the working behavior. **Harness removed (PR #435); FRE-835 + FRE-828 canceled; gating-watcher stopped+disabled.** Survivor: FRE-832 (prime-master checkpoint, merged). Lesson kept as a habit: adversarial fresh-context subagent before a one-way door. |
| **0110** | External dispatch orchestrator (build/adr workers) | **Proposed** — dispatch/poll half **superseded by ADR-0113**; RC substrate + Linear-native dispatch contract retained. (PR #362, FRE-783; impl FRE-785–788 shipped.) |
| **0109** | Entity-Taxonomy V1→V2 Redesign (**10-type**) | **Accepted** + **Amendment 1** (FRE-782: +KnowledgeArtifact +QuantityMeasure, 3-rater IAA-validated, κ 0.900). Chain: FRE-769/770/773 Done; **FRE-784** (10-type promote, build1) → 771 (prompt swap) → 772 (KG migration). |
| **0108** | Stored-artifact vision re-processing | **Proposed** (PR #330). Impl FRE-743–748 + bug FRE-749 Needs-Approval. |
| **0107** | User identity for Claims + log/trace propagation | **Accepted** (PR #327). FRE-738 **Done** (deployed + AC-4 5-claim backfill); 739/740 Approved (739 = assembled seam). |
| **0106** | System/User boundary by output_kind | **Accepted**. Children FRE-728–732 Approved (W2). |
| **0105** | Self-improvement pipeline + isolated System graph | **Accepted** (impl PR #304/#305). Children FRE-714–721 Approved (W1). |
| **0104** | Multi-path retrieval (RRF fuse) | **Proposed** (code merged flag-dark; awaits master flag-flip live proof). |
| **0103** | Recall principle (no clean floor; adaptive operating point) | **Accepted**. |
| **0102** | Vision doc/PDF ingestion | **Accepted** — un-paused 2026-07-04 but LOW priority. |
| **0101** | Agent vision ingestion of attachments | **Accepted** (functionally implemented — FRE-691/669 Done, AC-10b outlier-waived). |
| **0100** | Relevance-bounded recall | **Accepted**. |
| **0099** | Config management & validation | **Implemented** (seam confirmed on FRE-652/PR #366). Stages 648–652 all merged; chain **Awaiting-Deploy** (behavior-neutral rebuild → tickets Done). Next: config-UI capstone + lifecycle audit. Residual: FRE-789 benchmark-YAML drift. |
| **0098** | Memory substrate & lifecycle | **Accepted** (implements 0097). Claims write-path live. |
| **0092** | Context-Compaction Observability | **Implemented**. |
| **0090** | Telemetry Surface Contract | **Accepted** (governs Telemetry Surface Audit). |
| **0089** | Artifact Execution Security | **Implemented** + Addendum A curated `/lib/`. |
| **0088** | Execution Topology Observability | **Accepted**; spine + read surfaces shipped. |
| **0087** | Memory Recall Quality | **Accepted 2026-06-27** (parent FRE-435). |
| **0084** | Pedagogical Architecture (Socratic Tutor) | **Accepted**; M1 Done; M3 gated on M2. |
| **0082** | Tier-Aware Model Selection | Partially superseded by 0084; D1 plumbing may ship (FRE-432). |
| **0081** | Cache-Aware Context Layout | **Core chain COMPLETE + live**; follow-ups (D4-trim/D5/D6) in ADR-0081 Extended. |

Earlier ADRs (0040–0080) Implemented/Accepted — see `docs/architecture_decisions/`.

---

## Historical waves (complete — retained as index)

Waves **A/B/C/E/I/J** ✅ complete. **D** planned, impl deferred (FRE-214 §8.7). **F/G** partial. **H** (memory/context) partial. Detail → `git log` + `docs/plans/completed/`. ADR-0081 cache chain (D1/D4/D2/D3) shipped + live.

---

## How This File Works

- **Linear is the task tracker** — this file tracks priorities and sequencing only. Do not re-enumerate per-ticket tables here (they drift).
- **Dispatch** = Linear (state + `stream:*` label + priority + blocked-by relations; contract in `.claude/skills/lifecycle-rules.md` § Dispatch). Priority order = the numbered list above. Cross-project sequencing = [`sessions/2026-07-02-priority-sequencing.md`](sessions/2026-07-02-priority-sequencing.md).
- **Update after every ship**: apply any dispatch mutations in Linear + bump "Last updated". Move session narrative to `completed/` when the header grows past ~1 screen.
- **Specs** → `docs/specs/` · **ADRs** → `docs/architecture_decisions/` · **Session plans** → `docs/superpowers/plans/` · **Archive** → `docs/plans/completed/`.
