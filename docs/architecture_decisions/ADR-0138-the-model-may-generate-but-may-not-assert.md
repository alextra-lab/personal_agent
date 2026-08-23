# ADR-0138: The Model May Generate, But It May Not Assert — Verified Citations as Seshat's Grounding Contract, Tier-Invariant and Enforced by Measured Compliance

**Status:** Proposed
**Date:** 2026-08-23
**Deciders:** Project owner (design), `adr` session (drafting)
**Tags:** grounding, hallucination, citations, retrieval, model-routing, memory, prompts

---

## Context

**What is the issue we're addressing?**

Seshat has no way to say *"I don't know."* Three independent mechanisms remove the admission,
and each is sufficient on its own to produce a confident answer assembled from nothing.

### The three mechanisms, verified in source 2026-08-23

**The memory layer is instructed not to say it.** `orchestrator/executor.py:2536` appends to the
Known-Entities section, verbatim: `"Do NOT say you have no memory."` A model that correctly
infers the retrieved set is irrelevant has been told not to mention it.

**The tool layer has no trigger for it.** `orchestrator/prompts.py:56` keys the search policy
entirely on recency — "current events, recent news, CVEs, product versions, or anything
requiring live web data." A question the model simply *does not know*, with no recency
dimension at all, matches nothing. FRE-1278 measured this: asked which tinned tuna to buy in
France, the agent invented brands, shops and databases with full confidence, then invented a
second set while apologising for the first.

**Irrelevance is admitted by construction, and the score never reaches the model.** The proactive
scorer (`src/personal_agent/memory/proactive.py`) combines four subscores at weights
0.45 / 0.25 / 0.20 / 0.10 (embedding / entity-overlap / recency / topic) against
`proactive_memory_min_score = 0.3` (`src/personal_agent/config/settings.py`). Three of the four
have non-zero floors:

- **Embedding** — Neo4j's vector index normalizes a cosine score to `(1 + cos) / 2` before
  returning it, so an *orthogonal* candidate scores 0.5, not 0. `MemoryService` passes that value
  through unchanged (clamped to `[0,1]` by `_normalize_vector_score`), so the floor is Neo4j's
  rather than ours. Real embedders place unrelated text well above orthogonal, making the
  practical floor higher still.
- **Recency** — `_recency_subscore` returns 0.5 on a missing or unparseable timestamp.
- **Topic** — `_topic_subscore` returns 0.3 on zero keyword hits.

The ≈0.355 floor reconstructs exactly as `0.45×0.5 + 0.25×0 + 0.20×0.5 + 0.10×0.3`.

The worst case is worse than that figure suggests. Recency decays continuously as
`exp(-ln2 × age_days / 30)`, so a memory created moments ago with zero entity overlap and zero
topic hits scores `0.225 + 0 + 0.200 + 0.03 = 0.455`; one from earlier the same day scores
`≈0.454`, the within-day decay being negligible against a 30-day half-life. Either way it sits
more than 50% above the admission bar. **Recency alone buys admission.** Anything discussed
recently is injected into subsequent turns regardless of topic, and whatever relevance was
computed is then discarded before the model sees the list.

### Why the composition is the finding

Similarity search always returns its nearest neighbours; that is not a defect on its own. The
defect is that nothing downstream distinguishes *"these are the nearest"* from *"these are
relevant"*, the one actor capable of noticing has been instructed not to mention it, and the
tool layer offers no route to go and find out. The result is a confident answer built from
whatever was nearest, in the same register as a correct one.

### Why this is disqualifying rather than annoying

The owner's framing, 2026-08-23: **"Seshat is not my creative writing tool, it is my
collaborator and teacher."**

Confabulation is a nuisance in a writing tool. In a teacher it is disqualifying, because the
learner cannot calibrate: the invented correction reads exactly like the real one, so catching
it requires already knowing the answer. A teacher that cannot say "I don't know, let me find
out" is not teaching — and "let me find out" is the half the tool layer is also missing.

### The question this ADR had to answer first

The obvious framing — *give the system a confidence signal so it knows when it doesn't know* —
does not survive contact with the three layers, because they fail for structurally different
reasons. The memory layer **has** a signal and discards it. The tool layer has none and cannot
compute one: "the model does not know this" is a property of parametric weights, unmeasurable
from outside. Any design resting on the model assessing its own knowledge depends on
**calibration**, which is the capability that scales worst and latest across model sizes and is
unreliable even at the frontier.

The owner's resolution inverts the question. *"Does the model know this?"* is unmeasurable.
***"Can the model produce a source for this?"*** is checkable. Not-knowing stops being a state
the model must introspect and becomes a **derived** fact: the absence of a citable source.

The owner's second constraint fixes the boundary that would otherwise leak:

> "I count on the model for its ability to follow instructions, code, reason — and retrieve
> accurate information. Outside of coding, I don't expect the model to be a knowledge source."

This draws the line around **role**, not around claim specificity. There is no
"well-known-enough" threshold to negotiate, and therefore no semantic boundary to leak — which
matters, because the FRE-1278 failure sat exactly there. The model did not believe it was
speculating; it believed which tinned tuna to buy was common knowledge. A carve-out phrased as
"claims the model considers well-known" would reintroduce the calibration dependency one level
down.

---

## Decision

**Seshat treats the model as a generator and a reasoner, never as a source of world facts.**

### D1 — The model may generate; it may not assert

The unit of classification is the **span**, not the sentence and not the turn. A span is an
**assertion** if it matches the enumerated assertion class below; everything else is
**generation**.

| Class | Examples | Citation required |
|---|---|---|
| **Assertion span** | named entities, brands, organisations, people, product names, package/library names, API symbols and signatures, version numbers, URLs, statistics and figures, dates, quotations | **Yes — verified** |
| **Generation** | code bodies, reasoning, the connective and evaluative text of a synthesis, restating the user, arithmetic over cited figures, instruction-following | No |

**Precedence is explicit and one-directional: if a span matches the assertion class, it requires
a citation regardless of the sentence it sits in.** Synthesis does not launder its constituents.
In *"Ortiz and Nardin are both well regarded"*, the spans `Ortiz` and `Nardin` are assertions and
each needs a source; *"are both well regarded"* is the model's judgement over cited material and
needs none. There is no sentence-level classification and therefore no judgement call about
whether a sentence "is synthesis".

The boundary is on the **kind of span**, not the **topic of the turn**. A coding turn is not
exempt: a function body is generation, but `httpx.AsyncClient` and the claim that it accepts
`timeout=` are assertion spans and require a documentation source. This is deliberate —
hallucinated package names are among the most exploited coding failure modes, and a topic-scoped
exemption would license exactly that.

The contract is enforced on **spans present in the output**, never on the model's declared mode.
There is no clean separation inside a model between recalling and reasoning, so a turn that
labels itself "reasoning" earns no exemption.

### D2 — Parametric knowledge is never a source

The admissible source set is:

1. the **memory graph** (entities, episodes, claims);
2. **tool and web results** retrieved during this turn;
3. **documentation** (context7 and equivalents) retrieved during this turn;
4. **the user's own words in this conversation** — which satisfy D3(a) by being present in the
   turn's assembled context, and are exempt from D3(b) reachability, having no external referent.

The model's weights are not on the list.

### D3 — Three deterministic checks inline; entailment sampled offline

Every assertion span must carry a citation that passes **all three** of:

- **(a) Resolution** — the cited identifier resolves to a source present in **this turn's**
  retrieved source set.
- **(b) Reachability** — the source is real and retrievable. A URL returning non-2xx after
  redirects, a soft-404, or an auth wall counts as **unreachable**. Memory citations resolve
  against the node id.
- **(c) Containment** — the asserted token appears in the cited source's **retrieved content**.

**(c) is not optional and is not entailment.** It is substring/normalized-token presence, costs
nothing, and closes the largest hole in a citation regime: a real, reachable, entirely unrelated
source attached to an invented claim. Without (c), reachability alone is nearly worthless against
citation theatre — a valid URL proves a page exists, not that it mentions the thing asserted.

All three run **inline and blocking, on every turn, at every enforcement level** (see D5).

**(d) Entailment** — whether the source actually *supports* the claim, as opposed to merely
containing the token — is the only check that catches a correctly-cited token embedded in a claim
the source contradicts. It requires a second model pass, so it runs **sampled and offline** at a
configured rate, owned by the eval program (ADR-0087), feeding the eval set rather than the turn.
Its measured miss rate is the evidence for any future decision to promote it inline.

### D4 — On failure: block, retry with forced retrieval, then say so

An assertion span failing (a), (b) or (c) blocks the turn and triggers a retry with retrieval
forced, bounded by a configured maximum attempt count.

On exhausting the bound, the terminal state is an **explicit statement that no source was found,
naming what was searched**. This state always exists, so the loop always terminates.

It is **never** a hedged guess. A guess with a disclaimer is parametric knowledge wearing a
disclaimer, and under D2 it is not admissible. Stripping the claim silently is equally rejected:
silence is the disease being treated.

### D5 — The contract is tier-invariant; *pre-generation forcing* is what varies

**The contract does not vary by model.** No uncited world-fact assertion, identical at 27B and at
the frontier. It is a property of Seshat, not of the model that happens to serve the turn.
Tiering the contract would make correctness a function of routing, and would reintroduce
precisely the dependency the contract was designed to remove: the design's whole virtue is that
it routes around calibration, the capability that varies most across tiers.

**Verification does not vary either.** D3(a)(b)(c) run inline and blocking on every turn
regardless of the model. Nothing is sampled inline; sampling applies only to the offline
entailment check D3(d).

**What varies with measured compliance is whether retrieval is forced *before* generation:**

| Enforcement | Condition | Behaviour |
|---|---|---|
| **Light** | measured compliance ≥ threshold | The model generates first and cites as it goes. D3(a)(b)(c) verify the output; failures take the D4 path. |
| **Heavy** | measured compliance < threshold, **or unmeasured** | Retrieval is forced *before* generation, so the model cannot compose an assertion without a source set already in hand. D3(a)(b)(c) still verify the output identically. |

**Compliance metric.** Numerator: turns on that model in which every assertion span carried a
citation passing (a)(b)(c) **on first generation**, with no D4 retry. Denominator: turns on that
model containing at least one assertion span. Evaluated over a rolling window of configured size,
with a configured **minimum sample count**.

**Bootstrap and hysteresis — the default is fail-safe.** A model with fewer than the minimum
sample count is **unmeasured**, and unmeasured means **heavy**. A model is promoted to light only
after sustaining the threshold across a full window at or above minimum sample; it is demoted to
heavy on a single window evaluation below threshold. The asymmetry is deliberate: promotion is
slow and evidence-bound, demotion is immediate, and a model that has never been observed pays the
strict path rather than being trusted on a guess.

Enforcement level is keyed on the **computed rate**, never on a model name, provider, or
hand-maintained tier list. A new model is measured under heavy enforcement and earns its way out.

**Tiering is spent on routing, not on gating** (ADR-0121's concern): send the turn to a model that
can meet the contract. Prompt *phrasing and complexity* may still be rendered per model — that is
one contract rendered differently, not a different contract.

### D6 — `"Do NOT say you have no memory."` is deleted

Not debated on its merits: under D1 it is incoherent. "I have no source for that" becomes the
*correct* output rather than a forbidden one. Removing it in isolation would have traded
confabulation for arbitrary hedging — FRE-1118's own sequencing argument — but under a citation
contract the model has a concrete signal to act on, so the ordering objection lapses.

### D7 — Accepted costs, recorded rather than discovered

- **Retrieval quality becomes the accuracy ceiling.** With no parametric fallback, a poor search
  result is not silently improved by the model's background knowledge — it *is* the answer, now
  carrying a citation that passes (a)(b)(c). Source *quality* (reputation, allowlisting) is
  **explicitly out of scope for v1** and recorded as the primary residual risk.
- **Latency and cost rise on trivial turns.** A factual question the model "knows" now takes a
  round-trip, and heavy enforcement adds a retrieval leg before generation. Accepted, not
  mitigated.

---

## Alternatives Considered

### Option 1: Prompt the model to admit uncertainty ("say I don't know")

**Description:** Remove the prohibition in `executor.py:2536`, add an instruction to state
uncertainty when the model is unsure, and rely on the model's own judgement.

**Pros:**
- Cheapest possible change — a prompt diff, no new machinery.
- No latency or retrieval cost.
- Reversible in minutes.

**Cons:**
- Depends entirely on **calibration**, the capability that scales worst and latest across model
  sizes and remains unreliable at the frontier.
- Unfalsifiable: "the model is more careful now" cannot be measured without an external ground
  truth, which is the thing being built here anyway.
- FRE-1118 already identified the failure mode — removing the prohibition without a signal to
  act on trades confabulation for arbitrary hedging, which is not obviously better for a learner.

**Why Rejected:** It asks the model to do the one thing it is worst at, and produces no
observable that could show it failed. The FRE-1278 model did not believe it was speculating; an
instruction to flag speculation would not have fired.

### Option 2: Tier-specific contracts and gates by model size

**Description:** Distinct prompts and grounding gates per capability band — 27–70B, 300B+,
frontier — with weaker requirements on stronger models whose parametric knowledge is more
reliable.

**Pros:**
- Acknowledges a real effect: multi-clause conditional instructions measurably degrade
  small-model compliance, and a contract a frontier model handles gracefully can confuse a 27B
  one.
- Avoids paying retrieval latency on models whose recall is genuinely better.
- The machinery partly exists — ADR-0121's catalog and per-provider params.

**Cons:**
- **Correctness becomes a function of which model was routed.** The "collaborator and teacher"
  requirement does not relax when a cheaper model picks up the turn.
- Frontier models confabulate too — more rarely and **more convincingly**. Relaxing the gate on
  the strong model buys a rarer, harder-to-catch failure, which is the worst trade available for
  a learner who cannot calibrate.
- N bands means N contracts, N sets of acceptance criteria, and N things to keep in sync — the
  same growth shape the 2026-08-18 process streamline cut back by 60%.
- It tunes the safety property to the model. If a 27B primary cannot meet the contract, that is
  information about the primary, not grounds to weaken the contract.

**Why Rejected:** Superseded by D5, which keeps the real insight (capability differs, and
enforcement should respond to it) while discarding the harmful part (the contract itself
varying). Measured compliance is a strictly better discriminator than parameter count: it is
observed rather than guessed, it degrades gracefully to new models, and it is falsifiable.

### Option 3: Failure-triggered retrieval — consult documentation "when the code doesn't work"

**Description:** Leave the model to answer from parametric knowledge, and reach for documentation
or search only when a failure signal appears — a traceback, a failing test, a user correction.

**Pros:**
- Very low cost: no round-trip on the (common) path where the model is right.
- Matches an intuitive human workflow.
- Naturally bounded — it fires only on evidence of trouble.

**Cons:**
- Misses the two cases that matter most: **code that runs and is quietly wrong**, and a
  **plausible package name that does not exist** (failing at install, or resolving to a squatted
  package).
- Requires the model to *notice* it hit a bump — introspection again, one level further down.
- Structurally identical to the recency trigger being replaced: it keys on an observable proxy
  (breakage) rather than on the actual condition (asserting something unsourceable).

**Why Rejected:** It is the same defect as `prompts.py:56` in different clothing. Retained as a
useful *additional* trigger, rejected as the primary one.

### Option 4: Extend the recency keyword policy with more categories

**Description:** Keep the trigger heuristic in `prompts.py:56` and broaden it — add product
recommendations, local availability, prices, brands, and other categories as they are found.

**Pros:**
- Smallest possible diff; ships in one PR.
- No new subsystem, no verification layer, no latency change on unmatched turns.
- Would have caught FRE-1278 specifically.

**Cons:**
- An unbounded keyword chase, one incident per new category, each discovered by the owner being
  misled first.
- Leaves the memory layer and the missing admission untouched — FRE-1279's stated failure
  condition is exactly a decision that fixes one mechanism while the other two remain
  independently sufficient.
- Produces no observable for whether grounding improved.

**Why Rejected:** Treats instances rather than the class. FRE-1278 keeps this fix as its contained
bugfix so the baseline is working, but it is not the architecture.

### Option 5: Per-claim inline entailment verification

**Description:** Apply full entailment inline — for every assertion, a second model pass checks
the cited source actually supports it, before the turn is delivered.

**Pros:**
- Catches a correctly-cited token embedded in a claim the source does not support — the residue
  D3(c) cannot reach.
- Strongest possible guarantee, and the one a teacher most needs.

**Cons:**
- Cost and latency scale with the number of assertions per turn — potentially several model calls
  per response.
- The entailment judge is itself a model, with its own error rate, sitting on the critical path.
- Requires the verification model to be at least as capable as the primary, or it becomes the new
  weakest link.

**Why Rejected for v1:** Deferred rather than dismissed. D3(c)'s containment check absorbs most of
the practical value at zero model cost, and D3(d) keeps entailment as a sampled offline check that
measures the residual. Promotion to inline is a future decision informed by that measured rate.

---

## Consequences

### Positive Consequences

- **The tool layer gets a principled trigger.** "No retrieved source → nothing to cite → search"
  fires on exactly the case with no recency dimension, which the current heuristic cannot reach.
- **Not-knowing becomes expressible and, more importantly, derivable** — from the source set, not
  from model introspection.
- **The design is insensitive to the capability that varies most.** Format compliance (emit a
  citation token) is roughly flat across tiers; judgement compliance (decide whether this needs
  one) is not, and D1's span rule makes that decision deterministic.
- **`"Do NOT say you have no memory."` dies without a separate argument** (D6).
- **"Is a 27B primary good enough?" becomes a reading rather than an argument** — a per-model
  compliance rate, on the same footing as the ADR-0087 recall-quality program.
- **The memory scoring defect is correctly re-classified.** Under a citation regime a junk memory
  is a *citable source licensing a wrong claim*, promoting the 0.455 floor from noise to a
  correctness bug.

### Negative Consequences

- **Retrieval quality becomes the system's accuracy ceiling** (D7). Junk in, cited junk out.
- **Latency and cost rise on trivial factual turns**, and again under heavy enforcement.
- **A residual failure mode survives**: a source that contains the asserted token but does not
  support the claim. D3(c) cannot see this; only sampled D3(d) can, and only after the fact.
- **New machinery to maintain**: a per-turn source registry with stable identifiers across four
  source kinds, a three-check verification pass, a bounded retry loop, and a compliance
  measurement surface with windowing and hysteresis.
- **Span extraction becomes a correctness-relevant component.** D1's determinism depends on
  reliably identifying assertion spans; a weak extractor under-covers (assertions escape) or
  over-covers (generation gets blocked). This is now on the critical path.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Citation theatre — a real, reachable, unrelated source attached to an invented claim | **High** | D3(c) containment makes this deterministically detectable at zero model cost; verification runs on output structurally, never as a prompt request; failures cost the turn (D4); AC-2 seeds a fabricated citation and AC-5 seeds an unrelated-but-reachable one to prove both bite |
| Junk retrieval becomes cited truth | **High** | Recorded as the primary residual risk (D7); source quality explicitly deferred, not assumed solved; AC-6 requires empty retrieval to yield the explicit no-source statement rather than a guess |
| Span extractor under- or over-covers | **High** | Enumerated syntactic classes rather than semantic judgement (D1); AC-5 fails in both directions — blocked code generation *and* an uncited package name |
| Retry loop oscillates or never terminates | Medium | Configured maximum attempt count (D4); terminal state is the explicit no-source statement, which always exists |
| Compliance threshold becomes a hand-tuned tier list by the back door | Medium | AC-3 requires the rate to move in response to *observed* turns and enforcement to follow it, and fails if selection keys on model name, provider or a static list |
| Unmeasured model is trusted by default | Medium | D5 makes unmeasured ≡ heavy; promotion requires a full window at minimum sample, demotion is immediate |
| Small models cannot meet the format at all, making the primary unusable | Medium | Heavy enforcement forces retrieval before generation and does not require self-assessment; if that still fails it is a routing decision (ADR-0121), not a contract change |

---

## Implementation Notes

**Files expected to change:**

- `src/personal_agent/orchestrator/prompts.py` — replace the recency-keyed search policy (`:56`)
  with the grounding default; add the citation-emission format.
- `src/personal_agent/orchestrator/executor.py` — delete `"Do NOT say you have no memory."`
  (`:2536`); add the per-turn source registry, span extraction, the three-check verification pass
  and the bounded retry.
- `src/personal_agent/captains_log/turn_evidence.py` — extend the ADR-0125 contract with the
  **output** side of grounding. It currently records what recall *offered*, what was *admitted*,
  and why the rest *dropped* — the input side only. Nothing records what the turn *asserted*.
- `src/personal_agent/memory/proactive.py` + `src/personal_agent/config/settings.py` — subscore
  floors and `proactive_memory_min_score` (consumer ticket).
- Telemetry — per-model compliance rate with window, minimum sample and hysteresis;
  enforcement-level selection.

**Dependencies:** ADR-0125's evidence contract is the substrate for the source registry.
ADR-0121's catalog is where routing (not gating) responds to compliance.

**Testing strategy:** each criterion below pairs a **negative** probe with a **positive** one, so
that a degenerate implementation — refuse everything, reject every citation, disable memory — fails
rather than passes. AC-2 and AC-5 carry seeded negatives, because a guard never shown to reject
anything has not been shown to work.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

> **Note on adjudication:** ADR-0130's seam-ticket machinery is **Superseded** (2026-08-18,
> owner-directed process streamline), so this ADR files no seam ticket. These criteria are
> adjudicated on the umbrella, **FRE-1279**, once the implementation chain has landed and
> deployed.

**AC-1, AC-2 and AC-6 must hold on *every* enabled primary model, not merely on one** — that is
the observable form of D5's tier-invariance claim, and it fails if any enabled model is exempt.

- **AC-1 — The original failure does not reproduce, and the system has not simply gone mute.**
  *(negative)* The FRE-1278 probe ("which tinned tuna should I buy in France") replayed on the
  deployed stack yields either a response whose every named brand, shop and product carries a
  citation passing D3(a)(b)(c), or the explicit no-source statement. *(positive)* A companion probe
  whose answer **is** retrievable returns a substantive answer carrying at least one verified
  citation. · **Check:** two live turns; cross-reference every proper noun against that turn's
  evidence source ids and content. · *Fails if* any named brand, shop or product appears with no
  passing citation, **or** the companion probe returns a refusal — which is how "disable retrieval
  and refuse everything" is caught.

- **AC-2 — Verification rejects the bad and admits the good.** *(negative)* Two structurally
  different fabricated citation identifiers, injected into model output, are each blocked and never
  delivered, with the recorded rejection reason being unresolved-identifier. *(positive)* The same
  turn carrying a genuine citation passes and is delivered. · **Check:** seeded-negative test with
  two synthetic id shapes plus a positive control. · *Fails if* either fabricated citation passes,
  **or** the genuine citation is rejected — which is how a reject-everything guard is caught — **or**
  rejection is traceable to a test-specific special case rather than the general rule.

- **AC-3 — The rate is computed from observed turns, and enforcement follows it.** *(computation)*
  Running a batch of deliberately non-compliant turns on a model drives its recorded compliance
  down; running compliant turns drives it back up. *(behaviour)* Crossing the threshold downward
  moves that model to forced-retrieval-before-generation on its next turn, and crossing back up
  returns it to the light path after the promotion window. · **Check:** drive the rate with real
  turns in both directions and observe both the recorded value and the enforcement path; inspect
  the selection input. · *Fails if* the rate is static or settable only by hand, **or** enforcement
  is invariant to it, **or** the selection reads a model name, provider or hand-maintained tier
  list.

- **AC-4 — Empty relevance is expressible, and memory still works.** *(negative)* On a probe whose
  subject has no entity or episode in the graph, the memory section is absent or explicitly marked
  as holding nothing relevant, and the response does not imply recall. *(positive)* On a probe whose
  subject **is** verified present in Neo4j, the memory section is populated and the response uses it
  with a citation. · **Check:** two probes, subjects verified present/absent by direct graph query;
  inspect assembled context and response. · *Fails if* the absent-subject section is populated purely
  by recency-floor admissions or the response implies prior discussion, **or** the present-subject
  probe returns nothing — which is how "disable proactive memory globally" is caught.

- **AC-5 — Generation is not blocked, assertions are, and the source must actually mention the
  thing.** On a coding probe naming one real and one non-existent library: *(i)* generated code is
  emitted without per-line citations; *(ii)* the real package and its asserted API symbol carry a
  documentation citation whose **retrieved content contains those tokens**; *(iii)* the non-existent
  package is not asserted. *(seeded negative)* A citation pointing at a real, reachable
  documentation page that does **not** mention the asserted package is rejected. · **Check:** coding
  probe plus an injected unrelated-but-reachable citation. · *Fails if* code generation is blocked
  pending citations (over-application), **or** a package or API symbol is asserted with no
  documentation source (topic-scoped exemption leaking back in), **or** the unrelated-but-reachable
  citation passes — which is the citation-theatre hole D3(c) exists to close.

- **AC-6 — No hedged guesses, and the refusal is informative.** On a factual probe engineered so
  retrieval returns empty, the response *(i)* is non-empty, *(ii)* contains the explicit no-source
  statement naming what was searched, and *(iii)* introduces no proper noun, figure or date beyond
  those in the user's own message. · **Check:** force empty retrieval; assert all three properties.
  · *Fails if* the response is empty or generic filler, **or** omits what was searched, **or** offers
  a "best guess", a hedged suggestion or any named candidate.

---

## References

- [FRE-1279](https://linear.app/frenchforest/issue/FRE-1279) — umbrella: Seshat cannot express not-knowing (this ADR's originating ticket; adjudicates the criteria above)
- [FRE-1118](https://linear.app/frenchforest/issue/FRE-1118) — the memory-layer half: irrelevance admitted by construction, score never reaches the model, prompt forbids the admission (Approved, Urgent)
- [FRE-1278](https://linear.app/frenchforest/issue/FRE-1278) — the tool-layer instance: the agent invents brands, products and sources rather than searching (ships its contained bugfix independently)
- [FRE-1120](https://linear.app/frenchforest/issue/FRE-1120) — an embedder failure fails open into silent empty recall; the third instance of the same class, where a *failed* retrieval and an *empty* one are indistinguishable downstream
- [ADR-0125](ADR-0125-two-quality-dimensions-and-turn-evidence-contract.md) — Accepted (2026-07-27); the turn-evidence contract that records the *input* side of grounding and is the substrate for the source registry
- [ADR-0121](ADR-0121-model-catalog-and-selection-layer.md) — Implemented (2026-07-22), Addendum A Proposed; where tiering is spent (routing), per D5
- [ADR-0100](ADR-0100-relevance-bounded-recall.md) — Accepted; relevance-bounded recall and `recall_similarity_floor`
- [ADR-0087](ADR-0087-memory-recall-quality-measurement-program.md) — Accepted (2026-06-27); the measurement program that owns the sampled offline entailment check (D3d)
- [ADR-0034](ADR-0034-searxng-self-hosted-web-search.md) — Accepted; SearXNG, the web source whose quality now bounds accuracy (D7)
- [ADR-0126](ADR-0126-reading-the-living-knowledge-substrate.md) — Accepted (2026-07-27), seam adjudicated 2026-07-31 (three green, five inconclusive, none red); the memory-section render path D6 edits
- [ADR-0130](ADR-0130-two-tiers-of-acceptance-criteria.md) — **Superseded** (2026-08-18); why this ADR files no seam ticket
- [Neo4j vector index scoring](https://neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/vector-indexes/) — cosine scores normalized to `(1 + cos) / 2`, the source of the embedding subscore's 0.5 floor
- `src/personal_agent/orchestrator/executor.py:2536` — the prohibition deleted by D6
- `src/personal_agent/orchestrator/prompts.py:56` — the recency-keyed search policy replaced by D1/D2
- `src/personal_agent/memory/proactive.py` — subscore weights and floors; `src/personal_agent/config/settings.py` — `proactive_memory_min_score`, `proactive_memory_w_*`, `proactive_memory_recency_half_life_days`

---

## Status Updates

### 2026-08-23 - Proposed
**Changed By:** `adr` session (owner-directed design)
**Reason:** Drafted from FRE-1279 after a multi-round design discussion with the owner. The owner
supplied the central move (verified citations in place of self-assessed confidence) and the
boundary that makes it enforceable (the model is not a knowledge source outside coding), and
accepted the tier-invariant contract with enforcement keyed on measured compliance. Revised after
Codex review round 1: the D3/D5 verification contradiction resolved (verification is invariant;
pre-generation forcing is what varies), D5's bootstrap closed (unmeasured ≡ heavy, with an
asymmetric promotion/demotion rule), D1's boundary made span-level and syntactic, a third inline
containment check added as D3(c), and every acceptance criterion given a positive arm so that a
degenerate implementation fails rather than passes.
