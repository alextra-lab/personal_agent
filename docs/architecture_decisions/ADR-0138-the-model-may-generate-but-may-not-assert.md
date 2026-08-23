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

**The default is deny.** Outside the exempt regions below, any span making a claim about the
world requires a verified citation. The default follows directly from D2: if parametric knowledge
is not a source, then a world-fact claim with no source has no admissible provenance, whatever
grammatical form it takes.

An earlier draft inverted this — enumerating an *assertion* class and treating everything else as
generation. That under-covers, and the gap is not marginal: *"this fish is high in mercury"* is a
checkable factual claim containing no named entity, and would have escaped a
named-entity-triggered rule entirely. Enumerating what must be cited is unbounded; enumerating
what need not be is finite.

**Exempt regions** (no citation required):

| Region | Rule |
|---|---|
| **Code bodies** | Everything inside a code fence, **except dependency declarations** — imports, package manifests, install commands — which are verified against the package registry or documentation. This preserves the anti-squatting property that motivated including coding turns at all, without demanding a citation for every symbol in a generated function. |
| **Prose about generated code** | Not exempt. `httpx.AsyncClient` *used* in a code body is a proposal to be executed and tested; the prose claim *"`httpx.AsyncClient` accepts `timeout=`"* is an assertion and requires a documentation source. Use versus assert is the line. |
| **Derived arithmetic** | Exempt when every input is itself cited. Computing `5` from a cited `2` and a cited `3` introduces no new world fact. |
| **Restating the user** | Content traceable to the user's words this conversation (D2 item 4). |
| **Connective and evaluative text** | The model's judgement over cited material — *"…are both well regarded"* — carries no citation of its own; the spans it ranges over do. |

**Span classification is a named component, not a regex.** This is the honest position, and it
replaces the previous draft's claim of syntactic determinism, which did not survive review. No
regular expression partitions "world-fact claim" from "generation." Classification is performed by
an explicit **span extractor** — a small model or a structured-output pass — and its quality is
therefore a bounded, measurable property of the system rather than an assumption:

- The contract's strength is bounded by **extraction recall**: a claim the extractor misses is a
  claim the contract never sees.
- **Extraction precision** bounds usability: false positives block legitimate generation.
- Both are measured against a labelled probe set and carry a stated bar (AC-7). Extraction quality
  is a first-class risk, recorded in the risk table, not a detail deferred to implementation.

This is still a far weaker dependency than the one it replaces. Deciding *"is this span a claim
about the world?"* is a labelling task with ground truth and a measurable error rate. Deciding
*"do I actually know this?"* is calibration, has no ground truth, and is the capability that
scales worst. The design trades an unmeasurable dependency for a measured one.

**Citation binding is explicit by construction.** Each non-exempt span carries its own adjacent
citation marker in the output format. Binding is not inferred from proximity, clause or sentence
boundaries — `Ortiz [S1] is better than Nardin [S2]`, never `Ortiz is better than Nardin [S1]`
leaving it ambiguous which source covers what. An unbound or multiply-bound span is a format
violation and takes the D4 path.

### D2 — Parametric knowledge is never a source

The admissible source set is:

1. the **memory graph** (entities, episodes, claims);
2. **tool and web results** retrieved during this turn;
3. **documentation** (context7 and equivalents) retrieved during this turn;
4. **the user's own words in this conversation** — present in the turn's assembled context.

The model's weights are not on the list.

**Sources with no external referent** (the user's words; memory nodes resolved by id) satisfy
D3(b) **vacuously** — there is nothing to fetch, so reachability is not-applicable and counts as
passed, not as skipped. Turns citing only such sources enter the D5 compliance metric on the same
footing as any other. This keeps D3's "all three" invariant literally true rather than
carrying a silent exception.

### D3 — Three deterministic checks inline; entailment sampled offline

Every assertion span must carry a citation that passes **all three** of:

- **(a) Resolution** — the cited identifier resolves to a source present in **this turn's**
  retrieved source set.
- **(b) Reachability** — the source is real and retrievable. A URL returning non-2xx after
  redirects, a soft-404, or an auth wall counts as **unreachable**. Memory citations resolve
  against the node id.
- **(c) Containment** — the asserted token appears in the cited source's **retrieved content**,
  under the normalization contract below.

**(c) is not optional and is not entailment.** It is normalized token presence, costs nothing, and
closes the largest hole in a citation regime: a real, reachable, entirely unrelated source attached
to an invented claim. Without (c), reachability alone is nearly worthless against citation theatre —
a valid URL proves a page exists, not that it mentions the thing asserted.

**Normalization contract.** Containment is matched on **token boundaries**, never raw substring —
`Ham` must not match inside `Birmingham`, and boilerplate in navigation or footers is excluded from
the matched content. The comparison must tolerate, at minimum: case and Unicode normalization;
digit-group separators and decimal-precision variance (`1,000` ≡ `1000`, `3.0` ≡ `3`); common unit
expressions of the same quantity; and registered aliases (`IBM` ≡ `International Business
Machines`) where an alias table exists.

**False rejections are a first-class cost, not an implementation detail.** A containment miss on a
legitimate assertion forces the D4 no-source path and produces a refusal the user did not deserve.
The tolerated variance classes above are therefore stated here, and the false-rejection rate carries
its own measured bar (AC-8). Where normalization is genuinely ambiguous — paraphrase, translation,
an unregistered alias — the span is recorded as **unverifiable-by-containment** and routed to D4
with that reason distinguished in telemetry from a true no-source outcome, so the two failure modes
never blur in the compliance metric.

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
naming what was searched**.

That statement is itself contract-compliant by **provenance**, not by exemption: its content is
drawn only from the turn's own record — the search terms actually issued (a tool-result fact, D2
item 2) and the user's own words (D2 item 4). It introduces no new world-fact span, so it cannot
recurse into another verification failure, and the loop always terminates.

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

**Compliance metric — measured only where it is not confounded.** Numerator: turns in which every
non-exempt span carried a citation passing (a)(b)(c) **on first generation**, with no D4 retry.
Denominator: turns containing at least one non-exempt span.

**The metric counts only turns where retrieval was *not* pre-forced.** Heavy enforcement supplies
sources before generation, so first-generation compliance measured under heavy enforcement is
largely a measurement of the enforcement, not of the model. Scoring those turns would let a model
that only complies when spoon-fed earn promotion, fail under light, be demoted, recover under
heavy, and oscillate indefinitely.

**Probation sampling breaks the resulting deadlock.** If only light-path turns count, an unmeasured
model — which is heavy by default — would never accrue observations. So a configured small fraction
of a heavy model's turns are routed to the light path specifically to generate unconfounded
observations. Probation turns are fully verified like any other; only the pre-generation forcing is
withheld.

**Bootstrap, band and cooldown — the default is fail-safe.**

- A model with fewer than the configured **minimum sample count** of unconfounded observations is
  **unmeasured**, and unmeasured means **heavy**.
- Promotion and demotion use **separate thresholds** (a hysteresis band), never one value, so a
  model sitting near the line does not flap.
- Promotion requires sustaining the upper threshold across a full window at or above minimum
  sample; demotion follows a single window below the lower threshold.
- A demoted model serves a configured **cooldown** before it is eligible for promotion again.
- **Window staleness closes the frozen-denominator hole.** Turns with no non-exempt span are
  excluded from the denominator, so a model that stops producing recognized spans would otherwise
  coast indefinitely on a stale favourable window. A window that ages past a configured maximum
  without sufficient new observations reverts the model to **unmeasured**, hence heavy. Compliance
  must be continuously re-earned, never banked.

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
- **Span extraction is on the critical path and bounds the whole contract.** D1 depends on a
  classifier, not a regex — the syntactic-determinism claim did not survive review. A weak extractor
  under-covers (claims escape the contract) or over-covers (generation is blocked). Its recall and
  precision are measured (AC-7) rather than assumed, but the contract can never be stronger than its
  recall.
- **False refusals are a new user-visible failure mode.** A containment miss on a legitimate
  assertion produces a refusal the user did not deserve, and at scale would read as the system
  becoming useless rather than honest. Bounded by AC-8 and kept distinguishable in telemetry.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Citation theatre — a real, reachable, unrelated source attached to an invented claim | **High** | D3(c) containment makes this deterministically detectable at zero model cost; verification runs on output structurally, never as a prompt request; failures cost the turn (D4); AC-2 seeds a fabricated citation and AC-5 seeds an unrelated-but-reachable one to prove both bite |
| Junk retrieval becomes cited truth | **High** | Recorded as the primary residual risk (D7); source quality explicitly deferred, not assumed solved; AC-6 requires empty retrieval to yield the explicit no-source statement rather than a guess |
| Span extractor under- or over-covers, bounding the contract | **High** | Default-deny with a finite exempt list rather than an unbounded assertion enumeration (D1); recall reported per class including entity-free factual claims, with a stated bar (AC-7); AC-5 fails in both directions — blocked code generation *and* an uncited dependency |
| False refusals from containment misses on legitimate assertions | **High** | Normalization contract fixes the variance classes that must be tolerated (D3c); rate bounded and measured (AC-8); containment-unverifiable distinguished from true no-source in telemetry so a wave of false refusals cannot read as honesty |
| Enforcement oscillation — heavy preloads sources, inflating the rate that earns promotion | **High** | The metric counts only non-pre-forced turns; probation sampling supplies unconfounded observations without deadlock; separate promote/demote thresholds plus cooldown (D5) |
| Compliance banked on a stale window while the model degrades | Medium | Window staleness reverts a model to unmeasured, hence heavy (D5) |
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
> adjudicated on the umbrella, **FRE-1279**, once the implementation chain has landed and deployed.

**Probes are drawn from a held-out set, not enumerated here.** Review round 2 established that any
criterion naming its own probe can be satisfied by special-casing that probe — supporting exactly
the one package the test uses, templating exactly the one refusal it checks. So every criterion
below is adjudicated against a **held-out probe set** maintained outside this document, sampled at
adjudication time, with stated coverage and a pass-rate bar rather than a single instance. An
implementation cannot special-case probes it has not seen.

**Coverage requirement.** The held-out set spans, at minimum: retrievable and non-retrievable
questions; each exempt region in D1 (code bodies, dependency declarations, derived arithmetic,
restatement, connective text); factual claims **with and without** named entities — the latter
because the "high in mercury" class is exactly what the previous draft's rule missed; and each
source kind in D2.

**AC-1, AC-2 and AC-6 must hold on *every* enabled primary model**, which is the observable form of
D5's tier-invariance claim, and fail if any enabled model is exempt.

- **AC-1 — Grounded when it can be, honest when it cannot, and not merely mute.** Over a held-out
  sample of ≥30 factual questions split between retrievable and non-retrievable: every response
  either carries citations passing D3(a)(b)(c) for all non-exempt spans, or is the explicit
  no-source statement — **and** the retrievable half is answered substantively at or above a stated
  rate. · **Check:** live turns; every non-exempt span cross-referenced against that turn's evidence
  source ids and content. · *Fails if* any uncited non-exempt span ships, **or** the retrievable
  half's substantive-answer rate falls below bar — which is how "refuse everything" is caught, since
  blanket refusal passes the honesty arm and fails this one.

- **AC-2 — Each of the three checks independently rejects, and valid citations still pass.**
  Randomly generated (not fixed) citation identifiers exercise three distinct negatives: one failing
  **(a)** only (unresolvable id), one passing (a) but failing **(b)** (resolvable id, unreachable
  source), and one passing (a)(b) but failing **(c)** (real reachable source not containing the
  asserted token). Each is blocked, with the recorded rejection reason naming the specific check.
  A positive control citation passes and is delivered. · **Check:** seeded-negative suite with
  per-check attribution plus positive control. · *Fails if* any of the three negatives passes —
  which is how "implement only id-membership and skip reachability and containment" is caught —
  **or** the positive control is rejected, **or** rejection reasons do not distinguish which check
  fired.

- **AC-3 — The metric measures what it claims, and enforcement follows it.** *(validity)* Compliance
  computed by the system agrees, within a stated tolerance, with independent labelling of the same
  held-out turns — where a turn counts compliant only if **every** non-exempt span passes, not if
  any citation is merely present. *(unconfounded)* The metric is computed only from non-pre-forced
  turns, demonstrated by showing heavy-enforcement turns absent from the denominator. *(behaviour)*
  Driving real compliance below the lower threshold demotes the model on its next turn; sustained
  recovery above the upper threshold promotes it only after the window and cooldown elapse. ·
  **Check:** labelled ground-truth comparison, denominator inspection, and threshold crossings driven
  in both directions. · *Fails if* system-computed compliance diverges from labelling beyond
  tolerance — which is how "score compliant when ≥1 citation exists" is caught — **or** pre-forced
  turns appear in the denominator, **or** enforcement selection reads a model name, provider or
  static tier list, **or** a model promotes without serving cooldown.

- **AC-4 — Empty relevance is expressible, and recall still works across its kinds.** Over a
  held-out set spanning entities, episodes and alias-reached subjects, with presence or absence
  verified by direct graph query: absent subjects yield a memory section that is absent or explicitly
  marked empty and a response that does not imply recall; present subjects yield a populated section
  used with a citation, at or above a stated rate **for each kind**. · **Check:** graph-verified
  probes per kind; inspect assembled context and response. · *Fails if* absent-subject sections are
  populated by recency-floor admissions or responses imply prior discussion, **or** any kind falls
  below bar — which is how "disable recall and implement exact entity lookup only" is caught, since
  episodes and aliases fail it.

- **AC-5 — Generation flows, assertions are checked, and refusal is visible.** Over a held-out set
  of coding probes: generated code emits without per-span citations; dependency declarations are
  verified against registry or documentation; prose claims about APIs carry documentation citations
  whose retrieved content contains the asserted tokens; and a **non-existent package is refused
  explicitly, naming the failure** — not silently omitted. A seeded citation pointing at a real,
  reachable page not mentioning the asserted package is rejected. · **Check:** coding probe set plus
  the unrelated-but-reachable seeded negative. · *Fails if* code generation is blocked pending
  citations, **or** a dependency or prose API claim ships uncited, **or** the unrelated-but-reachable
  citation passes, **or** a non-existent package is dropped silently rather than refused — which is
  D4's ban on silent stripping, asserted here because round 2 showed a passing implementation could
  violate it.

- **AC-6 — No hedged guesses, and honesty is not a special case of empty retrieval.** On held-out
  probes covering **both** empty retrieval **and** non-empty-but-insufficient retrieval, the response
  is non-empty, names what was searched, and introduces no proper noun, figure or date beyond the
  user's own message and the retrieved sources. · **Check:** both retrieval conditions across the
  held-out set. · *Fails if* the response is empty or generic filler, **or** omits what was searched,
  **or** offers a best guess, hedged suggestion or named candidate, **or** the honest behaviour appears
  only under empty retrieval — which is how "template the empty case and leave everything else
  unverified" is caught.

- **AC-7 — Span extraction is good enough to carry the contract, and is known to be.** Extraction
  recall and precision are measured against a labelled held-out corpus and meet stated bars, with
  recall reported **per class** including factual claims carrying no named entity. · **Check:**
  labelled-corpus scoring, reported per class. · *Fails if* either metric is below bar, **or** any
  class is unreported — since the contract's strength is bounded by recall, an unmeasured extractor
  makes every other criterion unfalsifiable.

- **AC-8 — Verification does not manufacture refusals.** The false-rejection rate — legitimate,
  genuinely-supported assertions forced onto the D4 path — is at or below a stated bar over a
  held-out set that deliberately includes the D3 normalization variance classes (digit grouping,
  decimal precision, units, registered aliases, case and Unicode). Containment-unverifiable outcomes
  are distinguished in telemetry from true no-source outcomes. · **Check:** variance-class probe set;
  compare system outcome against known-supported ground truth. · *Fails if* the rate exceeds bar,
  **or** the two outcome kinds are conflated — which would let a wave of false refusals read as
  honest not-knowing.

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
pre-generation forcing is what varies), D5's bootstrap closed (unmeasured ≡ heavy), a third inline
containment check added as D3(c), and every acceptance criterion given a positive arm.

Codex review round 2, which materially changed the design rather than tightening it. **D1's claim
of syntactic determinism did not hold** — the enumerated assertion classes required semantic
judgement and, worse, under-covered: a factual claim carrying no named entity escaped the contract
entirely. D1 was inverted to **default-deny with a finite exempt list**, and span classification is
now an explicit measured component (AC-7) rather than an assumed-deterministic rule. The precedence
rule was scoped so it no longer blocks legitimate code generation, splitting *use* of a symbol from
*assertion* about it, and narrowing the in-code obligation to dependency declarations. **The D5
compliance metric was confounded by its own enforcement level** — heavy enforcement preloads
sources, so a model that complies only when spoon-fed would have oscillated between tiers forever;
the metric now counts only non-pre-forced turns, with probation sampling to avoid deadlock, a
hysteresis band, cooldown, and window staleness to stop compliance being banked. D3(c) gained a
normalization contract with a measured false-rejection bound (AC-8), citation-to-span binding was
made explicit by output format, the D2 reachability exemption was resolved as a vacuous pass, and
D4's terminal refusal was shown compliant by provenance rather than exemption. All acceptance
criteria moved to **held-out probe sets with coverage and rate bars**, because round 2 demonstrated
that any criterion naming its own probe can be satisfied by special-casing that probe.
