# ADR-0103: Recall is Retrieval — No Clean Similarity Floor; Separation is Structural

**Status:** Accepted
**Date:** 2026-07-01
**Deciders:** Owner, Architect (adr session)
**Tags:** memory, retrieval, recall-quality, measurement, principle

---

## Context

**What is the issue we're addressing?**

The memory-recall stream began as the owner's standing symptom (FRE-435): the agent answering
*"No prior discussions on this topic"* when prior context demonstrably existed. ADR-0100 traced
that to the retrieval **query layer** — recall built its candidate set by recency, let the vector
index only re-score the survivors, and then discarded the relevance scores it computed — and
replaced recency-keyed candidacy with relevance-keyed candidacy plus a similarity floor. That fix
was real, but it set up the **next** question, and the answer is the reason this ADR exists.

**The next question was: could a *better model* give recall a clean separation?** If a stronger
embedder or a cross-encoder reranker opened a clean gap between true matches and near-misses, the
`recall_similarity_floor` ADR-0100 introduced could be calibrated once, as a static constant, and
recall would be a solved threshold problem. FRE-694 (3 embedder runtimes × 3 quant levels × 3
sizes + cloud Voyage) and FRE-695 (3 reranker runtimes + cloud) measured exactly this.

**They proved the opposite, and the result is robust.** Across every configuration the positive
(true-match) and negative (no-record) score clouds **overlap everywhere**: best embedder Youden's
J only **0.59–0.64**, best reranker **0.785**; the hardest distractors always outscore the easiest
true matches. The cause is not dirty data — it is **structural**. What kills the separating gap is
**topical density**: near-neighbours on the *same topic* as the query but not the actual answer (a
"vision" query pulls mantis-shrimp eyes, X-ray vision, Rayleigh scattering — all genuinely about
vision, all scoring high). A bi-encoder cosine, or a cross-encoder reranker, cannot tell *"on the
topic"* from *"is the answer,"* because in meaning-space those things **are** neighbours.
Counter-intuitively, disparate data is *easier*; a dense, topically-clustered corpus — which a rich
personal memory is by design — is the worst case, and it gets **worse as the corpus grows**.

**Clean separation comes from structure — but only on closed vocabulary.** Taxonomy, entity types,
relationships, and recency windows turn relevance into a **deterministic filter**
(`type = Person AND after = Y`) instead of a fuzzy threshold. But the code says our vocabulary is
only **partly** closed:

- **`type` is soft-closed.** The extractor prompt asks for exactly one of seven values (Person,
  Organization, Location, Technology, Concept, Event, Topic — `second_brain/entity_extraction.py`,
  under the World/Personal/System knowledge class), but nothing **enforces** it: storage is
  `consolidator.py:605` `entity_data.get("type", "Unknown")` and `service.py:601`
  `.get("type", "")` (empty = keep the existing node type). No whitelist, no `Literal` — closed by
  convention, not by contract.
- **`topic` is open.** There is no `topic` field — a topic is an *Entity of type `Topic`* whose
  `name` is **free text the model writes**. "vision", "perception", "eyesight" become three
  different nodes. The only guard is the extractor's soft "normalize to canonical form" rule — an
  admission that the space is open.

This dissolves the apparent tension between "structure wins" and "but we built semantic search for a
reason." **A hard predicate separates cleanly only where the vocabulary is closed; on an open
vocabulary it reintroduces the exact mismatch semantic search exists to solve** (`topic = "vision"`
silently drops the note filed under `"perception"`).

**What needs to be decided:** this conclusion currently lives only in the research docs
(`docs/research/2026-06-29-fre-694-embedder-separation.md`,
`2026-06-30-fre-695-reranker-separation.md`,
`2026-06-30-recall-as-retrieval-and-the-dual-domain.md`) and MASTER_PLAN. It must be formalized as
a **decision record** so it *governs* implementation — so a future session does not quietly resume
chasing a clean floor (re-embedding, reranker-shopping, or calibrating one cosine cutoff), and so
FRE-655 is re-scoped off "calibrate the FRE-489 hard floor" before it is built. This ADR records
the **posture**; its sibling ADR-0104 records the **architecture** that follows from it. The two
were split deliberately: the posture is settled and measured (Accepted); the architecture's design
is still open (ADR-0104, Proposed).

---

## Decision

**Adopt the following posture for memory recall. It is a principle, not a mechanism — it constrains
every recall implementation and supersedes the "calibrate a clean floor" framing.**

1. **Recall is retrieval — the product surface of the knowledge graph.** Every path (dense vector,
   lexical, structural predicate, graph traversal, proactive, topic) is one route to the same
   buried knowledge. The architectural question is not "tune the recall floor"; it is "reach the
   knowledge reliably." (The architecture that follows is ADR-0104.)

2. **No single similarity score gives a clean separation floor on dense personal memory — measured,
   not assumed.** Best embedder Youden's J 0.59–0.64; best reranker 0.785; positive/negative clouds
   overlap everywhere (FRE-694/695). The result is runtime- and quant-robust (MLX ≡ llama.cpp to
   three decimals; 8-bit ≡ bf16). **This gets *more* true as the corpus diversifies, not less.**

3. **The re-embed / reranker-shopping question is closed: no.** No embedder — local or cloud SOTA —
   opens a clean floor, and recall already saturates (R@5 ≈ 0.98–1.00) at the production 0.6B
   embedder. A one-way-door re-embed is **not** justified *for separation*. (Embedder choice may be
   revisited for other reasons — latency, multilingual coverage — but never as a floor fix.)

4. **Separation and precision come from structure, on *closed* axes only.** Closed axes (`type`,
   `recency`, `relationship`) → hard predicates, clean. Open axis (`topic`, meaning, content) →
   semantic similarity + reranker, irreplaceable. **Structure-where-closed, semantic-where-open.**
   Structure narrows by what is closed; the embedder + reranker rank by meaning *within* that
   narrowed set, where no predicate can help. (Closing our soft-closed `type` is a precondition for
   the structural path — carried by the ADR-0098 chain, FRE-637.)

5. **The operating point is a soft, multi-signal, adaptive decision — never a static calibrated
   cutoff.** At the surface there is still a binary act (return something, or say "no prior
   discussions"), but it is a dial several signals *vote* on, not a line in the sand calibrated once.

6. **The reranker is the strongest single lever (+0.19 over the best embedder) but only as a soft,
   probabilistic operating point** (~88% recall @ ~9% FP) — **never a hard cutoff**.

7. **The probe (FRE-489/670) is a regression instrument, not an optimization target.** Use it to
   catch recall regressions, never to set production constants. A number measured on n≈54 probe
   cases is not a production threshold.

**The humility that keeps this honest (not a hedge — a scope boundary on the claim).** The
measurement is sound but was taken on a *limited* corpus: single user, young graph, first months
polluted by infra health-checks, deliberately diverse and growing. Split the finding:

- The **robust half** — *"no single score is a clean cutoff"* — gets **more** true as the corpus
  grows and diversifies. This is what the posture rests on.
- The **fragile half** — reading `J = 0.785` as *the* ceiling for personal memory, or "structure
  gives a clean floor" as a solved destination — is **overreach** on n≈54 and a non-stationary
  graph. This ADR does **not** claim either. Structure replaces a fuzzy threshold with hard
  predicates *on closed axes*; it does not make the scores separate better, and it is not a
  finished floor.

So the meta-conclusion is not *"we found the floor"* — it is *"stop chasing any single
clean-separation mechanism, because this class of corpus is unlikely to yield one."*

**Scope boundary (explicit, so it does not erode).** The dual-domain generalization recorded in the
research doc §6 — that a bounded, standardized corpus (e.g. SOC telemetry: MITRE/CVE/OCSF) would
make the calibrated-floor approach *valid* — is **analogy and hypothesis, not a finding, and is not
built here.** This is the owner's personal research project ("Forever the Student"); a
domain-specialized partner, if it ever exists, is a **fork** reusing the domain-agnostic core, never
a feature grafted on. Any future session feeling the pull to "just add a SOC connector/ontology"
stops at this line.

---

## Alternatives Considered

### Option 1: Keep chasing a clean floor (re-embed / reranker-shop / calibrate one cosine cutoff)
**Description:** Treat recall separation as a model/threshold problem: swap in a stronger embedder or
reranker, or calibrate a single `recall_similarity_floor` as a static production constant.
**Pros:**
- Conceptually simple — one number, calibrated once.
- Matches the ADR-0100 `recall_similarity_floor` framing that FRE-655 was originally scoped against.

**Cons:**
- **Falsified by measurement.** FRE-694/695 showed no embedder or reranker opens a clean gap;
  the clouds overlap at every configuration.
- The single-corpus, n≈54 conditions mean any "calibrated" constant is fitted to noise and rots as
  the graph grows.
- Wastes a one-way-door re-embed (full KG re-embedding, RAM-bound on the GPU-less VPS) for zero
  separation gain.

**Why Rejected:** The premise — that a clean floor exists to be found — is disproven. Chasing it is
motion, not progress.

### Option 2: Fold this into ADR-0100 as an amendment (no separate principle ADR)
**Description:** Append the FRE-694/695 conclusion to ADR-0100 rather than authoring a standalone
principle.
**Pros:**
- One fewer ADR; keeps the recall record in one place.

**Cons:**
- ADR-0100 is a **specific mechanism** (relevance-bounded candidate generation) that is Accepted and
  delivered; amending its record to carry a broad, cross-cutting *principle* muddies both.
- The principle must **govern** future work (ADR-0104, the FRE-655 re-scope, structure-wiring) and
  stand on its own as the thing they cite — an amendment buried in a mechanism ADR is easy to miss.
- The principle (settled) and the architecture (design-open) have different maturities and belong at
  different statuses; ADR-0100 is neither.

**Why Rejected:** A principle that constrains multiple downstream decisions deserves its own
decision record, not a footnote on a mechanism.

### Option 3: One combined ADR (principle + multi-path architecture together)
**Description:** Write a single ADR covering both the no-clean-floor posture and the multi-path
retrieval architecture.
**Pros:**
- One document tells the whole story end-to-end.

**Cons:**
- Forces **one status** onto two different maturities: the principle is settled and measured
  (Accepted); the architecture's design is explicitly still open (§7 of the research doc — arm set,
  RRF params, operating point — "where we meet"). A combined ADR is either prematurely-Accepted on
  the architecture or under-committed on the principle.
- The principle's job is to *outlive and govern* whichever architecture wins; coupling them makes the
  principle hostage to the architecture's revisions.

**Why Rejected:** The maturities differ; the split (this ADR Accepted, ADR-0104 Proposed) expresses
that honestly. This is the owner's decision (FRE-494).

### Option 4 (chosen): A standalone principle ADR, paired with a separate architecture ADR
**Description:** This ADR records the posture (Accepted); ADR-0104 records the multi-path
architecture (Proposed); the design spec + build tickets flesh out ADR-0104.
**Why Rejected:** Not rejected — chosen.

---

## Consequences

### Positive Consequences
- **Stops the floor-chase.** A future session that reaches for re-embedding or single-cutoff
  calibration now hits a decision record that says *measured, no.*
- **Orients recall toward the right axes** — structure-where-closed, semantic-where-open — which is
  exactly what ADR-0104 (multi-path) and the structure-wiring child build on.
- **Re-scopes FRE-655 cleanly** off "calibrate the FRE-489 hard floor" and onto choosing a soft
  operating point (or folding into multi-path).
- **Honest about its own limits** — the robust/fragile split means the posture won't be
  over-read into "nothing can improve recall."

### Negative Consequences
- **A principle without an architecture can read as abstract.** Mitigated by shipping it *paired*
  with ADR-0104 in the same PR, so the "so what do we build" answer is one document away.
- **Risk of over-correction** — reading "no clean floor" as "recall is unimprovable." It is the
  opposite: recall improves by *multiple paths* and *structure*, just not by a single threshold.
  The Decision §5–§7 wording guards against this explicitly.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Someone re-introduces a hard cosine/rerank cutoff as a gate | Medium | AC-1/AC-3 below are grep-/review-checkable guardrails; ADR-0104 inherits them |
| The negative result is over-read as "recall can't be improved" | Medium | Decision §5–§7 + pairing with ADR-0104; the multi-path play is the constructive answer |
| The n≈54 measurement is treated as a production constant | Medium | AC-2 (probe is instrument, not target); Decision §7 states it |
| The dual-domain / SOC idea leaks into this project as build work | Low | Explicit scope boundary in Decision; research doc §7 |

---

## Implementation Notes

**This is a principle ADR — it ships no code.** It governs, and is enforced at code-review plus the
checkable guardrails below. Its consumers:

- **ADR-0104** (multi-path retrieval architecture) — rests directly on §1, §4, §5, §6; inherits the
  no-hard-gate guardrails (AC-1 floor, AC-2 reranker) and the open-axis-predicate guardrail (AC-3).
- **FRE-706 — recall operating-point re-scope** (child #2) — the re-scope *is* the enactment of
  §5–§7: it supersedes FRE-655's hard-floor calibration and instead chooses a soft operating point,
  or folds into multi-path.
- **FRE-707 — structure-wiring** (child #3) — enacts §4: wire the closed-axis predicates
  (`type`, recency-as-predicate, relationship hops) into the recall query. Gated on FRE-637
  (ADR-0098 `type` enforcement), because §4's "structure-where-closed" only holds once `type` is
  closed by contract, not convention.

No migration, no schema change, no flag.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

A principle ADR's acceptance is expressed as **guardrail invariants** the recall code must not
violate. AC-1…AC-4 are **behavioral or structural** and fail against a real, plausible regression;
AC-5 is the single **enactment** criterion (the principle biting a live ticket) and is documentary
by nature — the behavioral weight sits on AC-1…AC-4.

- **AC-1 — The similarity floor is a noise guard, not a separating gate.** The ADR-0100
  `recall_similarity_floor` sits **below** the true-match score distribution — it drops pure
  no-record noise; it does **not** sit in the positive/near-miss overlap zone trying to separate
  true matches from distractors (which the measurement proved impossible). **Check:** on the FRE-489
  probe, the *lowest-scoring true positive* still clears the configured floor; only no-record
  negatives fall below it. *Fails if* any FRE-489 true positive is dropped by the floor — which is
  exactly what a floor tightened into a "clean separation" cutoff does, since the clouds overlap (a
  separating cutoff must sacrifice true positives). Operational teeth of §5/§6.

- **AC-2 — The reranker orders; it never filters to empty.** The reranker
  (`memory/service.py:1818`) re-scores for *ordering* only. **Check:** a recall that has N candidates
  before reranking returns the same N after (re-ordered), never fewer; no code path drops candidates
  on a reranker-score threshold. *Fails if* the reranker output is thresholded to exclude candidates
  — the §6 "never a hard cutoff" violation.

- **AC-3 — No recall path applies a hard predicate on the *open* axis.** Candidacy is never filtered
  by an open-vocabulary equality (`topic = $x`, or free-text entity-`name` equality); the open axis
  is served by vector + reranker only. Hard predicates are confined to closed axes (`type`,
  `recency`, `relationship`). **Check:** read the recall Cypher — no candidacy filter on a free-text
  `topic`/`name` equality. *Fails if* a recall query gates candidacy on an open-vocabulary match
  (the `topic = "vision"` silently-drops-`"perception"` failure). This is the operational form of
  *structure-where-closed, semantic-where-open* (§4).

- **AC-4 — The FRE-489/670 probe is physically an offline instrument, not wired to production.**
  **Check:** no module under `src/personal_agent/` imports from `scripts/eval/` or reads its output
  artifacts; the probe runs only under eval scripts / CI. *Fails if* a production config or recall
  path imports probe output — the §7 "instrument, not target" violation, caught **structurally**
  (an import edge) rather than by provenance intent.

- **AC-5 — The hard-floor framing is retired from the live recall backlog (enactment).** No open
  recall *build* ticket scopes "calibrate the FRE-489 hard floor cutoff"; the FRE-655 successor
  **FRE-706** (child #2) explicitly chooses a **soft operating point** instead. **Check:**
  read the re-scope child and any open recall build ticket; assert none scopes a hard separating
  floor and the successor's operating point is a soft/reranker signal. *Fails if* any live recall
  ticket still calibrates a hard cosine floor. Documentary by nature — the behavioral guardrails are
  AC-1…AC-4.

**Seam owner:** none — this is a standalone principle, not a decomposed build. Its behavioral
guardrails (AC-1, AC-2, AC-3, AC-4) are **inherited** by ADR-0104 and the recall children; its one
enactment (AC-5) is proven when the FRE-655 re-scope child is filed. It does not wait on an
assembled seam.

---

## References

- ADR-0100 — Memory Recall: Relevance-Bounded Candidate Generation (the mechanism this principle
  refines; the `recall_similarity_floor` framing this ADR re-reads as a noise guard, not a clean
  cutoff; PR #267).
- ADR-0104 — Multi-Path Retrieval with Rank Fusion (the architecture that follows from this
  principle; authored in the same PR; Proposed).
- ADR-0097 — Ingested-Knowledge Taxonomy (the World/Personal/System class + closed-axis structure).
- ADR-0098 — Memory Substrate & Lifecycle (Accepted 2026-06-27; the typed structure and `type`
  enforcement the closed-axis predicates depend on; build chain FRE-637; PR #263).
- ADR-0087 — Memory-Recall Quality: A Measurement-First Program (the measurement backing).
- ADR-0035 — Reranker integration (the soft-signal step, kept soft here).
- `docs/research/2026-06-29-fre-694-embedder-separation.md` — no embedder opens a clean floor.
- `docs/research/2026-06-30-fre-695-reranker-separation.md` — no reranker opens it either;
  separation is structural; topical density is the cause.
- `docs/research/2026-06-29-fre-670-semantic-probe.md` — the vocabulary-divergent probe
  (semantic-over-lexical; BM25's FRE-489 "win" collapses).
- `docs/research/2026-06-30-recall-as-retrieval-and-the-dual-domain.md` — the reflection this ADR
  formalizes (§§2–6 settled; §7 the open questions this ADR and ADR-0104 resolve).
- FRE-494 — the authoring ticket (two ADRs sequenced + three children).
- FRE-700 — the discussion that produced the research doc (Done; PR #287).
- FRE-705 — Multi-path retrieval design spec (child #1; ADR-0104's seam owner).
- FRE-706 — Recall operating-point re-scope (child #2; the AC-5 enactment — supersedes FRE-655's
  hard-floor calibration framing per Decision §5–§7).
- FRE-707 — Wire closed-axis predicates into recall (child #3; enacts §4; blocked on FRE-637).
- FRE-655 — the closed floor-calibration ticket whose hard-floor framing FRE-706 supersedes (AC-5).
- FRE-637 — ADR-0098 `type` extraction/emission contract (closes the `type` axis; gates the
  structural path).
- Code: `second_brain/entity_extraction.py:36` (the 7-value `type` prompt),
  `consolidator.py:605` (`.get("type","Unknown")`), `memory/service.py:601` (`.get("type","")`,
  keep-existing) — unenforced `type` storage; `memory/service.py:1818` (`rerank(...)`, the single
  soft-signal call site, vector path only — FRE-699).

---

## Status Updates

### 2026-07-01 - Proposed
**Changed By:** Architect (adr session)
**Reason:** Formalizes the FRE-694/695 measurement conclusion as a governing principle, per FRE-494
(ADR-A of two). Splits the settled posture from the design-open architecture (ADR-0104).

### 2026-07-01 - Accepted
**Changed By:** Owner
**Reason:** Principle is settled and measured; it unblocks the FRE-655 re-scope and orients
ADR-0104. Owner set the status at authoring (FRE-494 pre-merge decision: one PR, ADR-0103 Accepted ·
ADR-0104 Proposed). Master confirms the field at the integration gate.
