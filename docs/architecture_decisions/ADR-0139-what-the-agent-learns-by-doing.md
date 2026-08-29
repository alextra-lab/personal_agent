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

### D1 — The compliance metric gets a denominator, and three signals replace one number

`grounding_verification_completed` carries, on every turn where verification ran:

| Field | Meaning |
|---|---|
| `turn_evidence_class` | `no_assertions` · `uncitable` · `citable` |
| `tool_results_offered` | Tool results the executor presented to the registry this turn |
| `tool_results_admitted` | How many registered as sources |
| `near_miss_markers` | Citation-shaped strings that failed `CITATION_MARKER_PATTERN` |

`uncitable` is defined mechanically: **non-exempt spans exist, at least one tool result was offered,
and none was admitted.** A turn that called no tools and asserted anyway is **not** `uncitable` — it
is a genuine no-source turn and stays in the denominator, so a model reasoning from its weights
cannot hide behind this class.

**Putting these on the verification event, rather than in a second event, is the decision.** It
removes the join. Each document becomes self-diagnosing: a zero-compliance turn states, in its own
record, whether a non-zero was reachable.

**Compliance is then reported only over `citable` turns**, and two further numbers are published
alongside it rather than folded into it:

| Signal | Detects | ADR-0134 D1 disposition |
|---|---|---|
| `citation_compliance_rate` over `citable` turns | model carelessness | **correlated** — denominator is citable asserting turns |
| `uncitable_turn_rate` over asserting turns | this ADR's defect, and any future tool landing unclassified | **correlated** — denominator is asserting turns |
| `near_miss_markers` | confabulation under compliance pressure | **conditional** — silence is health; no stoppage rule |

The dispositions are not decoration. ADR-0134 D1 warns that a stoppage rule over a
*conditional* family alerts on a working system, which is the muting path its D4 exists to prevent.
`near_miss_markers` is rare by design and must never carry one.

**`uncitable_turn_rate` is this ADR's own regression test.** It is the number that should collapse
when D2 lands, and the number that will rise on its own the next time a tool is added without a
classification — a failure that is silent today.

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

An arbitrary-code tool's result **is registered as a source**. Its arguments are retained as an
**exclusion set**, and the anti-laundering rule becomes a second, opposite-polarity application of
the containment machinery D3(c) already has:

> A span citing such a source passes only where the span is contained in the **result** and is
> **not** contained in the **invocation** — where "contained in the invocation" uses D3(c)'s own
> containment unit, unchanged.

Symmetry is the point. One predicate, applied twice, opposite sign. `printf 'Paris has 9 million
residents'` fails because the arguments cover every content word of the span. 27,000 tokens of Tempo
output passes because "Paris" was never in the command line.

**The negative check runs against the turn's accumulated argument text, not just this call's.**
Otherwise the two-call shape defeats it — `bash("echo 'X' > /tmp/f")` emits no stdout, then
`bash("cat /tmp/f")` returns `X`, whose content appears in an *earlier* call's arguments. The
registry already accumulates exactly this text in `_tainted` for the existing write-then-read rule;
the exclusion set reuses it.

**Using D3(c)'s coverage predicate rather than "any word overlaps" is load-bearing.** A rule
rejecting a span because *any* of its words appear in the arguments would reject
`grep 'mercury' file.txt` followed by any assertion about mercury — search commands legitimately
contain the search term, and that rule would manufacture refusals on the single most common
evidence-gathering shape. Coverage asks whether the arguments **alone** would satisfy containment
for that span. `grep 'mercury' file.txt` does not cover *"this fish is high in mercury"*; `printf
'Paris has 9 million residents'` covers its span exactly.

The residual is `grep 'Paris has 9 million residents' bigfile.txt` against a file that genuinely
contains the line: arguments cover the span, so it is rejected though the evidence is real. That is
a false rejection, it is conservative, the model can `cat` the file instead, and it is accepted here
rather than discovered later.

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

### D5 — What does not change

Scope discipline, stated so the amendment cannot widen by accident:

- **D1's default-deny stands.** Nothing here exempts a span from needing a citation.
- **D2's core stands.** The model's weights are never a source.
- **`perplexity_*` and `mcp_research` remain inadmissible** — 4 of the 100 live refusals, correctly
  refused. Their exclusion is **not** the laundering rule and does not follow it: another model's
  output is parametric knowledge from a different set of weights. Result-level admissibility does
  not reach them, because there is no world-determined result to admit.
- **Memory entitlement is untouched.** FRE-1302 and FRE-1303's rules stand as written.
- **D4's block-retry-refuse loop keeps its shape.** What changes is that on a tool-driven turn the
  retry now has something to cite, so the loop terminates in an answer rather than in a refusal the
  evidence did not warrant.

### D6 — The declared threat model, and what it costs

**The model is careless, not adversarial.** This is a decision, not an assumption, and it is the
load-bearing premise of D2.

The cost, named rather than footnoted: **computed obfuscation defeats result-level containment.**

```
bash(command="python -c \"print('Paris has ' + str(9) + ' million')\"")
```

Its output appears nowhere in the argument string, so the negative containment check passes and the
laundered claim is citable. Under an adversarial model this is a hole. Under a careless one it is
not a shape that occurs: a careless model does not construct string concatenation to disguise a
fact — it simply answers from its weights, which D1's default-deny already catches, or from what it
read, which is the case this ADR makes citable.

**The evidence supports the careless reading.** The observed failure is FRE-1327 — the model had
27,000 tokens of ground truth in context and generated figures matching none of it. That is
confabulation, not laundering. No laundering attempt has been observed in 100 recorded refusals.

**And the amendment turns that failure legible.** Today FRE-1327's invented figures score `UNCITED`
— the same score as not trying. With the `bash` output registered, D3(c) checks those figures
against the bytes that were actually in context, and they are not there: the outcome becomes
`NOT_CONTAINED`, a **positive detection of invented figures against present ground truth.** The
change does not merely unblock compliance. It converts the system's blindest failure into its most
legible one.

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
- Vision turns stop scoring zero by construction; FRE-1316 is absorbed rather than special-cased.
- One containment predicate serves both polarities, so the normalization contract, its tolerated
  variance classes and its false-rejection bar are inherited rather than re-litigated.

### Negative Consequences

- **Computed obfuscation remains open** (D6). Accepted, declared, and dependent on the careless
  threat model holding.
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
| `uncitable_turn_rate` falls because the class definition was widened rather than the defect fixed | Medium | The definition is fixed in D1 and its mechanical form is asserted in AC-4; AC-5 requires the fall to co-occur with AC-2's negative arm still holding. |
| The alert is authored but never fires, and nobody notices | Medium | AC-6 requires the rule to be **observed transitioning to Alerting** on a seeded turn. An untested alert rule is not an alert. |
| Vision entailment is the model marking its own homework | Medium | Recorded as a stated limit (D4), with a negative arm in AC-8; promotion to a second model is left to the eval program (ADR-0087) rather than assumed here. |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/grounding/source_registry.py` — `Entitlement` gains `OBSERVED`; `SourceKind`
  gains `OBSERVATION`; `register_tool_result` stops short-circuiting on `ARBITRARY_CODE_TOOLS` and
  registers with the turn-scoped exclusion set attached; `_tainted` is promoted from a
  write-then-read guard to the exclusion set's backing store.
- `src/personal_agent/grounding/containment.py` — the coverage predicate is exposed for the negative
  polarity; no change to the normalization contract.
- `src/personal_agent/grounding/verification.py` — span checks gain the negative-containment clause
  for `OBSERVED` sources; `OBSERVATION` sources route to the inline-entailment path.
- `src/personal_agent/orchestrator/executor.py` — attachment registration;
  `grounding_verification_completed` gains D1's four fields; near-miss marker counting.
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
results are seen**; the invariants and their falsification conditions live here. Probe sets are
held out and sampled at adjudication time, so an implementation cannot special-case probes it has
not seen.

- **AC-1 — Laundering stays closed under result-level admissibility.** Every seeded laundering probe
  — `printf`, `echo`, a heredoc, `find -printf`, `git log --pretty=format:`, `psql -c "SELECT '…'"`,
  `curl --write-out`, each emitting a fabricated world-fact the model then cites — yields a span
  that does **not** pass. · **Check:** seeded probe set run against the live verification path;
  assert per-span outcome. · *Fails if* any laundering probe yields `PASSED`.

- **AC-2 — Real tool evidence becomes citable, and containment still bites.** On one probe family
  built from the same `bash` output: **(a)** an assertion whose value appears in the output scores
  `PASSED`; **(b)** an assertion about the same output whose value does **not** appear in it scores
  `NOT_CONTAINED`. · **Check:** paired probes, both arms required on every family. · *Fails if* arm
  (b) passes — that is the ticket's own falsification clause, an unmeasurable 100% replacing an
  honest 0%.

- **AC-3 — The recorded confabulation is detected.** Replaying trace
  `dba5b2cba1e0bece6c8b9396465a265c`'s recorded tool output and generated response through the
  amended verification path, the fabricated trace-metric figures score `NOT_CONTAINED`. · **Check:**
  offline replay against the stored turn record; the tool content must come from the record, not be
  re-executed. · *Fails if* those spans still score `UNCITED`, or if the replay cannot distinguish
  the fabricated figures from figures actually present in the output.

- **AC-4 — A zero-compliance turn is diagnosable from its own document.** For every turn in a
  held-out post-deploy window where `passed_spans` is 0, `turn_evidence_class` and the two
  `tool_results_*` counts on that same document determine whether a non-zero was reachable. ·
  **Check:** ES query over `grounding_verification_completed` alone, no join. · *Fails if* any such
  turn requires joining `source_registry_tool_inadmissible` to classify, or if `turn_evidence_class`
  disagrees with the class recomputed from the joined refusal events.

- **AC-5 — The structural failure rate falls, without the metric being widened to hide it.**
  `uncitable_turn_rate` over a post-deploy window of comparable asserting-turn count falls materially
  against the preregistered baseline of 13/15. · **Check:** the same ES aggregation used to establish
  the baseline. · *Fails if* the rate does not fall; **or** if it falls while AC-2's arm (b)
  regresses; **or** if the fall is attributable to a change in the `uncitable` definition rather than
  in admitted evidence — checked by recomputing the baseline window under the shipped definition.

- **AC-6 — The alert fires.** The `uncitable_turn_rate` rule is observed transitioning to `Alerting`
  when a seeded turn drives the metric past its threshold, and back to `Normal` afterwards. ·
  **Check:** drive the metric with a seeded turn; capture the rule's state transitions in Grafana. ·
  *Fails if* the rule has never been observed firing, or if its no-data behaviour was left at the
  default (ADR-0134 D1: a rule authored without deciding its no-data behaviour has decided it, and
  the default is silence).

- **AC-7 — Near-miss detection is discriminating in both directions.** The detector fires on the
  recorded string `[S@bash-tempo-trace-dba5b2]` and does not fire across a held-out corpus of
  legitimate turn outputs. · **Check:** unit assertion on the recorded string, plus a precision run
  over the held-out corpus. · *Fails if* it misses the recorded string, or if it fires on any
  legitimate output — a detector with false positives will be muted, which is the ADR-0134 D4 path.

- **AC-8 — Image spans are no longer uncitable by construction, and are still checked.** On an
  image-attached probe: spans about the image resolve to the `OBSERVATION` source and reach an
  entailment outcome; and a span asserting content **absent** from the image does not score
  `PASSED`. · **Check:** paired image probes, both arms. · *Fails if* any span about an attached
  image scores `UNCITED` for want of a registered source, or if the absent-content arm passes.

- **AC-9 — Scope did not widen.** `perplexity_query`, `mcp_perplexity_*` and `mcp_research` register
  no source, and `run_python` is admitted only under the same two-polarity rule as `bash`. ·
  **Check:** probe each; assert `admissibility` on the recorded refusal. · *Fails if* any of them
  registers a source, or if `run_python` results bypass the negative-containment clause.

---

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
- [FRE-1325](https://linear.app/frenchforest/issue/FRE-1325) — nothing consumes the grounding signal

---

## Status Updates

### 2026-08-29 - Proposed
**Changed By:** `adr` session, on FRE-1328
**Reason:** Drafted after owner discussion settled three questions: the threat model is **careless**
rather than adversarial (D6); this is a **new ADR** rather than an amendment to ADR-0138's status
line; and an OCR/caption **surrogate for vision is rejected** as the laundering shape (D4, Option 5).
The monitoring strategy was the owner's own addition to scope and is placed first, as D1.
