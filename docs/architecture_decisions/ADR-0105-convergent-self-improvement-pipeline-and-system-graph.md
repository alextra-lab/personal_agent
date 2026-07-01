# ADR-0105: Convergent Self-Improvement Pipeline & Isolated System Graph

**Status:** Proposed
**Date:** 2026-07-01
**Deciders:** Project owner
**Tags:** self-improvement, insights, captains-log, observability, postgres, system-kg, feedback-loop

**Extends:** ADR-0030 (Captain's Log Dedup & Self-Improvement Pipeline), ADR-0040 (Linear as Async Feedback Channel)
**Refines:** ADR-0098 D1 (System class) — for the self-improvement pipeline's own data, "System" is a *physically separate store*, not only a gated-in-Core class

---

## Context

**What is the issue we're addressing?**

The FRE-703 dashboard value-audit surfaced (did not cause) three structural problems in the agent's self-improvement machinery. They are grounded in the live code and substrate (verified 2026-07-01 against `/opt/seshat/.claude/worktrees/adrs`).

**1. The self-improvement loop is open and blind.** The pipeline produces a lot of signal — 465 insights/30d (265 actionable) plus 1,872 reflections — but there is no observable path from a proposal to the ticket it should become, nor from a ticket's outcome back to the proposal:

- `linear_issue_id` is populated on only **6 of 1,872** reflection docs, mostly since-canceled. The mechanical cause is the ADR-0040 review-bandwidth gate (`promotion.py:279–300`): a hard pause when `count_open_issues > issue_budget_threshold` (200), capped at 5 issues/run (`promotion_initial_cap`). That throttle is *correct by design* — it protects owner review bandwidth — but it is invisible, so conversion looks like a bug rather than a governed rate.
- The linkage exists **only on the Captain's-Log side**. The Insights-Engine surface (`agent-insights-*`, monthly) has **no ticket-linkage field at all**.
- **No outcome ever returns to the proposal.** A shipped or canceled ticket never teaches the engine which proposals were valuable. There is a 30-day suppression registry keyed on rejection fingerprint (ADR-0040), but no positive/negative signal attached to the *source proposal or source type*.

**2. "Insights Engine" and "Reflection Insights" read as two systems but are already one.** Both promote through the same `promotion.py` (ADR-0030). The Insights Engine (`insights/engine.py`, FRE-24) is a *statistical* detector — it scans telemetry (cost, delegation, skill-routing, graph staleness) and emits `Insight` records, and with `insights_wiring_enabled` also emits Captain's-Log proposals. Reflection (`captains_log/reflection.py`, ADR-0010) is an *LLM/DSPy* reflector over session captures emitting `CaptainLogEntry` / `ProposedChange`. Downstream of "a proposal was produced," they are the **same `ProposedChange` model** (what/why/how + category/scope/fingerprint/seen_count) with the same fingerprint dedup and the same promotion path. The split is historical (reflection first via ADR-0010; the engine later via FRE-24), not designed — and it manifests as two disjoint dashboards for one funnel.

**3. The signal is stored flat when it is inherently relational.** Proposals derive from stats, promote to tickets, tickets produce outcomes, and proposals/stats correlate with and influence one another. Those are multi-hop relationships. They live today only as flat ES documents (`agent-insights-*`, `agent-captains-reflections-*`), where correlation and influence cannot be expressed as traversals — only as aggregations that cannot answer "which stat patterns produce proposals that ship" or "which proposals cluster."

**4. Repetition is *semantic*, and dedup/suppression happen too late.** Measured on the live corpus (942 reflections with proposals): **832 distinct fingerprints of 942** (~88% textually distinct) yet **topically concentrated** (performance 43%, observability 23%, reliability 11%). So the pile of `~1,800 awaiting_approval` proposals is not literal-duplicate — it is **the same idea re-phrased**, which the text-exact `dedup.py` fingerprint lets through, and which nothing suppresses until *promotion* (if it ever reaches it). The producer has no awareness of its own history at the moment it generates, so it re-proposes ideas already decided (shipped or rejected). Dedup must be **semantic** and must act **at generation**, not only at promotion.

**What needs to be decided.** How to (a) converge the two producers into one pipeline with a source discriminator without rebuilding the promotion path; (b) give the relational layer a graph home that is **physically isolated** from the user-memory knowledge graph; (c) make the funnel — including the ADR-0040 throttle — observable; (d) close the loop so ticket outcomes reweight *and actively suppress* source proposals; and (e) dedup semantically **at generation** by making the store a producer-side read surface — with the similarity signal's reliability **measured, not assumed**. Two design decisions were reserved for this ADR and settled with the owner (2026-07-01): the **store engine** for the isolated System graph, and its **relationship to ADR-0098's System class + FRE-639**. This revision (2026-07-01) folds in the generation-time read surface (D9), semantic-dedup-behind-a-measurement-gate (D10), and the active-suppression sharpening of the loop-close (D7).

---

## Decision

Converge to **one self-improvement pipeline with pluggable sources**; model its relational layer as a graph in an **isolated Postgres schema**, physically separate from the Neo4j user-memory KG; make the funnel observable; and close the outcome→source loop. Build on `promotion.py`; do not rebuild it.

### D1 — One pipeline, a `source` discriminator (converge, don't rebuild)

Model a single self-improvement stream. The two producers stay distinct implementations — a statistical detector and an LLM reflector are legitimately different — but everything **downstream of "a proposal was produced"** is unified:

- Add a `source` discriminator to the shared proposal model — `Literal["statistical_detector", "reflection"]` (extensible). The producers already converge on `ProposedChange`; this formalizes it.
- Rename so the concept reads as **"sources of insights,"** not two products: reflection is *a source*, the statistical detectors are *sources*. The umbrella concept is "Insights / self-improvement."
- There is **exactly one promotion entrypoint** (`promotion.py`). Both sources reach it; the Insights-Engine surface is brought onto the same promotion + linkage path as reflection (today only reflection is wired to linkage).

### D2 — The relational layer is a graph in an **isolated Postgres schema**

The self-improvement data is modeled as a graph — nodes (`Proposal`, `Stat`, `Ticket`, `Outcome`) and edges (`DERIVES_FROM`, `PROMOTED_TO`, `PRODUCED`, `CORRELATES_WITH`, and influence edges) — so correlation, influence, and proposal→ticket→outcome paths are first-class traversals, which flat ES aggregations cannot express.

**Store: a dedicated Postgres schema (e.g. `sysgraph`) in the existing `pgvector/pgvector:pg17` instance.** Nodes/edges as tables; multi-hop via recursive CTEs; `pgvector` (already present) for optional "similar proposals" nearness. Chosen over a dedicated graph engine because:

- **Volume is low** (hundreds–low-thousands of nodes over the project lifetime; written on the consolidation schedule and on promotion, not a hot path) — recursive-CTE traversal is comfortably adequate; a graph engine is unjustified weight.
- **RAM is the binding constraint on prod** (~10 GiB, no GPU, shared with Neo4j/ES/embedder/reranker). Postgres is already running, so this adds **zero persistent RAM** — the decisive operational win over a second Neo4j container (~1 GiB+).
- **Isolation is enforced at three layers, none a forgettable filter.** *Engine-level (structural):* a different engine from the Neo4j user KG means a Cypher recall/tutor traversal *physically cannot* return a `Proposal`/`Ticket`/`Outcome` node — there is no shared traversal path, so the query-time-class-filter failure mode is impossible. *Permission-level (roles & grants):* the residual leak vector is application code that opens *both* stores and joins them in one query path — that is closed at the database, not by discipline: a **dedicated Postgres role owns `sysgraph` and holds grants only there**, and the recall/user-facing connection uses a role with **no grant to `sysgraph`** (and, symmetrically, the `sysgraph` role has no grant to the user-facing tables). A recall-path connection that tried to `SELECT` from `sysgraph` gets a **permission error**, not a silent cross-join. *Repository-level:* `sysgraph` is reachable in code only through its own repository (a distinct engine/connection object); no recall/tutor code path (`MemoryService` and callers) constructs or opens it. Engine separation kills traversal leakage by construction; the role/grant policy kills application-layer join leakage at the DB permission layer (AC-2 proves it with a permission-denied test); the repository boundary makes the separation legible in code. No single query author has to remember a filter.

**Validity envelope (when this decision holds — cross it and re-open the dedicated-graph-engine ADR).** Postgres+CTE is the right store *while* the workload stays **shallow-path** (proposal→ticket→outcome is depth 2–3; correlation is one-hop `CORRELATES_WITH` clustering plus pgvector similarity — **not** multi-hop pattern mining or frequent deep traversals) and the graph stays **small** (order **≤ 50k nodes/edges**; the realistic lifetime projection is well under that). The **flip trigger to an embedded graph DB (Kuzu)** is crossing *either* bound: heavy/first-class exploratory graph analytics, **or** the node/edge count approaching 50k. The `sysgraph` repository is the seam that keeps the flip cheap — it is the only code that opens the store, so the engine can change behind it without touching producers or the promotion path.
- **Writes are transactional with promotion.** Postgres already owns the promotion-adjacent write path (sessions, cost). Proposal→Ticket linkage and Outcome ingestion commit atomically with the promotion write — no cross-store dual-write race.
- **`pgvector` makes the vector requirement a non-issue** — "similar proposals" nearness needs no separate vector-capable store.

**Two-layer split within the System domain** (per the ticket): the Postgres `sysgraph` schema is the **relational/correlation layer**; **ES stays the time-series/dashboard layer** (`agent-insights-*` and reflections indices remain the emit surface the funnel dashboard reads). The graph is not a replacement for ES; it is the layer ES cannot be.

### D3 — Refine ADR-0098 D1: pipeline-System is a separate store (complementary to FRE-639)

ADR-0098's **System class** and this ADR's **System graph** share a name but are structurally different data:

- ADR-0098 System = **user-conversation turns whose subject is infra** ("is Postgres healthy?"). That data is physically entangled in the user's turn/session stream and cannot be relocated without fracturing turn integrity; gating it **within Core** (FRE-639) is the correct tool. **Unchanged by this ADR.**
- This ADR's System = the **self-improvement pipeline's own relational model** (proposals/stats/tickets/outcomes). It was **never in the user KG**; graph-modeling it is net-new, so there is nothing to migrate — it is *born* in the isolated `sysgraph` store.

So this ADR **refines** ADR-0098 D1 by naming the principle "the self-improvement pipeline's System data lives in a physically separate store," and leaves FRE-639's in-Core System-class gate intact. They compose: user-turn System stays gated in Core; pipeline-System is born isolated. (Rejected alternative: relocate ADR-0098's user-turn System entities into `sysgraph` too — that turns FRE-639 from a class-gate into a soul-subgraph migration, raising blast radius for no gain.)

### D4 — Bidirectional, queryable linkage — on both sources

Building on `promotion.py`: the source proposal carries the ticket id **and** the ticket node carries the source proposal id, queryable both ways. Critically, the **Insights-Engine surface (`agent-insights-*`), which has no linkage field today, gets one** — so linkage is not reflection-only. In the graph this is the `PROMOTED_TO` edge; on the ES/document side it is an explicit `linear_issue_id` field on the insight document (not the current aspirational null).

### D5 — Verbatim substance carry-through on promotion (owner firm AC, 2026-07-01)

When a proposal is promoted, the created Linear ticket MUST carry the proposal's **full substance — what / why / how / rationale (and `experiment_design` where present) — verbatim**, not a thin auto-summary. The substance already exists end-to-end in the model (`ProposedChange.what/why/how`, `CaptainLogEntry.rationale/experiment_design/expected_outcome/potential_implementation`). Today `promotion.py` builds a `[{category}] {pc.what[:80]}` title and a formatted description; the requirement is that the description carries every substantive field in full so the promoted ticket is *evaluable and verifiable by the master gate* — the same "why + what, decision-ready" bar held for hand-written tickets. A promoted ticket lacking what/why/how/rationale is not actionable and must not be created.

### D6 — One observable funnel; the ADR-0040 gate is observable, not removed

Replace the two disjoint dashboards with **one funnel** — `produced → promoted → shipped vs canceled` — **faceted by source**, reading real data. The ADR-0040 throttle becomes a **first-class, queryable funnel state** ("throttled: open-issue budget"), not a silent `log.warning` — the fix is to make the gate observable, not to remove it. Conversion telemetry is emitted with **explicitly-mapped ES fields** (per FRE-704, so they are not silently dropped when a daily index hits the 300-field cap). Dashboard is built in the Kibana UI and Playwright-verified to render (never hand-authored Lens ndjson).

### D7 — Close the loop: outcome → source signal

A ticket outcome — `shipped` / `canceled-as-noise` / `owner-rejected` (via the ADR-0040 label channel) — writes an `Outcome` node linked to its source `Proposal` (`Ticket-[:PRODUCED]->Outcome`, `Proposal-[:PROMOTED_TO]->Ticket`) and updates a **realized-value signal on the source**. The next promotion run **reads** that signal to weight or suppress proposal sources/types by realized value over time. This closes the arc the suppression registry only half-served: not just "suppress this fingerprint" but "this *source/type* has earned/lost promotion priority."

**Minimal first algorithm (specified so this is decided, not deferred; build may A/B a richer one).** The signal is keyed on `(source, category)` — the smallest granularity that lets the loop distinguish "reflection cost-proposals ship" from "statistical delegation-proposals get rejected":

- **Outcome weights:** `shipped = +1.0`, `owner-rejected = −1.0`, `canceled-as-noise = −0.5`, `deferred = 0` (no signal — right idea, wrong time).
- **Windowed, smoothed value** per key over a **trailing 90-day window** (verdicts age out; no all-time calcification): `v = Σweights / (n + 2)` — additive smoothing with prior `2` pulls cold-start keys toward `0` so one early verdict cannot swing a source.
- **Promotion ranking:** the existing `seen_count` priority is *modulated*, not replaced — `priority × (1 + clamp(v, −0.5, +0.5))`. Positive-track keys rank up, negative-track down, bounded so one bad key cannot fully silence a source.
- **Suppression:** if `v ≤ −0.4` over `n ≥ 5` in-window outcomes, the `(source, category)` is deprioritized to the bottom for a **30-day cooldown** — parallel to, not a replacement for, the ADR-0040 fingerprint suppression registry.
- **Active suppression of already-*decided* kinds (not merely record).** **Definition — a kind is `decided` when it has any terminal outcome except `deferred`: `shipped ∪ owner-rejected ∪ canceled-as-noise`.** (`deferred` = "right idea, wrong time" per the ADR-0040 Defer label, and `awaiting` are explicitly **not** decided.) Recording an `Outcome` is necessary but not sufficient. A proposal whose semantic kind (D10 cluster; the interim key `(source, category)` — or `(source, category, facet)` where facets exist — until D10's probe resolves) is `decided` gets that status stamped, and D9's generation-time read consults it to **stop the re-proposal at the source**. The open loop persists precisely because a decided verdict never suppresses the next re-proposal — closing it means the loop *acts on* the outcome, not just stores it. (The measured `~1,800 awaiting_approval` pile is the symptom of record-without-act.)

These constants are the starting point, not a fixed law; the acceptance test (AC-6) asserts the *mechanism* (an outcome changes `v`, marks the kind decided, and the next run acts on both), so a build A/B can retune weights/window without reopening this ADR.

### D8 — Operational readiness: Postgres tuning & auto-scheduled maintenance

The `sysgraph` schema adopts Postgres, which today runs **stock PG17 defaults** (verified live: `shared_buffers=128MB`, `work_mem=4MB`, `random_page_cost=4`, `effective_io_concurrency=1`, `track_io_timing=off`, `effective_cache_size=4GB`, autovacuum on with stock scale factors). Because tuning is **instance-wide** and the prod host is **RAM-binding**, this ADR specifies a **conservative, RAM-aware** profile and an **auto-maintenance schedule**, carried as a *separate* implementation ticket (an instance-wide infra change applied by build/master with the Postgres restart — not the pipeline work, and not applied by the adr session):

- **SSD-appropriate planner costs** (safe, RAM-neutral, correctness-improving on the current spinning-disk defaults): `random_page_cost≈1.1`, `effective_io_concurrency≈200`, `track_io_timing=on` (observability for `EXPLAIN (BUFFERS)`).
- **RAM-aware buffers** — modest, host-shared: right-size `shared_buffers` and `effective_cache_size` to Postgres's *actual* share of the ~10 GiB host (not the 4 GiB planner default that assumes a dedicated box); a small `work_mem` bump for recursive-CTE sorts. Exact values set against the deployed memory envelope in the ticket, not guessed here.
- **Auto-maintenance:** autovacuum is the baseline auto-maintenance; for the `sysgraph` tables confirm it runs (per-table thresholds if the write pattern warrants), plus a scheduled `VACUUM (ANALYZE)` and pgvector index maintenance (`REINDEX`/rebuild cadence) for the vector index if similar-proposal search is enabled.
- **pgvector baseline (reuse the established one; do not introduce new/unbounded dimensionality):** proposal embeddings use **`vector(1024)`** — the deployed embedder's native dimension (Qwen3-Embedding-0.6B; matches the `embedding_dimensions` setting and the existing `artifacts` schema, `docker/postgres/migrations/0003_artifacts_schema.sql`), indexed **HNSW with `vector_cosine_ops`** (identical to `artifacts`). Target: top-k similarity **< 50 ms p95** over the (low-thousands) proposal corpus — trivially met at this scale, stated so a later regression is detectable. Whether the vector column is *used* for semantic dedup is gated on D10's separation probe; if the probe fails (or explicit `CORRELATES_WITH` edges suffice), the column is omitted rather than left unindexed.

### D9 — `sysgraph` is also a generation-time READ surface (producer-side recall-before-emit)

The System graph is not only a downstream store; the producers **read it before they emit**. Before a reflection or statistical producer records a proposal, it queries `sysgraph` — semantic similarity ("is there an equivalent existing proposal?") plus a status/outcome traversal ("what happened to it?") — and branches:

- **similar exists AND decided** (shipped, owner-rejected, or canceled-as-noise — the `decided` set defined in D7) → **do not re-record**; at most annotate "already addressed by proposal *X*";
- **similar exists AND still awaiting** → **reinforce** the existing proposal (increment `seen_count` / add a link) instead of creating a near-duplicate;
- **nothing similar** → **generate new**.

This moves dedup and self-awareness **upstream to generation**, where D7's promotion-time suppression is only a backstop. It stops repetition at the source (no wasted LLM generation, no near-duplicate pile — the measured `832/942` semantic-repeat problem is prevented, not swept up later) and makes the generator **aware of its own history** — the "engine that learns" goal. Both producers are **background** (reflection per FRE-710; the statistical engine on the consolidation event), so the extra read has **no user-facing latency cost**. Generation-time read is the **front line**; promotion-time suppression (D7) is the backstop — they compose, neither replaces the other.

**"Similar" is resolved by D10** — the semantic match if the probe passes, else the explicit `(source, category, facet)` fallback key. **Interim behavior before D10's probe resolves:** D9 keys on `(source, category, facet)` where facets exist, else `(source, category)` with conservative matching — labeled explicitly as **non-semantic fallback**, so a build never silently ships "semantic" dedup on an unmeasured floor.

**Fail-open, and observably so.** The read must **fail open**: if `sysgraph` is unreachable the producer degrades to "generate new" — a background job is never blocked by the store. But fail-open silently weakens dedup during an outage, so each fail-open degrade is **counted and reported** (a telemetry counter + a funnel/alert signal), so a duplicate spike during an outage is explainable and bounded rather than mysterious.

### D10 — Semantic (not fingerprint) dedup — **measured before adopted** (the separation-probe gate)

The existing `dedup.py` keys on a **text-exact fingerprint**, so the same idea in different words passes as new — the live corpus proves it: **832 distinct `proposed_change` fingerprints out of 942** (~88% textually distinct) yet **topically concentrated** (performance 43%, observability 23%, reliability 11%). The repetition is **semantic** (one idea, many phrasings), not literal-duplicate. Dedup must therefore cluster by **meaning** (D9's "is there an equivalent?"), not by fingerprint.

**But semantic dedup is only as reliable as the similarity signal — the same clean-floor discipline ADR-0103 forced, applied to a new domain.** ADR-0103's *no-clean-floor* verdict is **domain-scoped** to dense, topically-overlapping personal memory (FRE-670 probe). The insights corpus is a *different distribution* — structured, templated (what/why/how/rationale), topically-distinct — so an embedder **may** yield non-overlapping good/bad cosine distributions (a thresholdable floor). It may equally fail: per-turn reflections (FRE-710) create genuine near-duplicates that could be denser-within-category than they are separable. **This is not assertable either way without measurement** (the FRE-489 lexical-masking lesson).

**Decision:** treat this as a **build-phase measurement gate**, not an in-ADR assertion. Reuse the **FRE-670 / ADR-0103 separation probe** (which ADR-0103 defines as a regression instrument) on the **real proposal/insight corpus** before any vector-clustering code commits:

- **If positive/negative cosines separate** → adopt semantic dedup on a **clean similarity floor** with a **light embedder and no reranker**;
- **If they do not** → **fall back to explicit category + facet grouping** over the graph's explicit edges (a mechanism this domain supports natively, unlike recall) — no vector clustering. The **facet taxonomy** (the required facets per category, their extraction source, and how they are stored as explicit `sysgraph` attributes/edges) is a **build-ticket deliverable**, defined and stored before fallback dedup is enabled — it is not fixed in this ADR, but it is not optional either.

**Retrieval-stack scoping (do NOT inherit the User-KG recall stack).** The System graph's primary value is **structural traversal over explicit edges** (deterministic, no embedder). Vectors serve exactly **one narrow job** — clustering semantically-similar proposals — and only if the probe passes. Therefore:

- **No cross-encoder reranker.** It exists to reorder near-duplicates in dense personal memory where no floor separates them; an insight-clustering false-similar is a cosmetic grouping error, not a recall miss. FRE-697's reranker evaluation does **not** transfer here.
- **Embedder only if needed, and on an always-on private CPU path.** System-KG work is **background**, not a hot user turn, and must **not depend on the laptop/Mac-GPU tunnel** (the isolation principle). If an embedder is needed, **reuse the existing prod 0.6B VPS embedder** (`embeddings:8503`, already the `vector(1024)` source) **or ride FRE-697's ONNX-on-VPS conclusion** — never a separate or laptop-dependent path.
- **Exception to watch:** if a future consumer wants to *semantically retrieve past insights into agent context* (FRE-349 territory), that is recall-like and re-opens the embedder question — but FRE-708's core (correlation, observability, loop-closing, dedup) is **traversal-first**.

---

## Alternatives Considered

### Option 1: Keep the flat-ES-only model (no graph)
**Description:** Continue storing proposals/insights only as ES documents; express correlation via ES aggregations.
**Pros:**
- Zero new storage; no schema.
- Dashboards already read ES.
**Cons:**
- Cannot express multi-hop correlation/influence or proposal→ticket→outcome paths — the ticket's problem #3.
- Conversion and loop-closure remain unmeasurable/uncloseable as they are today.
**Why Rejected:** It is the status quo that FRE-703 flagged as broken. Aggregations are not traversals; the relational questions are exactly what ES cannot answer.

### Option 2: Model the System graph inside the user-KG Neo4j with a `class=System` label + query-time filter
**Description:** Reuse the existing Neo4j; tag self-improvement nodes `System` and filter them out of recall.
**Pros:**
- No new store; native Cypher.
- Consistent with ADR-0098's class axis.
**Cons:**
- Isolation is a **forgettable filter** — one recall query that omits the filter leaks operational nodes into tutor/recall, the precise failure the ticket forbids.
- Couples the agent's ops data to the soul subgraph's blast radius.
**Why Rejected:** The isolation requirement is *physical* ("no shared traversal path"), not a discipline the next query author must remember. A shared engine cannot provide it.

### Option 3: Embedded graph DB (Kuzu)
**Description:** A dedicated, in-process, file-backed graph engine for `sysgraph`.
**Pros:**
- Graph-native Cypher-like traversal; near-zero idle RAM; can add vectors later.
- Directly honors "it's a graph."
**Cons:**
- A new, less-mature dependency and a **second query dialect** + separate backup path — for the *lowest-stakes* data in the system.
- No transactional coupling with the promotion write (dual-store).
**Why Rejected:** Unjustified for hundreds–low-thousands of nodes when recursive CTEs suffice and Postgres is already present. Retained explicitly as the **flip trigger** in D2 if ad-hoc graph exploration ever becomes first-class — an independent Codex review (2026-07-01) converged on the same recommendation and the same flip condition.

### Option 4: Second Neo4j container
**Description:** A separate Neo4j instance mirroring the user-KG tooling.
**Pros:**
- Identical Cypher/driver/ops to the user KG; strongest "real graph" story; physical isolation.
**Cons:**
- ~1 GiB+ **persistent RAM** on a binding-RAM host; a second graph service to run/monitor/back up — heaviest option for the lowest-stakes data.
- Community edition offers no in-instance second database, so isolation *requires* the extra instance (and its RAM cost).
**Why Rejected:** The RAM cost is real and recurring on the constrained prod host; the operational weight is disproportionate to the data's stakes and volume.

### Option 5 (scope): Unify — relocate ADR-0098's user-turn System entities into `sysgraph`
**Description:** Treat all "System" as one domain and move user-turn infra entities out of Core into the isolated store.
**Pros/Cons:** Conceptually tidy, but turns FRE-639 from a clean class-gate into a live-substrate **migration touching the soul subgraph** — higher blast radius, coupled release, no functional gain.
**Why Rejected:** The two "System"s are different data (D3). Complementary isolation delivers the isolation win with none of the migration risk.

### Option 6 (dedup): Keep fingerprint-only dedup + promotion-time suppression only
**Description:** Leave `dedup.py`'s text-exact fingerprint as the dedup key and suppress only at promotion (D7).
**Pros:**
- No embedder, no read-before-emit, no measurement gate — simplest.
**Cons:**
- The measured `832/942` distinct fingerprints prove fingerprinting **already fails** on semantic repeats.
- Promotion-time-only means the LLM still **generates** the near-duplicate (wasted cost) and it **piles up** (the `~1,800 awaiting` symptom) before anything suppresses it; the generator never learns.
**Why Rejected:** It is the status quo that produced the pile. Fingerprint dedup demonstrably lets the actual (semantic) repetition through; suppressing only at promotion treats the symptom, not the source.

### Option 7 (dedup): Adopt semantic (vector) dedup **without** measuring corpus separation
**Description:** Assume the insights corpus has a clean similarity floor and ship vector clustering directly.
**Pros:**
- Faster to build; no probe step.
**Cons:**
- Assumes a floor exists — the exact mistake FRE-489 (lexical-masking) and ADR-0103 (no-clean-floor on *personal memory*) warn against. If the corpus does **not** separate, semantic dedup silently mis-clusters — collapsing distinct proposals or missing real duplicates.
**Why Rejected:** Un-measured. ADR-0103's no-floor verdict is domain-scoped, so it neither licenses assuming a floor *nor* assuming none here — only a measurement resolves it. Hence the D10 separation-probe gate, with an explicit non-vector fallback (category + facet grouping).

---

## Consequences

### Positive Consequences

- **One legible system.** Reflection and the statistical detectors read as sources of one insights stream through one promotion path — no more two-products confusion, one funnel instead of two disjoint dashboards.
- **Conversion becomes measurable** and the ADR-0040 throttle becomes a visible governed rate rather than an apparent bug.
- **The loop closes.** Ticket outcomes reweight source proposals — the first realized-value feedback the engine has ever had, the prerequisite ADR-0040 names for any future autonomy move (FRE-586).
- **Correlation/influence are first-class** graph traversals, isolated by engine from user recall — the ops graph cannot pollute the tutor corpus, by construction.
- **Zero new RAM/service** on the binding-RAM host; linkage writes are transactional with promotion.
- **Promoted tickets are evaluable** — verbatim substance carry-through makes auto-promoted tickets meet the same decision-ready bar as hand-written ones.
- **Repetition stops at the source.** Generation-time read-before-emit (D9) + semantic dedup (D10) prevent the near-duplicate pile the fingerprint key let through — no wasted LLM generation, and the generator becomes aware of its own history ("the engine that learns").
- **The vector decision is measured, not assumed** (D10 probe gate) — the System KG gets the *simple* retrieval recall could not (thresholdable floor + light embedder, no reranker) **only if the corpus earns it**, with an explicit non-vector fallback.
- **A latent instance-wide Postgres tuning gap is closed** (SSD costs, RAM-aware buffers, IO observability, auto-maintenance).

### Negative Consequences

- **A new store to own** (`sysgraph` schema, migration, repository) plus a new cross-store linkage (proposal↔ticket) whose provenance must not dangle.
- **Recursive-CTE correlation queries are more verbose** than Cypher — tolerable at this volume, revisited only at the D2 flip trigger.
- **Producer convergence touches a live pipeline** — the `source` discriminator and single-entrypoint refactor must not regress the currently-shipping human-closed loop (ADR-0040).
- **Instance-wide Postgres parameter change** requires a Postgres restart (an always-ask-class deploy) and affects every Postgres consumer, not just `sysgraph`.
- **A generation-time read is added to both producers** — a `sysgraph` query before each emit. Negligible latency (both producers are background), but it couples generation to store availability; the read must fail *open* (degrade to "generate new" if the store is unreachable) so a `sysgraph` outage never blocks reflection/insight generation.
- **Semantic dedup depends on a measurement that may say "no."** If the probe fails, the vector-clustering approach is dropped for the category+facet fallback — a real possibility the build must plan for, not a guaranteed capability.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Cross-store linkage (proposal↔ticket) dangles — an orphaned `Ticket`/`Outcome` node or a null back-reference | Medium | Joinability probe (ADR-0074) over the linkage; linkage written transactionally with promotion; AC-3/AC-6 assert both directions resolve |
| Producer-convergence refactor regresses the shipping ADR-0040 loop | Medium | Single promotion entrypoint preserved (`promotion.py`), not rewritten; convergence adds a discriminator, not a new path; existing promotion tests kept green |
| RAM-aware tuning misjudged on a shared host → Postgres starves Neo4j/ES or itself | Medium | Conservative, host-shared sizing set against the *deployed* memory envelope; values verified via `SHOW` post-deploy; applied by build/master with the restart, not blind |
| The realized-value signal is written but never read (a dead field) | Medium | AC-6 requires the *next promotion run* to demonstrably reflect the signal, not merely that it was stored |
| System store leaks into user recall via a future shared code path | High | Three-layer isolation (D2): engine separation (Postgres vs Neo4j) + **role/grant policy** (recall role has no grant to `sysgraph`) + repository boundary; AC-2 proves it with a **permission-denied test**, not just a grep |
| Verbatim carry-through bloats tickets / hits Linear limits | Low | Carry full substance in the description body (Markdown), not the title; truncate only display-title, never the substance fields |
| Semantic dedup mis-clusters — collapses distinct proposals or misses real dupes | Medium | D10 separation-probe gate: adopt vector clustering **only if** the corpus separates; explicit category+facet fallback otherwise; AC-10 asserts the branch matches the measured result |
| Generation-time read blocks producers if `sysgraph` is down | Medium | The read fails **open** — unreachable store degrades to "generate new," never blocks generation (a background job); D9 backstopped by D7 promotion-time suppression |
| Embedder scope-creep — the System KG grows a User-KG-style reranker/multi-path stack | Medium | D10 scopes it out explicitly: no reranker; embedder only if the probe passes, on the always-on private VPS CPU path (`embeddings:8503` or FRE-697 ONNX), never laptop-GPU; AC covers "no reranker dependency" |

---

## Implementation Notes

**Files affected (primary):**
- `src/personal_agent/captains_log/models.py` — add `source` discriminator to the proposal model.
- `src/personal_agent/insights/engine.py` — emit through the unified path; carry the linkage field onto `agent-insights-*`.
- `src/personal_agent/captains_log/promotion.py` — verbatim substance carry-through (D5); bidirectional linkage (D4); emit the ADR-0040 throttle as a queryable funnel state (D6); write graph nodes/edges + outcome ingestion (D2/D7).
- `src/personal_agent/sysgraph/` (new) — the isolated System-graph repository (the only code that opens the `sysgraph` schema); nodes/edges tables + recursive-CTE traversals + the generation-time read (D9: similarity + status traversal, fail-open) + probe-gated pgvector similarity.
- `src/personal_agent/insights/engine.py` and `src/personal_agent/captains_log/reflection.py` — insert the generation-time read-before-emit branch (D9) into both producers, before they record a proposal.
- `src/personal_agent/captains_log/dedup.py` — semantic dedup (D10) replacing/augmenting the text-exact fingerprint key, **behind the separation-probe gate** (falls back to category+facet grouping if the probe fails).
- separation-probe harness (reuse the FRE-670 / ADR-0103 instrument) run against the real `agent-captains-reflections-*` / `agent-insights-*` corpus — the D10 build-phase gate.
- `docker/postgres/migrations/00XX_sysgraph_schema.sql` — new schema (no Alembic; per project policy).
- `docker/postgres/` tuning + `docker-compose.yml` Postgres `command:`/config — D8 (separate ticket; build/master-applied).
- Kibana funnel dashboard (built in UI, exported, Playwright-verified) + explicit ES field mappings (FRE-704).

**Dependencies / coordination:**
- Coordinates with **FRE-639** (ADR-0098 T3 System gate) — complementary, not blocking (D3).
- Coordinates with **FRE-704** (ES 300-field cap) for the explicit conversion-field mappings.
- Relates to **FRE-586/FRE-598** (proposal acceptance-rate signal / KG-quality anomaly pipeline).
- **D10 separation probe** reuses **FRE-670 / ADR-0103**; **embedder** rides **FRE-697** (ONNX-on-VPS) or the prod `embeddings:8503` — no reranker; **FRE-349** (semantic retrieval into context) is the watched exception; **FRE-710** (coarser reflection cadence) reduces the near-duplicate inflow the dedup must absorb.

**Testing strategy:** unit tests for the `source` discriminator + verbatim carry-through (string-containment against source fields); integration test for the end-to-end loop (D7) against the test substrate; joinability probe for linkage; Playwright render-check for the funnel; the D10 separation-probe eval on the real corpus as the gate before semantic-dedup code lands.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 — One pipeline, source-discriminated.** *Outcome:* both producers flow through a single promotion path, every produced proposal labeled by source. · **Check:** query produced proposals and confirm rows with both `source=statistical_detector` and `source=reflection`; structurally confirm there is exactly one promotion entrypoint both reach. · *Fails if* any produced proposal lacks a `source`, or reflection and insights still promote via separate code paths.
- **AC-2 — Physical isolation, provable at the DB permission layer.** *Outcome:* a user-recall/tutor query can never return a self-improvement node, and vice versa — enforced by grants, not by a filter or code discipline. · **Check:** (a) *permission-denied test* — a connection using the recall/user-facing Postgres role issued `SELECT … FROM sysgraph.*` returns a **permission error** (the recall role holds no grant to `sysgraph`); (b) *engine test* — a Cypher traversal from any user-KG node to a `Proposal`/`Ticket`/`Outcome` node is impossible (different engine); (c) *repository grep* — no `MemoryService`/recall code path constructs or opens the `sysgraph` connection. · *Fails if* the recall role can read `sysgraph` (grant leak), the System graph is a labeled subgraph inside the user-KG Neo4j, or any recall path opens `sysgraph`.
- **AC-3 — Bidirectional linkage, both sources.** *Outcome:* for a promoted proposal from *each* source, ticket id and source-proposal id resolve each other. · **Check:** pick one promoted `reflection` proposal and one promoted `statistical_detector` insight; query proposal→`linear_issue_id` and ticket→source-id, both non-null and mutually consistent; confirm the `agent-insights-*` document carries `linear_issue_id`. · *Fails if* linkage resolves only on the reflection side (today's state), or either direction is null for a real promoted pair.
- **AC-4 — Verbatim substance carry-through.** *Outcome:* the promoted Linear ticket body contains the source proposal's full what/why/how/rationale (and `experiment_design` where present), not a paraphrase/truncation. · **Check:** string-containment assertion — each source field's full text appears verbatim in the created ticket description; the title truncation (`pc.what[:80]`) does not apply to the substance body. · *Fails if* the description summarizes, or omits any of what/why/how/rationale that is present on the source.
- **AC-5 — One observable funnel, faceted by source, cap-safe fields.** *Outcome:* a single dashboard shows produced→promoted→shipped/canceled from real docs, split by source, with the throttle state visible. · **Check:** the funnel renders (Playwright) with non-zero real counts and a `source` facet and a "throttled: budget" state; the conversion fields are **explicitly mapped** in the ES template (not dynamic), verified against the index mapping (FRE-704); the two prior dashboards are retired. · *Fails if* counts are placeholder/zero, the fields are dynamic (droppable), or the throttle is not a visible state.
- **AC-6 — Loop closed end-to-end on ≥1 real path.** *Outcome:* a real ticket's outcome updates a signal on its source proposal that the next promotion run acts on. · **Check:** take one real ticket that reached an outcome; confirm an `Outcome` node linked to the source `Proposal` (not orphaned); confirm the source's `(source, category)` `v` changed by the expected outcome weight (query before/after); confirm the *next* promotion run's ranking/suppression reflects the changed `v` (the signal is read, not just written). · *Fails if* the `Outcome` node is orphaned, or the signal is written but no promotion code reads it (a dead field).
- **AC-7 — Postgres operationally ready.** *Outcome:* the deployed Postgres runs the tuned, RAM-aware profile and auto-maintains the `sysgraph` tables. · **Check:** `SHOW` on the deployed instance returns the tuned values (SSD costs + right-sized buffers, not stock defaults); and auto-maintenance is *demonstrably running* on `sysgraph` — either `pg_stat_user_tables.last_autovacuum`/`last_analyze` becomes non-null after a **seeded write load above the configured autovacuum thresholds**, or the scheduled `VACUUM (ANALYZE)` job's last successful run for `sysgraph` is shown (the low steady-state volume can otherwise leave autovacuum legitimately idle). · *Fails if* the instance still reports `random_page_cost=4`/`effective_io_concurrency=1`, or neither auto-maintenance path can be shown to have run for `sysgraph`.
- **AC-8 — Separation-probe gate ran and drove the branch (D10).** *Outcome:* the vector-vs-fallback decision is grounded in a recorded, replayable measurement on the real corpus, not an assumption. · **Check:** a **versioned probe artifact** (committed to the repo or stored in the build/eval output) records the corpus source + query, the time window, item counts, the labeled positive/negative pair counts, the cosine distributions, the chosen threshold/floor, the pass/fail decision, the probe code version, and the run id; and the shipped dedup branch is **mechanically checked against that artifact** (vector clustering only if the artifact says "separated," category+facet grouping otherwise). · *Fails if* semantic/vector dedup ships with **no** such artifact (a PR-description number does not count), or the artifact says "did not separate" yet vector clustering was adopted anyway (assuming a floor — Option 7).
- **AC-9 — Generation-time read prevents the semantic re-proposal (D9 + D10).** *Outcome:* a producer facing an equivalent already-*decided* idea does not create a new near-duplicate; facing an equivalent *awaiting* idea, it reinforces rather than duplicates. · **Check:** replay a proposal whose equivalent is already `decided` (shipped, owner-rejected, or canceled-as-noise per D7) → **no new proposal row is created** (at most an annotation); replay one whose equivalent is still `awaiting` → the existing proposal's `seen_count`/links increment and **no near-duplicate row appears**. Compare against a control with the read disabled to show the duplicate *would* have been created. · *Fails if* the producer emits a fresh row for an already-decided or already-awaiting equivalent (read is a no-op), or if the read hard-fails *closed* and blocks generation when `sysgraph` is unreachable (must degrade to "generate new"). With `sysgraph` unreachable, the degrade must also be **observable** — the fail-open counter increments **and** the degrade surfaces on the funnel/alert signal (D9), not just an internal counter.
- **AC-10 — No inherited recall stack (D10 scoping), proven by instrumentation.** *Outcome:* the System KG runs traversal-first with, at most, a light always-on private-CPU embedder and **no reranker**, and no dependency on the laptop/Mac-GPU tunnel. · **Check:** an integration test runs the D9/D10 correlation + dedup path with (a) the laptop/Mac-GPU tunnel **deliberately unreachable** and (b) a **test double that fails on any reranker endpoint/module invocation** — the run **succeeds**, using only `embeddings:8503` or the FRE-697 ONNX endpoint when embeddings are enabled; **and** a dependency/call-path scan shows no System-KG module imports or calls the User-KG reranker/recall stack. · *Fails if* the path invokes the reranker double (a reranker is on the path), or the run fails with the tunnel offline (a laptop-GPU dependency), or the scan finds a User-KG-recall import on the System-KG path.

**Seam owner (assembled intent):** the **loop-closure integration (AC-6)** remains the primary seam — it closes only when AC-1 (converged source), AC-2 (isolated store), AC-3 (linkage), and AC-4 (substance) all land: one real proposal must travel produced → promoted (with verbatim substance, linked both ways) → shipped/canceled → outcome → **kind marked decided** → next-run-reweighted-and-suppressed, through the converged pipeline and the isolated store, and show correctly on the funnel (AC-5). The **generation-time dedup seam (AC-9)** closes only once AC-8's probe has run and the store is a live read surface — it is asserted with, and depends on, AC-8. No single child ticket proves either; **master holds the decomposed ADR against AC-6 and AC-9** and neither closes because the last child merged — only because one real proposal demonstrably completes each arc. The observability (AC-5), operational (AC-7), and scoping (AC-10) seams are asserted independently.

---

## References

- ADR-0030 — Captain's Log Deduplication & Self-Improvement Pipeline (the promotion path this extends)
- ADR-0040 — Linear as Async Feedback Channel (the gate + label protocol this makes observable and closes)
- ADR-0097 — Ingested-Knowledge Taxonomy (the Personal/World/Stance vocabulary; System is the negative space)
- ADR-0098 — Memory Substrate & Lifecycle Architecture (D1 System class; this ADR refines it for pipeline data)
- ADR-0074 — Joinability probe (provenance integrity for the cross-store linkage)
- ADR-0010 — Structured LLM Outputs via Pydantic (the reflection producer)
- FRE-708 — this ADR's originating ticket (Refine the Insights Engine and make it observable)
- FRE-639 — ADR-0098 T3 System gate (coordinated; complementary, not blocked)
- FRE-704 — ES 300-field-cap dynamic-field drop (explicit conversion-field mappings)
- FRE-703 — Dashboard value-audit (surfaced the problem)
- FRE-586 / FRE-598 — proposal acceptance-rate signal / KG-quality anomaly pipeline
- ADR-0103 — Recall: no clean floor, structural separation (the separation-probe instrument D10 reuses; its no-floor verdict is *domain-scoped* to personal memory, hence D10's measurement)
- FRE-670 — semantic separation probe (the built tooling D10's gate runs on the insights corpus)
- FRE-697 — ONNX cross-encoder on VPS CPU (the always-on private embedder path the System KG rides if vectors are needed; no reranker)
- FRE-710 — coarser reflection cadence (reduces the per-turn near-duplicate inflow the dedup absorbs)
- FRE-349 — semantic retrieval of insights into agent context (the watched exception that would re-open the embedder question)
- Codex independent store review, 2026-07-01 (converged on Postgres + the Kuzu flip trigger)
- External design-input (Gemini), 2026-07-01 — guardrails folded into D2 (role/grant isolation + ≤50k-node validity envelope) and D8 (pgvector 1024/HNSW baseline); confirmed the workload is shallow-path (depth 2–3 + similarity), not heavy multi-hop analytics
- `docker/postgres/migrations/0003_artifacts_schema.sql` — the existing `vector(1024)` + HNSW `vector_cosine_ops` precedent the pgvector baseline reuses

---

## Status Updates

### 2026-07-01 - Proposed
**Changed By:** Project owner (adr session, Opus)
**Reason:** Authored from FRE-708 (Approved). Two reserved design decisions settled with the owner: store engine = Postgres dedicated schema (isolated by engine; independent Codex second opinion concurred), and scope = complementary to ADR-0098's System class (no relocation; FRE-639 unchanged). Awaiting Codex review + owner acceptance.

### 2026-07-01 - Revised (still Proposed) — generation-time dedup + semantic-behind-a-measurement-gate
**Changed By:** Project owner (adr session, Opus)
**Reason:** Folded in the owner's FRE-708 comments: **D9** (`sysgraph` is a generation-time read surface — producers read-before-emit and branch decided/awaiting/novel, front-line dedup with D7 as backstop); **D10** (semantic dedup, not fingerprint — grounded in the measured 832/942 semantic-repeat; adopted **only behind** the FRE-670/ADR-0103 separation-probe gate on the real corpus, with a category+facet fallback; no reranker; embedder only on the always-on private VPS CPU path, never laptop-GPU); and **D7 sharpened** to active suppression of already-*decided* kinds, not merely recording the outcome. New alternatives (Options 6–7), risks, and criteria AC-8/9/10 added; AC-9 is a second assembled seam (depends on AC-8). Awaiting Codex review + owner acceptance.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
