# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest) — per-ticket state.
> **Source of truth for priorities**: this file (sequencing only; do not re-enumerate Linear here).
> **Last updated**: 2026-07-04 (master re-prime + plan reduction 96k→lean). PR #350 (FRE-697) merged `0bdc6b0` → Done (ONNX reranker benchmark, eval-only, no deploy). Board resequenced: build1 NEXT = **FRE-769** (owner-confirmed); FRE-760 → Backlog; ADR-0102 un-paused but LOW priority. Prior-session narrative archived → [`completed/2026-07-04-master-plan-archive.md`](completed/2026-07-04-master-plan-archive.md).

---

## 🎛️ Stream Board

Master-maintained dispatch. `/build 1`, `/build 2`, and `/adr` resolve their NEXT ticket **and** the context action here. Master advances this at each merge. WIP = 1 live ticket per stream.

<!-- STREAM-BOARD:START -->
| Stream | Command | NEXT (build this) | Context | Queued after |
|--------|---------|-------------------|---------|--------------|
| build · Stream 1 · **Opus** | `/build 1` | **FRE-769** [O] _(ADR-0109 V2 taxonomy do-first gate — owner-confirmed 2026-07-04; extraction root-cause fix, owner's #2 priority. ⛔ **FRE-760 → Backlog** — re-scoped Low, blocked by FRE-773.)_ | CLEAR | **V2 taxonomy chain** (root-cause of the extraction ceiling per FRE-766): 769 gate → **770** gold re-label (8-type) → **771** 8-type prompt + powered A/B → **772** KG migration (Opus, near-one-way-door) + **773** relationship-half (independent gate). Then **FRE-699** (recall parallelism) → **472**. _Context: FRE-630 Ph1 benchmark done (In Progress, multi-phase); FRE-432 Ph0 done, Ph1/2 remain (In Progress); 755/756 instrumentation. **Recall flag-flip is master-owned**: multipath on + floor 0.60 + FRE-489/670 + FRE-658 window probe._ |
| build2 · Stream 2 · **Opus** _(owner switched Sonnet→Opus 2026-07-03; ticket tier tags are complexity labels, not the session model)_ | `/build 2` | **FRE-649** [S] _(ADR-0099 stage 1 — cross-config guard + divergence policy + drift correction; unblocked by 648. Swap candidates: 676 group-memory-identity · 710 captains-log cadence · 767 chip-discreteness)_ | CLEAR | **Priority waves (Opus/Sonnet):** W0 stability (Config 649 · Memory 620/632/676/657/677 · Linear 710) → W1 substrate (Config 650→652+735 · Memory 639–642 · Linear 720/714–716) → W2 System-boundary (728/730→731→732→729 · 738) → W3 measurement (Pedagogical 561→564→453 · Linear 719/718 · Memory 660/647). Full order → [`sessions/2026-07-02-priority-sequencing.md`](sessions/2026-07-02-priority-sequencing.md). |
| adr · **Opus** | `/adr` | **owner-pick** _(queue thin; ADR-0108 shipped Proposed #330)_ | CLEAR | **639↔ADR-0105 reconciliation** (gate-vs-isolate for the System class — see Pillars) · FRE-565 past-conversations ADR · ADR-0094/0095 accept-or-kill (held pending Inference 432/516 measure) · pedagogical-reflection ADR (unfiled). _(ADR-0108 impl chain FRE-743–748 + bug FRE-749 = build-stream work, all Needs-Approval — owner gate.)_ |
<!-- STREAM-BOARD:END -->

**Context flag:** default **CLEAR** (fresh context per ticket). **KEEP** only when NEXT is a direct follow-on to what the stream just shipped (same files/substrate) — run on the warm context, do not `/clear`.

**Parked / held (not auto-dispatched):** ADR-0102 vision-doc chain FRE-682–689 (**un-paused 2026-07-04 but LOW priority** — buildable, sequenced behind Memory; not dispatched) · Inference ADR-0094/0095 trees FRE-600–604 / 607–611 (held pending FRE-432/516 measurement) · FRE-713 (trigger-gated, Backlog).

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
| **0109** | Entity-Taxonomy V1→V2 Redesign (8-type) | **Accepted 2026-07-03** (`a43bc22`). Root-cause fix for the extraction ceiling (FRE-766). Build chain FRE-769→773 Approved. |
| **0108** | Stored-artifact vision re-processing | **Proposed** (PR #330). Impl FRE-743–748 + bug FRE-749 Needs-Approval. |
| **0107** | User identity for Claims + log/trace propagation | **Accepted** (PR #327). Impl FRE-738/739/740 Approved. |
| **0106** | System/User boundary by output_kind | **Accepted**. Children FRE-728–732 Approved (W2). |
| **0105** | Self-improvement pipeline + isolated System graph | **Accepted** (impl PR #304/#305). Children FRE-714–721 Approved (W1). |
| **0104** | Multi-path retrieval (RRF fuse) | **Proposed** (code merged flag-dark; awaits master flag-flip live proof). |
| **0103** | Recall principle (no clean floor; adaptive operating point) | **Accepted**. |
| **0102** | Vision doc/PDF ingestion | **Accepted** — un-paused 2026-07-04 but LOW priority. |
| **0101** | Agent vision ingestion of attachments | **Accepted** (functionally implemented — FRE-691/669 Done, AC-10b outlier-waived). |
| **0100** | Relevance-bounded recall | **Accepted**. |
| **0099** | Config management & validation | **Accepted**. Impl FRE-648 Done → 649→652 (build2). |
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
- **Dispatch** = the 🎛️ Stream Board. Priority order = the numbered list above. Cross-project sequencing = [`sessions/2026-07-02-priority-sequencing.md`](sessions/2026-07-02-priority-sequencing.md).
- **Update after every ship**: advance the Stream Board + bump "Last updated". Move session narrative to `completed/` when the header grows past ~1 screen.
- **Specs** → `docs/specs/` · **ADRs** → `docs/architecture_decisions/` · **Session plans** → `docs/superpowers/plans/` · **Archive** → `docs/plans/completed/`.
