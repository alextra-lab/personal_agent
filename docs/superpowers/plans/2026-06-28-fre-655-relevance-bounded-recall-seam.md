# FRE-655 — ADR-0100 Relevance-Bounded Recall: A/B + floor calibration (assembled seam)

**Ticket:** FRE-655 (Approved, Tier-1) · **ADR:** ADR-0100 · **Depends on:** FRE-653 + FRE-654 (both merged)
**Branch:** `fre-655-relevance-bounded-recall-seam`
**Build-session bound (owner-confirmed):** *measure + propose*. Deliver the A/B + floor-calibration proposal as a PR (harness extension + writeup), bring proposed floor/cutoff values back for owner sign-off. **The flag-flip rollout, live verify on the shared gateway, and AC-6 ES-queryability are master/owner + deploy-gated — NOT this PR.**

## The crux (fidelity defect found in the existing harness)
The FRE-435 harness (`scripts/eval/fre435_memory_recall/harness.py`, written for FRE-491 pre-FRE-653) drives recall via `MemoryRecallQuery(query_text=case.query, limit=...)` with **no `entity_names`**. But the FRE-653 relevance-bounded branch in `query_memory` is gated on `entity_recall = bool(entity_names or entity_types)`. With no entity_names, both flag states fall to the legacy bare `MATCH (c:Turn)` path → **the A/B would show zero difference**. So the harness must first be made faithful to the prod recall path (`request_gateway/context.py:198` passes `entity_names=_capitalized_entity_hints(user_message)` + `query_text`). This fidelity fix is prerequisite to any meaningful A/B.

## Acceptance criteria carried (ADR-0100, the assembled seam — measured here, flag-on-live deferred)
- **AC-1a** — query_memory recall invariant to recency: flag-on recovers the >30-day false negatives the FRE-491 baseline denied.
- **AC-1b** — query_memory_broad surfaces a >90-day entity (broad path driven in the harness).
- **AC-2** — returned set relevance-ordered (the old-relevant ranks ahead).
- **AC-3** — recall holds under added recent distractors (`--distractor-background`).
- **AC-4** — a negative query (unrelated to corpus) returns nothing above the floor.
- **AC-5** — recall@k for an out-of-window positive unchanged at 5× corpus **and** `candidate_set_size ≤ proactive_memory_vector_top_k` (from the `memory_recall` event). Recall-invariance is the discriminator.
- **AC-7** — flag off reproduces the FRE-491 baseline exactly.
- **AC-6** — empty-result signal queryable in ES: **deferred to master rollout** (needs the flag live in prod). Measured here only as event-payload agreement on the test substrate.

## Plan (revised per codex review)

### Step A — harness fidelity + **dual-path routing** (`harness.py`)
1. **Faithful recall driving, both paths per case** (codex risk 1+2 — prod splits: non-`MEMORY_RECALL` → `query_memory` with `entity_names`; `MEMORY_RECALL` → `recall_broad` with no hints):
   - **Entity path:** `entity_names = _capitalized_entity_hints(case.query)` (import from `request_gateway.context`), pass `entity_names` + `query_text` to `MemoryRecallQuery` → `adapter.recall`. Hints from the query text only, never from `expected` (no cheating).
   - **Broad path:** `adapter.recall_broad(query_text=case.query, recency_days=90, limit=20)` for **every** case; score whether `expected.entity_names` appear in the broad entities (AC-1b).
   - Report **per-path** recall. Codex found **4/21 cases produce empty hints** (bespoke_probe ~lines 92,117,288,328) — those are structurally dead on the entity path and are measured on the **broad** path; annotate them so a zero entity-path delta there is not read as a failure.
2. **Flag/floor toggle via env (no fragile mutation):** run the harness as separate processes with `AGENT_RELEVANCE_BOUNDED_RECALL_ENABLED` and `AGENT_RECALL_SIMILARITY_FLOOR` set **before import** (the harness reads settings at import; codex Q4 confirms post-import mutation works too but env-before-import is cleaner for an A/B). The harness **records the effective `settings.*` values** in the `RunReport` config block for provenance.

### Step B — floor calibration by **global threshold sweep** (codex risk 4 — not per-case eyeballing)
3. First **verify the cosine score range** empirically: run one `db.index.vector.queryNodes('entity_embedding', …)` against the seeded test KG and confirm `score` ∈ [0,1] cosine (codex Q2) before proposing any value.
4. Per case capture the **expected-entity cosine** (positive) and **max-distractor cosine** (negative) via `_query_entity_vector_candidates` (FRE-654 helper). Then **sweep** candidate floors 0.0→0.95 (step 0.05): at each floor compute recall@k + precision/false-positive across **all** cases (reuse `metrics.py`), and pick the floor at the recall/noise **Pareto knee**. Record the full sweep table; the proposed floor is global, evidenced, not anecdotal.

### Step C — run the A/B (measurement, on the test substrate)
5. `make test-infra-up`; verify the embedder endpoint (:8503) reachable.
6. Runs (each writes a gitignored JSON under the harness `out/`):
   - **Control** — flag off → must reproduce the FRE-491 baseline (**AC-7**).
   - **Treatment** — flag on, floor 0.0 → false-negative recovery on the entity path (**AC-1a**) and broad path (**AC-1b**).
   - **AC-1a invariance** — sweep `recency_days ∈ {1, 30, 365}` on the entity path; the >30-day positive must surface at **all three** (the ADR discriminator), not just default 30.
   - **AC-2** — assert the returned order is relevance-ranked (old-relevant index < recent-irrelevant index) on a treatment case.
   - **AC-3 / AC-5** — treatment + `--distractor-background N` at 1× and 5×; recall@k for the out-of-window positive unchanged **and** `candidate_set_size ≤ proactive_memory_vector_top_k` (from the `memory_recall` event). Recall-invariance is the discriminator.
   - **AC-4** — the negative cases already in the probe (empty `expected.entity_names`, ~yaml 444-480) return nothing above the chosen floor.
7. Capture `memory_recall` events (FRE-653) for `candidate_set_size` / `top_vector_score` (AC-5 + floor distribution). AC-6 payload-agreement (`empty_result` vs actual payload) is checkable on the **test** substrate; live ES-queryability is deferred to master.

### Step D — analysis + proposal (`docs/research/2026-06-28-fre-655-relevance-bounded-recall-ab.md`)
8. A/B table (recall@k, false-negative count, per-path), the floor distribution (positive vs distractor cosines), and the **proposed `recall_similarity_floor` + recency-weight cutoff values** with the evidence. Raw run JSON stays gitignored (curated summary only — never commit raw dumps).
9. **Owner sign-off:** present the proposed floor/cutoff values (AskUserQuestion); record the confirmed values in the writeup.

## Files
1. `scripts/eval/fre435_memory_recall/harness.py` — fidelity fix, flag/floor args, broad retrieval, floor-capture.
2. `scripts/eval/fre435_memory_recall/report.py` — record flag/floor + the calibration rows.
3. `scripts/eval/fre435_memory_recall/scoring.py` — broad-path scoring helper (pure).
4. `docs/research/2026-06-28-fre-655-relevance-bounded-recall-ab.md` — the A/B + calibration writeup.
5. Tests: `tests/` unit coverage for the new pure helpers (hint wiring, broad scoring, floor-row capture).

## Tests / gates
`make test-k K="recall or fre435"` → `make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit`. The A/B runs themselves are the AC evidence (reported), not unit tests.

## Out of scope (master/owner — explicitly deferred)
The flag default flip, the gateway deploy + live verify, AC-6 live ES-queryability, the `config/` rollout change. Floor/cutoff values are **proposed** here and **confirmed by the owner**; master rolls out.
