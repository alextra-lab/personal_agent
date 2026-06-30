# Recall is Retrieval: the Structure/Semantic Division, and the Dual-Domain Generalization

**Date:** 2026-06-30 · **Type:** Research / reflection (knowledge capture, not a decision record)
**Origin:** FRE-700 discussion (adr session). Continuation of the memory-recall stream
(FRE-435 → ADR-0100 → FRE-694/695).
**Status of the decisions herein:** *open.* This doc records what was learned and how it
generalizes. The implementation choices it surfaces — the recall ADR(s), the FRE-655 re-scope,
the multi-path retrieval design — are deliberately **left undecided** for a focused follow-on.

---

## Why this doc exists

Two days of measurement (FRE-694/695) closed a question that began as "make recall better" and
ended somewhere much larger: *what recall actually is, where its quality comes from, and why this
particular knowledge base is the hard case.* The conclusions are conceptually settled but the
implementation is not, and the gap between "clear on the concept" and "clear on the build" is
exactly where the work now sits. This captures the concept so the build session doesn't have to
re-derive it — and so a future reader understands why the obvious-looking fixes were rejected.

It also draws one **scope boundary** explicitly (see §7), because it is the kind of line that
erodes silently if left implicit.

---

## 1. The arc

The stream began as *"improve recall"* — the owner's standing symptom (FRE-435): the agent
answering *"no prior discussions on this topic"* when prior context demonstrably existed.

- **It wasn't tuning — it was broken.** ADR-0100 traced the symptom to the retrieval *query
  layer*: recall built its candidate set by **recency** (a 30/90-day hard cutoff plus a
  timestamp-ordered `LIMIT`), let the vector index only *re-score the survivors*, and then —
  the quiet third defect — discarded the relevance scores it computed and returned candidates in
  *timestamp* order anyway. Old-but-relevant turns were never candidates; the literal "no prior
  discussions" denial was a recency gate, not a retrieval-quality ceiling.
- **We fixed something that didn't work at all.** ADR-0100 (relevance-bounded candidate
  generation) replaced recency-keyed candidacy with vector-first candidacy + a similarity floor +
  recency demoted to a weight. Real value — but it set up the *next* question.
- **Then two days understanding the data we have.** FRE-694/695 asked whether a *better model*
  could give recall a clean separation — and in answering, taught us more about the shape of the
  corpus than about any model.

## 2. What the measurement actually proved

The robust, defensible result — the part that survives every caveat:

> **No single similarity score gives a clean separation floor on this data.** Across 3 embedder
> runtimes × 3 quant levels × 3 sizes + cloud Voyage (FRE-694), and 3 reranker runtimes + cloud
> (FRE-695), the positive (true-match) and negative (no-record) score clouds **overlap
> everywhere.** Best embedder Youden's J only **0.59–0.64** (local 4B ~0.59 at its best
> dimension; cloud Voyage up to 0.64 at 512 dims); best reranker 0.785.
> The hardest distractors always outscore the easiest true matches.

Two decisions fall straight out of that, and they are worth banking:

- **The re-embed / reranker-shopping question is closed: no.** No embedder — local or cloud SOTA —
  opens a clean floor, and recall already saturates (R@5 ≈ 0.98–1.00) at the production 0.6B
  embedder. A one-way-door re-embed is not justified *for separation.* The result is also
  runtime- and quant-robust (MLX ≡ llama.cpp to three decimals; 8-bit ≡ bf16) — the old FRE-656
  "embedder looks weak" confound was specifically **Q4**, not quantization in general.
- **The reranker is the strongest single lever** (+0.19 over the best embedder) but only as a
  **soft, probabilistic operating point** (~88% recall @ ~9% FP), *never* a hard cutoff.

## 3. Where separation actually comes from — and the vocabulary boundary

FRE-695's deeper finding: the overlap is **not a dirty-data artifact, it is structural.** What
kills the separating gap is **topical density** — near-neighbours on the *same topic* as the query
but not the actual answer (a "vision" query pulls mantis-shrimp eyes, X-ray vision, Rayleigh
scattering; all genuinely about vision, all score high). A bi-encoder cosine — or a cross-encoder
reranker — cannot tell *"on the topic"* from *"is the answer,"* because in meaning-space those
things *are* neighbours. Counter-intuitively, **disparate data is easier**; a dense,
topically-clustered corpus — which a rich personal memory is by design — is the worst case.

Clean separation comes from **structure**: taxonomy, entity types, relationships, recency windows
turn relevance into a **deterministic filter** (`type = Person AND after = Y`, plus `topic = X`
*only where that vocabulary is closed* — the topic caveat lands two paragraphs down)
instead of a fuzzy threshold. Structure replaces the floor with hard predicates; it does not make
the scores separate better.

But structure only delivers that on a **closed vocabulary** — and the code says ours is only
partly closed:

- **`type` is soft-closed.** The extractor prompt (`second_brain/entity_extraction.py:31`) asks
  for *exactly one* of seven values (Person, Organization, Location, Technology, Concept, Event,
  Topic) — but nothing **enforces** it. Storage is unenforced: `consolidator.py:604` writes
  `entity_data.get("type", "Unknown")`, while `service.py:601` uses `.get("type", "")` and its
  Cypher treats empty as "keep the existing node type." No whitelist, no `Literal` — closed by
  convention, not by contract.
  Closed by convention, not by contract.
- **`topic` is open.** There is no `topic` field — a topic is an *Entity of type `Topic`* whose
  `name` is **free text the model writes.** "vision", "perception", "eyesight" become three
  different nodes. The only guard is the extractor's soft "normalize to canonical form" rule —
  an admission that the space is open.

This is the crux, and it dissolves the apparent tension between "structure wins" and "but we built
semantic search for a reason":

> **A hard predicate separates cleanly only where the vocabulary is closed. On an open vocabulary
> it reintroduces the exact mismatch semantic search exists to solve** (`topic = "vision"` silently
> drops the note filed under `"perception"`).

So the architecture is not "predicates replace semantic search." It is a **division of labour by
axis**: closed axes (`type`, `recency`, `relationship`) → hard predicates, clean. Open axis
(`topic`, meaning, content) → semantic similarity + reranker, irreplaceable. Structure narrows by
what is closed; the embedder + reranker rank by meaning *within* that narrowed set, where no
predicate can help. The reranker stays exactly where FRE-695 put it: a soft signal, on the one
axis that is genuinely fuzzy.

*(Aside: BM25 is **not** the structured path. It is another **scorer** — same category as the
embedder, just lexical. Its FRE-489 "win" over the vector embedder was an artifact of that probe's
query/document vocabulary overlap; FRE-670's vocabulary-divergent probe collapsed it. An exact
match on a rare token is the closest a scorer gets to acting like a predicate, but it is brittle —
no synonyms, no "as of last March" — not a real filter.)*

## 4. The humility — why a clean floor is the wrong *goal* here

The measurement is sound, but it must be read against the corpus it was taken on, and that corpus
is **limited**: a single user, a young graph, the first months polluted by infra health-checks,
and — by design — *deliberately diverse and constantly growing* as the owner learns, reads, and
studies more.

The limited-data fact does not weaken the FRE-694/695 conclusion; it **redirects** it. Split the
finding in two:

- The **robust half** — "no single score is a clean cutoff" — gets *more* true as the corpus
  grows and diversifies (more near-neighbours, more topical density). The hard case gets harder.
- The **fragile half** would be reading "J = 0.785" as *the* ceiling for personal memory, or
  "structure gives a clean floor" as a solved destination. Both are overreach on n = 54 probe
  cases and a young, non-stationary graph. (FRE-694/695's own limitations sections say as much:
  offline geometry, n = 54, probe-specific, extrema outlier-sensitive.)

So the honest meta-conclusion is not *"we found the floor"* — it is *"stop chasing any single
clean-separation mechanism, because this class of corpus is unlikely to yield one — and certainly
has not at the small, single-corpus, n=54 scale measured here."* The probe
(FRE-489/670) is an **instrument, not a target**: use it to catch regressions, not to set
production constants.

One nuance, so we don't overcorrect: the operating point does **not** vanish. At the surface there
is still a binary act — return something, or say "no prior discussions." What we drop is the
pretense that the threshold is a *clean, static constant calibrated once*. It becomes a **soft,
multi-signal, adaptive** decision — a dial several signals vote on, not a line in the sand.

## 5. The reframe — everything is recall

The most useful shift in the whole stream: **this stopped being about "recall" as a narrow
subsystem.** Retrieval *is* the product surface of the knowledge graph. Every path — dense vector,
structural predicate, graph traversal, the proactive path, the topic path — is one route to the
same buried knowledge. So the architectural question is not "tune the recall floor"; it is "how do
we reliably reach the truth/insight/knowledge in the KG, by **multiple paths**."

The shape that follows is **a design inference from the empirical work, not itself measured**
(not yet decided — see §7):

- **Multi-strategy retrieval** — dense vector / lexical (full-text) / structural predicate / graph
  traversal / multi-query (paraphrase) expansion. Each arm has different failure modes; the union
  covers more. Multi-query expansion is the direct mitigation for the *open-vocabulary* miss —
  ask in several vocabularies and you bridge "vision"/"perception" without canonicalization.
- **Fusion by rank, not score** — Reciprocal Rank Fusion. FRE-695 stressed that embedder and
  reranker score scales are arbitrary and **not comparable across arms**; RRF fuses on rank
  position and sidesteps calibrating incompatible scales. It also rewards **agreement** — an item
  surfaced by several independent paths is a stronger (soft) relevance signal than any lone cosine.
- **Then rerank the fused set and hand it to the main model** — which is the final arbiter of
  *truth*; retrieval delivers *material*, judgement happens one layer up.

Honest framing: multi-path is a **recall** play (raise the odds we retrieved it at all — directly
attacking the "no prior discussions" false-negative), **not** a floor fix; the final sieve is the
same soft reranker. And because probe recall already saturates, a *metric* won't validate it up
front — its payoff is in the **lived tail** (out-of-vocabulary, multi-hop queries a single path
whiffs on) and in **antifragility**: when you cannot trust any single signal — small,
non-stationary, unlabeled data — ensembling several is the correct response. The limited-data
argument is the strongest case *for* multi-path, not against the stream.

## 6. The dual-domain generalization

*Caveat up front: this section is **analogy and hypothesis, not a finding.** FRE-694/695 measured
one personal corpus; none of the SOC claims below are measured. They are domain reasoning about
how the same architecture would behave on a structured corpus — plausible, and worth recording as
a direction, but not established by the probe.*

The sharpest test of the whole thesis: contrast this corpus with a **bounded** one — a SOC
(Security Operations Center) collaborative partner, scoped to "all things SOC." Recall *presents
differently through the telemetry* there, and working out *why* validates the architecture rather
than complicating it.

A SOC universe is finite **by construction**, and its vocabulary is **already closed and
standardized**: MITRE ATT&CK techniques, CVE IDs, kill-chain phases, OCSF/STIX/ECS schemas, hosts,
IPs, alert types, severities. Those are precisely the **closed-axis predicates** that give clean
separation. And the corpus is **born structured** — a SIEM alert / EDR event / netflow record is
typed telemetry, not free-text conversation to extract from. The entire write-side problem
(open topic vocabulary, soft-closed types, `"Unknown"` drift, LLM extraction) **largely
evaporates** — the structure arrives pre-labelled and industry-standardized.

So the two domains are the **same engine with the ratio turned the other way**:

| | Personal agent (the hard case) | SOC partner (the structured case) |
|---|---|---|
| Vocabulary | open, self-generated | closed, standardized (MITRE / CVE / OCSF) |
| Corpus | extracted from conversation | born as typed telemetry |
| Recall mix | semantic-dominant, structure thin | predicate + graph-traversal dominant, semantic minority |
| Ground truth | none (N=1, non-stationary) | **partly real** — indicator/hash matches are clean; investigation relevance + analyst recall are not automatically so |

And the asymmetry that is genuinely good news: **the SOC space gives you the stationary, labelled
data that personal memory denies you.** Everything in §4 — "you cannot optimize a threshold against
this data; build for robustness, not tuning" — was true *because personal memory has no ground
truth.* In a SOC, a detection fired or it did not; the **metric-driven, calibrated-floor approach
rejected for personal memory becomes valid.** Different spaces, genuinely different recall
character — and the *same* underlying law: structure-where-closed, semantic-where-open, multi-path,
adaptive operating point.

The deeper point: **the personal agent is the stress test that keeps the architecture honest.** A
SOC-first build would have let clean predicates carry it so far it might never have discovered the
structure/semantic division — and would quietly *assume* structure, then fall over the moment the
vocabulary opened. Building for the hardest corpus (open, fuzzy, non-stationary, unlabeled) yields
an architecture that **generalizes *down*** to the measurable one. The two days on FRE-694/695 were
not a detour from a personal-memory problem; they were tracing what looks like a *general pattern
of KG retrieval* — a hypothesis worth carrying, not a proven law from one n=54 corpus — on the case
most likely to expose it.

If a domain-specialized partner is ever built, the clean factoring is **one platform, two
instantiations** — never one deployment serving both:

- **Domain-agnostic core** (ports unchanged): the KG substrate, typed entities + bitemporal
  Claims, gateway → orchestrator → tools, multi-path retrieval + fusion, the reranker as a soft
  signal. The retrieval *arms* are identical; only their weighting differs.
- **Domain pack** (specializes): the ontology/taxonomy (P/W/S + 7 generic types ↔ MITRE/STIX/OCSF),
  the ingestion adapters (LLM extraction from chat ↔ SIEM/EDR connectors with schema
  normalization), the recall-mix tuning, and the eval/ground-truth set.

Two honest cautions, so this is not cheerleading: a SOC **flips the scaling challenge** (personal
= small-data / high-diversity; SOC = big-data / low-schema-diversity telemetry firehose — the
retrieval *logic* ports, the *infrastructure* does not), and it **raises the governance bar hard**
(multi-user, audit, access control, SLAs, chain-of-custody) in ways a single-user exploratory
agent does not prepare for. The domain pack is real, additive work — but additive to a core that
exists, *provided* the seam stays clean.

## 7. The boundary, and the open implementation questions

**Scope boundary (explicit, so it does not erode).** SOC is **not** built into this project. This
is the owner's personal research project — the pedagogical north star, "Forever the Student" — and
it stays that. A SOC partner, if it ever exists, is a **fork** that reuses the domain-agnostic
core, not a feature grafted on here. Any future session feeling the pull to "just add a SOC
connector / ontology / dataset" should stop at this line. The dual-domain insight is documented
precisely so it can be *carried* without being *built here*.

**What is settled (this doc):** the concept — §§2–6. The structure/semantic division, the
no-clean-floor reality, recall-as-multi-path-retrieval, the dual-domain generalization, and this
boundary.

**What is deliberately left open (a focused follow-on, where concept meets implementation):**

- **The recall ADR(s).** Whether to record this as one ADR (recall = multi-path retrieval over a
  living typed KG; no clean floor; structure + reranker as soft signals) or two (the
  negative/posture *principle* separate from the multi-path *architecture*). Decision statements
  and acceptance criteria not yet written.
- **FRE-655 re-scope.** It was framed to "calibrate the FRE-489 floor cutoff." Under this thesis it
  is **not** calibrating a hard cosine floor — it is choosing a soft operating point (likely on the
  reranker), or being folded into the multi-path design. Needs explicit re-scoping before build.
- **The multi-path retrieval design.** The arm set, RRF parameters, the adaptive operating point,
  dedup across paths, and where it plugs into the gateway / context-assembly seam (Stage 6). A real
  spec effort — and the place the owner named as "where we meet": clear on concept, building the
  implementation together.
- **Wiring structure into recall at all.** Today recall is fuzzy-cosine-first; the closed-axis
  predicates (`type`, recency-as-predicate, relationship hops) exist in the substrate
  (ADR-0097/0098) but are **not** in the recall query. Closing that is its own piece of work.

---

## References

- ADR-0100 — Memory Recall: Relevance-Bounded Candidate Generation (the Phase-2 fix this stream
  built on; the similarity-floor framing this reflection refines).
- ADR-0097 / ADR-0098 — Knowledge taxonomy + memory substrate & lifecycle (the structure that
  closed-axis predicates would filter on).
- ADR-0087 — Memory-Recall Quality: a measurement-first program (the measurement backing).
- ADR-0035 — Reranker integration (the soft-signal step).
- `docs/research/2026-06-29-fre-694-embedder-separation.md` — no embedder opens a clean floor.
- `docs/research/2026-06-30-fre-695-reranker-separation.md` — no reranker opens it either;
  separation is structural; topical density is the cause.
- `docs/research/2026-06-29-fre-670-semantic-probe.md` — the vocabulary-divergent probe
  (semantic-over-lexical; BM25 collapses).
- FRE-655 — floor calibration (to be re-scoped per §7).
- FRE-699 — recall-path frequency / rerank-load (the reranker fires only on the vector
  `search_memory` path today; paths are siloed, not fused).
- Code: `src/personal_agent/second_brain/entity_extraction.py:31` (the 7-value `type` prompt),
  `consolidator.py:604` (`"Unknown"` fallback) / `memory/service.py:601` (`""` fallback, keep-existing) — unenforced `type` storage,
  `memory/service.py` (recall candidate Cypher — vector-first + similarity floor on cosine).
