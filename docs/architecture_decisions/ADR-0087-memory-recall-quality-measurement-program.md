# ADR-0087 — Memory-Recall Quality: A Measurement-First Program (Diagnose → Gate → Architecture)

**Status:** Proposed — 2026-06-05
**Related:** ADR-0084 (Pedagogical Architecture — the most *demanding consumer* of recall and the source of the quality bar; this ADR is a prerequisite substrate for its M3/M4/M5), ADR-0081 + the *ADR-0081 Extended — Context & Memory Injection Quality* stream (owns what reaches the prompt *after* recall selects it — composes downstream of this ADR), ADR-0073 / FRE-374 (cross-fact constraint layer — the write-path invariant work this builds on), ADR-0060 (Knowledge-Graph Quality Stream — freshness/consolidation, a dependent consumer), ADR-0039 (Proactive Memory — `suggest_relevant()`, a dependent consumer), ADR-0074 (identity / joinability probe — reused as a write-completeness instrument), ADR-0025–0030 (memory & knowledge foundation)
**Implements:** FRE-435 Phase 1 (new project: *Memory Recall Quality*). Phase 2 (improve) is gated on this ADR's findings and becomes its own follow-on ADR.
**Evidence:** `docs/research/2026-05-21-memory-integration-probe-report.md` (live-corpus substrate probe); methodology precedent FRE-433/434 + `scripts/eval/fre433_cache_ab/`
**External grounding:** Mem0, Zep/Graphiti, Letta/MemGPT, A-MEM (Zettelkasten), LongMemEval — see §References

---

## Context

### The owner-observed symptom

> "Memory recall is lacking — information isn't reliably arriving in the knowledge graph, retrieval is weak, and the agent sometimes says **'No prior discussions on this topic'** when there should be prior context." — owner, 2026-06-02 (FRE-435)

Today memory quality is judged **qualitatively**. The owner's directive is to apply the same rigor that fixed the cross-turn KV cache (FRE-433/434: *measure-don't-assert*, hypothesis table, live A/B, backend-aware truth-source, flag-gated → verified → rollout) to **quantify** memory quality *before* changing the system, and only then to pick a fix.

### What we already measured — and its gap

The 2026-05-21 *Memory Integration Probe* (live VPS Neo4j: 489 sessions, 3,399 turns, 4,008 entities, 19,517 edges) established that the pipeline **concatenates rather than integrates**, and named four measured harms:

1. ~370 tokens of memory-section overhead on **76.9%** of gateway turns (~497k tokens/month), paid regardless of use.
2. **Empty descriptions** on the most-mentioned entities (`Paris` ×328, `London` ×168 — emitted with no description).
3. **Cross-contaminated descriptions** on load-bearing entities (`Neo4j` described as the definition of Cypher).
4. **Self-incoherence**: the renderer appends *"Do NOT say you have no memory"* while pointing the model at a list that is empty/wrong on its top items.

Two substrate root-cause leads carry forward, **both shifted by FRE-375** since the probe was written:
- *Then (historical):* the probe found `MemoryService` overwriting the description on every merge (last-write-wins). FRE-375 **inverted this to first-write-wins** to stop test overwrites — the live merge is now `e.description = CASE WHEN e.description IS NULL OR e.description = '' THEN $description ELSE e.description END` (`src/personal_agent/memory/service.py:703`). The harm therefore changed *shape*, not severity: a wrong-or-empty **early** description is now **frozen** — there is no UPDATE path to ever correct it (the failure mode Mem0's UPDATE op exists to fix).
- 9.3% of entity pairs carry redundant relationship types.

FRE-375 also fixed the synthetic-traffic contamination (the 87%-NULL-`session_id` finding), so the live corpus is now a viable replay source.

**The gap that ADR-0087 closes:** the 2026-05-21 probe measured the *substrate* (what is stored, and structural harm). It explicitly did **not** measure the **end-to-end recall function** — *given a real query, does the relevant prior context reach the answer, and how often does the system falsely claim "no prior discussions"?* That end-to-end, query-driven measurement — the owner's actual symptom — has never been quantified. This ADR builds the instrument that does.

### Why this is a pillar, not a pedagogical sub-task (decided with the owner)

Memory recall is a **substrate pillar with multiple consumers**, not a feature of any one stream:

| Consumer | Leans on recall for |
|---|---|
| **Pedagogical Architecture** (ADR-0084) | active-recall opening ritual, thread-pulling, cross-thread correlation — the *most demanding* consumer |
| **Proactive Memory** (ADR-0039) | `suggest_relevant()` cross-session injection |
| **KG Quality** (ADR-0060) | freshness/consolidation reranking |
| **ADR-0081 Extended** | quality of what actually reaches the prompt *after* recall selects it |

Folding recall into the pedagogical project would bury a shared dependency inside a project about Socratic tutoring, forcing the other three consumers to depend on a milestone in the wrong place. Therefore: **its own project (*Memory Recall Quality*)**, with a **first-class, explicit coupling to the pedagogical North Star** — the pedagogical objective *sets the quality bar* (what "good recall" must support is defined by thread-pulling + active recall, §D6), and ADR-0084's M3/M4/M5 declare a hard dependency on this work. A pillar that leans into pedagogy for its success criteria, but stands on its own.

### The external landscape (how others solve the same objective)

The owner asked that the hypothesis set be grounded in how successful systems meet this objective, not asserted from intuition. The 2025–2026 agent-memory field clusters into four reference points, each mapping to one of our candidate fixes:

| System | Core idea | Maps to our hypothesis |
|---|---|---|
| **Mem0** | extract salient facts per message; its memory-update design compares each new fact against existing memories and chooses a write operation — documented as **ADD / UPDATE / DELETE / NOOP** in the Mem0 paper (e.g. "moved Mumbai→Bangalore" *updates/deletes* the stale fact) | the **UPDATE** op is the analog of the missing correction path for our frozen first-write-wins descriptions (**H2**); the extraction phase maps to write-completeness (**H1**) |
| **Zep / Graphiti** | **temporal** knowledge graph; every fact carries a validity window (true-from → superseded-at); graph traversal for multi-hop recall. Reports **94.8%** on Deep Memory Retrieval vs MemGPT 93.4%, ~90% lower latency than full-context | KG-fix path with temporal validity (**H2/H3/H5**) |
| **Letta / MemGPT** | OS-style virtual memory: page facts in/out of context via function calls; recall store (recent) + archival store (long-term) | tiered retrieval / threshold tuning (**H4**) |
| **A-MEM** | **Zettelkasten** agentic memory: LLM-generated contextual descriptions per note, autonomous links between related notes, notes *evolve* as new experience arrives | the academic formalization of the owner's **Markdown + LLM-wiki** intuition (**H6**) |

**LongMemEval** (ICLR 2025; V2 is agentic) is the field-standard benchmark for long-term interactive memory — ~500 questions over 100k-token multi-session histories, scoring information extraction, multi-session reasoning, temporal reasoning, knowledge updates, and abstention (the "should say I don't know" case — our false-negative's mirror image).

### Why an ADR for a spike — what is actually being decided

Phase 1 is a measurement spike, not a feature. An ADR is warranted because the spike **pre-commits the project to a measurement contract and a set of decision gates**: the metric definitions, the probe-set sourcing, the hypothesis table, and — crucially — the *quantified triggers* that will select the Phase-2 architecture *before* we see the numbers. Pre-registering the gates is what makes the eventual architecture choice evidence-driven rather than narrative-driven (the failure mode FRE-433's methodology exists to prevent). **This ADR does not pick the Phase-2 memory architecture.** It decides *how* that pick will be made.

### Scope boundary (decided with the owner)

- **In scope (Phase 1, this ADR):** a reusable memory-recall quality harness; a quantified baseline (write-completeness, retrieval precision/recall, false-negative rate) on a bespoke live-corpus probe + a LongMemEval external yardstick; a hypothesis-table verdict; a routed, evidence-backed recommendation for Phase 2.
- **Out of scope (Phase 2, gated → follow-on ADR):** building any fix — Mem0-style write ops, temporal-KG validity windows, retrieval/reranker changes, or the Markdown + LLM-wiki note layer. Which of these we build is **selected by the gates in §D5**, not decided here.
- **Phase 1 changes no production behavior.** Measurement is read-only against the live substrate plus offline replay; any write-path instrumentation runs against the **test substrate** (FRE-375: Neo4j :7688 / ES :9201 / Postgres :5433). No flag flip, no deploy.

---

## Decision

**Adopt a measurement-first memory-recall quality program**, defined by the following sub-decisions.

### D1 — A dual-path metric model: write-completeness + retrieval quality

Recall can fail at two independent stages; the harness must attribute failure to the right one. Metrics:

**Write-completeness** (does the episode become a usable, joinable fact?):
- **extraction-fire rate** — episodes that *should* yield KG content where entity extraction actually fired (`src/personal_agent/second_brain/entity_extraction.py::extract_entities_and_relationships()`).
- **landing rate** — fired extractions that produced a **non-empty** semantic fact in Neo4j (`src/personal_agent/memory/promote.py::run_promotion_pipeline()` → `MemoryService`).
- **description-integrity** — fraction of load-bearing entities whose description is correct and not cross-contaminated (LLM-judge + manual spot-label; directly probes the **frozen first-write-wins** description at `src/personal_agent/memory/service.py:703`).
- **joinability** — facts that join back to a real Postgres session (reuse the ADR-0074 joinability probe).

**Retrieval quality** (given a query, does relevant prior context surface?):
- **recall@k / precision@k** on the bespoke probe (sweep *k* to separate "not in the index" from "ranked too low").
- **false-negative rate** — fraction of queries where prior context demonstrably exists but the system returns nothing or says "no prior discussions." **This is the headline metric** (the owner's symptom).
- **MRR / nDCG** (secondary) — ranking quality, to evaluate `src/personal_agent/memory/reranker.py::rerank()`.

### D2 — The probe set: bespoke live-corpus gate + LongMemEval external yardstick

- **Gate (primary):** a labeled probe set mined from Seshat's **own** corpus — real multi-session histories and the actual "no prior discussions" failures the owner has hit. Each case = `(history setup, query, expected recall)`. Example: *setup* = the real 2026-05-12 session discussing the diffraction limit; *query* = "what did we say about the diffraction limit?"; *expected* = the system surfaces that discussion (does not deny it). This decides **"did we fix the owner's problem."** The set **must include pedagogical-shaped cases** (active recall of a due concept; thread-branch retrieval; cross-domain match) so the bar reflects what ADR-0084 needs (§D6).
- **Yardstick (secondary):** a **LongMemEval subset** (ICLR 2025; a 2026 agentic "V2" also exists — we pin the exact variant at harness-build time), run unmodified, to place Seshat against **published LongMemEval results**. This decides **"where do we sit vs the field."** It is explicitly *not* the gate — optimizing for a benchmark's failure modes instead of ours is a known trap. (Note: Zep's headline **94.8%** is on the *Deep Memory Retrieval* benchmark, **not** LongMemEval — landscape context, not a LongMemEval comparator.)

### D3 — The harness (analog of `scripts/eval/fre433_cache_ab/`)

A reusable harness under `scripts/eval/fre435_memory_recall/` that: (a) loads a probe set (bespoke or LongMemEval), (b) drives **real sessions** end-to-end (write path + retrieval path, against the test substrate), (c) scores the D1 metrics, (d) emits a structured run report (per-case + aggregate) and a hypothesis-attribution breakdown. Backend-aware truth-source discipline (per FRE-433): the harness reads recall outcomes from the **actual** retrieval call, not a proxy log field. Raw run dumps stay out of git (curated summaries only).

### D4 — The hypothesis table (grounded in the landscape)

| # | Hypothesis | Locus | Landscape analog | Discriminating measurement |
|---|---|---|---|---|
| **H1** | Write gap — facts never reach the KG | extraction not firing / not landing | Mem0 extraction phase | extraction-fire + landing rate |
| **H2** | Frozen description — a wrong/empty early write is never corrected | first-write-wins merge (`service.py:703`); no UPDATE path | Mem0 UPDATE op; Zep validity windows | description-integrity on top-mention entities (indexed but wrong) |
| **H3** | Retrieval ranking — fact present but not surfaced | `memory/reranker.py`, `memory/embeddings.py`, top-K | Zep graph traversal; reranking | recall@k with *k* sweep (is the fact in the index at all?) |
| **H4** | Threshold / query construction — false "no prior discussions" | recall controller threshold; query embedding | Letta recall-store paging | false-negative rate; threshold ablation |
| **H5** | KG *model* insufficient — structure can't represent the question | flat entity-edge vs temporal/contextual | Zep temporal KG; A-MEM note evolution | cases that fail under *any* retrieval tuning |
| **H6** | Wrong substrate for narrative/episodic recall | concatenate-not-integrate (2026-05-21) | A-MEM Zettelkasten markdown notes + links | **diagnostic only in Phase 1**: count cases that remain failed after H1–H5 are addressed *in principle* — i.e. the fact is present and retrievable but the flat KG cannot represent the *narrative/episodic* relation asked for. The markdown-note prototype + A/B is **Phase-2** work (gate 3), not a Phase-1 deliverable. |

### D5 — Pre-registered decision gates → Phase-2 architecture ADR

After the Phase-1 baseline, the **dominant residual failure class** selects the Phase-2 direction. The gates' *discriminators* are fixed and ordered now (which D1 metric decides each branch); the **numeric cutoffs are a named Phase-1 deliverable** — calibrated with the owner against the pedagogical bar during harness build and **recorded before any Phase-2 routing claim** (Verification #6). The discriminator each gate keys on:

1. **Write-path gate (H1 / H2)** — keyed on write-completeness. Fires if facts are **absent** (low landing rate / joinability) **or present-but-frozen-wrong** (an indexed entity whose description-integrity is below the bar). → **KG write-path fix**: ensure landing for the absent case; add a Mem0-style **UPDATE** path / lift the first-write-wins freeze for the frozen-wrong case. Note H2 is *indexed but wrong*, so it is caught by description-integrity, **not** by an "in the index?" test — the two sub-cases are scored separately.
2. **Retrieval-path gate (H3 / H4)** — keyed on the recall@k sweep + false-negative rate. Fires if facts are **in the index but not surfaced** (high recall@large-*k*, high false-negative at production *k* / threshold). → **retrieval-path fix** (reranker, threshold, query construction).
3. **Architecture gate (H5 / H6)** — keyed on the residual-failure count after gates 1–2 are addressable in principle. Fires if facts are present and retrievable but the *structure* cannot represent what's asked (narrative/episodic recall). → route to a **Phase-2 architecture ADR** that prototypes the **Markdown + LLM-wiki / A-MEM note layer** alongside the KG and **A/Bs it on the same probe set**. (The prototype + A/B are Phase-2 actions, not Phase-1.)

Gates are *not mutually exclusive* — Phase-1 reports the share of failures attributable to each and routes to the dominant one(s). The Phase-2 architecture decision is a **follow-on ADR** (provisionally ADR-0088), gated on these results. The owner's flagged Markdown+LLM-wiki direction is carried as a **leading, pre-registered candidate (gate 3)** — tested against the same evidence as the write-path and retrieval-path fixes, not assumed.

### D6 — Coupling to the pedagogical North Star (the quality bar) + other consumers

The pedagogical objective (ADR-0084) defines *what "good recall" must support*, and therefore the **success threshold**, not an abstract number:
- **Active-recall opening ritual** (FRE-457) requires reliably retrieving a *due* concept at session open — sets the recall@k bar for "concept due for review."
- **Thread-pulling** (FRE-461) requires surfacing the right *branch* of a concept — sets a ranking-quality (MRR/nDCG) bar.
- **Cross-thread correlation** (FRE-461) requires finding cross-domain matches — adds a dedicated probe case class.

The bespoke probe set (D2) therefore includes pedagogical-shaped cases, and the Phase-1 report states the baseline **against the pedagogical bar**, not just in the abstract. Other consumers (Proactive Memory, KG Quality, ADR-0081-Extended injection) are recorded as dependents so a Phase-2 change is evaluated for their regressions too.

### D7 — Substrate isolation, observability, configuration

- All write-path measurement runs against the **test substrate** (FRE-375 ports); the harness refuses prod-fingerprint URIs (existing guard). Read-only retrieval probes against live are permitted; offline replay preferred.
- Run reports are structured (structlog + curated JSON summary), `trace_id`-tagged, and indexed in the eventual research doc; raw dumps gitignored.
- No new runtime config in Phase 1; the harness is script-invoked. Any Phase-2 fix introduces its own flag under the follow-on ADR.

---

## Consequences

### Positive

- The owner's qualitative symptom becomes a **single headline number** (false-negative rate) plus an attributable breakdown — the prerequisite for fixing the right thing.
- The Phase-2 architecture choice (KG-fix vs retrieval-fix vs Markdown/LLM-wiki) is **pre-registered and evidence-gated**, immune to narrative drift.
- A **reusable harness** that every downstream consumer (pedagogical, proactive, KG-quality) can run as a regression gate — the memory analog of FRE-463's pedagogical A/B framework.
- Grounding in Mem0/Zep/Letta/A-MEM + LongMemEval means we inherit known designs and can state where we sit against published numbers.

### Negative / tradeoffs

- **Two probe sources is more harness work** than bespoke-only; accepted because the external yardstick is cheap to run once cases are loaded and prevents us declaring victory on an untethered scale.
- **Bespoke labeling is manual** (no ground truth exists for "what should have been recalled"); mitigated by mining real failures and using LLM-judge with human spot-checks (referenced answers per the owner's standing preference).
- **Phase 1 ships no fix** — by design. The risk is impatience; mitigated by the gates making Phase 2 a short, decisive follow-on.
- **A-MEM/markdown remains a hypothesis** until gate 3 fires; if H1/H2 dominate we may fix the KG and never build it. This is the correct outcome of measuring first, but it defers the owner's flagged direction behind evidence.

---

## Verification (Phase-1 acceptance)

Phase 1 is **Done** when all hold (analog of FRE-433's acceptance):

1. `scripts/eval/fre435_memory_recall/` exists and runs end-to-end against the test substrate.
2. A **bespoke probe set** (≥ N labeled cases incl. the real "no prior discussions" failures and ≥3 pedagogical-shaped cases) is committed (curated, not raw dumps).
3. A **quantified baseline** is reported: write-completeness (extraction-fire, landing, description-integrity, joinability), retrieval precision/recall, **false-negative rate**, and MRR/nDCG.
4. A **LongMemEval subset** score is reported alongside, compared against **published LongMemEval results** (not Zep's DMR 94.8%, which is a different benchmark).
5. The **hypothesis table (D4) is resolved** — each hypothesis confirmed/refuted with its discriminating measurement (H6 diagnostically, without building a prototype).
6. The **D5 gate cutoffs are calibrated and recorded** (with the owner, against the pedagogical bar), and a **routed recommendation** names which gate fired and what the Phase-2 ADR should decide, with evidence.
7. A narrative **research doc** (`docs/research/2026-06-XX-memory-recall-quality.md`) per the owner's significant-work standard (dated, indexed, diagrams, dev/test-process section, references).

FRE-435 is a multi-phase ticket: shipping Phase 1 moves it **In Progress**, never Done, until the Phase-2 ADR + work lands.

## Open decisions (data-gated, resolved in Phase 2)

- Which Phase-2 gate fires (D5.1 / D5.2 / D5.3) — decided by the baseline.
- Whether the Markdown + LLM-wiki layer **replaces** or **complements** the KG (only relevant if gate 3 fires) — the A/B decides.
- Whether temporal validity windows (Zep-style) are adopted for the KG-fix path — relevant if H2 dominates.
- Probe-set size N and the exact pedagogical-bar thresholds — set during harness build with the owner.

## References

- `docs/research/2026-05-21-memory-integration-probe-report.md` — live substrate probe (four harms; the description-overwrite finding, since inverted to first-write-wins by FRE-375).
- FRE-433 / FRE-434 + `scripts/eval/fre433_cache_ab/` — methodology + harness precedent. `docs/research/2026-06-02-cache-aware-prompt-layout-and-compaction.md`.
- ADR-0084 — Pedagogical Architecture (quality-bar source). ADR-0073/FRE-374 — cross-fact constraint layer. ADR-0060 — KG Quality. ADR-0039 — Proactive Memory. ADR-0074 — joinability probe.
- **Mem0** — fact extraction + memory-update operations (ADD/UPDATE/DELETE/NOOP per the Mem0 paper, arXiv 2504.19413): https://arxiv.org/abs/2504.19413 · https://github.com/mem0ai/mem0
- **Zep / Graphiti** — temporal KG for agent memory (arXiv 2501.13956): https://arxiv.org/abs/2501.13956 · https://github.com/getzep/graphiti
- **Letta / MemGPT** — OS-style memory paging: https://www.letta.com/blog/letta-v1-agent
- **A-MEM** — agentic Zettelkasten memory (arXiv 2502.12110, NeurIPS 2025): https://arxiv.org/abs/2502.12110 · https://github.com/agiresearch/a-mem
- **LongMemEval** — long-term interactive memory benchmark (ICLR 2025): https://github.com/xiaowu0162/LongMemEval
