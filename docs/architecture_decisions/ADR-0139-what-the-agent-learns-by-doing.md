# ADR-0139: What the Agent Learns by Doing — Result-Level Admissibility, a First-Person Observation Tier, and a Denominator for the Compliance Metric

**Status:** Proposed
**Date:** 2026-08-29
**Deciders:** Project owner (design), `adr` session (drafting)
**Tags:** grounding, hallucination, citations, observability, alerting, vision

---

## Context

ADR-0138 made parametric knowledge inadmissible and required a verified citation on every
world-fact assertion. Its independence rule — D2 — states the constraint in **graded** form:

> A tool result is admissible **only to the extent** its content is not derived from the model's own
> arguments to that call.

It then applies that rule correctly to typed tools: for `fetch_url`, the fetched page is a source
and the model-chosen URL is not; for a database query, the returned rows are a source and the SQL
is not. Two sentences later it abandons the graded form for a **categorical** one — `bash` is "a
fully-excluded arbitrary-code tool … so it yields no admissible source, not even the page."

Under the stated principle, `cat config.yaml` yields admissible contents and an inadmissible
filename, exactly parallel to `fetch_url`. Under the categorical clause the identical bytes become
uncitable because of which tool carried them. **Same evidence, different verdict, decided by
transport rather than by provenance.** That is the defect this ADR closes.

### The measured consequence

Trace `dba5b2cba1e0bece6c8b9396465a265c`: four `bash` calls succeeded, their output reached the
model — input tokens rose 14,638 → 41,520 across roughly 27,000 tokens of real Tempo trace data —
and every result was refused with `source_registry_tool_inadmissible`. The registry the model was
then told to cite from held seven sources: the user's own message and six unrelated memories. **Not
one line of the evidence the turn was about.** D1's default-deny required a citation on every
assertion; D2 had removed the only source those assertions could cite. `passed_count: 0` was not a
failure to comply. It was the only arithmetically possible outcome.

This is not one bad turn. Measured over `agent-logs-2026-08` on 2026-08-29:

| Quantity | Value |
|---|---|
| Non-exempt spans across asserting turns | 222 |
| Spans that passed | **2** (0.9%) |
| Asserting turns scoring exactly zero | **13 / 15** |
| `model_authored_invocation` refusals | 100 — `bash` 96, `perplexity_query` 4 |
| `no_content` refusals | 6 — all `fetch_url` |
| Distinct traces carrying ≥1 refusal | 23 |

A constant near-zero across two different primary models is structural, not behavioural. A
model-quality explanation predicts variance; this has none.

### The general shape

**Anything the agent learns by *doing* has no admissible referent. Only what it learns by
*retrieving* does.** Vision is the same hole with a narrower framing — an attached image is not a
registered source, so every image turn scores zero by construction (FRE-1316, absorbed here).

### Why the obvious fix is already refuted, in this repository

The natural remedy is a conservative allowlist of shell command heads — `cat`, `git log`,
`psql SELECT`, `curl` admissible; `echo`, `printf`, heredocs excluded. `source_registry.py:29-35`
already considered and rejected exactly that, with worked counterexamples, three of which are those
same heads:

```
find . -maxdepth 0 -printf 'Paris has 9 million residents\n'
git log --pretty=format:'Paris has 9 million residents'
psql -c "SELECT 'Paris has 9 million residents'"
curl --write-out 'Paris has 9 million residents'
```

Its conclusion — "denylisting flags per head is the same unbounded chase one level down" — holds. An
allowlist of heads is not available to us.

### The observation that makes the problem tractable

Every one of those escape hatches works the same way: **the model writes the payload into its own
argument string.** Deciding *"what does this command do"* is undecidable for arbitrary shell.
Deciding *"does this claim's content appear in the command line"* is trivial, and it is the graded
rule D2 already states, applied literally rather than approximated by a per-tool table.

### Why the measurement could not tell us this

The diagnosis above required joining two events by `trace_id` — `source_registry_tool_inadmissible`
(DEBUG) against `grounding_verification_completed` (INFO) — a join nobody runs and no alert
performs. Neither document is self-diagnosing: the verification event reports a zero without saying
whether a non-zero was reachable.

**ADR-0134 D1 named this exact failure one layer down**, for `api_cost_recorded`: "half as many
documents as yesterday is equally consistent with *half the events were lost* and *half as many
requests arrived*." Distinguishing them requires relating the numerator to an independent measure of
the activity that should have produced it. `passed_count: 0` is the same shape — equally consistent
with a careless model and with a system that offered nothing citable — and it was authored without
the denominator that would separate them.

There is a second blindness. FRE-1327 recorded the model minting `[S@bash-tempo-trace-dba5b2]` — a
citation marker in our own format, naming the source it had actually used. The parser is
`citations.py:45`, `\[(S\d+@[0-9a-f]{16})\]`; that string fails on three counts, so it was never
seen as a citation attempt at all and the span scored `UNCITED` — byte-identical to a span where the
model did not try. Live data agrees: `UNRESOLVED` appears in **0** documents in August while
`uncited` appears in 11. **The strongest available evidence of confabulation-under-pressure is
currently indistinguishable from apathy.**

---

## Decision

**Admissibility is a property of the evidence, not of the pipe it arrived through. And a compliance
number without a denominator is not a measurement.**

D1 comes first deliberately: it is the instrument by which every other decision here is judged, and
it is implementable and shippable ahead of them.

### D1 — The compliance metric gets a denominator, and four signals replace one number

`grounding_verification_completed` carries, on every turn where verification ran:

| Field | Meaning |
|---|---|
| `turn_evidence_class` | `no_assertions` · `uncitable` · `citable` |
| `tool_results_offered` | Tool results the executor presented to the registry this turn |
| `tool_results_admitted` | How many registered as sources |
| `observed_span_outcomes` | Spans citing an `OBSERVED` source (D3), split by outcome: `passed`, `not_contained`, `invocation_covered` |
| `near_miss_markers` | Citation-shaped strings that failed `CITATION_MARKER_PATTERN` |

`uncitable` is defined mechanically: **non-exempt spans exist, at least one tool result was offered,
and none was admitted.** A turn that called no tools and asserted anyway is **not** `uncitable` — it
is a genuine no-source turn and stays in the denominator, so a model reasoning from its weights
cannot hide behind this class.

**Putting these on the verification event, rather than in a second event, is the decision.** It
removes the join. Each document becomes self-diagnosing: a zero-compliance turn states, in its own
record, whether a non-zero was reachable.

**Compliance is reported only over `citable` turns**, and the rest are published alongside rather
than folded in:

| Signal | Detects | Disposition (ADR-0134 D1) | Lifecycle |
|---|---|---|---|
| `citation_compliance_rate` over `citable` turns | model carelessness | **correlated** — denominator is citable asserting turns | steady-state |
| `uncitable_turn_rate` over asserting turns | this ADR's defect; any future tool landing unclassified | **correlated** — denominator is asserting turns | **closure metric** — see below |
| `observed_span_outcomes` — `passed` / `not_contained` / `invocation_covered` | confabulation against evidence that *was* present; laundering attempts | **correlated** — denominator is spans citing an `OBSERVED` source | steady-state |
| `near_miss_markers` | confabulation under compliance pressure | **conditional** — silence is health; no stoppage rule | steady-state |

**`uncitable_turn_rate` is a closure metric, and saying so is load-bearing.** Once D2 lands,
arbitrary-code results register, so `tool_results_admitted` is non-zero and this rate collapses **by
construction** — whether or not a single span ever passes. It therefore **cannot be the criterion
that proves D2 worked**, and any acceptance criterion resting on its fall alone is guaranteed to
pass while measuring nothing. That is exactly the failure FRE-1328 names: trading a measurable,
honest 0% for an unmeasurable 100%. The rate is retained because it remains a genuine sentinel for
the *next* tool that lands unclassified — a failure that is silent today — but the evidence that D2
delivered is `observed_span_outcomes`.

**`observed_span_outcomes` is the signal that must never go structurally to zero.** It is measured
on spans, after both polarities of D2's check have run, so it distinguishes the three states
registration alone cannot: evidence cited and supported (`passed`), evidence present but the claim
invented against it (`not_contained` — this is FRE-1327), and a laundering attempt refused
(`invocation_covered`). A vacuous implementation that registers everything and checks nothing shows
`passed` at 100% and `invocation_covered` at zero across the seeded probe set, which AC-1 and AC-5
both reject.

**Alerting is platform configuration.** No notifier, no routing logic, and no threshold code enters
`src/`; the emitted fields above are the whole application-side change. Rules live as
version-controlled Grafana configuration, and panels are built in the tool's own UI and exported,
never hand-authored.

**The dependency is stated honestly: ADR-0134 is `Proposed`, not Accepted.** This ADR adopts its
vocabulary — the family dispositions in the table above, and absence-as-a-firing-state — because it
is the clearest available articulation of the problem, and because its D2a Kibana stage was already
abandoned in favour of Grafana. But nothing here *requires* ADR-0134 to land. The binding constraint
comes from **ADR-0090 D3 (Accepted)**, which already rules that dashboards are version-controlled
files provisioned from `config/grafana/dashboards/`. Should ADR-0134 be rejected or reshaped, D1's
signals and their emission survive unchanged; only the disposition labels would need rewording.

**A known conservatism, recorded rather than discovered.** A turn that calls `bash` (refused) *and*
`search_memory` (admitted) classifies as `citable` and scores zero, even though its assertions
concerned the `bash` output. The classification fails toward counting against us, which is the safe
direction, and the case collapses once D2 lands. It is not a defect to be fixed by widening
`uncitable`, which would hand the model an escape.
### D2 — Admissibility is decided on the result, not on the invocation

**The tool set that today short-circuits admissibility is split in two**, because it currently
conflates two unrelated exclusions under one branch:

| Set | Members | Treatment |
|---|---|---|
| `MODEL_AUTHORED_CODE_TOOLS` | `bash`, `run_python`, `mcp_browser_evaluate`, `mcp_browser_run_code` | **Result-level admissibility**, below |
| `GENERATIVE_TOOLS` | `perplexity_*`, `mcp_research`, `mcp_sequentialthinking` | **Categorically excluded, unchanged** (D5) |

Today both live in one `ARBITRARY_CODE_TOOLS` frozenset with one categorical branch. Relaxing that
branch without splitting it would silently admit another model's generated prose as a source — the
exact widening D5 forbids — so the split is part of this decision, not an implementation detail.

A `MODEL_AUTHORED_CODE_TOOLS` result **is registered as a source**, at `OBSERVED` entitlement. The
anti-laundering rule becomes a second, opposite-polarity application of the containment machinery
D3(c) already has:

> A span citing such a source passes only where it is **contained in the result** and is **not
> contained in the turn's model-authored invocation text**.

**The negative check uses `ContainmentResult.contained`, never `.passed`.** This is the whole
correctness of the rule and it is not a naming detail. `.passed` is `outcome is CONTAINED`, while
`.contained` is `{CONTAINED, ENTAILMENT_REQUIRED}` — and an entity-free span such as *"this fish is
high in mercury"* resolves to `ENTAILMENT_REQUIRED`, never to `CONTAINED`. A negative check written
as `not result.passed` would therefore read `ENTAILMENT_REQUIRED` as "not present in the
invocation" and admit `printf 'this fish is high in mercury'` — **reinstating precisely the vacuity
ADR-0138's round-2 review found in D3(c)'s original containment unit, one polarity over.** Using
`.contained` fails closed on that class, which is correct: we cannot cheaply run entailment against
a command line, and the safe reading of "the invocation may cover this" is that it does.

**The invocation text is scoped to the source, not to the turn.** Each registered source retains
the invocation that produced it, and the negative check runs a span against **the invocation of the
source it cites** — nothing else.

*An earlier draft made this set turn-scoped and accumulating, on the reasoning that concurrent
dispatch made registration order nondeterministic. That reasoning was false and the fix it produced
was worse than the defect.* Dispatch is concurrent, but registration is not: `executor.py`'s
"Phase 3: Sequential record + result assembly" appends results **in `allowed_plans` order**, so
there was never an ordering problem to solve. And an accumulating turn-scoped set **breaks D4.** The
registry is created once per turn and the retry loop reuses it by construction
(`source_registry.py:_register` — "re-registering the same item, which the D4 retry loop does by
construction"), so invocation text from a rejected first attempt would still be excluding evidence
on the second. The common shape is not exotic: the model runs `rg 'fish high mercury' report.txt`,
is refused because the search phrase covers the span, retries with `cat report.txt` — and under a
turn-scoped set is refused *again*, for the first attempt's command. **The retry could never repair
the turn**, which is the one thing D4 exists to do.

Per-source scope removes that. It also restores the shape ADR-0138 already uses: the arguments
travel with the content, recorded at registration, and verification reads the record rather than
re-deriving anything.

**What per-source scope does not cover, stated rather than implied.** Two calls where the first
writes and the second reads — `bash("echo 'X' > /tmp/f")` then `bash("cat /tmp/f")` — are invisible
to it, because `cat /tmp/f` does not cover the span. That shape stays with the existing `_taint`
guard, which catches it only where a discrete argument value recurs (`write(path=…)` then
`read(path=…)`), and **not** for two `bash` calls whose whole command line is the argument.
`_reads_tainted`'s own docstring already records the remedy — "which needs the tool layer to report
its writes rather than the registry to guess them" — and this ADR inherits that residual unchanged
rather than pretending to close it. Under D6's careless threat model that is accepted; under an
adversarial one it is not, and Option 2 is the design instead.

**Using D3(c)'s coverage predicate rather than "any word overlaps" is load-bearing.** A rule
rejecting a span because *any* of its words appear in the arguments would reject
`grep 'mercury' file.txt` followed by any assertion about mercury — search commands legitimately
contain the search term, and that rule would manufacture refusals on the single most common
evidence-gathering shape. Coverage asks whether the invocation **alone** would satisfy containment
for that span. `grep 'mercury' file.txt` does not cover *"this fish is high in mercury"*; `printf
'Paris has 9 million residents'` covers its span exactly.

The residual is `grep 'Paris has 9 million residents' bigfile.txt` against a file that genuinely
contains the line: the invocation covers the span, so it is rejected though the evidence is real.
That is a false rejection, it is conservative, the model can `cat` the file instead, and it is
accepted here rather than discovered later.

**This rule tightens the typed path as well as loosening the arbitrary-code one, and that resolves
an open defect.** FRE-1306 records that `mcp_esql` earns a blanket `EXTERNAL` while its single
parameter is a model-authored ES|QL program, so `ROW claim = "Paris has 9 million residents"`
launders in one round-trip through a tool the parameter-schema boundary classifies as safe. That
ticket offered three options — reclassify the tool and lose every telemetry-query citation; parse
ES|QL in the turn path; or amend D2 to distinguish *selecting* from *composing*. The rule above is a
fourth that none of them anticipated: **it needs no parse and draws no select/compose line**,
because the composed literal appears in the invocation and coverage rejects it, while an
index-reading query keeps its citation because the returned rows do not. The negative check
therefore applies to **every** source whose parameters can carry a program — arbitrary-code tools
and query-language tools alike — and FRE-1306 is resolved by this ADR rather than beside it.

**`_strip_argument_echo` is retained** as field-level defence in depth. The span-level rule above is
the binding one.
### D3 — A third entitlement tier: `OBSERVED`

`Entitlement` gains a member between `EXTERNAL` and `AGENT_DERIVED`. It separates two questions D2
currently conflates: **is this admissible** (D2) from **how much do we trust it** (entitlement).

- `EXTERNAL` — independent on both of D2's axes: the arguments did not compose the result, and the
  agent did not author what the store returned.
- `OBSERVED` — the agent **witnessed** it. Arbitrary-code results surviving D2, and attachments
  (D4). Entitled to satisfy a citation; reported as its own tier so the metric can be sliced and so
  a future enforcement policy can treat the tiers differently **without another ADR**.
- `AGENT_DERIVED` — the agent wrote it. Denied, unchanged.

**The tiers are labels, not a comparable ordering.** `Entitlement` is a `StrEnum` with no
`__lt__`, and this ADR does not add one. "Below `EXTERNAL`" above describes intent for a future
policy, not a comparison any code performs; every gate that consumes entitlement does so by explicit
membership test. Introducing an ordering would invite `entitlement >= OBSERVED` checks whose meaning
drifts the next time a member is added.

Master's position, argued rather than deferred to: *a `bash`-obtained fact is more verifiable than a
memory-obtained one, because the command can be re-run and a memory node often cannot be
independently re-derived.* This ADR agrees on the merits and declines to act on it. The
`Entitlement` docstring records a live incident — an `Event` node reading *"Wednesday, July 1,
2026"*, a date the agent hallucinated, which entity extraction wrote to the graph and which then
passed all three D3 checks because **the source was the false claim**. We currently admit memory at
`EXTERNAL` with a demonstrated laundering incident, and excluded `bash` where laundering was
hypothetical. That inconsistency is real. Correcting it by *demoting memory* is a larger change with
its own blast radius and belongs to FRE-1302/FRE-1303's line of work, not here. `OBSERVED` sitting
below `EXTERNAL` is therefore a **provisional ordering recorded as such**, not a claim that retrieval
is epistemically superior to observation.

### D4 — Attachments are first-person observation; no surrogate

An attached image registers as a source of kind `OBSERVATION`, at `OBSERVED` entitlement.

- **D3(a) resolution** — resolves, like any registered source.
- **D3(b) reachability** — vacuous. The bytes are in the turn record; there is nothing to re-fetch,
  exactly as D2 already rules for turn-local tool evidence.
- **D3(c) containment** — **not applicable.** An image has no tokens. Spans citing an `OBSERVATION`
  source escalate to **inline entailment**, reusing the path D3 already runs for entity-free spans.

**No OCR or caption surrogate.** Registering a model-generated caption as the source's content and
citing it is the laundering shape D2 exists to close — the model's own words wearing a source's
identifier — and it would be the more dangerous member of that family, because a caption reads as
description rather than as assertion.

**The stated limit:** inline entailment over an image asks the same vision model that made the claim
whether the image supports it. That is thin, and it is recorded as a limit rather than presented as
a check. It is nonetheless strictly better than the status quo, where every image span scores
`UNCITED` for want of any source at all.

**The containment bypass keys on the registry's own `SourceKind`, never on anything the model can
write.** Skipping D3(c) is an exemption, and an exemption reachable from generated text is a hole: a
span that could nominate its own source kind — through a marker variant, a metadata field, or any
model-supplied string — would let a text claim buy its way out of containment by declaring itself an
observation. Today's citation format carries an identifier and nothing else, so the hole is not
reachable; this constraint exists to keep it that way as the format evolves. `SourceKind.OBSERVATION`
is assigned at registration by the executor and is not derivable from model output.

### D5 — What does not change

Scope discipline, stated so the amendment cannot widen by accident:

- **D1's default-deny stands.** Nothing here exempts a span from needing a citation.
- **D2's core stands.** The model's weights are never a source.
- **`perplexity_query`, `mcp_perplexity_ask`, `mcp_perplexity_reason`, `mcp_perplexity_research`,
  `mcp_research` and `mcp_sequentialthinking` remain categorically inadmissible** — enumerated
  rather than written `perplexity_*`, because a `frozenset` cannot express a wildcard and the
  membership must be transcribable without interpretation —
  — 4 of the 100 live refusals, correctly refused. Their exclusion is **not** the laundering rule
  and must not be made to follow it: another model's output is parametric knowledge from a
  different set of weights, so there is no world-determined result for a result-level check to
  admit. Because all of these share `ARBITRARY_CODE_TOOLS` with `bash` today, relaxing that one
  branch would widen admissibility to them by accident — which is why D2 splits the set rather than
  loosening its guard. AC-9 exists to catch this specific regression.
- **Memory entitlement is untouched.** FRE-1302 and FRE-1303's rules stand as written.
- **D4's block-retry-refuse loop keeps its shape.** What changes is that on a tool-driven turn the
  retry now has something to cite, so the loop terminates in an answer rather than in a refusal the
  evidence did not warrant.

### D6 — The declared threat model, and what it costs

**The model is careless, not adversarial.** This is a decision, not an assumption, and it is the
load-bearing premise of D2.

The cost, named rather than footnoted: **result-level containment is defeated by any composition
that emits the payload without the payload's tokens appearing in the invocation.** The Python
concatenation case is the obvious member —

```
bash(command="python -c \"print('Paris has ' + str(9) + ' million')\"")
```

— but review established that it is not the interesting one. **Shell provides the same escape
lexically, with no interpreter involved**, and it does so on the very command heads AC-1 probes:

```
printf '\x50aris has 9 million residents\n'
git log --pretty=format:%x50aris…
```

Character escapes, `$'…'` quoting, variable expansion, unquoted heredocs and string concatenation
all reproduce a target sentence while no token of it appears in the command line. **So the honest
statement of D2's reach is narrower than "it closes the laundering families":** it closes their
*literal* forms — the shapes a careless model actually produces — and it does not close their
*encoded* forms.

That is the price of the careless threat model, and it is why AC-1 tests literal-form probes and
says so, rather than claiming a coverage it does not have. Under an adversarial model this is a
hole, and Option 2 (typed wrappers only) is the correct design instead.

**The evidence supports the careless reading.** The observed failure is FRE-1327 — the model had
27,000 tokens of ground truth in context and generated figures matching none of it. That is
confabulation, not laundering. No laundering attempt, literal or encoded, appears in the 100
recorded refusals.

**And the amendment turns that failure legible.** Today FRE-1327's invented figures score `UNCITED`
— the same score as not trying. With the `bash` output registered, D3(c) checks those figures
against the bytes that were actually in context, and they are not there: the outcome becomes
`NOT_CONTAINED`, a **positive detection of invented figures against present ground truth.** The
change does not merely unblock compliance. It converts the system's blindest failure into its most
legible one, and `observed_span_outcomes` (D1) is where that detection surfaces.

**Nothing here excuses the confabulation itself** (FRE-1327), which remains a separate, open
failure. A citation-plumbing fix that made the fabrication *measurable* has not made it *rarer*, and
this ADR must not be read as though it had.
---

## Alternatives Considered

### Option 1: A conservative allowlist of shell command heads

**Description:** Admit `cat`, `git log`, `psql SELECT`, `curl`; exclude `echo`, `printf`, heredocs
and anything unclassifiable, failing closed.

**Pros:**
- Decidable without parsing arbitrary shell.
- Reads as conservative, and matches the intuition that some commands obviously read the world.

**Cons:**
- Three of those four heads have documented flag-level escape hatches.
- Requires a per-head denylist of flags, which is unbounded.

**Why Rejected:** Refuted in this repository before it was proposed. `source_registry.py:29-35`
enumerates `git log --pretty=format:`, `psql -c "SELECT '…'"` and `curl --write-out` as working
laundering channels and concludes that per-head flag denylisting "is the same unbounded chase one
level down." Adopting it would ship a rule the code's own docstring already refutes.

### Option 2: Typed wrappers only — keep the categorical exclusion, provision tools

**Description:** The status quo made explicit. `bash` stays inadmissible; every evidence source
gets a typed retrieval tool (`fetch_url` for `curl`, `read` for `cat`, and so on for Tempo,
Postgres, and whatever comes next).

**Pros:**
- Preserves the parameter-schema boundary exactly, with no new rule to review.
- Genuinely closes laundering, including the computed-obfuscation case D6 accepts.

**Cons:**
- An unbounded provisioning treadmill: every new data source needs a wrapper before it can ground
  anything.
- Leaves the metric confounded in the meantime — turns using an unwrapped source keep reading zero.
- Does not address vision at all.

**Why Rejected:** It is the correct answer under an **adversarial** threat model, and the owner
settled that question the other way (D6). Under a careless model it pays an unbounded cost to close
a hole nothing has been observed attempting, while the measured defect — 13 of 15 asserting turns
structurally uncitable — persists for as long as the treadmill runs.

### Option 3: Exempt tool-driven turns from the denominator

**Description:** Classify turns whose evidence is inadmissible as out of scope for the compliance
metric, as was proposed for vision on FRE-1316.

**Pros:**
- Cheapest option by a wide margin; needs no change to the admissibility rule.
- Makes the published metric honest immediately.

**Cons:**
- The system then asserts things about tool output with **no signal at all**.
- Removes the number that would reveal the defect, rather than the defect.

**Why Rejected:** It fails the ticket's own falsification clause — trading a measurable, honest 0%
for an unmeasurable silence. It also inverts ADR-0134 D1: a class of turn removed from measurement
is a class whose failures produce no evidence, which is the absence-is-invisible problem that ADR
was written against. D1 above keeps these turns measured, under a class of their own, and alerts on
the class.

### Option 4: Make `bash` admissible outright

**Description:** Delete `bash` and `run_python` from `ARBITRARY_CODE_TOOLS`.

**Pros:**
- One-line change; resolves the catch-22 immediately.

**Cons:**
- Reopens `printf 'Paris has 9 million residents'` in a single round-trip.

**Why Rejected:** It discards round 4 of ADR-0138's review, which closed precisely this channel.
The categorical exclusion over-closes; it is not wrong.

### Option 5: An OCR or caption surrogate for images

**Description:** Register a text rendering of the image — OCR output, or a generated caption — as
the source's content, so D3(c) containment has tokens to match.

**Pros:**
- Reuses containment unchanged; no new not-applicable branch.
- Cheaper per turn than inline entailment.

**Why Rejected:** A generated caption cited as evidence is the laundering shape exactly — the
model's own words wearing a source's identifier — and it is the more dangerous member of the family,
because a caption reads as neutral description rather than as assertion. OCR is narrower but covers
only images that are documents, leaving every photograph in the original hole while implying
coverage.

### Option 6: Sandboxed deterministic re-execution

**Description:** Re-run the command in a sandbox at verification time and admit the result only
where the two executions agree.

**Pros:**
- Would catch computed obfuscation, closing D6's residual.

**Cons:**
- Non-deterministic commands — anything touching time, network or live state — never agree, so the
  common case fails closed.
- Substantial per-turn latency and a sandbox on the turn path.

**Why Rejected:** ADR-0138 D2 already settled that turn-local tool evidence verifies against the
**recorded** result and never against a re-execution, precisely so a non-deterministic tool cannot
invalidate a citation after the fact. This option contradicts that ruling and would reopen it.

---

## Consequences

### Positive Consequences

- Tool-driven turns become answerable under `enforce`, removing the hard blocker on ADR-0138 D5's
  enforcement selection.
- FRE-1327's confabulation becomes a **positive detection** (`NOT_CONTAINED`) rather than a silence
  indistinguishable from apathy.
- The compliance metric measures compliance rather than "did this turn happen to reason from citable
  sources", which is the discrimination FRE-1285 keys enforcement on.
- A tool added without a classification now moves `uncitable_turn_rate` instead of moving nothing —
  the class of failure this ADR exists to close becomes self-reporting.
- **FRE-1306 is resolved rather than deferred.** The negative check reaches every source whose
  parameters can carry a program, so a composing `ROW`-only ES|QL query stops being citable without
  an ES|QL parser and without index-reading queries losing their citations — a fourth option beyond
  the three that ticket could see.
- Vision turns stop scoring zero by construction; FRE-1316 is absorbed rather than special-cased.
- One containment predicate serves both polarities, so the normalization contract, its tolerated
  variance classes and its false-rejection bar are inherited rather than re-litigated.

### Negative Consequences

- **Encoded composition remains open** (D6) — and it is wider than a Python one-liner: shell
  character escapes, `$'…'` quoting, variable expansion and unquoted heredocs all reproduce a
  sentence with none of its tokens in the command line, on the same heads AC-1 probes. Accepted,
  declared, and wholly dependent on the careless threat model holding.
- **A new false-rejection class: the exact-phrase search.** `rg 'fish high mercury' report.txt`
  returning *"This fish is high in mercury."* has every required content word in its own invocation,
  so `.contained` is true and the span is refused though the evidence is genuine. This is the direct
  cost of the `.contained` predicate and it is the common shape, not a corner. **It is recoverable
  by construction**: because the exclusion is per-source, D4's retry with a non-covering invocation
  — `cat report.txt` — succeeds. That recovery is the reason per-source scope is not merely tidier
  than turn-scoped, and AC-10 asserts it rather than assuming it.
- **Cross-call laundering through two `bash` calls is not closed** (D2). Inherited from `_taint`'s
  existing residual, which needs the tool layer to report its writes; accepted under D6.
- **A new false-rejection class**: a legitimate claim whose content words are covered by a command
  the model ran this turn — `grep 'Paris has 9 million residents' bigfile.txt` against a file that
  contains it. Conservative, and it counts against ADR-0138 AC-8's false-rejection bar.
- **Inline entailment volume grows** with every image-bearing turn, since image spans take the
  entity-free path.
- **Turn-scoped exclusion state grows** with the number of inadmissible calls in a turn, and every
  span-level check now runs the containment predicate twice.
- **`OBSERVED` below `EXTERNAL` is provisional** and rests on an inconsistency this ADR names but
  does not resolve (D3).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| The careless threat model is wrong, and laundering appears once `bash` is citable | High | `near_miss_markers` and the `NOT_CONTAINED` rate are both published per model; a laundering attempt raises neither, so AC-1's seeded probes are re-run at each enforcement promotion rather than once. Reverting is a one-line restoration of the categorical rule. |
| Admitting `bash` inflates compliance without any real check — the ticket's own falsification clause | High | AC-2 requires **both** arms on the same probe family: a claim present in the output passes, a claim absent from it scores `NOT_CONTAINED`. A vacuous implementation fails the second arm. |
| False rejections rise beyond the tolerated bar | Medium | Coverage semantics rather than word overlap (D2); measured against ADR-0138 AC-8's existing false-rejection bar, not a new one. |
| `uncitable_turn_rate` collapses by construction once results register, and is mistaken for proof the decision worked | **High** | Named in D1 as a **closure metric** and explicitly barred from AC-5, which keys on `observed_span_outcomes` instead — spans actually cited, checked and passed. The rate is retained only as the sentinel for the next unclassified tool. |
| The negative check is implemented on `ContainmentResult.passed`, silently readmitting entity-free laundering | **High** | D2 fixes the predicate as `.contained` and states why; AC-1's probe set includes an entity-free payload (`this fish is high in mercury`) whose rejection cannot be achieved by the `.passed` reading. |
| Relaxing the categorical branch widens admissibility to `perplexity_*`/`mcp_research`, which share the same frozenset | **High** | D2 splits the set as part of the decision rather than the implementation; AC-9 asserts both arms — generative tools register nothing, `run_python` is checked rather than blanket-refused. |
| Exclusion state carried across D4 attempts makes the retry loop unable to repair the turn it exists to repair | **High** | Invocation text is scoped to the source, never accumulated per turn (D2). AC-10 asserts the recovery directly: a refused covering-invocation span, retried with an independent retrieval, must pass. |
| `invocation_covered` is emitted as telemetry but never becomes a real verification outcome, so nothing blocks on it | Medium | Implementation notes require `CheckOutcome.INVOCATION_COVERED` as an enum member with defined blocking and retry-directive behaviour, not a log field; AC-1 asserts the outcome by name, so a log-only implementation fails it. |
| The alert is authored but never fires, and nobody notices | Medium | AC-6 requires the rule to be **observed transitioning to Alerting** on a seeded turn. An untested alert rule is not an alert. |
| Vision entailment is the model marking its own homework | Medium | Recorded as a stated limit (D4), with a negative arm in AC-8; promotion to a second model is left to the eval program (ADR-0087) rather than assumed here. |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/grounding/source_registry.py` — `ARBITRARY_CODE_TOOLS` **splits** into
  `MODEL_AUTHORED_CODE_TOOLS` and `GENERATIVE_TOOLS`; only the former loses its categorical branch,
  and `GENERATIVE_TOOLS` keeps it verbatim. `Entitlement` gains `OBSERVED`, `SourceKind` gains
  `OBSERVATION`, and a code-level ordering is **not** added (D3).
- `src/personal_agent/grounding/containment.py` — the coverage predicate is exposed for the negative
  polarity; no change to the normalization contract. Callers of the negative polarity must consume
  `ContainmentResult.contained`, not `.passed` (D2).
- `src/personal_agent/grounding/verification.py` — `CheckOutcome` gains **`INVOCATION_COVERED`** as
  a real member, not a telemetry string: it must carry defined blocking behaviour, a D4 retry
  directive, and serialization alongside the existing outcomes, and it belongs with
  `_TRUE_NO_SOURCE`'s siblings in the "the contract is working" family rather than with the
  normalizer limits. Span checks gain the negative-containment clause for `OBSERVED` sources,
  evaluated against **the cited source's own recorded invocation**; `OBSERVATION` sources route to
  the inline-entailment path, keyed on the registry-assigned `SourceKind` only.
- `src/personal_agent/grounding/source_registry.py` (cont.) — `RegisteredSource` retains the
  invocation text that produced it. The executor already passes it (`_register_tool_source` takes
  `arguments` alongside `content`, and its call site notes "the arguments travel with the content"),
  so this is retention, not new plumbing. `_tainted` keeps its existing write-then-read job and is
  **not** repurposed.
- `src/personal_agent/orchestrator/executor.py` — attachment registration;
  `grounding_verification_completed` gains D1's fields, including `observed_span_outcomes` split by
  outcome; near-miss marker counting.
- `src/personal_agent/grounding/citations.py` — the near-miss pattern, deliberately narrow:
  citation-shaped, containing `@`, failing `CITATION_MARKER_PATTERN`.
- `config/grafana/dashboards/` — a grounding panel set, built in the Grafana UI and exported per
  ADR-0090 D3; alert rules exported as version-controlled configuration per ADR-0134 D2.

**Sequencing.** D1 ships first and alone. It is independent of the admissibility debate, and it
establishes the pre-change baseline against which D2–D4 are judged. Changing the admissibility rule
first would leave us with no instrument capable of reading the result.

**Preregistered baseline**, measured 2026-08-29 against `agent-logs-2026-08`, recorded here before
any change is made: 2 of 222 non-exempt spans passed (0.9%); 13 of 15 asserting turns scored zero;
100 `model_authored_invocation` refusals, 96 of them `bash`; `UNRESOLVED` observed in 0 documents.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

> **Adjudication:** on the umbrella, **FRE-1328**, once the implementation chain has landed and
> deployed. No seam ticket (ADR-0130 superseded 2026-08-18).

Numeric bars are fixed in the implementation ticket that builds each check and **recorded before
results are seen**; the invariants and their falsification conditions live here. Probe sets are held
out and sampled at adjudication time, so an implementation cannot special-case probes it has not
seen.

**The suite as a whole rejects a blanket-refusal implementation, and that claim is deliberately
scoped.** Round 1 established that these criteria were satisfiable by an implementation that rejects
everything — categorical refusal passes any purely negative test, and categorical refusal is the
*status quo* this ADR exists to change. Round 2 then falsified the over-correction: **AC-4, AC-6 and
AC-7 do not carry positive arms and cannot**, because they test diagnosability, alert wiring and
marker parsing — properties independent of whether any source is ever admitted. The
admission-dependent controls live in **AC-1, AC-2, AC-3, AC-5, AC-8, AC-9 and AC-10**, and
adjudication requires all ten; no individual criterion is claimed to do work it cannot do.

- **AC-1 — Literal-form laundering is closed, for the stated reason, without closing everything.**
  Seeded probes on `MODEL_AUTHORED_CODE_TOOLS` — `printf`, `echo`, a heredoc, `find -printf`,
  `git log --pretty=format:`, `psql -c "SELECT '…'"`, `curl --write-out`, each emitting a fabricated
  world-fact **as a literal** — yield spans recorded as `invocation_covered`. **Positive control on
  the same tool:** a `cat`/`git log` probe whose asserted value is genuinely in the output and
  absent from the command line yields `passed`. · **Check:** seeded probe set through
  `register_tool_result` → `verify_turn`; assert the per-span outcome, not merely pass/fail. ·
  *Fails if* any laundering probe passes; **or** if the positive control does not pass (blanket
  rejection dressed as a fix); **or** if a laundering probe is rejected with any outcome other than
  `invocation_covered` (right answer, wrong reason — indistinguishable from the tool being excluded
  again). *Scope, per D6:* "literal-form" is defined **operationally, not aesthetically** — a probe is
  literal-form iff `check_containment(span, invocation).contained` is `True` for the serialized
  invocation before execution. That makes AC-1 a wiring test of the boundary D2 actually draws,
  rather than a claim about which human-readable constructions count as literal. Encoded forms
  (`printf` with hex escapes, `--pretty=format:%x50`, concatenation, ANSI-C quoting, variable
  expansion, unquoted heredocs) fail that predicate and are **out of scope by decision**, not
  oversight; a criterion claiming to close them would be false.

- **AC-2 — Real tool evidence becomes citable, and containment still bites.** On one probe family
  built from the same `bash` output: **(a)** an assertion whose value appears in the output scores
  `PASSED`; **(b)** an assertion about the same output whose value does **not** appear in it scores
  `NOT_CONTAINED`. · **Check:** paired probes, both arms required on every family. · *Fails if*
  arm (b) passes — the ticket's own falsification clause, an unmeasurable 100% replacing an honest
  0% — or if arm (a) fails.

- **AC-3 — Confabulation against present evidence is detected, and not only on the case we named.**
  Replaying trace `dba5b2cba1e0bece6c8b9396465a265c`'s recorded tool output and generated response
  through the amended path, the fabricated trace-metric figures score `NOT_CONTAINED`; **and** the
  same detection holds across a held-out set of recorded turns, not enumerated here, in which the
  response asserts figures absent from the turn's own tool output. · **Check:** offline replay
  against stored turn records; tool content comes from the record, never re-executed. · *Fails if*
  those spans still score `UNCITED`; **or** if the named trace passes while the held-out set does
  not — the signature of an implementation special-cased to the one trace this ADR cites.

- **AC-4 — A zero-compliance turn is diagnosable from its own document.** Over a post-deploy window
  containing **at least the preregistered minimum** of turns with `passed_spans == 0` (seeded if the
  natural rate is below it), `turn_evidence_class` and the `tool_results_*` counts on that same
  document determine whether a non-zero was reachable, and `tool_results_admitted` reconciles
  against the source list in that trace's `source_registry_snapshot`. · **Check:** ES query over
  `grounding_verification_completed`, reconciled against `source_registry_snapshot` on `trace_id`. ·
  *Fails if* the window contains fewer than the minimum such turns (the criterion is vacuous over an
  empty set and must not be reported as met); **or** if any such turn requires the DEBUG refusal
  event to classify; **or** if `tool_results_admitted` disagrees with the snapshot's source count.
  *Note:* reconciliation uses the snapshot, not the refusal events — no event fires on **successful**
  registration, so a refusal-only join cannot validate the admitted count.

- **AC-5 — The evidence is not merely registered, it is used and checked.** On a post-deploy window:
  spans citing an `OBSERVED` source are a material fraction of all non-exempt spans; their `passed`
  rate is materially above zero; `invocation_covered` is non-zero across the seeded probe set; and
  `not_contained` meets a **preregistered non-zero rate over a held-out set of deliberately
  mismatched probes** — claims about a tool result that the result does not support. Without that
  last arm an implementation can run only the negative invocation check, skip result containment
  entirely, and still show high `passed` plus seeded `invocation_covered`. ·
  **Check:** the `observed_span_outcomes` aggregation from D1. · *Fails if* `OBSERVED` spans exist
  but none pass (registration without usability); **or** if `invocation_covered` is zero across
  AC-1's probes while `passed` is high (registration without checking — the vacuous implementation);
  **or** if `passed` is high while AC-2 arm (b) regresses. **`uncitable_turn_rate`'s fall is
  deliberately *not* a criterion here**: D1 records that it collapses by construction once
  arbitrary-code results register, so keying on it would guarantee a pass while measuring nothing.
  The 13/15 baseline anchors the *problem statement*, not this criterion — it measures "zero passed
  spans", a different predicate from `uncitable`, and is reported alongside a recomputation of the
  same window under the shipped definition rather than compared to it directly.

- **AC-6 — The alert fires.** The `uncitable_turn_rate` rule is observed transitioning to `Alerting`
  when a seeded turn drives the metric past its threshold, and back to `Normal` afterwards. ·
  **Check:** drive the metric with a seeded turn; capture the rule's state transitions in Grafana. ·
  *Fails if* the rule has never been observed firing, or if its no-data behaviour was left at the
  default (ADR-0134 D1: a rule authored without deciding its no-data behaviour has decided it, and
  the default is silence).

- **AC-7 — Near-miss detection is discriminating in both directions.** The detector fires on the
  recorded string `[S@bash-tempo-trace-dba5b2]`, and its false-positive rate over a held-out corpus
  of legitimate turn outputs is at or below a **preregistered bar** fixed in the implementing ticket
  and justified against a deliberately broken baseline. · **Check:** unit assertion on the recorded
  string, plus a precision run over the held-out corpus. · *Fails if* it misses the recorded string,
  or if it exceeds the bar. *An absolute zero-false-positive requirement is deliberately not used:*
  over a finite corpus it is unfalsifiable in one direction and, once breached in production, invites
  muting — the ADR-0134 D4 path.

- **AC-8 — Image spans are checked, in both directions.** On image-attached probes: **(a)** a span
  asserting content genuinely present in the image scores `PASSED`; **(b)** a span asserting content
  **absent** from the image does not. · **Check:** paired image probes, both arms required. ·
  *Fails if* arm (a) fails — an entailment checker that always rejects satisfies the negative arm
  alone and delivers nothing — or if any span about an attached image scores `UNCITED` for want of a
  registered source.

- **AC-9 — Scope did not widen, and did not collapse either.** Each of `perplexity_query`,
  `mcp_perplexity_ask`, `mcp_perplexity_reason`, `mcp_perplexity_research`, `mcp_research` and
  `mcp_sequentialthinking` registers **no** source. `bash`, `run_python`, `mcp_browser_evaluate` and
  `mcp_browser_run_code` each admit a genuine non-laundering result under the two-polarity rule. ·
  **Check:** probe every named tool; assert `admissibility` on refusals and per-span outcome on
  admissions. · *Fails if* any generative tool registers a source; **or** if **any** member of
  `MODEL_AUTHORED_CODE_TOOLS` — `bash` included — is categorically rejected rather than checked,
  which is the status quo passing as the decision.

- **AC-10 — A refused covering invocation is repairable by retry.** A turn whose first attempt
  asserts from a covering invocation (`rg 'fish high mercury' report.txt`) is refused
  `INVOCATION_COVERED`; the D4 retry, issuing an independent non-covering retrieval
  (`cat report.txt`) over the same underlying evidence, **passes**. · **Check:** a two-attempt probe
  driven through the real D4 loop, not a unit call to the checker. · *Fails if* the second attempt
  is refused for the first attempt's invocation — the turn-scoped-exclusion defect round 2 found —
  **or** if the first attempt is not refused, which would mean the negative check never fired and
  the probe proved nothing.


## References

- [ADR-0138](ADR-0138-the-model-may-generate-but-may-not-assert.md) — the grounding contract this
  amends: D1 (default-deny), D2 (independence), D3 (the three checks), D4 (block-retry-refuse),
  D5 (tier-invariance and enforcement selection)
- [ADR-0134](ADR-0134-activity-alerting-absence-as-a-first-class-signal.md) — *Proposed* — D1
  (absence and shortfall; family dispositions), D2 (alerting is platform configuration, no notifier
  in `src/`). Its vocabulary is adopted here; the binding constraint is inherited from ADR-0090 D3
  instead — see D1
- [ADR-0090](ADR-0090-telemetry-surface-contract.md) — *Accepted* — D3: dashboards are
  version-controlled files, provisioned from `config/grafana/dashboards/` (amended 2026-08-08,
  FRE-1213)
- [ADR-0087](ADR-0087-memory-recall-quality-measurement-program.md) — *Accepted* — the eval program
  that owns held-out probe sets and the offline entailment arm
- [ADR-0028](ADR-0028-external-tool-cli-migration.md) — *Accepted, Implemented 2026-04-04* — tool
  integration tiers; `fetch_url` (FRE-1297) was provisioned under Phase 3 for D2
- `src/personal_agent/grounding/source_registry.py:29-50` — the parameter-schema boundary, the
  rejected head-allowlist and its worked counterexamples
- `src/personal_agent/grounding/citations.py:45` — `CITATION_MARKER_PATTERN`
- `src/personal_agent/grounding/verification.py:96-121` — `CheckOutcome` and `_TRUE_NO_SOURCE`
- Trace `dba5b2cba1e0bece6c8b9396465a265c` — four `bash` refusals and a seven-source registry holding
  none of the turn's evidence
- [FRE-1328](https://linear.app/frenchforest/issue/FRE-1328) — umbrella; adjudicates these criteria
- [FRE-1316](https://linear.app/frenchforest/issue/FRE-1316) — vision-derived assertions; absorbed
  here as a member of this class
- [FRE-1327](https://linear.app/frenchforest/issue/FRE-1327) — the confabulation case study; made
  detectable by D2, **not** resolved by it
- [FRE-1284](https://linear.app/frenchforest/issue/FRE-1284) — the compliance metric this
  de-confounds
- [FRE-1285](https://linear.app/frenchforest/issue/FRE-1285) — enforcement selection, which keys on
  that metric
- [FRE-1302](https://linear.app/frenchforest/issue/FRE-1302) · [FRE-1303](https://linear.app/frenchforest/issue/FRE-1303)
  — the memory entitlement axis, untouched here and named in D3's provisional ordering
- [FRE-1306](https://linear.app/frenchforest/issue/FRE-1306) — `mcp_esql` admits a model-authored
  literal; resolved by D2's negative check, which needs neither a parse nor a select/compose line
- [FRE-1325](https://linear.app/frenchforest/issue/FRE-1325) — nothing consumes the grounding signal

---

## Status Updates

### 2026-08-29 - Proposed
**Changed By:** `adr` session, on FRE-1328
**Reason:** Drafted after owner discussion settled three questions: the threat model is **careless**
rather than adversarial (D6); this is a **new ADR** rather than an amendment to ADR-0138's status
line; and an OCR/caption **surrogate for vision is rejected** as the laundering shape (D4, Option 5).
The monitoring strategy was the owner's own addition to scope and is placed first, as D1.

### 2026-08-29 - Review round 1 (Codex, adversarial)

**Changed By:** `adr` session, on FRE-1328
**Reason:** Seven blocking findings, all verified against source before acting on them.

- **D2's rule was overclaimed.** Shell reproduces a payload lexically — `printf '\x50aris…'`,
  `--pretty=format:%x50` — so the escape hatches do *not* all require the tokens in the invocation.
  D6 now states the reach honestly (literal forms closed, encoded forms out of scope by decision)
  and AC-1 scopes its probes to match rather than claiming coverage it lacks.
- **The negative check was specified vacuously.** `ContainmentResult.passed` is
  `outcome is CONTAINED`, but an entity-free span resolves to `ENTAILMENT_REQUIRED`, so
  `not result.passed` would have readmitted `printf 'this fish is high in mercury'` — ADR-0138's
  own round-2 vacuity, one polarity over. D2 now fixes the predicate as `.contained` and says why.
- ~~**The exclusion set had a concurrency bug.**~~ **Retracted in round 2 — this finding was
  wrong and the fix it produced was worse than the defect.** Dispatch is concurrent but
  *registration* is sequential and ordered ("Phase 3: Sequential record + result assembly … results
  are appended in `allowed_plans` order"), so there was no ordering problem. The turn-scoped set it
  introduced then broke D4: see the round-2 entry.
- **`uncitable_turn_rate` was a dead metric.** It collapses by construction once results register,
  so the original AC-5 was guaranteed to pass while measuring nothing — the ticket's own
  falsification clause. D1 now labels it a closure metric; `observed_span_outcomes` carries the
  steady-state signal and AC-5 keys on it.
- **The scope leak was real.** `bash`, `run_python` and `perplexity_*`/`mcp_research` share one
  `ARBITRARY_CODE_TOOLS` frozenset, so "stop short-circuiting on it" would have admitted generated
  prose as a source. D2 splits the set as part of the decision; AC-9 tests both arms.
- **Five of nine criteria admitted a blanket-refusal implementation** (AC-1, AC-3, AC-4, AC-8,
  AC-9). Every criterion now carries a positive arm, since categorical refusal *is* the status quo.
- **Forward-looking:** the containment bypass for images must key on the registry-assigned
  `SourceKind`, never on model-supplied text (D4).

Also folded in: **FRE-1306** is resolved by D2's negative check — a fourth option beyond the three
that ticket could see, needing neither an ES|QL parse nor a select/compose distinction.

### 2026-08-29 - Review round 2 (Codex, adversarial — on the round-1 deltas)

**Changed By:** `adr` session, on FRE-1328
**Reason:** Four blocking findings, all against **round 1's fixes** rather than the original draft —
the failure mode this ADR's predecessor exhibited in all four of its own rounds. Both code facts
were re-verified in source before acting.

- **Round 1's concurrency finding was false, and its fix broke D4.** `executor.py` dispatches
  concurrently but registers in "Phase 3: Sequential record + result assembly", appending "in
  `allowed_plans` order" — there was no nondeterminism. Worse, the turn-scoped exclusion set that
  finding motivated persists across D4 retries (one registry per turn, `_register` documenting that
  "the D4 retry loop does [re-register] by construction"), so a first attempt's `rg 'fish high
  mercury' report.txt` would keep excluding the evidence on every retry — **the retry could never
  repair the turn**, which is the one thing D4 exists to do. Invocation text is now scoped to the
  **source**, not the turn, and AC-10 asserts the repair directly.
- **The cross-call residual is now stated instead of accidentally covered.** Per-source scope does
  not see `bash("echo 'X' > /tmp/f")` followed by `bash("cat /tmp/f")`; that stays with `_taint`,
  which catches it only for discrete recurring argument values, never for two whole command lines.
  `_reads_tainted`'s own docstring already names the remedy (the tool layer must report its writes);
  this ADR inherits the residual rather than pretending to close it.
- **`.contained`'s false-rejection class is named and made recoverable.** An exact-phrase search
  whose every content word is in its own invocation is refused though genuine. It is the common
  shape, not a corner — and it is recoverable precisely *because* exclusion is per-source: the D4
  retry with `cat report.txt` succeeds. That recovery is AC-10's positive arm.
- **`invocation_covered` needed to be a real outcome.** It appeared only as a telemetry string;
  `CheckOutcome` has no such member, so nothing would have blocked on it. It is now specified as an
  enum member with blocking and retry-directive behaviour, and AC-1 asserts the outcome by name so a
  log-only implementation fails.
- **Round 1's "every criterion carries a positive arm" was itself an overclaim.** AC-4, AC-6 and
  AC-7 test diagnosability, alert wiring and marker parsing — none of which can carry one. The
  preamble now scopes the claim to the suite and names which seven criteria are admission-dependent.
- Also: `observed_span_outcomes` unified (the draft used two names for one field); the generative
  tools enumerated rather than written `perplexity_*`, since a `frozenset` has no wildcards; AC-9's
  failure clause widened from `run_python` to every `MODEL_AUTHORED_CODE_TOOLS` member.
