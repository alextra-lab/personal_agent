# ADR-0138: The Model May Generate, But It May Not Assert — Verified Citations as Seshat's Grounding Contract, Tier-Invariant and Enforced by Measured Compliance

**Status:** Accepted — 2026-08-23 (owner); amended 2026-08-25 — D2's `curl` illustration
corrected: `bash`+`curl` yields no admissible source at all under the shipped independence
rule, not the partial page-yes/URL-no case the original illustration described (FRE-1283
review). A typed fetch tool would get that partial admission, but none was live in this
deployment at the time; FRE-1297 provisioned the native `fetch_url` tool the illustration
now names. · **D2 amended again 2026-09-01 — once, in both
directions**: ADR-0098 Amendment A §A6 **narrows** it (entitlement follows the provenance terminus)
and ADR-0139 D2/D3/D7 **widens** it (admissibility decided on the result, at a new `OBSERVED` tier).
Both are recorded in the D2 amendment note below rather than left to be reconciled by a reader
(FRE-1349, FRE-1347).
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
| **Code** | Code, **except dependency declarations** — imports, package manifests, install commands — which are verified against the package registry or documentation. This preserves the anti-squatting property that motivated covering coding turns at all, without demanding a citation for every symbol in a generated function. The exemption attaches to *code*, **not to fencing**: prose placed inside a fence is prose. A fence claiming a natural-language or unrecognized type, or one whose content does not parse as the declared language, is not an exempt region — **and neither is natural-language content embedded inside otherwise valid code.** `print("Paris has 9 million residents")` parses cleanly as Python; a parse check alone would exempt it, making a string literal a delivery channel for an uncited assertion. String literals, comments and docstrings whose content is a world-fact claim are subject to the contract like any other prose. The exemption covers code the user is being *offered to run*, never text the model is *delivering as its answer*. |
| **Prose about code** | Not exempt. `httpx.AsyncClient` *used* in code is a proposal to be executed and tested; the prose claim *"`httpx.AsyncClient` accepts `timeout=`"* is an assertion requiring a documentation source. Use versus assert is the line. |
| **Derived arithmetic** | Exempt when every input is itself cited. Computing `5` from a cited `2` and a cited `3` introduces no new world fact. |
| **Attributed restatement** | Repeating the user's own words **with attribution** — *"you asked about X"*. Exempt because the claim is about what the user said, which the turn record holds. Presenting the same content as the model's own recommendation is **not** restatement and is not exempt. |
| **Connective and narrowly-evaluative text** | The model's judgement over cited material, **only where it introduces no externally checkable predicate of its own**. Comparatives and orderings over cited attributes qualify. Predicates such as *well regarded*, *safe*, *popular*, *recommended*, *reliable* do **not** — each is an externally checkable claim about the world and requires a source, however evaluative it sounds. An earlier draft used *"are both well regarded"* as the exemplar of exempt evaluation; that was wrong, and it was the common-knowledge trap reappearing one level down. |
| **System-record statements** | Claims about *this turn's own execution* — what was searched, what was retrieved, that nothing was found. Their referent is the turn record, not the world, so they are not world-fact claims. This is the exemption that makes D4's terminal state reachable; it is deliberately narrow and covers no claim whose truth depends on anything outside the turn record. |

**Ambiguous classification resolves to assertion.** Where a labeller or extractor cannot decide
whether a span is exempt evaluation or a checkable claim — *"A is better value than B"* reads as
either an ordering over cited prices or a market-value claim — it is treated as an **assertion** and
requires a source. Default-deny governs its own edge cases; adjudication guidance for recurring
ambiguities lives with the labelled corpus (AC-7), where it can be versioned and measured, not in
this document.

**Precedence on overlap is one-directional: non-exempt wins.** Where a span falls under both an
exempt region and the default-deny rule — the user supplying `pip install some-package` and asking
the model to run with it — the citation obligation stands. An exemption never rescues a span that
independently requires a source.

**Spans are non-overlapping atomic claims.** Extraction emits one span per atomic proposition, not
nested or overlapping regions, so *"Paris is France's capital and has 2.1 million residents"* is two
spans, each binding its own citation. This is what makes per-span binding and D3(c) containment
well-defined rather than dependent on how an extractor happened to segment.

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

**Sources with no external referent** — the user's words; memory nodes resolved by id; and
**turn-local tool evidence** (shell output, database rows, ephemeral API responses) — satisfy D3(b)
**vacuously**. There is nothing to re-fetch: the recorded result *is* the durable artifact, held in
the turn record, and reachability is not-applicable rather than failed. Verification for these
resolves against the recorded result, never against a re-execution, so a non-deterministic tool
cannot invalidate a citation after the fact.

> **Threat model (added 2026-09-02):** this contract's adversary is declared in
> [ADR-0140](ADR-0140-the-model-is-not-a-security-boundary.md), not here and not in any consumer.
> Under it the parameter-schema boundary stated below **is** the boundary on D2's invocation axis,
> and ADR-0139's widening of that axis is withdrawn — see the amendment note further down, which
> records which half of it still stands.

**Independence requirement — a tool result is a source only where it is not the model's own words
returning.** A tool result is admissible **only to the extent its content is not derived from the
model's own arguments to that call.** Without this, D2 is bypassed in a single round-trip: the model
runs `printf 'Paris has 9 million residents'`, cites the shell output, and a claim originating
entirely in its weights passes all three checks. The rule is mechanical — content traceable to the
tool-call arguments is not evidence:

- A typed fetch tool taking `url=...` as its only argument — `fetch_url` (FRE-1297) — the
  fetched **page** is a source; the URL the model chose is not. `curl` run through `bash`
  is **not** this case: `bash` is a fully-excluded arbitrary-code tool per D2's independence
  rule below, so it yields no admissible source, not even the page.
- a database query — the returned **rows** are a source; the SQL the model wrote is not.
- `printf`, `echo`, or any call whose output is a function of model-authored input — yields **no**
  admissible source at all.

This is the same principle as D2 itself, applied one layer down: a tool that returns what the model
told it to say is the model, wearing a tool's identifier. Turns citing only such sources enter the D5 compliance metric on the same
footing as any other. This keeps D3's "all three" invariant literally true rather than
carrying a silent exception.

#### Amendment note — 2026-09-01: D2 is narrowed and widened at once, and the two compose here

> **HALF WITHDRAWN 2026-09-02 (FRE-1357).** D2 has two axes and this note amended both. **The
> narrowing stands** — ADR-0098 Amendment A §A6's provenance-terminus rule on the **authorship** axis
> is live, is being implemented under FRE-1347, and nothing here disturbs it. **The widening is
> withdrawn** — ADR-0139 D2/D3/D7 on the **invocation** axis, under
> [ADR-0140](ADR-0140-the-model-is-not-a-security-boundary.md): arbitrary-code tools keep the
> categorical exclusion stated in the third bullet above, there is no `MODEL_AUTHORED_CODE_TOOLS`
> split, no `OBSERVED` tier for tool results (it survives only for D4 attachments), and no
> invocation-composition check. **`OBSERVED` and `OBSERVATION` are not introduced by this note.**
> The widening paragraphs below are retained as record and are not normative. One change does follow
> for this contract: `mcp_esql`'s single parameter is a model-authored query language, so under
> ADR-0140 T4's classification test it is **compose-capable and inadmissible** — the case ADR-0139's
> withdrawn check was covering.

D2 acquired **two independent amendments, drafted without reference to each other**. ADR-0098
Amendment A §A6 obliges this record explicitly ("this amends ADR-0138 D2, and must be recorded
there"); FRE-1349 required them applied **once, coherently**, rather than twice with a reconciliation
pass in between. Neither amendment's own text is restated here — this note says what D2 now means and
where each rule lives.

**The narrowing — ADR-0098 Amendment A §A6.** Entitlement follows the **terminus of the provenance
chain**. A typed memory retrieval — item 1 of the admissible set above — whose chain terminates at an
agent-authored turn or at `provenance_state = 'none'` earns `AGENT_DERIVED` and is **not** admissible
as a citation. Aggregation is most-restrictive: one `none`-terminus item drops the whole recall.
D2 previously treated a typed memory retrieval as admissible with reachability vacuous for
referent-less items; that is no longer sufficient on its own. Provenance is **not** an input to
`verify_turn`'s containment — it decides only the entitlement the recall registers with.

**The widening — ADR-0139 D2, D3 and D7.** Admissibility is decided on the **result**, not on the
pipe it arrived through, so the categorical exclusion of arbitrary-code tools in the third bullet
above is replaced for `MODEL_AUTHORED_CODE_TOOLS` (`bash`, `run_python`, `mcp_browser_evaluate`,
`mcp_browser_run_code`) by a result-level rule at a new `OBSERVED` entitlement tier. The generative
tools (`perplexity_*`, `mcp_research`, `mcp_sequentialthinking`) keep the categorical exclusion
verbatim. Model-composed payloads stay inadmissible — no longer by tool identity, but by an
invocation-composition check whose three arms are specified in ADR-0139 D2. **That check is not
total, and the amendment does not claim it is:** it closes fully-literal payloads (`printf 'Paris
has 9 million residents'`) and partially-composed ones, and leaves three residuals declared in
ADR-0139 D6 — encoded forms, the cross-call shared-state channel, and a one-content-token authored
frame filled by a non-figure substitution (`echo "Capital: $(whoami)"`). D2's *rule* is unchanged and
still says the model's own words returning are not evidence; what this amendment records is that the
rule is now enforced by a check with a stated reach rather than by a categorical tool exclusion with
none.

**Where they meet, and the rule that resolves it.** The seam is a result that is *both* a
first-person act and a read of stored state. **The terminus rule follows the bytes, not the tool.**
A live observation has no provenance chain, so §A6 has nothing to test and `OBSERVED` stands. A
**read-back of persistent state is a retrieval wearing an observation's clothes** and inherits §A6's
terminus test whatever tool carried it — so `bash("cat …")` addressed into an agent-writable store
earns `AGENT_DERIVED`, exactly as a memory recall with an agent-authored terminus does. Without this
rule the two amendments would cancel: A6 closes FRE-1338's leak in the knowledge graph while
ADR-0139 reopens the same shape on the filesystem. The rule binds at the **address** rather than the
author, which is a stated residual with a filed remedy, not an omission — see ADR-0139 D3.

**Net effect on the admissible set above.** Item 1 (memory graph) is *conditional* on its terminus.
Item 2 (tool and web results) is *widened* to arbitrary-code results that survive the
invocation-composition check, *except* where the result is a read-back of an agent-writable store, in
which case item 1's terminus rule governs instead. Items 3 and 4 are unchanged. The model's weights
are still not on the list, and nothing in either amendment exempts a span from needing a citation.

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

**Containment unit.** The check is not "some token from the span appears". For each atomic-claim
span, **every entity, every figure, and every predicate content word** must be present in the cited
source; connective and function words are ignored. For *"Paris has 2.1 million residents"*, both
`Paris` and the quantity must appear — matching `Paris` alone would recreate exactly the citation
theatre D3(c) exists to close, while demanding every non-stopword would manufacture refusals.

**The predicate is part of the unit, not an afterthought.** An earlier draft required only entities
and figures, which was **vacuous for precisely the class D1's inversion was introduced to catch**:
*"this fish is high in mercury"* contains neither, so the condition held over an empty set and any
source whatever passed. Requiring predicate content words (`mercury`) closes the direct form.

**Spans with no entity and no figure escalate to inline entailment.** For that class, containment
alone remains too weak to be meaningful — a page mentioning `mercury` does not thereby support
*"this fish is high in mercury"* — so D3(d) runs **inline for these spans** rather than offline.
This is a deliberate cost: the entity-free predicate class is the expensive one, and it is a
minority of spans, but the expense is real and is accepted here rather than discovered later.

Fixing this unit is a decision, not a tuning parameter, because AC-8's false-rejection measurement
can only be taken once the matching rule is settled.

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

That statement is reachable because it consists entirely of **system-record** spans (D1's final
exempt region): what was searched, and that nothing was found. Their referent is this turn's own
record rather than the world, so they are not world-fact claims and cannot recurse into another
verification failure. An earlier draft argued this from *provenance* instead, which did not hold —
D1 would still have demanded a citation for the span, and no retrieved source contains the sentence
"no source was found". The narrow exemption is the honest construction, and it is what guarantees
the loop terminates.

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

**Governance of the set and the bars.** The held-out set is a **versioned artifact owned by the
eval program** (ADR-0087), not an informal collection. Every numeric bar named below is fixed in the
implementation ticket that builds the corresponding check, and **recorded before results are seen** —
a bar set after inspecting the outcome measures nothing. Deliberately, the *rates* live in tickets
while the *invariants and their failure conditions* live here: an ADR settles what must be true and
what would falsify it; calibrating a threshold against a corpus that does not yet exist would be
inventing a number, not deciding something.

**Preregistration alone is not enough, so a floor principle binds the tickets.** Recording a bar
before seeing results prevents post-hoc tuning but not a vacuous bar — 0% per-class recall,
preregistered, satisfies the timing rule and means nothing. Every bar must therefore be justified
against the failure it prevents, and **demonstrated to reject a deliberately broken baseline**: a
bar that a known-broken implementation would pass is not a bar. That constraint is decidable at
ticket-review time without this ADR inventing a number it cannot ground.

**Coverage requirement.** The held-out set spans, at minimum: retrievable and non-retrievable
questions; each exempt region in D1 (code bodies, dependency declarations, derived arithmetic,
restatement, connective text); factual claims **with and without** named entities — the latter
because the "high in mercury" class is exactly what the previous draft's rule missed; and each
source kind in D2.

**AC-1, AC-2, AC-5 and AC-6 must hold on *every* enabled primary model**, which is the observable
form of D5's tier-invariance claim, and fail if any enabled model is exempt.

- **AC-1 — Grounded when it can be, honest when it cannot, and not merely mute.** Over a held-out
  sample of ≥30 factual questions split between retrievable and non-retrievable: every response
  either carries citations passing D3(a)(b)(c) for all non-exempt spans, or is the explicit
  no-source statement — **and** the retrievable half is answered substantively at or above a stated
  rate. · **Check:** live turns; spans identified by the **independent labelling** of AC-7's corpus, not by
  the system's own extractor, then cross-referenced against that turn's evidence source ids and
  content. Scoring against the extractor's own output would make the check circular — an extractor
  that recognises nothing would trivially find nothing uncited. · *Fails if* any uncited non-exempt span ships, **or** the retrievable
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
  static tier list, **or** a model promotes without serving cooldown, **or** probation sampling does
  not occur on a heavy model (leaving it unable ever to earn promotion), **or** a model whose window
  has gone stale is not returned to unmeasured.

- **AC-4 — Empty relevance is expressible, and recall still works across its kinds.** Over a
  held-out set spanning entities, episodes and alias-reached subjects, with presence or absence
  verified by direct graph query: absent subjects yield a memory section that is absent or explicitly
  marked empty and a response that does not imply recall; present subjects yield a populated section whose admitted items are
  **relevant to the probe** — judged against the labelled expectation, not merely present — and used
  with a citation, at or above a stated rate **for each kind**. · **Check:** graph-verified
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
  violate it. The set additionally includes **prose placed inside a fence** (a `text` fence, one whose
  content does not parse as its declared language, **and valid-parsing code carrying a world-fact
  claim in a string literal or comment**, e.g. `print("Paris has 9 million residents")`), all of
  which must be verified as prose: *fails if* fencing or mere parseability buys exemption.

- **AC-6 — No hedged guesses, and honesty is not a special case of empty retrieval.** On held-out
  probes covering **both** empty retrieval **and** non-empty-but-insufficient retrieval, the response
  is non-empty, names what was searched, and contains **no uncited non-exempt span of any kind** —
  stated this way, not as "no proper noun, figure or date", because that older phrasing predates
  D1's inversion and would let an entity-free claim such as *"it is high in mercury"* pass. · **Check:** both retrieval conditions across the
  held-out set. · *Fails if* the response is empty or generic filler, **or** omits what was searched,
  **or** offers a best guess, hedged suggestion or named candidate, **or** the honest behaviour appears
  only under empty retrieval — which is how "template the empty case and leave everything else
  unverified" is caught.

- **AC-7 — Span extraction is good enough to carry the contract, and is known to be.** Extraction
  recall and precision are measured against a labelled held-out corpus and meet stated bars, with
  recall **meeting the bar in every class** — reporting alone is not sufficient — including factual
  claims carrying no named entity, and including prose inside fences. · **Check:** labelled-corpus
  scoring, per class. · *Fails if* either metric is below bar overall, **or** any single class is
  below bar, **or** any class is unreported — since the contract's strength is bounded by recall, an unmeasured extractor
  makes every other criterion unfalsifiable.

- **AC-8 — Verification does not manufacture refusals.** The false-rejection rate — legitimate,
  genuinely-supported assertions forced onto the D4 path — is at or below a stated bar over a
  held-out set that deliberately includes the D3 normalization variance classes (digit grouping,
  decimal precision, units, registered aliases, case and Unicode). Containment-unverifiable outcomes
  are distinguished in telemetry from true no-source outcomes. **Paired arm:** the false-*acceptance*
  rate — unsupported assertions whose citation nonetheless passes (a)(b)(c) — is at or below its own
  stated bar over a set of deliberately mismatched source/claim pairs, which **must include
  entity-free and figure-free predicate claims** (the class an entities-and-figures-only containment
  rule passed vacuously) **and model-authored tool output** (`printf`-style laundering, per D2's
  independence requirement). · **Check:** variance-class
  probe set for rejections, mismatched-pair set for acceptances, both against known ground truth. ·
  *Fails if* either rate exceeds bar — the paired arm is what stops an accept-everything containment
  check from acing this criterion on zero false rejections — **or** the two outcome kinds are
  conflated, which would let a wave of false refusals read as honest not-knowing.

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

### 2026-08-23 - Accepted
**Changed By:** Project owner (accepted); `adr` session (drafting)
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

Codex review round 3 closed three exemption leaks — evaluative text laundering checkable predicates,
the code exemption attaching to fencing rather than code, and D4's terminal state argued from
provenance where a narrow system-record exemption was needed — plus the containment unit, overlap
precedence and span atomicity.

Codex review round 4 (narrow, scoped to the round-3 delta, run past the skill's three-round cap at
the owner's offer) found the round-3 fixes had themselves introduced two full bypasses. **Turn-local
tool evidence had no independence requirement**, so `printf '<claim>'` cited as shell output
laundered parametric knowledge into an admissible source in one round-trip; D2 now admits a tool
result only where its content is not derived from the model's own arguments to the call. **The new
containment unit was vacuous for entity-free claims** — "every entity and figure must be present"
holds over an empty set for *"this fish is high in mercury"*, the exact class D1's inversion existed
to catch — so the unit now includes predicate content words, and spans carrying no entity or figure
escalate to inline entailment at an accepted cost. Valid-parsing code was also still a prose channel
(`print("...")`), now closed and probed by AC-5.

**Review disposition.** Four rounds were run; in each of the first three, the fix applied introduced
the next round's defect, and round 4 continued the pattern. The findings narrowed in scope across
rounds — round 2 re-architected D1 and D5, round 4 touched three clauses — but did not reach zero.
The ADR is therefore **Accepted with its review history recorded** rather than as a defect-free
document — an ADR records a decision and its known weaknesses, and the remaining risk sits in
specification detail that implementation tickets and their acceptance bars are the right instrument
to close. Owner accepted 2026-08-23 after round 4, declining a fifth round.
