# Session Closeout — 2026-06-27 (master)

Consolidation of a long master session. Two halves: (1) routine integration + ops hygiene, (2) a deep dive into why the KG "knows wrong things about the owner," which surfaced a coherent memory-quality agenda and a backlog/visibility meta-problem.

---

## 1 · What shipped this session

**PRs integrated (master gate):** FRE-236 (iOS bg SSE, PWA v27) · FRE-591 (sessions.user_id schema) · ADR-0095 + ADR-0096 (arch-review forks, Proposed) · FRE-555/557/523 closeouts · PR #255 self-diagnosing-architecture brief · FRE-488/489 (recall harness + gate set) · FRE-395 (PWA ESLint) · FRE-339 (PWA runtime-config, deployed v28). FRE-489 required a recovery (#260) after a stacked-PR base-retarget failure.

**Ops hygiene:**
- **Branch cleanup** — pruned **73 remote + 60 local** merged branches; integrated *verify-and-cleanup* into `/build` + `/adr` Step 0 (retire merged branch local→remote; anchors protected) so it can't re-accrue.
- **Disk** — `docker builder prune -a` freed **122 GB** (84% → 19%); build cache was 906 stale entries.
- **KG correction** — removed 2 false owner-residence facts: `Alex-[LIVES_IN]->Pont-de-Lagarde` and `Alex-[LOCATED_IN]->Torcello` (owner *visited*, doesn't live there; kept `RELATED_TO`).

---

## 2 · What we established (findings)

### 2a · The KG memory system is broken — but the decisions exist; implementation is the gap
Full anatomy in memory `project_kg_memory_system_broken_anatomy`. Summary:

| # | Finding | Decision exists? | State |
|---|---|---|---|
| Owner identity | TWO disconnected "Alex" nodes — `:Person{is_owner:true}` (config bootstrap, empty) vs `:Entity:Person` (extraction, all facts, unflagged); different MERGE keys | ADR-0052 (Accepted) | shipped split |
| Curation gate | extraction writes straight to `:Core`; nothing judges facts (→ visit became `LIVES_IN`, `DISCUSES` garbage entity, case-variant dups) | ADR-0071 (**Proposed**) | unbuilt |
| Correctability | `first-write-wins` (service.py:700, FRE-375) freezes entity description/type forever | — | blocks corrections |
| Freshness | wired + ENABLED (consumer running, weekly review scheduled, 14,211 dormant edges flagged) but **proposes-not-evicts** (by design) and its proposals fed the **FRE-598-wedged** gate | ADR-0042 (Accepted/impl) | running but not delivering |
| Extraction | sub-par: wrong relationship *types*, wrong/partial facts, hallucinated entities | ADR-0087 program | needs SOTA work |

**Key principle:** *freshness ≠ correctness.* Decay/eviction fixes UNUSED data; wrong-but-active facts need the GATE + extraction correctness + a correctability escape. Also: the local model **hallucinates graph reads** — always verify KG claims against Neo4j.

### 2b · The harness manufactures tickets that bury the owner (the meta-problem)
The Captain's-Log / brainstem self-improvement pipeline auto-creates Linear tickets, currently **8 live in Needs-Approval**:
- `[performance]` ×6 (FRE-623–628) — reflection proposals to change prompt components.
- `[knowledge]` ×2 — FRE-622 (ADR-0042 dormant-edge review), FRE-629 (extraction-spike anomaly).

All carry only the generic `Improvement` label, **no project, no tier, no "auto-generated" marker** → indistinguishable from human tickets, unhomed, buried. This is the same pipeline PR #255's brief reframes as a *category error* (detect = trigger; deliver = investigated proposal, not a raw ticket), the same gate FRE-598 unwedged, and the detector FRE-620 fixes.

---

## 3 · What needs to be done

### A · Memory-quality program — **Approved, queued** (project: Memory Recall Quality)
| Ticket | Tier | What | Gate |
|---|---|---|---|
| FRE-630 | Opus | KG extraction → SOTA (benchmark · prompts · gates; `VISITED` vs `LIVES_IN`) | — |
| FRE-631 | Opus | Implement ADR-0071 curation gate | **needs ADR-0071 Accepted first** |
| FRE-632 | Sonnet | Fix ADR-0052 owner node-split (merge the two Alex nodes) | codex graph-migration |
| FRE-633 | Sonnet | Audit ADR-0042 freshness live (bump-on-recall? proposals post-FRE-598?) | — |
| FRE-634 | Opus | first-write-wins correctability escape | reconcile w/ FRE-375 |
- Plus existing: FRE-620 (KGQ detector fix), FRE-621 (graph hygiene) — both belong to this cluster.
- **Owner decision:** true auto-evict/promote exceeds ADR-0042's deliberate "propose-for-review" stance → a small ADR-0042 *amendment* if wanted (not blocking the five).

### B · Harness-ticket visibility + backlog control (**recommended; not yet filed — deliberately not adding to the approval pile**)
1. **Mark auto-created tickets at creation** — a distinct label (e.g. `auto:captains-log`) + a project, so they're visible and filterable, never indistinguishable from human tickets.
2. **Triage/cull the current 8** (FRE-622–629) — home the [knowledge] ones to Memory Recall Quality; route or close the [performance] prompt-reflection ones (decide their home: ADR-0058 self-improvement / ADR-0078 prompt-mgmt).
3. **The real fix is PR #255's self-diagnosing `/adr` cycle** — detect→investigated-proposal under deterministic gating, so the harness stops dumping raw tickets for the owner to triage. Greenlight that cycle when ready.

### C · Owner decisions pending (no master action until then)
- Read **ADR-0094 / 0095 / 0096** → then approve their P1s (FRE-601 · 608 · 613).
- **FRE-435 cross-run recall** needs an owner-run eval pass-2 (master won't fire live turns).
- Optional/standing: PWA-image slimming idea; Dependabot (1 high + 1 moderate); the self-diagnosing `/adr` cycle.

---

## 4 · State snapshot
`main` clean · **no open PRs** · disk 19% · branches pruned · streams idle with a full memory-quality queue (build → FRE-632/633 or its backlog; adr → idle/self-diagnosing cycle). Master fired no live gateway turns.
