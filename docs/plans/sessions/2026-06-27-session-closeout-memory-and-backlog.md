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

### 2b · The real meta-problem is the development workflow (not the harness)
**Corrected diagnosis (owner, 2026-06-27).** The harness created only **8** tickets — it is *not* the source of the backlog, and the backlog is not the problem. The problem is the **development workflow**: incomplete or improper implementations pass as "Done," and every thread we pull surfaces another one ("tickets from tickets"). The backlog is a *symptom*.

Tonight's own evidence: ADR-0052 (shipped split), ADR-0042 (Accepted/implemented but not delivering), FRE-228 (Done but undelivered), FRE-606 (Done but CI-inert), FRE-489 (MERGED to the wrong base). None failed for lack of ability — they failed because **"Done" never had to mean "verified working."** That missing gate is the root cause; the backlog and the tickets-from-tickets are what it produces.

Two *minor* sub-items (hygiene, not the fix): the 8 harness auto-tickets ([performance]×6 FRE-623–628, [knowledge]×2 FRE-622/629) carry no project/tier/auto-marker → unhomed; and the [performance] tickets report a *signal* ("reflection proposed changing `tool_use_rules` 11×") with **no diagnosis** (what's wrong, why, what change) — a trigger dressed as a proposal.

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

### B · Development-workflow quality — **the root problem (dedicated thread)**
The backlog is a *symptom* of incomplete implementations passing the gate. This is a dev-best-practices / definition-of-done problem, to be worked in a **dedicated thread** (not patched here). Threads to pull:
- What **"Done" must mean** — verification-before-done; why bad/incomplete work passes review (depth vs. trust).
- The **tickets-from-tickets / WIP-sprawl** dynamic; **scope discipline** (finish-before-start); whether planning is too scattered across parallel streams.
- *Minor hygiene sub-item* (not the cure): label harness auto-tickets + give them a project, make the [performance] proposals state a concrete diagnosis, and triage the current 8.

### C · Owner decisions pending (no master action until then)
- Read **ADR-0094 / 0095 / 0096** → then approve their P1s (FRE-601 · 608 · 613).
- **FRE-435 cross-run recall** needs an owner-run eval pass-2 (master won't fire live turns).
- Optional/standing: PWA-image slimming idea; Dependabot (1 high + 1 moderate); the self-diagnosing `/adr` cycle.

---

## 4 · State snapshot
`main` clean · **no open PRs** · disk 19% · branches pruned · streams idle with a full memory-quality queue (build → FRE-632/633 or its backlog; adr → idle/self-diagnosing cycle). Master fired no live gateway turns.
