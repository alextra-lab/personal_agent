# FRE-1479 — Calibrating the broad-recall relevance bound on the serving reranker

**Date:** 2026-09-10 · **Ticket:** FRE-1479 ("Memory Recall Quality") · **Backing:** ADR-0148 D4.
**Component measured:** Voyage `rerank-2.5` (`config/models.yaml:541`) — the arm the `reranker` role
actually resolves to in production.
**Substrate:** the FRE-375 test stack (Neo4j :7688). No production row was read or written.
**Artifact:** `config/calibration/broad_recall_relevance_bound.json`.

## The question, and why it falls to this path

FRE-1477 measured the serving **embedder** and found no bound satisfying ADR-0148 D4's two
constraints. D4 names the response and it is not a lower bar:

> the response is a reranker-side bound or a different arm, not a quietly chosen number.

This is that reranker-side bound. The question is the same one, asked of a different component:

> **On Voyage `rerank-2.5`, is there a bound that rejects the median top-ranked non-match while
> still admitting at least 90% of the labelled positives?**

The prior is not encouraging. FRE-695 measured this arm at Youden J 0.73 and judged even the best
reranker it tested (J 0.785) at roughly *"88% recall @ ~9% FP"* — under D4's 90%.

## Answer

**Yes — and it is the first bound this ADR chain has been able to set.**

| Quantity | Value |
|---|---|
| Labelled positives | **119** (entity documents 57, turn documents 62) |
| Negatives (per query, strongest non-match) | 54 |
| Positive median | 0.5742 |
| Negative median — the value the bound must reject | **0.3379** |
| **Configured bound** | **0.3389** |
| Share of the whole positive set it admits | **93.3%** (required: 90%) |
| Entity-document positives admitted | 93.0% |
| Turn-document positives admitted | 93.5% |
| Negatives rejected | **50.0%** |

Both constraints hold, so `broad_recall_relevance_bound` is configured at the measured value
and the gate is live. The 57 entity positives are the same 57 FRE-1477 counted on this probe
set, which is a useful cross-check that the two harnesses label the same corpus.

**Neither document shape drags the other.** 93.0% against 93.5% is the reason both populations
had to be measured rather than assumed alike: had the turn half come in at, say, 70%, one bound
over the union would have been suppressing the lexical arm's whole contribution behind an
aggregate that still read above 90%.

### Read the second number too: this is a soft operating point, not a clean floor

**The bound rejects 50.0% of the negatives.** That is by construction — the lowest bound strictly
above the negative median rejects exactly half of them — and it is reported rather than left to be
inferred, which is why `BoundProposal` carries the field at all.

So the honest statement of what ships is: *the gate removes half of the top-ranked non-matches
while keeping 93.3% of genuine matches.* That is a material improvement over admitting every one
of them, and it is nothing like a clean separation. FRE-695 reached the same conclusion about
every reranker it tested and said so directly — the reranker is *"the strongest lever"*, to be used
*"as a soft, probabilistic operating point … never a hard cutoff"*, and clean separation *"has to
come from structure, not a score"*.

D4 asks for a bound that rejects the median non-match without suppressing genuine relevance. This
one does. It does not make absence reliably reachable on its own, and this document does not claim
that it does.

### What this settles about FRE-1477's handback

FRE-1477 measured the serving embedder, found no bound satisfying both constraints, and handed the
owner three courses. The first was *"a reranker-side bound … already the mechanism FRE-1479 must
build for broad recall."*

**That course works, on this path.** The embedder could not clear 90% at any bound; the reranker
clears it at 93.3%. FRE-695 predicted the direction — best embedder J 0.59 against best reranker
J 0.785 — and the measurement bears it out on the arms actually serving.

It does **not** discharge FRE-1477. The proactive path scores on an embedding term, not a reranker
score, and FRE-695's own finding is that the two scales are not comparable. The proactive bound
remains unset and the decision remains the owner's.

## Method

### What is measured: the production pair, not a model of it

`MemoryService._resolve_item_texts` builds the documents and `memory.reranker.rerank` scores them.
Neither is re-implemented. That matters more here than it did for FRE-1477, because **two document
shapes reach the admission gate and they are not alike**:

| Shape | Built as | Reaches the boundary as |
|---|---|---|
| entity | `name + ' ' + description` (`service.py:5255`) | itself |
| turn | `coalesce(summary, user_message, '')` (`service.py:5266`) | the entities it discusses (`service.py:5417`) |

A turn surfaced by the lexical arm expands into the entities it discusses, and each inherits the
*turn's* score. A bound measured only on entity-shaped documents would then gate turn-derived
entities with a number that never described them. Both populations are measured, one bound governs
their union — because one bound governs the path — and the artifact records the two halves
separately so a reader can see whether one shape drags the other.

The metric is FRE-694's and FRE-695's: per expected entity a positive, and per query the strongest
non-match as the negative. That negative statistic is the right one rather than a convenient one —
on a query whose answer is not in the corpus, the top-ranked candidate *is* the strongest non-match,
so it describes the item admission is actually decided on.

### The bound rule is FRE-1477's, imported rather than copied

`choose_bound` is used unchanged from `scripts/eval/fre1477_relevance_calibration/analysis.py`.
ADR-0148 D4 states one rule for every path; a second copy would become a second rule the moment
either changed. What differs between the paths is the score space, and a score space is data.
`test_fre1479_calibration_analysis.py` asserts the function identity directly, so a later copy fails
a test rather than drifting quietly.

## Instrument validation — and two runs that failed it

FRE-694's discipline is to validate the instrument against the production path before reporting any
number from it. That discipline earned its keep twice here, and both failures are recorded because
in each case the *verdict* would have looked plausible without them.

### P1 — cross-implementation agreement

`separation_benchmark.py`'s `voyage-rerank-2.5` arm is an independently authored pipeline: its own
corpus build (`"{name}: {description}"`), its own HTTP client, its own retry policy, its own response
parser. Its aggregates are compared against the production path's at FRE-694's own 0.02 tolerance.

**First failure — the two sides were not measuring the same population.** The run compared 93
production candidates (49 entity documents plus 44 turn documents) against the independent harness's
49 notes, and called the difference a parity delta: `neg_max` 0.0664, `pos_median` 0.0469. The
independent harness reads entity rows from the probe YAML and builds no turn documents at all, so
there was nothing on its side to compare the turn half against. P1 is now restricted to the
entity-document population both sides actually build. The turn population rides the same production
code path P1 validates, and P2R and P3 cover it.

**Second failure — a median tolerance applied to a maximum.** With populations aligned, `pos_median`
and `neg_median` agreed to **0.0039** and **0.0078**. `neg_max` still differed by **0.0859**.

That was investigated rather than tuned away, and the investigation is the reason the gate changed:

- Both implementations selected the **same entity** as the top-ranked non-match in every divergent
  case — `ctrl4-king-spain` chose "undisclosed cancer" on both sides, `mus3-intervals-plain` chose
  "parallel fifths" on both. The corpora agree on content and on ranking, so it is not the
  corpus-construction error P1 exists to catch.
- The residual is the score of an *identical* (query, document) pair, and it is **two-sided** —
  production scored higher on five of the twelve largest divergences and lower on the rest. A
  construction error biases one way. FRE-1477's own first run failed exactly that way, all three
  deltas leaning together, and the cause was a 4096-against-1024 dimension mismatch. This does not
  have that shape.
- Its source is the deliberate text difference between the two implementations, `name + ' ' +
  description` against `"{name}: {description}"`. An independent implementation is *supposed* to
  differ there; that is what makes it independent.

So a 0.02 tolerance built for medians was being applied to the maximum of 54 noisy values, where the
single largest draw governs. FRE-695 recorded the same sensitivity for this harness at this sample
size — *"n = 54 … extrema are outlier-sensitive, hence robust p5/p95 alongside J"* — and used robust
percentiles for it. P1 now gates on `pos_median`, `neg_median` and `neg_p95`. **`neg_max` is still
measured and still committed; it is reported, not gated.**

**The re-run confirmed the diagnosis independently.** `neg_p95` came in at a delta of **0.0021** —
tighter than either median, and the tightest of the four statistics:

| Statistic | Production | Independent | Δ | |
|---|---|---|---|---|
| `pos_median` | 0.5742 | 0.5781 | 0.0039 | gated |
| `neg_median` | 0.3213 | 0.3291 | 0.0078 | gated |
| `neg_p95` | 0.5123 | 0.5145 | **0.0021** | gated |
| `neg_max` | 0.5586 | 0.6445 | 0.0859 | reported |

The two distributions agree everywhere, **including the upper tail one percentile below the
maximum**. Only the single largest draw diverges. That is what a lone outlier looks like, and it is
not what a systematic construction error looks like.

**One claim this does not support.** The two sides disagree on whether the negative maximum exceeds
the positive median — production says no (0.5586 against 0.5742), the independent harness says yes
(0.6445 against 0.5781). ADR-0148's reachability argument is stated in those terms, so the
disagreement matters. Since the gap is per-pair scoring noise on a single extremum, the honest
reading is that **the separation ceiling is too close to call on this reranker at n=54** — not that
Voyage clears it. The more flattering production number is not claimed here.

**A gate was relaxed after it failed, and that deserves the plain statement.** What justifies it is
the evidence above rather than the inconvenience: the check that P1 exists to perform — do two
independently built corpora agree — passes, and the statistic that failed is one FRE-695 had already
documented as unsuitable for a fixed tolerance at n=54. The bound itself is chosen from the negative
*median* and the positive distribution, the two statistics that agreed most tightly, so the verdict
below does not rest on the contested one.

### P2R — instrument sanity, replacing a check that has no analogue

FRE-1477's P2 compared the Neo4j vector index's score against a client-side cosine over the vectors
the index holds. **A reranker has no index.** The model reads the query and the document and emits a
number; nothing sits between them to be unfaithful. Simulating a second check there would report a
validated instrument on no evidence, so it is recorded as absent and FRE-695's own gate takes its
place: a trivially relevant document must outrank a trivially irrelevant one. That catches what P1
cannot — a failure both implementations share, such as a wrong model id or an endpoint answering
with a constant.

### P3 — listwise sensitivity, a declared limitation measured rather than asserted

This harness scores the whole corpus per query. Production reranks a fused set capped at
`reranker_input_cap` (25). Reproducing that cap would mean running the retrieval arms, which drags an
embedder and a fusion step into a reranker calibration and makes the number describe three
components instead of one.

A rerank request is listwise, so the wider candidate set *could* move a pair's score. P3 measures how
far it actually does: the same query scored over the full corpus and over the capped set, largest
per-document difference across the shared documents.

**Measured: 0.000000.** The maximum per-document score movement between the full 93-document
corpus and the 25-document production cap was exactly zero. Voyage's relevance score for a
(query, document) pair does not depend on which other candidates accompany it in the request, so
scoring the whole corpus costs nothing in fidelity and the declared limitation turns out to be
no limitation at all. Recorded as a number rather than dropped, so a future arm that *does* show
listwise coupling fails the check instead of inheriting this one's conclusion.

## What ships

**The bound.** `broad_recall_relevance_bound = 0.338891`, committed as the setting's default and
bound to `config/calibration/broad_recall_relevance_bound.json` by
`config_guard.check_broad_recall_bound_calibration`. A stale, missing, mismatched or malformed
artifact raises a finding and leaves the previous bound in force.

**The plumbing D4 named as its own work.** The reranker score now survives `_rerank_fused_items`
to `_format_broad_recall_context`. It did not before: the scores went into a local map, were used
to sort, and were discarded, so the admission boundary had nothing to gate on and admitted on
fused rank order.

**Provenance on the score, which the ADR did not anticipate needing.** ADR-0148 D4 says
`_rerank_fused_items` "returns the items unchanged" when the reranker is disabled, raises, or
returns nothing. Only the first is true. `rerank()` never raises and never returns empty — it
catches every failure itself and returns a passthrough whose scores are `1 / (i + 1)`, rank order
wearing the score field. And a primary outage falls back to a *different model* whose real scores
sit on a scale this bound does not describe.

So a score now carries the id of the model that produced it, and the gate checks that id before
comparing. Without it, a Voyage outage would have silently gated Qwen scores with a Voyage bound,
and a total outage would have gated rank position — the exact defect the ticket exists to remove,
reintroduced one layer down. This is FRE-1170's disease, and it is the same fold-in FRE-1477 made
one component over when it found `vector_score = recall_similarity_floor`.

**A rejection that says which kind it is.** `DropReason.RECALL_RELEVANCE_UNAVAILABLE` is distinct
from `RECALL_RELEVANCE_BOUND`. The bound reason says "measured, and below the bar"; the new one
says "never measured, or measured by something the bound does not describe". Merging them would
make a silently degraded reranker read as a corpus holding nothing relevant.

### What does not ship, and is explicitly left to FRE-1476

ADR-0148 D4 requires that a path which can supply no relevance value report `UNAVAILABLE` rather
than claim absence. **That vocabulary does not exist yet** — `MemoryContextStatus` and its four
states are FRE-1476's, unstarted at the time of writing.

An earlier revision of this work planned to express it by returning `None` from the broad path, on
the reasoning that `pipeline.py:204-209` already flags `memory_unavailable` for a `None`
`memory_context` on a `MEMORY_RECALL` turn. **That does not work, and ADR-0148 itself says why.**
`assemble_context` passes every result through `_inject_behavioural_stances`, which runs when the
context is `None` and builds a populated list from the curated standing set — D2's own "Reason one:
a standing layer populates the context on most turns". The `None` is gone before the pipeline
checks it. Returning `None` would also have discarded `recent_sessions`, which the same formatter
appends to the same list.

The signal therefore rides the **discard report**, a recall-layer channel the stance layer does not
touch and the turn-evidence record already reads. The two rejection reasons are distinguishable
there today, and FRE-1476 composes them into `NOTHING_RELEVANT` and `UNAVAILABLE` when it lands.

## References

- ADR-0148 D4 — `docs/architecture_decisions/ADR-0148-absence-must-be-reachable-and-sayable.md`
- FRE-1477 — `docs/research/2026-09-10-fre-1477-proactive-relevance-calibration.md` (the embedder
  incompatibility that sends the question here)
- FRE-695 — `docs/research/2026-06-30-fre-695-reranker-separation.md` (reranker scales are arbitrary
  and not comparable across arms; extrema at n=54)
- FRE-694 — `docs/research/2026-06-29-fre-694-embedder-separation.md` (the parity discipline and its
  tolerance)
- FRE-1170 — the reranker degrades to passthrough silently
- Harness — `scripts/eval/fre1479_reranker_calibration/`
