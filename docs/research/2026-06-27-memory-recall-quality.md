# Memory-Recall Quality — Phase-1 Baseline & Findings (ADR-0087 / FRE-491)

**Date:** 2026-06-27 (updated 2026-06-28)
**Status:** Phase-1 diagnosis complete. Ceiling baseline + structural finding measured; the extraction-tax (extract-mode) measurement is a sequenced follow-on (see §6).
**ADR:** [ADR-0087](../architecture_decisions/ADR-0087-memory-recall-quality-measurement-program.md) (Phase 1)
**Ticket:** FRE-491 (baseline run + hypothesis resolution + D5 cutoffs) · parent FRE-435
**Harness:** `scripts/eval/fre435_memory_recall/` (FRE-488 scaffold, FRE-489 gate set)

---

## 1. TL;DR

Running ADR-0087's harness for the first time against the real retrieval path produced three results, in order:

1. **The FRE-488 harness, as shipped, could not measure recall at all** — every positive case was a false-negative *by construction*, on any substrate. A defect in the **test instrument**, not in Seshat's memory code; it was masked because the FRE-488 seed AC ran in zero-vector keyword mode where false-negative=1.0 is the *expected* degraded result. FRE-491 fixed the instrument (§3).

2. **The owner's symptom is a deterministic *query-layer* defect, not a write or embedding problem.** Recall is recency-first, semantic-second, with two recency gates upstream of any vector/reranker step: a **hard 30-day cutoff** (`recency_days=30` default → discussions older than 30 days are excluded → recall **denies**, "no prior discussions") and a **recency-bounded `LIMIT` window** (recent chatter crowds out the relevant older turn → **wrong-but-recent** context). The entity vector index only *re-ranks* survivors of these gates — it never *expands* the candidate set — so a perfect semantic match (measured at **0.82 cosine, rank #1**) is still unrecallable. Embeddings are **not** the bottleneck (§4).

3. **A measurement framework: the "ceiling" and the gap decomposition.** Recall quality = *the quality of every deterministic, controllable layer* (extraction · retrieval Cypher · prompts · reranking). The harness measures the **ceiling** (all deterministic layers perfect) and decomposes the gap to it per layer. The recency finding is a *query-layer (Cypher) defect that holds the ceiling down* — fixing it raises the ceiling and extends its reach across time, with no model change (§5).

---

## 2. The owner's symptom (recap)

> "Memory recall is lacking … the agent sometimes says **'No prior discussions on this topic'** when there should be prior context." — owner, 2026-06-02 (FRE-435)

---

## 3. Finding #1 — the instrument was broken (harness, not memory code)

Three compounding defects made every positive case a false-negative regardless of substrate:

1. **`query_memory` never returns entities.** All three return paths build `MemoryQueryResult(conversations=…, relevance_scores=…)` with no `entities=` (`service.py:1630`); vector matches become `vector_scores` used only to re-rank conversations (`service.py:1531–1601`). So `recall().entities` is always empty.
2. **The harness scored entity ids** (`entity:<name>`), which can only match returned entities — which never come back.
3. **Neither write mode created Turns.** `seed_replay` wrote bare `Entity` nodes; `run_promotion_pipeline` only calls `promote_entity` (`promote.py:38`). But recall retrieves `:Turn` nodes; entities surface via a Turn's `key_entities` / `Turn-[:DISCUSSES]->Entity`, created by `create_conversation` (`service.py:335`) in the production write path. No Turns → recall returned nothing → `denied=True` everywhere.

Independently confirmed by Codex (line-cited). **This is the test harness, not evidence about the memory code.**

### 3.1 The fix (FRE-491)

- **Real write-path seeding** — `seed_replay`/`seed_extract` store each setup turn via `create_conversation` (`store_turn`), so entities are reachable through `Turn-[:DISCUSSES]->Entity`.
- **Episode-coverage scoring** — `flatten_recall` emits each recalled episode's `key_entities` as `entity:<name>`, ranked by the episode's relevance. A hit = *a recalled Turn discusses the expected entity* (faithful to ADR §D1). Reported as "did a relevant turn surface", not answer-quality.
- **Per-case isolation** (`--wipe-between-cases`, default on, guarded to TEST) so reused names under first-write-wins don't cross-contaminate cases.
- **`ensure_vector_index()`** after connect — a fresh test Neo4j has no vector index, which silently degrades recall to keyword-only.

---

## 4. Finding #2 — the root cause is the retrieval query layer (two recency gates), not write or embedding quality

`query_memory` fetches a Turn candidate set by recency and *then* re-scores it with the entity vector index + reranker (`service.py:1467–1601`); vector search **never expands** the candidate set. Two gates sit upstream:

### 4.1 Gate A — the hard 30-day cutoff (`recency_days=30`)

`MemoryRecallQuery.recency_days` defaults to **30** (`protocol.py:108`), applied as `WHERE c.timestamp >= now − 30d` (`service.py:1471`). Controlled verification — same entity, same query, only the cutoff varied:

| discussion age | `recency_days` | episodes returned | denied? |
|---|---|---|---|
| 5 days | 30 (default) | 1 | False — found |
| 5 days | None | 1 | False — found |
| **40 days** | **30 (default)** | **0** | **True — "no prior discussions"** |
| 40 days | None | 1 | False — found |

A discussion >30 days old is excluded outright → **denial**, from a one-line default. **This is the owner's literal symptom.**

**Production exposure (verified at the running container, not just the repo):** the *automatic* per-turn recall path runs with this window — `request_gateway/context.py:200` → `recency_days=30` (90 on a secondary path; `executor.py:2036/2002` → 30/90). The *explicit* "search memory" tool uses 3650 (`tools/memory_search.py:180`). So the agent's **passive memory is a ~30-day window**; it only reaches older memories if it deliberately invokes search. **A 30-day-gated recall path is short-term memory wearing a long-term label** — directly at odds with the pedagogical North Star (recall across months).

### 4.2 Gate B — the recency-bounded `LIMIT` window

Within 30 days, only the `LIMIT` most-recent Turns are candidates. The harness mines real live Turns (read-only; 2,133 available) as a **distractor background** timestamped newer than the case's relevant Turn (`--distractor-background N`). **Bespoke gate (21 cases), replay, real embeddings, `limit`=10:**

| distractor `N` | FN-rate (denial) | miss-rate | recall@5 | recall@10 | MRR |
|---|---|---|---|---|---|
| 0 | 0.00 | 0.00 | **1.00** | 1.00 | 0.49 |
| 5 | 0.00 | **1.00** | **0.00** | 0.00 | 0.04 |
| 10 | 0.00 | 1.00 | 0.00 | 0.06 | 0.01 |
| 25 | 0.00 | 1.00 | 0.00 | 0.00 | 0.00 |
| 50 | 0.00 | 1.00 | 0.00 | 0.00 | 0.00 |

Recall collapses under modest recency pressure, and **FN-rate stays 0.00** — the system does not deny, it returns recent-but-irrelevant turns (`retrieval_miss`). So Gate B = "silently answered from the wrong context." *(Granularity caveat: episode-coverage scoring injects each recalled episode's `key_entities` into the ranked list, so the entity-space cliff is sharper than a pure top-k-episode metric; the collapse is robust, the exact N is granularity-sensitive — an episode-granularity recall metric is a noted follow-up.)*

### 4.3 The embedding model is **not** the bottleneck

Graph confirmed populated (11 Turns; "Diffraction Limit" present with a real 1024-dim embedding; 1 Turn discussing it). Recall returned **10 episodes, `denied=False`** — `recall@k=0` means *the relevant item wasn't selected*, not an empty DB. A **direct** vector query for the same question returns the right entity **#1 at 0.82 cosine**:

```
direct vector top-5: Diffraction Limit 0.82 (#1) · Numerical Aperture 0.70
```

The knowledge is present and semantically findable; recall just doesn't use vector search for *candidate selection*. **Embedding influence is structurally capped to re-ranking whatever survives Gates A+B** — zero influence on anything excluded by them. A better embedding model would not move the symptom; the fix is candidate generation (the Cypher query layer).

---

## 5. The measurement framework — ceiling, actual, and per-layer gap

Recall quality is the product of every deterministic, controllable layer. Define the **ceiling** = all such layers at their best (perfect extraction · semantic-first retrieval Cypher · optimal prompts · optimal reranking) — *what good looks like*. The total recall gap decomposes into named, individually-measurable layers, and the dual-mode harness is the instrument that separates them:

| layer | optimal form | how the harness isolates it | status |
|---|---|---|---|
| extraction | perfect entities / `key_entities` / descriptions | replay (perfect) vs extract (real model) → **extraction tax** | extract-mode pending (§6) |
| **retrieval Cypher** | semantic-first, no blind age-gate | replay + vary query / `recency_days` / candidate-gen → **query tax** | **measured — §4 (the dominant defect)** |
| query/embedding prompts | optimal construction | swap builder, hold rest → **prompt tax** | future |
| reranking | optimal reorder of true candidates | rerank on/off within window → **rerank tax** | future |

Two important consequences:

- **The replay baseline is a *current-Cypher* ceiling, not the true ceiling.** It perfects extraction but runs through the existing recency-gated query — so the recency defect (§4) is *holding the measured ceiling below the achievable one*. The recency finding is therefore not separate from the ceiling; it is the largest single thing depressing it, and it is a **deterministic** fix (no model change).
- **The program is two-front, in order:** (1) raise the ceiling's *reach* — fix the query-layer recency gate so "good" extends across months; (2) close the gap to the ceiling — drive extraction + prompts + reranking toward optimal (better models + deterministic guidance), toward a SOTA recall pipeline. Whatever gap survives all deterministic optimization is genuinely model-limited.

### Hypothesis table (ADR-0087 §D4) — resolution

| # | Hypothesis | Verdict |
|---|---|---|
| H1 | facts never reach KG | **Not the dominant gate.** Replay landing 100%; extract-mode (real extraction-fire/landing) deferred (§6). |
| H2 | frozen early description | **Open / not the dominant gate.** `service.py:703` first-write-wins confirmed in code; description-integrity is proxy-only (LLM-judge deferred) → resolve by manual spot-check + extract-mode, not a gate number. |
| H3 | ranking — present but not surfaced | **Secondary.** Vector index ranks the right entity #1 (0.82); ranking only operates *within* the recency window, so it cannot be the primary cause. |
| **H4** | **threshold / query construction false-negative** | **CONFIRMED — dominant.** The candidate-generation recency gates (A: 30-day cutoff → denial; B: `LIMIT` window → wrong context) are the mechanism, proven §4. |
| H5 | KG model insufficient | Not implicated. |
| H6 | wrong substrate (narrative/episodic) | Diagnostic only (Phase 1); not implicated. |

**Dominant gate: D5.2 (retrieval-path), specifically *candidate generation* (the Cypher query layer) — not reranker/threshold tuning.** The Phase-2 remedy is **structural** (semantic-first candidate generation / drop the blind age-gate), which is itself the answer to ADR-0087's "which gate fired"; numeric D5 cutoffs are largely moot for a structural fix and are recorded as such rather than calibrated.

---

## 6. What remains (the program's next moves)

1. **Recency de-gate (highest value, deterministic):** fix the automatic recall query layer — semantic-first candidate generation and remove/raise the blind `recency_days=30` age-gate — so recall reaches across months. Own ADR (Phase-2). *Candidate for a fast, narrow mitigation given severity.*
2. **Extract-mode prod-faithfulness:** register the ADR-0065 **CostGate**, pin the **prod extraction model (gpt-5.4-mini)** with host-reachable routing, and run the extract pass → the **extraction tax** (H1/H2 write-completeness). *Requires a paid-call (OpenAI mini) authorization.* (Lives in the FRE-488 env-pinning layer.)
3. **SOTA extraction guidance:** deterministic scaffolding (structured prompts, validation, canonicalization, contradiction-linting) to close the extraction gap toward the ceiling.
4. **Episode-granularity recall metric** (the §4.2 scoring-granularity caveat).
5. **LongMemEval external yardstick** — **FRE-490** (Approved, separate; `load_longmemeval` still a stub).

> Note: extraction-model routing is **correct** — prod runs `gpt-5.4-mini` per `config/models.cloud.yaml` (loaded via `AGENT_MODEL_CONFIG_PATH`); the dev default `config/models.yaml` is nano. An earlier "drift" note was a mis-read of the dev file and is retracted. The harness defaults to the dev config, hence the extract-mode prod-pinning item above.

---

## 7. Method & process notes

- **Backend-aware truth-source** (FRE-433 discipline): recall outcomes read from the actual `recall()` return; the report stamps `embedding_backend`, `wipe_between_cases`, `distractor_background_n` so a degraded/trivial run is never misread.
- **Substrate isolation** (FRE-375): all writes go to the test stack (Neo4j :7688 / ES :9201 / Postgres :5433); the distractor mine is **read-only** against live (ADR-0087 §D7).
- **Embedding/reranker are prod-identical** (`Qwen3-Embedding-0.6B` :8503 / `Qwen3-Reranker-0.6B` :8504; same container, localhost vs docker-DNS), so the replay retrieval mechanism is prod-representative. Only the *extraction* model differs (dev nano / prod mini) — see §6.
- **No raw dumps in git:** run reports (incl. real distractor turn text) land in the gitignored `telemetry/evaluation/fre435-memory-recall/`; only curated aggregates are committed. No PII in any committed artifact.

## 8. References

- ADR-0087 — Memory-Recall Quality measurement program.
- FRE-433/434 + `docs/research/2026-06-02-cache-aware-prompt-layout-and-compaction.md` — methodology precedent.
- `docs/research/2026-05-21-memory-integration-probe-report.md` — prior substrate probe.
- Code: `memory/service.py` (`query_memory`, `create_conversation`, `ensure_vector_index`), `memory/protocol.py` (`recency_days`), `request_gateway/context.py`, `scripts/eval/fre435_memory_recall/`.
