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

**Irrelevance is admitted by construction, and the score never reaches the model.** The
proactive scorer (`memory/proactive.py`) combines four subscores at weights 0.45 / 0.25 / 0.20 /
0.10 (embedding / entity-overlap / recency / topic) against `proactive_memory_min_score = 0.3`.
Three of the four have non-zero floors: a Neo4j cosine vector score is `(1 + cos) / 2`, so an
orthogonal candidate scores 0.5, not 0; `_recency_subscore` returns 0.5 on a missing timestamp;
`_topic_subscore` returns 0.3 on zero keyword hits. The stated floor of ≈0.355 reconstructs
exactly as `0.45×0.5 + 0.25×0 + 0.20×0.5 + 0.10×0.3`.

The worst case is worse than that figure suggests. A memory **from today** with zero entity
overlap and zero topic hits scores `0.225 + 0 + 0.20 + 0.03 = 0.455` — more than 50% above the
admission bar. **Recency alone buys admission.** Anything discussed recently is injected into
subsequent turns regardless of topic, and whatever relevance was computed is then discarded
before the model sees the list.

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

Two classes of output, with different obligations:

| Class | Examples | Citation required |
|---|---|---|
| **Generated** | code, reasoning, synthesis, restating the user, arithmetic, instruction-following, judgement over cited facts | No |
| **Asserted** | named entities, brands, organisations, people, products, package/library existence and API surface, versions, URLs, statistics, dates, quotations | **Yes — verified** |

The boundary is on the **kind of output**, not on the **topic of the turn**. A coding turn is
not exempt: a function body is generation, but "this library exists and has this method" is an
assertion and requires a documentation source. This is deliberate — hallucinated package names
are among the most exploited coding failure modes, and a topic-scoped exemption would license
exactly that.

The contract is enforced on **assertions present in the output**, never on the model's declared
mode. There is no clean separation inside a model between recalling and reasoning, so a turn
that labels itself "reasoning" earns no exemption.

### D2 — Parametric knowledge is never a source

The admissible source set is: the **memory graph**, **tool and web results**, **documentation**
(context7 and equivalents), and **the user's own words in this conversation**. The model's
weights are not on the list.

### D3 — "Verified" means existence and reachability, inline; entailment, offline

- **(a)** The cited identifier resolves to a source **retrieved in this same turn**.
- **(b)** That source is real and reachable (URL resolves; memory node exists).

(a) and (b) are deterministic, require no second model pass, and run **inline and blocking**.

**(c)** — whether the source actually *supports* the claim (entailment) — is the only check that
catches a genuine source attached to a claim it does not make, but per-claim inline entailment
is a cost and latency decision we decline for v1. It runs **sampled and offline**, feeding the
eval set rather than the turn.

### D4 — On failure: block, retry with forced retrieval, then say so

An assertion failing (a) or (b) blocks the turn and triggers a bounded retry with retrieval
forced. If the retry also fails, the correct output is an **explicit statement that no source
was found, naming what was searched**.

It is **never** a hedged guess. A guess with a disclaimer is parametric knowledge wearing a
disclaimer, and under D2 it is not admissible. Stripping the claim silently is equally
rejected: silence is the disease being treated.

### D5 — The contract is tier-invariant; enforcement is set by measured compliance

**The contract does not vary by model.** No uncited world-fact assertion, identical at 27B and
at the frontier. It is a property of Seshat, not of the model that happens to serve the turn.
Tiering the contract would make correctness a function of routing, and would reintroduce
precisely the dependency the contract was designed to remove: the design's whole virtue is that
it routes around calibration, the capability that varies most across tiers.

**Enforcement strength does vary — as a function of measured compliance, not of parameter
count.** Compliance is observable: does this model emit well-formed citations, and do they
verify?

- Measured compliance **at or above** the configured threshold → light enforcement: the model's
  own citations are trusted inline, verification samples.
- Measured compliance **below** threshold → heavy enforcement: deterministic pre-check, retrieval
  forced before generation, block-and-retry per D4.

Enforcement level is keyed on the **recorded rate**, never on a model-name or tier list. A new
model is measured and lands where it lands; there is no taxonomy to maintain and no argument
about which tier a given release belongs to.

**Tiering is spent on routing, not on gating** (ADR-0121's concern): send the turn to a model
that can meet the contract. Prompt *phrasing and complexity* may still be rendered per model —
that is one contract rendered differently, not a different contract.

### D6 — `"Do NOT say you have no memory."` is deleted

Not debated on its merits: under D1 it is incoherent. "I have no source for that" becomes the
*correct* output rather than a forbidden one. Removing it in isolation would have traded
confabulation for arbitrary hedging — FRE-1118's own sequencing argument — but under a citation
contract the model has a concrete signal to act on, so the ordering objection lapses.

### D7 — Accepted costs, recorded rather than discovered

- **Retrieval quality becomes the accuracy ceiling.** With no parametric fallback, a poor search
  result is not silently improved by the model's background knowledge — it *is* the answer, now
  carrying a citation. Source *quality* (reputation, allowlisting) is **explicitly out of scope
  for v1** and recorded as the primary residual risk; only presence and reachability are
  enforced.
- **Latency and cost rise on trivial turns.** A factual question that the model "knows" now
  takes a round-trip. This is accepted, not mitigated.

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
observable that could show it failed. The FRE-1278 model did not believe it was speculating;
an instruction to flag speculation would not have fired.

### Option 2: Tier-specific contracts and gates by model size

**Description:** Distinct prompts and grounding gates per capability band — 27–70B, 300B+,
frontier — with weaker requirements on stronger models whose parametric knowledge is more
reliable.

**Pros:**
- Acknowledges a real effect: multi-clause conditional instructions measurably degrade
  small-model compliance, and a contract a frontier model handles gracefully can confuse a 27B one.
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

**Description:** Leave the model to answer from parametric knowledge, and reach for
documentation or search only when a failure signal appears — a traceback, a failing test, a user
correction.

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

**Why Rejected:** Treats instances rather than the class. FRE-1278 keeps this fix as its
contained bugfix so the baseline is working, but it is not the architecture.

### Option 5: Per-claim inline entailment verification

**Description:** Apply full verification level (c) inline — for every assertion, a second model
pass checks the cited source actually supports it, before the turn is delivered.

**Pros:**
- The only level that catches the characteristic failure of citation systems: a real source
  attached to a claim it does not make ("citation theatre").
- Strongest possible guarantee, and the one a teacher most needs.

**Cons:**
- Cost and latency scale with the number of assertions per turn — potentially several model
  calls per response.
- The entailment judge is itself a model, with its own error rate, sitting on the critical path.
- Requires the verification model to be at least as capable as the primary, or it becomes the new
  weakest link.

**Why Rejected for v1:** Deferred rather than dismissed. D3 keeps (c) as a sampled, offline check
feeding the eval set, which retains the detection capability without putting a model judge on
every turn. Promotion to inline is a future decision informed by the measured theatre rate.

---

## Consequences

### Positive Consequences

- **The tool layer gets a principled trigger.** "No retrieved source → nothing to cite → search"
  fires on exactly the case with no recency dimension, which the current heuristic cannot reach.
- **Not-knowing becomes expressible and, more importantly, derivable** — from the source set,
  not from model introspection.
- **The design is insensitive to the capability that varies most.** Format compliance (emit a
  citation token) is roughly flat across tiers; judgement compliance (decide whether this needs
  one) is not, and D1 makes that decision deterministic.
- **`"Do NOT say you have no memory."` dies without a separate argument** (D6).
- **"Is a 27B primary good enough?" becomes a reading rather than an argument** — a per-model
  compliance rate, on the same footing as the ADR-0087 recall-quality program.
- **The memory scoring defect is correctly re-classified.** Under a citation regime a junk memory
  is a *citable source licensing a wrong claim*, promoting the 0.455 floor from noise to a
  correctness bug.

### Negative Consequences

- **Retrieval quality becomes the system's accuracy ceiling** (D7). Junk in, cited junk out.
- **Latency and cost rise on trivial factual turns.**
- **A new failure mode is created: citation theatre** — plausible sources attached to unsupported
  claims. Inline (a)+(b) does not catch it; only sampled (c) does, and only after the fact.
- **New machinery to maintain**: a per-turn source registry with stable identifiers across four
  source kinds, a verification pass, a retry loop, and a compliance measurement surface.
- **Synthesis remains a judgement call.** "Ortiz and Nardin are both good" is reasoning over cited
  facts; the boundary between cited fact and uncited synthesis will need adjudication in practice.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Citation theatre — sourced confabulation, more convincing than the current kind | **High** | Verification runs on output structurally, never as a prompt request; failures cost the turn (D4); sampled offline entailment (D3c) measures the theatre rate, and AC-2 seeds a fabricated citation to prove the check bites |
| Junk retrieval becomes cited truth | **High** | Recorded as the primary residual risk (D7); source quality explicitly deferred, not assumed solved; AC-6 ensures empty retrieval yields no named entities rather than a guess |
| Retry loop oscillates or never terminates | Medium | Bounded retry count; terminal state is the explicit no-source statement (D4), which always exists |
| Over-application blocks legitimate generation (code, reasoning) | Medium | D1's boundary is on output kind with an enumerated assertion class; AC-5 asserts code generation is not blocked while library claims still cite |
| Compliance threshold becomes a hand-tuned tier list by the back door | Medium | AC-3 requires enforcement to change as a function of the *recorded rate*, and fails if it keys on model name or a tier list |
| Small models cannot meet the format at all, making the primary unusable | Medium | Enforcement tiering (D5) degrades to deterministic pre-check + forced retrieval, which does not require the model to self-assess; if that still fails, it is a routing decision (ADR-0121), not a contract change |

---

## Implementation Notes

**Files expected to change:**

- `src/personal_agent/orchestrator/prompts.py` — replace the recency-keyed search policy (`:56`)
  with the grounding default; add citation-emission format.
- `src/personal_agent/orchestrator/executor.py` — delete `"Do NOT say you have no memory."`
  (`:2536`); add the per-turn source registry and the verification/retry pass.
- `src/personal_agent/captains_log/turn_evidence.py` — extend the ADR-0125 contract with the
  **output** side of grounding. It currently records what recall *offered*, what was *admitted*,
  and why the rest *dropped* — the input side only. Nothing records what the turn *asserted*.
- `src/personal_agent/memory/proactive.py` + `config/settings.py` — subscore floors and
  `proactive_memory_min_score` (consumer ticket).
- Telemetry — per-model citation-compliance rate; enforcement-level selection.

**Dependencies:** ADR-0125's evidence contract is the substrate for the source registry.
ADR-0121's catalog is where routing (not gating) responds to compliance.

**Testing strategy:** each criterion below is checked by a probe against the deployed stack plus
an assertion over that turn's evidence record. AC-2 is a **seeded negative** — a fabricated
citation injected deliberately — because a guard that has never been shown to reject anything
has not been shown to work.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

> **Note on adjudication:** ADR-0130's seam-ticket machinery is **Superseded** (2026-08-18,
> owner-directed process streamline), so this ADR files no seam ticket. These criteria are
> adjudicated on the umbrella, **FRE-1279**, once the implementation chain has landed and
> deployed.

- **AC-1 — The original failure does not reproduce.** The FRE-1278 probe ("which tinned tuna
  should I buy in France") replayed on the deployed stack yields either (a) a response whose every
  named brand, shop and database carries a citation resolving to a source retrieved in that same
  turn, or (b) an explicit no-source statement. · **Check:** live turn + cross-reference every
  proper noun in the response against that turn's evidence source ids. · *Fails if* any named
  brand, shop, product or database appears with no matching source id for that turn.

- **AC-2 — The verification actually rejects.** A turn in which a **fabricated** citation
  identifier (resolving to nothing in the turn's retrieved set) is injected into the model output
  is blocked and never delivered. · **Check:** seeded-negative test injecting a synthetic citation
  id; assert the turn takes the D4 block-and-retry path. · *Fails if* the fabricated citation
  passes verification, or if the turn is delivered with the unresolvable citation intact.

- **AC-3 — Enforcement responds to the measured rate, not to a tier list.** Setting a model's
  recorded compliance below the configured threshold causes its next turn to take the
  forced-retrieval path; restoring it above threshold returns the turn to the light path. ·
  **Check:** manipulate the recorded rate for one model, observe the enforcement path on the next
  turn in both directions. · *Fails if* the enforcement path is invariant to the recorded rate, or
  if it is selected by model name, provider or a hand-maintained tier list.

- **AC-4 — Empty relevance is expressible.** On a probe whose subject has no entity or episode in
  the graph, the assembled memory section is either absent or explicitly marked as holding nothing
  relevant, and the response does not imply recall. · **Check:** probe with a subject verified
  absent from Neo4j; inspect the assembled context and the response. · *Fails if* the memory
  section is populated purely by recency-floor admissions, or the response claims or implies prior
  discussion of the subject.

- **AC-5 — Generation is not blocked, and coding turns still cite facts.** On a coding probe,
  generated code is emitted without per-line citations, while every named package and asserted API
  surface carries a documentation source. · **Check:** coding probe naming at least one real and
  one non-existent library; inspect output and evidence. · *Fails if* code generation is blocked
  pending citations (over-application), **or** a package name or API signature is asserted with no
  documentation source (reading A leaking back in).

- **AC-6 — No hedged guesses.** On a factual probe engineered so retrieval returns empty, the
  response contains no specific named entity beyond those in the user's own message. · **Check:**
  force empty retrieval; assert the response introduces no new proper nouns, figures or dates. ·
  *Fails if* the response offers a "best guess" list, a hedged suggestion, or any named candidate.

---

## References

- [FRE-1279](https://linear.app/frenchforest/issue/FRE-1279) — umbrella: Seshat cannot express not-knowing (this ADR's originating ticket; adjudicates the criteria above)
- [FRE-1118](https://linear.app/frenchforest/issue/FRE-1118) — the memory-layer half: irrelevance admitted by construction, score never reaches the model, prompt forbids the admission (Approved, Urgent)
- [FRE-1278](https://linear.app/frenchforest/issue/FRE-1278) — the tool-layer instance: the agent invents brands, products and sources rather than searching (ships its contained bugfix independently)
- [FRE-1120](https://linear.app/frenchforest/issue/FRE-1120) — an embedder failure fails open into silent empty recall; the third instance of the same class, where a *failed* retrieval and an *empty* one are indistinguishable downstream
- [ADR-0125](ADR-0125-two-quality-dimensions-and-turn-evidence-contract.md) — Accepted (2026-07-27); the turn-evidence contract that records the *input* side of grounding and is the substrate for the source registry
- [ADR-0121](ADR-0121-model-catalog-and-selection-layer.md) — Implemented (2026-07-22), Addendum A Proposed; where tiering is spent (routing), per D5
- [ADR-0100](ADR-0100-relevance-bounded-recall.md) — Accepted; relevance-bounded recall and `recall_similarity_floor`
- [ADR-0087](ADR-0087-memory-recall-quality-measurement-program.md) — Accepted (2026-06-27); the measurement posture this ADR's compliance rate joins
- [ADR-0034](ADR-0034-searxng-self-hosted-web-search.md) — Accepted; SearXNG, the web source whose quality now bounds accuracy (D7)
- [ADR-0126](ADR-0126-reading-the-living-knowledge-substrate.md) — Accepted (2026-07-27), seam adjudicated 2026-07-31 (three green, five inconclusive, none red); the memory-section render path D6 edits
- [ADR-0130](ADR-0130-two-tiers-of-acceptance-criteria.md) — **Superseded** (2026-08-18); why this ADR files no seam ticket
- `src/personal_agent/orchestrator/executor.py:2536` — the prohibition deleted by D6
- `src/personal_agent/orchestrator/prompts.py:56` — the recency-keyed search policy replaced by D1/D2
- `src/personal_agent/memory/proactive.py` — subscore floors; `config/settings.py` — `proactive_memory_min_score`, `proactive_memory_w_*`

---

## Status Updates

### 2026-08-23 - Proposed
**Changed By:** `adr` session (owner-directed design)
**Reason:** Drafted from FRE-1279 after a multi-round design discussion with the owner. The
owner supplied the central move (verified citations in place of self-assessed confidence) and
the boundary that makes it enforceable (the model is not a knowledge source outside coding), and
accepted the tier-invariant contract with enforcement keyed on measured compliance.
