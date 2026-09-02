# ADR-0139: What the Agent Learns by Doing — Result-Level Admissibility, a First-Person Observation Tier, and a Denominator for the Compliance Metric

**Status:** Proposed — **partially withdrawn 2026-09-02** (review round 6, FRE-1357). **Live: D1
(as amended), D4, D5 and the new D8.** **D2, D3 and D7 are withdrawn**, and **D6 is retired**, under
[ADR-0140](ADR-0140-the-model-is-not-a-security-boundary.md). **The revision table below is the
single authority on what is live**; this line is a summary and loses to it on any disagreement. The
premise: the model is not a security boundary,
so admissibility is a capability property and may not rest on a content predicate over a tool's own
result. The withdrawn sections are retained below as record, each under a withdrawal banner.
ADR-0138 D2's parameter-schema boundary — its **invocation** axis — is **restored, not amended**;
D2's separate **authorship** axis stays narrowed by ADR-0098 Amendment A §A6 (FRE-1347, Approved and
untouched here). This ADR's amendment of the invocation axis
lapses with D2.
**Date:** 2026-08-29
**Deciders:** Project owner (design), `adr` session (drafting)
**Tags:** grounding, hallucination, citations, observability, alerting, vision

---

## Revision note — what stands, what is withdrawn (2026-09-02, FRE-1357)

| Decision | Status | Why |
|---|---|---|
| **D1** — denominator and the signal family | **Stands.** In implementation (FRE-1332, FRE-1333) | An instrument, not a boundary. It is what makes the replacement path orderable |
| **D2** — result-level admissibility, three-arm invocation check | **Withdrawn** | A model-layer control standing as the boundary for the invariant (ADR-0140 T3). Round 6 measured its cost: arm 2 rejects the whole source on `grep 'passed_count' logs.json`, `rg 'source_registry tool' src/`, `git log --grep='cost gate'`, a `psql` column header and an ES\|QL `KEEP` projection — contradicting this ADR's own AC-11 |
| **D3** — the `OBSERVED` tier and the address-bound terminus | **Withdrawn as written.** The `OBSERVED` tier itself **survives, scoped to D4** | The tier is a capability fact where the harness received the bytes (attachments). The address rule was undecidable by the means this ADR permits — extracting a read's target from a command line needs the shell parse it refuses everywhere else |
| **D4** — attachments are first-person observation | **Stands.** Untouched by all six round-6 findings | An attachment is caller-supplied; the model did not author the bytes. Admissible at the capability layer |
| **D5** — generative tools stay excluded | **Stands**, and is now the general rule rather than an exception |
| **D6** — the declared threat model | **Retired.** Superseded by [ADR-0140](ADR-0140-the-model-is-not-a-security-boundary.md) | A program-level premise cannot live inside one consumer; and its axis (careless vs adversarial) was the wrong axis |
| **D8** — the replacement path: typed retrieval, provisioned on demand | **Live**, added by this revision | It is what D2 and D3 are replaced *by*. Its premise is ADR-0140 T4; the application — which tools, in what order — is stated here because it is a question about this problem |
| **D7** — near-miss markers | **Withdrawn except its first row.** A near-miss scores `UNRESOLVED` rather than `UNCITED`; the resolver, rows 2–3 and `MALFORMED_CITATION` are dropped | Row 1 needs no resolution and grants no admissibility, so it is a pure observability gain. The rest was unreachable (`_identifier_for` short-circuits at `verification.py:309` before any of it) and non-discriminating |

**The replacement path is D8, below.** The measured defect D2 was written to close — 2 of 222 spans
passing — is **not** closed by this revision; it is made visible and ordered instead. That trade is
stated plainly rather than softened: see ADR-0140's Negative Consequences.

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

### D1 — The compliance metric gets a denominator, and a family of signals replaces one number

`grounding_verification_completed` carries, on every turn where verification ran:

| Field | Meaning |
|---|---|
| `turn_evidence_class` | `no_assertions` · `uncitable` · `citable` |
| `tool_results_offered` | Tool results the executor presented to the registry this turn |
| `tool_results_admitted` | How many registered as sources |
| `observed_span_outcomes` | Spans citing a source at `OBSERVED` **entitlement** (D3, D4), split by **whichever `CheckOutcome` each span received** |
| `invocation_checked_span_outcomes` | Spans citing a source flagged **`invocation_check_required`** (D2), split by **whichever `CheckOutcome` each span received** |

**Both fields split by the outcome that actually occurred, not by a fixed list, and that is a
correctness requirement rather than a convenience.** A `bash` span is `OBSERVED` *and*
invocation-checked, so it appears in both fields and can carry `invocation_covered`; an enumeration
naming only `passed`/`not_contained`/`not_entailed` for the first field would have no bucket for it,
and the span would be dropped from the surface or silently miscounted. The outcomes each field is
*relied upon* for are named in the signal table below; the outcomes each field may *contain* are the
whole of `CheckOutcome`.
| `near_miss_markers` | Citation-shaped strings that failed `CITATION_MARKER_PATTERN`, split by whether D7 resolved them |

> **D1 AMENDED 2026-09-02 (FRE-1357).** D1 stands, but three of its fields were keyed on decisions
> that no longer exist, and saying "D1 stands" unqualified would have shipped that contradiction.
> **`observed_span_outcomes` narrows to D4**: `OBSERVED`'s only members are attachments, so its
> denominator is image spans. **`invocation_checked_span_outcomes` lapses** with D2 — nothing sets
> `invocation_check_required`, so the field has no population; it is dropped rather than emitted
> empty. **`near_miss_markers` keeps its count and loses the "split by whether D7 resolved them"
> dimension**, since the resolver is withdrawn. The paragraphs below are retained because their
> *reasoning* — that entitlement and compose-capability are different questions, and that a metric
> keyed on one is blind to the other — is why ADR-0140 T4 keys the boundary on the schema rather than
> on the tier. **FRE-1332 merged these fields on 2026-09-02 (`c4660d8c`), before this revision**, so
> the emitter exists: `invocation_checked_span_outcomes` is populated at
> `orchestrator/executor.py:2036` and will now always be empty, since nothing sets
> `invocation_check_required`. Pruning it is a **follow-up on that ticket's chain, not a bounce** — an
> emitted field with no population misleads a reader, it does not break a consumer. `near_miss_markers`
> already ships as a single `unresolved` count (`:2043`) rather than a D7-resolution split, so no
> change is owed there.

**The two span-outcome fields are keyed on different properties, and collapsing them into one is the
defect round 5 was called to fix.** Round 3 established that entitlement answers *how far do we trust
this* while `invocation_check_required` answers *can this call have composed its own result*, and
re-keyed D2's **check** accordingly. It left this **metric** keyed on `OBSERVED`. `mcp_esql` earns
`EXTERNAL` (`source_registry.py:346`, and the module docstring at `:787` records why), so an
`mcp_esql` laundering attempt would have been refused correctly by the check and then been invisible
to the surface this ADR designates as the evidence that the check works — the FRE-1306 detection the
document headlines, absent from its own instrumentation. The same conflation, in the same document,
one layer over from where round 3 caught it.

The two fields also do not nest: an attachment (D4) is `OBSERVED` and carries no invocation, so it
appears only in the first; `mcp_esql` is `EXTERNAL` and invocation-checked, so it appears only in the
second; `bash` is both and appears in both. A single union field would have had to name a property
that is true of all three, and there is none.

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
| `observed_span_outcomes` — `passed` / `not_contained` / `not_entailed` | confabulation against evidence that *was* present, including image evidence | **correlated** — denominator is spans citing an `OBSERVED`-entitlement source | steady-state |
| `invocation_checked_span_outcomes` — `passed` / `not_contained` / `invocation_covered` | laundering attempts, on **every** tool that can compose its own result — `bash` and `mcp_esql` alike | **correlated** — denominator is spans citing an `invocation_check_required` source | steady-state |
| `near_miss_markers` | confabulation under compliance pressure | **conditional** — silence is health; no stoppage rule | steady-state |

> **Superseded in its premise 2026-09-02:** D2 is withdrawn, so arbitrary-code results do **not**
> register and this rate does **not** collapse by construction. It stops being a closure metric and
> becomes the **standing** measure of what Route 1 costs — the population D8's wrapper roadmap is
> ordered by, and the one ADR-0140 AC-3 and AC-5 are written against. The reasoning below is retained
> because it is why the rate may never be used as evidence that a widening worked.

**`uncitable_turn_rate` is a closure metric, and saying so is load-bearing.** Once D2 lands,
arbitrary-code results register, so `tool_results_admitted` is non-zero and this rate collapses **by
construction** — whether or not a single span ever passes. It therefore **cannot be the criterion
that proves D2 worked**, and any acceptance criterion resting on its fall alone is guaranteed to
pass while measuring nothing. That is exactly the failure FRE-1328 names: trading a measurable,
honest 0% for an unmeasurable 100%. The rate is retained because it remains a genuine sentinel for
the *next* tool that lands unclassified — a failure that is silent today — but the evidence that D2
delivered is the two span-outcome fields.

> **LAPSED WITH D2, 2026-09-02 (FRE-1357).** The paragraph below is written for two fields and two
> polarities of a check that no longer exists. `invocation_covered` is not a reachable outcome, so
> `invocation_checked_span_outcomes` has no population and the three-state distinction below collapses
> to two. **`observed_span_outcomes`, narrowed to D4 attachments, is the one signal that must not go
> structurally to zero**, and the vacuous-implementation argument below survives for it unchanged:
> an implementation that registers image sources and never checks them shows `passed` at 100% with
> `not_contained` at zero across the seeded probe set. The signal table above still lists the lapsed
> field; it is retained as record and is not an emission obligation.

**Those two fields are the signals that must never go structurally to zero.** They are measured on
spans, after both polarities of D2's check have run, so together they distinguish the three states
registration alone cannot: evidence cited and supported (`passed`), evidence present but the claim
invented against it (`not_contained`), and a laundering attempt refused (`invocation_covered`).
*`not_contained` is **not** FRE-1327's outcome: that span resolves ambiguously and lands at
`UNRESOLVED` — see D7.* A vacuous implementation that registers everything and checks
nothing shows `passed` at 100% and `invocation_covered` at zero across the seeded probe set, which
AC-1 and AC-5 both reject.

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

> **WITHDRAWN 2026-09-02 (FRE-1357), under [ADR-0140](ADR-0140-the-model-is-not-a-security-boundary.md) T3/T4.**
> This decision made a content predicate over a tool's own result the boundary for admissibility.
> ADR-0140 rules that a model-layer control may never be the sole control for an invariant, and that
> admissibility is decided from what the harness knows about *how* a result was obtained. Arbitrary-code
> tools therefore stay inadmissible and ADR-0138 D2's parameter-schema boundary is restored on its
> **invocation** axis; its **authorship** axis stays narrowed by ADR-0098 Amendment A §A6.
> **The text below is retained as record** — it is the reasoning five review rounds produced, and the
> reason the alternative was reachable. It is not a live decision and must not be implemented.

**The tool set that today short-circuits admissibility is split in two**, because it currently
conflates two unrelated exclusions under one branch:

| Set | Members | Treatment |
|---|---|---|
| `MODEL_AUTHORED_CODE_TOOLS` | `bash`, `run_python`, `mcp_browser_evaluate`, `mcp_browser_run_code` | **Result-level admissibility**, below |
| `GENERATIVE_TOOLS` | `perplexity_query`, `mcp_perplexity_ask`, `mcp_perplexity_reason`, `mcp_perplexity_research`, `mcp_research`, `mcp_sequentialthinking` | **Categorically excluded, unchanged** (D5) |

Today both live in one `ARBITRARY_CODE_TOOLS` frozenset with one categorical branch. Relaxing that
branch without splitting it would silently admit another model's generated prose as a source — the
exact widening D5 forbids — so the split is part of this decision, not an implementation detail.

A `MODEL_AUTHORED_CODE_TOOLS` result **is registered as a source**, at `OBSERVED` entitlement. The
anti-laundering rule becomes a second, opposite-polarity application of the containment machinery
D3(c) already has:

> A span citing such a source passes only where it is **contained in the result** and the source
> **survives the invocation-composition check** below.

#### The invocation-composition check has three arms, and each closes a different class

An earlier draft stated the negative polarity as a single span-level clause — *the span is not
contained in the recorded invocation*. That clause is correct and insufficient, and the gap is not
an encoded form. Coverage over required tokens is **all-or-nothing**, so a single computed hole
defeats it:

```
bash(command="echo \"Paris has $(ls | wc -l) million residents\"")
    → result:     Paris has 3 million residents
    → required:   ('paris', '#3000000', 'residents')      ← measured, not assumed
    → invocation: ('echo','paris','has','ls','wc','l','million','residents')
    → paris and residents present; #3000000 absent
    → missing is non-empty → .contained is False → "not covered" → the span PASSES
```

*(The required set is the normalizer's actual output. `million` does not survive as a content token:
`claim_unit` folds the magnitude word into the figure, so the span requires `#3000000` and not
`{#3, million}`. Round 5's first draft of this section asserted the latter and was wrong — the
conclusion is unchanged, but the arithmetic is now measured rather than reasoned, because a
plausible-looking token set is exactly how this document has shipped defects before.)*

No escape sequence, no interpreter, no encoding — a plain literal frame with one substitution. D6
claimed this ADR closes laundering's *literal* forms; this is a literal form and the single-arm
clause leaves it open. Round 5 closes it with two further arms rather than by loosening the first,
because loosening coverage to *partial* overlap would reject `grep 'mercury' file.txt` followed by
*"this fish is high in mercury"* — the commonest evidence-gathering shape there is, and the one D2
went out of its way to protect.

For a source flagged `invocation_check_required`, all three arms run. **Any** arm firing rejects:

| Arm | Predicate | Class closed |
|---|---|---|
| **1 — span coverage** | the recorded invocation **contains** the cited span (`ContainmentResult.contained`) | fully-literal payloads: `printf 'Paris has 9 million residents'` |
| **2 — fragment contiguity** | the **longest common contiguous run** of `normalize_tokens(invocation)` and `normalize_tokens(result)` is **≥ 2 tokens** | partial composition generally — entity holes (`… 9 million $(whoami)`), entity-free predicates (`echo "the fish is high in $(basename /x/mercury)"`), multi-token frames |
| **3 — non-figure coverage** | the result's non-figure content-token set is **non-empty** *and* the invocation supplies every token in it | one-content-token frames with a computed figure: `echo "found: $(ls | wc -l)"` |

**The non-empty guard on arm 3 is load-bearing, not defensive boilerplate.** "The invocation supplies
every non-figure content token of the result" is a universal quantification, and over an empty set it
is **vacuously true**. `cat report.txt` where the file holds `2026` yields required `('#2026',)` and a
non-figure set that is empty — so without the guard, arm 3 rejects it, and with it every `date +%Y`,
every numeric `ls`, and every scalar `psql -tAc`. That is a false rejection of the plainest possible
observation, produced by the arm that exists to catch composition. It was found by review rather than
by construction, which is the reason the guard is stated in the rule instead of left to an
implementer.

**Arm 2 is defined on token sequences, not on shell syntax, and that is deliberate.** An earlier
round-5 draft said "some maximal *literal run* of the invocation", which reads well and defines
nothing: isolating a quoted argument from its command head requires parsing arbitrary shell, which
this ADR refuses to do everywhere else. The operational rule needs no parse — run the **existing**
`normalize_tokens` over the invocation and over the result, and take the longest contiguous token
subsequence they share. Measured on the fixtures:

| Invocation → result | Longest shared run | Arm 2 |
|---|---|---|
| `printf '2026'` → `2026` | `('#2026',)` | misses — 1 |
| `echo "found: $(ls \| wc -l)"` → `found: 3` | `('found',)` | misses — 1 |
| `grep 'mercury' file.txt` → *this fish is high in mercury* | `('mercury',)` | misses — 1 |
| `cat report.txt` → `2026` | `()` | misses — 0 |
| `echo "Paris has 9 million $(whoami)"` → `Paris has 9 million debian` | `('paris','has','#9000000')` | **fires** — 3 |
| `printf 'Paris has 9 million residents'` → same | `('paris','has','#9000000','residents')` | **fires** — 4 |
| `echo "the fish is high in $(basename /x/mercury)"` → *the fish is high in mercury* | `('the','fish','is','high','in')` | **fires** — 5 |

Note that the command head cannot inflate the run: `printf` and `cat` do not appear in their own
output, so they contribute nothing. **This table is measured against the shipped normalizer, not
reasoned** — round 5's own review found the previous draft's arm-1 fixture wrong precisely because
`normalize_tokens("printf '2026'")` is `('printf', '#2026')`, two tokens, which an undefined
"literal run" could have been read as tripping arm 2.

Arms 2 and 3 are **source-level** — they decide whether this result was composed at all — while
arm 1 is span-level. That difference is deliberate: arm 1 must stay span-scoped because a legitimate
result can be large and contain the payload alongside genuine evidence
(`bash("cat report.txt; echo 'Paris has 9 million residents'")`), while arms 2 and 3 are properties
of the result as a whole.

**Why arms 2 and 3 are both needed, stated as the failure of either alone.** Arm 2 misses a frame of
one content token, because `N = 1` would fire on `grep 'mercury' file.txt` — the invocation's single
literal appears verbatim in every matched line — and reject every search command. Arm 3 misses an
entity hole, because `echo "Paris has 9 million $(whoami)"` leaves `debian` in the result and unsupplied
by the invocation. Neither is a tuning failure; they close complementary halves and the union is the
rule.

**The arms overlap heavily, and no claim of exclusivity is made — but all three are load-bearing.**
Measured on the fixtures: `printf 'Paris has 9 million residents'` trips all three;
`echo "the fish is high in $(basename /x/mercury)"` trips arm 2 **and** arm 3, because `mercury`
reaches the invocation through the path argument; `echo "Paris has $(ls | wc -l) million residents"`
trips arms 2 and 3 though it was introduced as arm 2's case. Each arm nonetheless has a fixture the
other two miss:

| Arm | Fixture the other two miss | Why the others miss it |
|---|---|---|
| 1 | `printf '2026'` → assertion *"There are 2026."* | required is `('#2026',)`; the invocation's literal run is one token, so arm 2 misses at `N ≥ 2`, and the result's non-figure set is empty, so arm 3's guard disables it |
| 2 | `echo "Paris has 9 million $(whoami)"` | `debian` is absent from the invocation, so arm 1's coverage and arm 3's non-figure coverage both fail |
| 3 | `echo "found: $(ls \| wc -l)"` | the frame is one content token, below arm 2's threshold; `#3` is absent from the invocation, so arm 1 misses |

**Round 5's first draft got arm 1 wrong twice** and both errors are recorded because they are
instructive. It called arm 1 *"retained for precision, not closure"* — false: `printf '2026'` is a
fully-literal payload that **only** arm 1 closes, so dropping arm 1 would reopen the very class AC-1
exists for. And it justified arm 1 with
`bash("cat report.txt; echo 'Paris has 9 million residents'")`, claiming arm 1 refuses one span while
leaving the report citable — also false, because arm 2 detects the echoed literal run and refuses the
**whole source** regardless. That last point stands as a real cost of the source-scoped arms:
**a single laundering fragment in an otherwise genuine result costs the whole source its citations.**
It is conservative, it is the direction that fails safe, and it is not mitigated by arm 1.

**Why the threshold is a token count and not something more principled.** The obvious refinement is
to key arm 2 on *what* the echoed fragment carries — reject when it holds an entity or a figure,
which would remove the arbitrary `N`. That refinement is a trap, and naming it here is the point:
`echo "the fish is high in $(basename /x/mercury)"` echoes a fragment carrying **neither** an entity
nor a figure, and reproduces an entity-free predicate claim in full. Keying on entity/figure
membership therefore readmits exactly the entity-free vacuity round 1 of this document already found
in the `.passed`/`.contained` choice, one arm over. The token count is coarse and safe; the
principled-looking predicate is neither.

**What survives all three arms.** Encoded forms (D6, unchanged); the cross-call channel (D6,
unchanged); and **a one-content-token authored frame with a non-figure hole** —
`echo "Capital: $(whoami)"`. The last is new to round 5 and is accepted on the ground that a frame of
one content token cannot carry a proposition on its own: the assertion's content comes from the
substitution, which came from the world. It is recorded in AC-1's fixture file with that reason, so
the boundary is read rather than inferred.

**Arm 1 uses `ContainmentResult.contained`, never `.passed`.** This is the whole
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

**What per-source scope does not cover — the general form, not a list of two cases.** Any channel by
which *one* call's invocation text reaches *another* call's output within the same turn is invisible
to a per-source check, because the cited source's own invocation is clean. Concurrent dispatch makes
this reachable without touching disk:

```
call A:  bash("python -c 'import time; time.sleep(5)' 'Paris has 9 million residents'")
call B:  bash("sleep 1; ps -eo args")
```

B's output carries A's command line; B's invocation carries nothing. Review round 3 found this, and
it is **not** the write-then-read case — no file, no store. The same family includes `bash` writing
a path that a typed `read` then addresses under a different spelling, and browser-evaluate feeding
browser-observation. `_taint` closes only the sub-case where a discrete argument value recurs
verbatim (`source_registry.py:_reads_tainted` requires exact equality), so most of the family is
open.

**This is conceded, not closed, and the concession is deliberate.** Restoring a turn-scoped check
would close it and would reintroduce the D4 poisoning above — a defect that fires on ordinary turns,
traded against a channel that requires deliberately staging a process-listing side channel. Under
D6's careless threat model that trade is wrong. The residual is recorded in D6 alongside encoded
composition, both standing or falling with the same threat-model decision; under an adversarial
model, Option 2 is the design instead.

**Source identity must include the invocation, or the retry cannot repair anything.** `_register`
deduplicates on `(kind, origin, content)`. Both `rg 'fish high mercury' report.txt` and
`cat report.txt` have origin `bash`, and where the file holds only the matching line they return
**byte-identical content** — so the retry's `cat` would reuse the first source, *carrying the first,
covering invocation*, and be refused again. "Recoverable by construction" is therefore false unless
the key changes: for sources subject to the negative check, the recorded invocation participates in
source identity. AC-10 tests exactly this byte-identical case.

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

**The negative check is keyed on a registry-assigned property, not on entitlement — and that is what
makes the FRE-1306 claim reachable.** An earlier draft applied the clause to `OBSERVED` sources,
which does not reach `mcp_esql` at all: it is a typed retrieval earning `EXTERNAL`, so an
`OBSERVED`-only clause would have left the very case this ADR says it resolves untouched. Round 3
caught that. The registry therefore marks each source `invocation_check_required`, set for
**every tool whose parameters can carry a program** — `MODEL_AUTHORED_CODE_TOOLS`, and
query-language tools such as `mcp_esql` — and verification keys on that flag. Entitlement answers
*how far do we trust this*; `invocation_check_required` answers *can this call have composed its own
result*. They are different questions and conflating them is what made the promise unreachable.

With that flag, FRE-1306 resolves. It records that `mcp_esql` earns a blanket `EXTERNAL` while its
single parameter is a model-authored ES|QL program, so `ROW claim = "Paris has 9 million residents"`
launders in one round-trip through a tool the parameter-schema boundary classifies as safe. That
ticket offered three options — reclassify the tool and lose every telemetry-query citation; parse
ES|QL in the turn path; or amend D2 to distinguish *selecting* from *composing*. The rule above is a
fourth that none of them anticipated: **it needs no parse and draws no select/compose line**,
because the composed literal appears in the invocation and coverage rejects it, while an
index-reading query keeps its citation because the returned rows do not. AC-11 asserts both arms.

**`_strip_argument_echo` is retained** as field-level defence in depth. The span-level rule above is
the binding one.
### D3 — A third entitlement tier: `OBSERVED`

> **WITHDRAWN AS WRITTEN 2026-09-02 (FRE-1357).** The `OBSERVED` **tier survives**, scoped to D4:
> it records that *the harness received these bytes*, which is a capability fact. What is withdrawn is
> its extension to arbitrary-code results (which D2's withdrawal removes) and the **address-bound
> terminus rule**, which is not decidable by the means this ADR permits: extracting a read's target
> from a command line requires the shell parse the ADR refuses everywhere else, and string-matching the
> invocation instead reproduces the failure `_reads_tainted`'s own docstring records — "a write and a
> read that name the same target differently — an absolute path against a relative one". The terminus
> for typed reads is the cross-turn write ledger (FRE-1356), which is the mechanism rather than an
> upgrade to one. **Retained below as record.**

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
independently re-derived.* The `Entitlement` docstring records a live incident — an `Event` node
reading *"Wednesday, July 1, 2026"*, a date the agent hallucinated, which entity extraction wrote to
the graph and which then passed all three D3 checks because **the source was the false claim**. We
admitted memory at `EXTERNAL` with a demonstrated laundering incident, and excluded `bash` where
laundering was hypothetical.

**That inconsistency has since been corrected, in master's direction, and not by this ADR.**
ADR-0098 Amendment A A6 (merged 2026-08-30) rules that entitlement follows the **terminus of the
provenance chain**: a typed memory retrieval whose chain terminates at an agent-authored turn, or at
`provenance_state = 'none'`, earns `AGENT_DERIVED` and is **not** admissible as a citation.
Aggregation is most-restrictive, so one `none`-terminus item drops the whole recall. An earlier draft
of this section deferred the correction to FRE-1302/FRE-1303 and called `OBSERVED`-below-`EXTERNAL`
a *provisional ordering*; that deferral is now stale in one direction — memory is no longer
uniformly `EXTERNAL` — and the ordering language is withdrawn rather than restated, since D3 already
rules the tiers are labels and not a comparable ordering.

#### How `OBSERVED` composes with A6's terminus rule

A6 narrows **retrieval**; D2 and D3 widen **first-person observation**. They meet at one seam, and
this is the resolution FRE-1349 AC-4 requires — stated as a rule, not as two adjacent paragraphs.

**The terminus rule follows the bytes, not the tool.** A6 applies wherever a provenance chain
*exists*. A live observation has none — the agent acted and something happened — so it registers at
`OBSERVED` and A6 has nothing to test. But **a read-back of persistent state is a retrieval wearing
an observation's clothes**, whatever tool carries it, and it inherits A6's terminus test in full.
`bash("cat /tmp/notes.md")` over a file the agent wrote **in an earlier turn** is FRE-1338's shape —
one session's own authorship laundered into the next — moved from the knowledge graph to the
filesystem and arriving through the door D2 opens. `_taint` cannot see it: it is turn-scoped and
matches argument values exactly (`source_registry.py:1076`), so it closes the intra-turn
`write`→`read` pair and nothing beyond it.

**The rule binds at the address, because that is what is decidable today.** A read whose target is an
**agent-writable store** — the sandbox scratch directory, agent-authored artifact rows, the knowledge
graph — earns `AGENT_DERIVED` regardless of the tool that carried it. A read addressed outside those
stores earns `OBSERVED`. This is a property of *where you read*, not of *who wrote*, so it needs no
history and no new machinery.

**It is coarse in both directions and that is recorded, not discovered later.** It over-denies a file
the owner placed in the scratch directory, and it under-denies a file the agent wrote into the
repository tree. The upgrade that makes the terminus real is a **cross-turn write ledger** — the
remedy `_reads_tainted`'s own docstring already names, "the tool layer must report its writes",
extended past the turn boundary. That is separately sequenceable work and is filed rather than folded
in; until it lands, the address list is the terminus and the residual is the gap between an address
and an author.

### D4 — Attachments are first-person observation; no surrogate

> **D4 STANDS, and its dependencies are restated here rather than referenced into withdrawn text
> (FRE-1357).** The `OBSERVED` entitlement and the `OBSERVATION` source kind survive **scoped to this
> decision** — they record that *the harness received these bytes*, which is a capability fact under
> ADR-0140 T4. The three gates named below are **ADR-0138 D3's**, not withdrawn D3's; the numbering
> collision is unfortunate and the reference is to ADR-0138. And the containment bypass plus inline
> entailment is a **judgement check** in ADR-0140 T3's sense, not a boundary: the boundary is that no
> attachment means no source and the span is blocked, which is capability-enforced. The entailment
> arm asking the same vision model that made the claim is therefore a declared-thin judgement, which
> T3 permits — and AC-8 measures it as one.

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

> **AMENDED 2026-09-02 (FRE-1357).** D5's rule stands and is now the *general* case rather than an
> exception: no arbitrary-code or generative tool mints a source. Two claims below are false as
> written and are corrected here rather than edited in place — D2 does **not** split
> `ARBITRARY_CODE_TOOLS` (the set is unchanged), and the D4 retry does **not** gain tool evidence to
> cite. AC-9, which this section cites, has lapsed; ADR-0140 AC-1 carries the surviving obligation
> and extends it to `mcp_esql`.

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
- **Memory entitlement is not changed *by this ADR*.** It is changed by ADR-0098 Amendment A A6,
  which narrowed it on 2026-08-30; FRE-1302 and FRE-1303's rules stand as written except where A6
  supersedes them. Nothing here widens memory admissibility, and D3 records how `OBSERVED` composes
  with A6's terminus rule rather than leaving the two to be reconciled by a reader.
- **D4's block-retry-refuse loop keeps its shape.** What changes is that on a tool-driven turn the
  retry now has something to cite, so the loop terminates in an answer rather than in a refusal the
  evidence did not warrant.

### D6 — The declared threat model, and what it costs

> **RETIRED 2026-09-02 (FRE-1357).** Superseded by
> [ADR-0140](ADR-0140-the-model-is-not-a-security-boundary.md), which declares the threat model at
> program level. Two things were wrong here, and the location was the first of them: a premise that
> decides whether an entire approach is correct cannot be a subsection of that approach — stated here,
> it was invisible to ADR-0138, whose admissibility rule it governed. The second is the axis:
> "careless or adversarial" reasons about model *intent*, and ADR-0140 T1 rules that intent is not a
> design input at all. The behavioural observation this section records — no laundering attempt in the
> 100 recorded refusals — remains true and remains admissible for prioritisation. **Retained below as
> record.**

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
all reproduce a target sentence while no token of it appears in the command line.

**An earlier draft of this section then overclaimed, and round 5 corrects it.** It said D2 "closes
the *literal* forms — the shapes a careless model actually produces". That was false as written:
`echo "Paris has $(ls | wc -l) million residents"` is a literal form, produces a fabricated
world-fact in one round trip, and defeated the single-arm negative check because coverage over
required tokens is all-or-nothing and one computed hole is enough. D2's three arms close it. **The
honest statement of D2's reach, after round 5:**

| Laundering form | Status |
|---|---|
| Fully-literal payload (`printf 'Paris has 9 million residents'`) | **closed** — arm 1 |
| Partial composition, frame ≥ 2 content tokens (`echo "Paris has $(…) million residents"`, `echo "the fish is high in $(…)"`) | **closed** — arm 2 |
| Partial composition, one-token frame with a computed figure (`echo "found: $(ls \| wc -l)"`) | **closed** — arm 3 |
| Partial composition, one-token frame with a non-figure hole (`echo "Capital: $(whoami)"`) | **open** — accepted; a one-token frame carries no proposition |
| Encoded forms (`printf '\x50aris…'`, `--pretty=format:%x50`, ANSI-C quoting, concatenation in an interpreter) | **open** — accepted by decision |
| Cross-call shared state (`ps -eo args`, write-then-read across spellings) | **open** — accepted by decision |

The three open rows stand or fall together with the threat model below.

**The second accepted residual is the cross-call shared-state channel.** Concurrent dispatch lets
one call's invocation text surface in another call's output — `bash("… 'Paris has 9 million
residents'")` running alongside `bash("sleep 1; ps -eo args")` — where the cited source's own
invocation is clean and the per-source check sees nothing. Write-then-read across differing argument
spellings, and browser-evaluate feeding browser-observation, are the same family. Closing it needs a
turn-scoped check, which D2 shows would reintroduce the D4 retry poisoning: a defect that fires on
ordinary turns, traded against a channel that requires deliberately staging a process-listing side
channel. **That trade is wrong under a careless model and right under an adversarial one**, which is
precisely the decision this section records.

Both residuals stand or fall together with the threat model. Under an adversarial model this is a
hole, Option 2 (typed wrappers only) is the correct design instead, and AC-1's fixture file is where
the boundary is recorded so a future reader can see what was conceded rather than infer it.

**The evidence supports the careless reading.** The observed failure is FRE-1327 — the model had
27,000 tokens of ground truth in context and generated figures matching none of it. That is
confabulation, not laundering. No laundering attempt, literal or encoded, appears in the 100
recorded refusals.

**And the amendment turns that failure legible — but not by D2 alone, and round 5 corrects the
claim.** An earlier draft said that with the `bash` output registered, D3(c) would check FRE-1327's
invented figures against the bytes in context and the outcome would become `NOT_CONTAINED`. That is
**false for the trace this ADR names**, and it was false for a reason worth stating plainly:
containment is never reached. `_verify_span` (`verification.py:295`) returns `UNCITED` the moment
`_identifier_for` yields `None`, and FRE-1327's model wrote `[S@bash-tempo-trace-dba5b2]`, which
fails `CITATION_MARKER_PATTERN` (`citations.py:45`) on three counts. No `CitedSpan` exists, so no
identifier, so no source, so no containment. **Registering the output does not make an unparseable
marker parse.** Every gate after the first is unreachable for that span, and the document's most
quoted sentence rested on a code path that cannot execute.

**Round 5's first repair of this claim was also wrong, and the second correction is the one that
holds.** D7 was introduced to make the span reach containment, and it does not — not on this trace.
Registry identifiers are `S{ordinal}@{16-hex-digest}`, so `bash-tempo-trace-dba5b2` matches no
identifier; resolving instead by source attribute finds **four** candidate `bash` sources in this
trace and therefore resolves nothing. **The claim that this ADR converts FRE-1327's fabrication into
`NOT_CONTAINED` is withdrawn.** What it converts is the *reason recorded*: the span moves from
`UNCITED` — indistinguishable from a model that never tried — to **`UNRESOLVED`**, the model naming
a source that does not exist. That is the confabulation-versus-apathy gap the Context actually names,
and it is smaller than the sentence it replaces. It is also why the Context's observation that
`UNRESOLVED` sits at **0 documents** is a symptom of this defect rather than an incidental
statistic.

`NOT_CONTAINED` remains reachable on well-formed citations (AC-2 arm (b)) and on near-misses that do
resolve unambiguously, and the two span-outcome fields (D1) are where those detections surface —
a `bash` source is `OBSERVED` *and* invocation-checked, so it lands in both. AC-3 is rewritten to
adjudicate on an equivalent trace, since its named one cannot carry it.

**Nothing here excuses the confabulation itself** (FRE-1327), which remains a separate, open
failure. A citation-plumbing fix that made the fabrication *measurable* has not made it *rarer*, and
this ADR must not be read as though it had.
### D7 — A malformed citation marker is a rejection with a reason, not a silence

> **WITHDRAWN EXCEPT ROW ONE, 2026-09-02 (FRE-1357).** What survives: **a span whose only
> citation-shaped marker fails `CITATION_MARKER_PATTERN` scores `UNRESOLVED`, not `UNCITED`** — the
> whole of the confabulation-versus-apathy fix, needing no resolution and granting no admissibility.
> Withdrawn: the attribute resolver, rows two and three, and `MALFORMED_CITATION`. Round 6 established
> that the rest was unreachable and non-discriminating. **(a)** `parse_citations` yields no `CitedSpan`
> for a near-miss, so `_identifier_for` returns `None` and `_verify_span` short-circuits to `UNCITED`
> at `verification.py:309` before any of D7 runs; giving it an entry point means making a near-miss
> bind a region, which changes ADR-0138 D1's segmentation contract. **(b)** `strip_citation_markers`
> is built from the same pattern, so a near-miss's own characters stay in the span text — measured,
> `claim_unit` over the FRE-1327-shaped span yields `('trace','four','bash','calls','succeeded',
> 'second','tempo','dba5b2')`, three tokens minted by the marker itself (`S` folds to the unit
> synonym `second`). Row three is therefore unreachable by construction and row two always returns
> `NOT_CONTAINED` for a protocol-character reason. **(c)** Attribute precedence is undefined: a
> candidate matches on `origin` **or** an ≥8-hex digest prefix and the match must be unique, so
> `[S@bash-<the correct digest>]` on a four-`bash` turn resolves *ambiguously* — the rule is strictly
> worse the more the model gets right. **Retained below as record.**

**A citation-shaped string that fails `CITATION_MARKER_PATTERN` stops short-circuiting to `UNCITED`.**
It is resolved against the registry on **registry-minted attributes only**, and the span it binds
then runs **the standard gate sequence** — entitlement, reachability, containment — exactly as a
well-formed citation does. The one difference is at the end: **a near-miss span can never score
`PASSED`.**

The defect this closes is stated in the Context and then not acted on: `UNRESOLVED` appears in **0**
documents while `uncited` appears in 11, so *"the model minted a marker naming the source it had
actually used"* is byte-identical, in every record we keep, to *"the model did not try"*. D1's
`near_miss_markers` counts those strings; counting says a near-miss happened and says nothing about
the turn's verdict, which is where every consumer looks.

#### Three outcomes, because there are three situations — and only one of them is new

Round 5's first draft used a single new outcome for every near-miss, and its own review broke that in
one line: a near-miss matching **no** source and a near-miss matching **one source that holds the
claim** are not the same situation. In the first, no admissible source was brought to bear on the
span; in the second, one was, and only the marker was malformed. A single member cannot be both
inside and outside `_TRUE_NO_SOURCE`, and `no_source_count` is computed from that membership alone
(`verification.py:615`).

| Near-miss resolves to | Then | Outcome |
|---|---|---|
| no source, or more than one | gates not run | **`UNRESOLVED`** — existing member, correctly inside `_TRUE_NO_SOURCE` |
| exactly one source | the standard gate sequence rejects it | **that gate's own outcome**, unchanged — `SOURCE_NOT_ENTITLED`, `UNREACHABLE`, `NOT_CONTAINED`, `INVOCATION_COVERED` … |
| exactly one source | the standard gate sequence would return `PASSED` | **`MALFORMED_CITATION`** — the one new member |

**Only the third row needs a new outcome, and it is the only row where a source demonstrably existed
and supported the claim.** `MALFORMED_CITATION` is therefore a rejection barred from
`_TRUE_NO_SOURCE` and `_MACHINE_UNDECIDED`, for the reason round 3 gave for `INVOCATION_COVERED`:
booking it as an absence would report a botched citation against present, supporting evidence as
having had no evidence at all.

**The first row is the one that does most of the work, and it needs no new member.** `UNRESOLVED`
already means *this identifier resolves to no source in this turn's registry*, which is precisely
what a near-miss matching nothing is. Routing near-misses there rather than to `UNCITED` is the whole
of the confabulation-versus-apathy fix — and it is why the Context's observation that `UNRESOLVED`
sits at **0 documents** is a symptom of this defect rather than an incidental statistic.

**The second row is deliberately not enumerated.** Round 5's first draft wrote a closed three-row
lattice that jumped straight to containment, which contradicts gate ordering: a near-miss resolving
to an `AGENT_DERIVED` memory source is rejected `SOURCE_NOT_ENTITLED` at `verification.py:330`, long
before containment, and such sources are routinely registered. Deferring to the standard sequence
removes the contradiction and removes the temptation to keep a second, drifting copy of the gate
order in this document.

#### Resolution matches registry-minted attributes only, and never the label

Registry identifiers are minted as `S{ordinal}@{16-hex-digest}` (`IDENTIFIER_DIGEST_CHARS` is 16).
The recorded near-miss `[S@bash-tempo-trace-dba5b2]` has no ordinal and no digest, so it **cannot
match an identifier**, and a rule written against identifiers would deliver nothing. Resolution
therefore matches on:

- **`origin`** — the registry-assigned tool name (`bash`, `mcp_esql`, …); and
- **a digest prefix of at least 8 hex characters** of the source's minted identifier.

**`label` is excluded, and excluding it is a security property rather than a simplification.**
`RegisteredSource.label` is not uniformly registry-controlled: for memory sources it derives from the
recalled item's own identity — an entity `name`, a stance `target` (`captains_log/turn_evidence.py:240`)
— which is knowledge-graph content and therefore agent-influenceable. Matching on it would let the
model steer which source its malformed marker resolves against, by first writing the label it later
names. `origin` and the digest are minted by the registry from data the model does not author.

**Matching is defined on segments, not substrings.** The near-miss's inner text is lowercased and
split on runs of non-alphanumeric characters; a candidate matches when **some segment equals its
`origin` exactly**, or when some segment is a **≥ 8-character prefix of its digest**. Exact segment
equality rather than substring containment is the operational half of the rule: without it
`[S@bashful]` matches a `bash` source, and the line between *the model named the tool* and *the model
wrote a word containing the tool's name* is left to an implementer. On the recorded near-miss
`bash-tempo-trace-dba5b2` the segments are `bash`, `tempo`, `trace`, `dba5b2`; `bash` matches four
sources by origin, and `dba5b2` — valid hex, but 6 characters — contributes nothing.

**The 8-character floor exists because a shorter one manufactures detections.** A single hex
character matches roughly one source in sixteen by coincidence, so `[S@a]` would resolve
"unambiguously" whenever exactly one registered source happened to have a digest starting `a` — and
produce a rejection against a source the model never meant. A match must be **unique across the whole
registry**; two candidates is ambiguity, and ambiguity is row one.

**D7 engages only where the span's selected binding is itself a near-miss.** Verification records one
outcome per span and binds one identifier per span (`verification.py:173`, `:289`), so multiplicity is
already decided before D7 is reached and D7 does not reopen it. **A well-formed marker always wins:**
where a span carries both a valid marker and a near-miss, the valid marker binds, the span is an
ordinary citation, and `PASSED` stays reachable. Without that precedence a stray near-miss could
*degrade* a genuine citation, handing the model a way to poison its own compliant spans — the
opposite of the admission channel Option 9 was rejected for, and just as unwanted.

#### This does not rescue FRE-1327, and the ADR no longer claims it does

FRE-1327's trace carries **four** `bash` refusals. `bash-tempo-trace-dba5b2` matches `origin = bash`
four times over, and `dba5b2` is a prefix of the **trace** id, not of any source digest. The match is
ambiguous, so the span lands in row one: **`UNRESOLVED`**.

**The claim that this ADR converts FRE-1327's fabrication into `NOT_CONTAINED` is withdrawn** —
first because the marker never parses, and now because it never resolves either. What the ADR
delivers on that trace is narrower and still the thing the Context asked for: the span stops reading
as *the model did not try* and starts reading as *the model named a source that does not exist*,
which is what confabulation-under-compliance-pressure looks like in a record.

A best-effort nearest match is rejected rather than tuned. It would manufacture a `NOT_CONTAINED`
against a source the model never named — a false detection in the one signal this ADR asks the reader
to trust — and on this very trace it would have to pick one of four `bash` calls arbitrarily.

#### `PASSED` is unreachable from a near-miss, by construction

That is the design, not a conservatism. Letting a fuzzy match earn a pass would create an admission
channel steered by text the model writes, which is what D4 forbids for the containment bypass ("keys
on the registry's own `SourceKind`, never on anything the model can write"). Here the model's string
may **select** among sources the registry already holds, on attributes it does not author, and can
never **create** admissibility. The span is blocked under D1's default-deny in every row above.
**All D7 changes is what the record says about why.**

**`NOT_CONTAINED` is reachable without D7**, on a well-formed citation whose claim its source does
not support; that is AC-2 arm (b) and it does not depend on this decision.

### D8 — The replacement path: typed retrieval, provisioned on demand and ordered by D1

Added 2026-09-02 (FRE-1357) as what D2 and D3 are replaced *by*, not merely withdrawn *for*. The
premise is [ADR-0140](ADR-0140-the-model-is-not-a-security-boundary.md) T4: a source is admissible
because of how the harness obtained it. The application is this ADR's, because which tools get
provisioned in what order is a question about *this* problem and must not be settled inside the
threat-model document — the error D6 made in the other direction.

**Arbitrary-code tools stay inadmissible, and that is now a decision rather than a status quo.**
`bash`, `run_python` and the browser-evaluate pair keep the categorical branch at
`source_registry.py:945`. No split, no `invocation_check_required`, no invocation-bearing source
identity, no negative containment check. ADR-0138 D2's parameter-schema boundary stands on its
**invocation** axis (its authorship axis is separately narrowed by ADR-0098 Amendment A §A6, which
this revision does not touch):
a tool whose parameters *select or address* is admissible; a tool whose parameters *compose* is not.

**Evidence the agent needs to cite is provisioned as a typed tool.** This is Option 2 of this ADR,
rejected in round 5 under D6 and correct under ADR-0140 T1. Round 5 rejected it on two grounds, and
**both are now stale**:

- *"Cheap to close the alternative instead."* Round 6 measured the cost of the three-arm check:
  it rejects the **whole source** for `grep 'passed_count' logs.json` (snake_case splits into two
  tokens), `rg 'source_registry tool' src/`, `git log --grep='cost gate'`, a `psql` column header and
  an ES|QL `KEEP` projection — refuting this ADR's own **AC-11**, which requires an index-reading
  ES|QL query to keep its citation. The alternative was not cheap; it was unmeasured.
- *"An unbounded provisioning treadmill, and the metric stays confounded meanwhile."* **D1 removes
  the second half and bounds the first.** `turn_evidence_class`, `tool_results_offered` and
  `tool_results_admitted` de-confound the metric, and they identify *which* turns went uncitable for
  want of which source. Provisioning becomes demand-driven and ordered rather than speculative and
  unbounded — you build the wrapper the measurement asks for.

**The sequencing is therefore: D1 first and alone** (unchanged — FRE-1332, in implementation), then
D4, then wrappers in the order D1's uncitable population ranks them. ADR-0140 AC-3 is what keeps
that honest in both directions: a wrapper shipped with no uncitable evidence behind it fails, and an
uncitable population that persists two windows with nothing filed fails too.

**The cost is not closed and is not disguised.** Until a source has a typed tool, assertions that
depend on it stay uncitable and those turns keep scoring zero — the measured defect this ADR opens
with. What changes is that they are **classified, counted and ranked** instead of being an
undiagnosable constant, and that the boundary they sit behind is one that holds. Exempting them from
the denominator to make the number look better is Option 3, rejected here and re-forbidden by
ADR-0140 AC-5.

---

## Alternatives Considered

> **READ WITH THE 2026-09-02 REVISION (FRE-1357).** These options were weighed under D6's retired
> threat model, and two verdicts have inverted. **Option 2 (typed wrappers only) is now the chosen
> design** — it is D8, correct under ADR-0140 T1 rather than under an adversarial model, and both of
> its stated grounds for rejection are stale (see D8). **Option 10 (default-deny read-back)** was
> rejected for closing D2, which is now withdrawn anyway; it is moot rather than wrong. Options 6, 7
> and 9 remain rejected on their own reasoning, which does not depend on the threat model. The
> rejection texts are retained unedited as the record of what was weighed and why.

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

### Option 7: Key the fragment arm on what it carries, not on how long it is

**Description:** Replace arm 2's `N ≥ 2` token count with a semantic predicate — reject when the
echoed invocation fragment contains an **entity or a figure**, reusing `claim_unit`'s existing
entity/figure split rather than introducing a tuned constant.

**Pros:**
- Removes an arbitrary threshold from the normative text, which is otherwise the least defensible
  number in this ADR.
- Reuses machinery the codebase already has and has already reasoned about, at
  `containment.py:812`, where the same hard/soft split decides `NOT_CONTAINED` from `UNVERIFIABLE`.
- Gives the same verdict as `N = 2` on every case in D6's table except one.

**Cons:**
- That one case is `echo "the fish is high in $(basename /x/mercury)"`. The fragment *"the fish is
  high in"* carries neither an entity nor a figure, so the rule does not fire, and an entity-free
  predicate claim is reproduced in full.

**Why Rejected:** It reinstates the **entity-free vacuity** this document's own round-1 review
already found once, in the `.passed`-versus-`.contained` choice — the same class of claim, one arm
over. A predicate that looks principled and fails on exactly the class the codebase has been burned
by twice is worse than a coarse constant that does not. The constant is recorded in D2 with this
reasoning attached, so the next reader does not re-derive the refinement and re-adopt the bug.

### Option 8: Concede partial composition; correct D6 and change nothing else

**Description:** Keep the single span-level negative check, record partial composition as a third
accepted residual beside encoded forms and the cross-call channel, and rewrite D6's "closes the
literal forms" claim to say which literal forms.

**Pros:**
- Zero implementation cost; the smallest possible round-5 diff.
- Arguably consistent with D6's careless threat model — composing a false frame around a genuinely
  computed value takes intent, and no laundering attempt of any form appears in the 100 recorded
  refusals.

**Cons:**
- The conceded channel is a **one-round-trip fabrication of an arbitrary world-fact**, needing no
  encoding, no interpreter and no staged side channel — categorically unlike the two residuals it
  would sit beside, both of which require deliberate construction.
- It concedes the shape closest to what a careless model actually emits: interpolating a real value
  into an authored sentence is ordinary shell style, not an attack.

**Why Rejected:** The other two residuals are conceded because closing them costs more than they are
worth *under a careless model*. This one is cheap to close — three arms over one existing primitive —
and its exploitation requires less deliberateness than either. Conceding it would leave the ADR
shipping a known one-round-trip channel while claiming the careless model as justification for not
closing the cheapest one.

### Option 9: Repair near-miss markers into full citations

**Description:** Let D7's resolution produce any outcome, `PASSED` included — a near-miss that
unambiguously names a registered source is treated as a citation the model got typographically
wrong.

**Pros:**
- Charitable to the model, and arguably the behaviour a human reader would apply.
- Recovers compliance on turns where the only failure was marker syntax.

**Cons:**
- Creates an admission channel steered by text the model writes. A permissive matcher becomes a
  target: sloppy markers stop being a failure mode and start being a strategy.

**Why Rejected:** D4 already rules that the containment bypass must key on registry-assigned state
"never on anything the model can write", and the same reasoning governs here. D7 keeps the model's
string in a **selecting** role over sources the registry already holds and bars it from a
**creating** one; the span stays blocked under D1's default-deny either way, so the charitable
reading buys nothing that matters and costs the invariant.

### Option 10: Default-deny `OBSERVED` to every read-back of persistent state

**Description:** Rather than an address list, deny `OBSERVED` to any result that reads persistent
state at all, until a cross-turn write ledger can establish the terminus — matching
`_entitlement_of`'s existing "absence resolves to `AGENT_DERIVED`" posture.

**Pros:**
- Safest available reading, and consistent with the module's own default-deny stance.
- Needs no address list to maintain, and no judgement about which stores the agent can write.

**Cons:**
- It denies `cat report.txt` a citation. That case is D2's opening argument — the exact parallel to
  `fetch_url` that motivates the whole ADR — so the option removes the decision's own worked example.
- Almost everything `bash` reads is persistent state; the exception is state the command computes,
  which is the narrowest slice of what the agent learns by doing.

**Why Rejected:** It closes the FRE-1338 seam by closing D2, which is not a trade this ADR can make
and remain the ADR it is. The address-level rule in D3 is coarser and admits a stated residual, but
it keeps the motivating case citable and names the ledger that upgrades it.

---

## Consequences

> **LARGELY LAPSED 2026-09-02 (FRE-1357).** Both lists and the risk table below were written for the
> withdrawn design: the positive consequences assume tool-driven turns become answerable and that the
> ES|QL negative check resolves FRE-1306, and the negative consequences and risks are almost entirely
> about admitting `bash`, the invocation arms, D7 resolution and address-bound classification. **None
> of those is a live consequence or a live risk.** What ADR-0139 now costs and risks is stated in D8
> and in [ADR-0140](ADR-0140-the-model-is-not-a-security-boundary.md)'s own Consequences and Risks —
> including the one that matters most, that everything learned by running a command stays uncitable
> until a wrapper exists. Retained below as record.

### Positive Consequences

- Tool-driven turns become answerable under `enforce`, removing the hard blocker on ADR-0138 D5's
  enforcement selection.
- FRE-1327's confabulation stops being a **silence**: its span moves from `UNCITED` — the same score
  as a model that never tried — to `UNRESOLVED`, the model naming a source that does not exist (D7).
  `NOT_CONTAINED` is **not** reachable on that trace and is no longer claimed; it is the outcome for
  confabulation carrying a *well-formed* citation, or a near-miss that resolves uniquely, rather than a silence
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
- **A new false-rejection class: the invocation-covered retrieval.** `rg 'fish high mercury'
  report.txt` returning *"This fish is high in mercury."*, or `grep 'Paris has 9 million residents'
  bigfile.txt` against a file that contains it, has every required content word in its own
  invocation — so `.contained` is true and the span is refused though the evidence is genuine. This
  is the direct cost of the `.contained` predicate and it is the common shape, not a corner. **It is
  recoverable**: because exclusion is per-source *and* the invocation participates in source
  identity, D4's retry with a non-covering invocation — `cat report.txt` — mints a distinct source
  and passes. AC-10 asserts that recovery on byte-identical results rather than assuming it. The
  residual rejections count against ADR-0138 AC-8's existing false-rejection bar.
- **Cross-call shared-state laundering is not closed** (D2) — process listings, write-then-read
  across differing argument spellings, browser-evaluate into browser-observation. Conceded under D6
  rather than traded for the D4 poisoning a turn-scoped check would reintroduce.
- **Inline entailment volume grows** with every image-bearing turn, since image spans take the
  entity-free path.
- **Per-source invocation retention** adds storage on every source requiring the negative check, and
  every such span-level check runs the containment predicate twice.
- **A one-content-token authored frame with a non-figure hole remains open** (`echo "Capital:
  $(whoami)"`) — the residual left after D2's three arms, accepted on the ground that a one-token
  frame carries no proposition, and recorded in AC-1's fixture file rather than inferred.
- **The terminus binds at the address, not the author** (D3). A read into an agent-writable store is
  denied `OBSERVED` even where the owner placed the file there, and a file the agent wrote outside
  those stores is still admitted. The cross-turn write ledger that closes the gap is filed, not
  folded in.
- **Arms 2 and 3 are source-level**, so a composed result is refused whole rather than per span.
  A single laundering fragment in an otherwise genuine result costs the whole source its
  citations — conservative, and the direction that fails safe.
- **`near_miss_markers` acquires a false-detection surface.** D7 converts near-misses from a count
  into a verification outcome, so an over-eager matcher would produce `NOT_CONTAINED` against sources
  the model never meant. D7 requires an unambiguous single match on registry-minted attributes for
  exactly this reason, and AC-7's precision bar governs it. The cost of that strictness is that the
  **majority** of near-misses resolve to nothing and stop at `UNRESOLVED` — including the one recorded
  instance we have, FRE-1327, whose trace holds four `bash` sources. D7 buys a named rejection, not a
  containment check, on that shape.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| The careless threat model is wrong, and laundering appears once `bash` is citable | High | `near_miss_markers` and the `NOT_CONTAINED` rate are both published per model; a laundering attempt raises neither, so AC-1's seeded probes are re-run at each enforcement promotion rather than once. Reverting is a one-line restoration of the categorical rule. |
| Admitting `bash` inflates compliance without any real check — the ticket's own falsification clause | High | AC-2 requires **both** arms on the same probe family: a claim present in the output passes, a claim absent from it scores `NOT_CONTAINED`. A vacuous implementation fails the second arm. |
| False rejections rise beyond the tolerated bar | Medium | Coverage semantics rather than word overlap (D2); measured against ADR-0138 AC-8's existing false-rejection bar, not a new one. |
| `uncitable_turn_rate` collapses by construction once results register, and is mistaken for proof the decision worked | **High** | Named in D1 as a **closure metric** and explicitly barred from AC-5, which keys on the two span-outcome fields instead — spans actually cited, checked and passed. The rate is retained only as the sentinel for the next unclassified tool. |
| A laundering attempt on a tool that is invocation-checked but not `OBSERVED` — `mcp_esql` — is refused correctly and never appears in the evidence surface | **High** | D1 splits the metric: `observed_span_outcomes` keys on entitlement, `invocation_checked_span_outcomes` keys on the flag. AC-11 requires the `mcp_esql` refusal to land in the second field by name, so a re-keyed aggregation that is never demonstrated on the case fails. |
| Only arm 1 is implemented, and partial composition ships open under a document claiming it closed | **High** | AC-12 seeds all three arms with preregistered fixtures, including a one-token-frame figure hole that arm 1 and arm 2 both miss. An arm-1-only implementation fails on that fixture specifically. |
| D7's near-miss resolution is implemented as a permissive nearest match, becoming an admission channel or a false-detection source | **High** | D7 bars `PASSED` from the lattice by construction and requires an unambiguous single match on source attributes. AC-13 asserts both directions, and **fails an implementation that reports `NOT_CONTAINED` on FRE-1327's trace** — with four `bash` sources there, that outcome can only come from picking a candidate arbitrarily. |
| One near-miss outcome is used for both "no source resolved" and "a source resolved and supported the claim", so `no_source_count` is wrong in one direction whichever frozenset it joins | **High** | D7 splits them: no/ambiguous match takes the **existing** `UNRESOLVED` (correctly inside `_TRUE_NO_SOURCE`), and only the resolved-and-supported row takes the new `MALFORMED_CITATION` (barred from it). AC-13 asserts `no_source_count` in **both** directions — zero on a row-(c)-only turn, equal to the span count on a row-(a)-only turn. Round 3 caught this accounting shape for `INVOCATION_COVERED`; round 5 reproduced it on `UNRESOLVED` and its review caught it again. |
| The near-miss resolver matches on `label`, letting the model steer resolution by first writing the label it later names | **High** | D7 restricts matching to registry-minted attributes — `origin` and a ≥8-character digest prefix — and states why `label` is excluded: for memory sources it derives from entity names and stance targets (`captains_log/turn_evidence.py:240`), which is agent-influenceable graph content. AC-13 asserts a one-character prefix does not resolve. |
| Arm 3's universal quantifier is implemented without the non-empty guard, rejecting every numeric-only observation | **High** | D2 states the guard in the rule; AC-12's positive controls name `cat` over numeric content, `date +%Y`, numeric `ls` and scalar `psql` explicitly, because round 5's own first draft failed all four. |
| `OBSERVED` is granted to a read-back of state the agent wrote in an earlier turn, reopening FRE-1338 on the filesystem | **High** | D3 binds the terminus at the address; AC-14 seeds a read into an agent-writable store and requires `AGENT_DERIVED`, with a read outside those stores earning `OBSERVED` on the same probe family. |
| The negative check is implemented on `ContainmentResult.passed`, silently readmitting entity-free laundering | **High** | D2 fixes the predicate as `.contained` and states why; AC-1's probe set includes an entity-free payload (`this fish is high in mercury`) whose rejection cannot be achieved by the `.passed` reading. |
| Relaxing the categorical branch widens admissibility to `perplexity_*`/`mcp_research`, which share the same frozenset | **High** | D2 splits the set as part of the decision rather than the implementation; AC-9 asserts both arms — generative tools register nothing, `run_python` is checked rather than blanket-refused. |
| Exclusion state carried across D4 attempts makes the retry loop unable to repair the turn it exists to repair | **High** | Invocation text is scoped to the source, never accumulated per turn (D2). AC-10 asserts the recovery directly: a refused covering-invocation span, retried with an independent retrieval, must pass. |
| `invocation_covered` is emitted as telemetry but never becomes a real verification outcome, so nothing blocks on it | Medium | Implementation notes require `CheckOutcome.INVOCATION_COVERED` as an enum member with defined blocking and retry-directive behaviour, not a log field; AC-1 asserts the outcome by name, so a log-only implementation fails it. |
| The alert is authored but never fires, and nobody notices | Medium | AC-6 requires the rule to be **observed transitioning to Alerting** on a seeded turn. An untested alert rule is not an alert. |
| Vision entailment is the model marking its own homework | Medium | Recorded as a stated limit (D4), with a negative arm in AC-8; promotion to a second model is left to the eval program (ADR-0087) rather than assumed here. |

---

## Implementation Notes

> **DO NOT IMPLEMENT — LARGELY LAPSED 2026-09-02 (FRE-1357).** This section instructs an implementer
> to split the tool set, add `invocation_check_required`, implement all three invocation arms, add
> D7's resolver and retain invocation text in source identity. **All of that is withdrawn.** What
> survives: D1's event fields **as amended in D1's own banner** (two of them lapse), and D4's
> attachment registration with `OBSERVED`/`OBSERVATION` scoped to attachments. The live implementation
> surface for the replacement path is **D8**, and the one code change the revision requires —
> `mcp_esql` leaving `TYPED_RETRIEVAL_TOOLS` — is specified in
> [ADR-0140](ADR-0140-the-model-is-not-a-security-boundary.md)'s Implementation Notes. Retained below
> as record.

**Files affected:**

- `src/personal_agent/grounding/source_registry.py` — `ARBITRARY_CODE_TOOLS` **splits** into
  `MODEL_AUTHORED_CODE_TOOLS` and `GENERATIVE_TOOLS`; only the former loses its categorical branch,
  and `GENERATIVE_TOOLS` keeps it verbatim. `Entitlement` gains `OBSERVED`, `SourceKind` gains
  `OBSERVATION`, and a code-level ordering is **not** added (D3).
- `src/personal_agent/grounding/containment.py` — the coverage predicate is exposed for arm 1 of
  the invocation-composition check; no change to the normalization contract. Arm 1's caller must
  consume `ContainmentResult.contained`, not `.passed` (D2). Arms 2 and 3 need two further
  helpers over the **existing** `normalize_tokens` contract, so the tolerated-variance classes are
  inherited rather than re-litigated: a maximal-literal-run extractor over an invocation string, and
  a non-figure-content-token set. Neither introduces a second normalization.
- `src/personal_agent/grounding/citations.py` (cont.) — near-miss resolution for D7. The near-miss
  detector (built by D1's ticket) yields the candidate string; resolution matches it against
  registered sources on **registry-minted attributes only** — `origin`, and a digest prefix of at
  least **8** hex characters — and never against the whole minted identifier, which a near-miss by
  definition fails to reproduce, nor against `label`, which for memory sources derives from
  agent-influenceable knowledge-graph content (`captains_log/turn_evidence.py:240`). A source is
  returned only on a match **unique across the registry**. Ambiguous and no-match are reported
  distinctly — both yield `UNRESOLVED`, but only the first is worth counting, and the ambiguous case
  is the majority: FRE-1327's own trace holds four `bash` sources.
- `src/personal_agent/grounding/verification.py` — `CheckOutcome` gains **`INVOCATION_COVERED`** as
  a real member, not a telemetry string. It is a **rejection** outcome — a source existed and was
  caught — and **must not** join `_TRUE_NO_SOURCE` or `_MACHINE_UNDECIDED`: membership there feeds
  `TurnVerification.true_no_source` and `GroundingRecord.no_source_count`, which mean *no admissible
  source exists*, and would misreport a caught laundering attempt as an absence of evidence. No
  special wiring is needed beyond the member: blocking, the D4 retry directive and serialization
  already operate generically on any non-`PASSED` outcome. Span checks gain the negative-containment
  clause for sources flagged `invocation_check_required`, evaluated against **the cited source's own
  recorded invocation** and running **all three arms** (D2); `OBSERVATION` sources route to the
  inline-entailment path, keyed on the registry-assigned `SourceKind` only. `CheckOutcome` also gains exactly one
  member for D7 — **`MALFORMED_CITATION`** — barred from `_TRUE_NO_SOURCE` and `_MACHINE_UNDECIDED`
  for `INVOCATION_COVERED`'s reason: it is reached only where a source existed *and supported the
  claim*, so counting it as an absence corrupts `no_source_count`. A near-miss that resolves to no
  source or to more than one takes the **existing** `UNRESOLVED`, which is already correctly inside
  `_TRUE_NO_SOURCE` and needs no change. A near-miss that resolves uniquely runs **the standard gate
  sequence unmodified** — entitlement, reachability, containment — and keeps whatever rejection it
  yields; only a `PASSED` is rewritten to `MALFORMED_CITATION`. That rewrite is the final step and is
  applied **after** any asynchronous entailment pass, so a later `ENTAILMENT_REQUIRED` → `PASSED`
  transition cannot reopen the admission path behind it. Implementing D7 as "skip the gates for
  near-misses" would lose the detections it exists to produce; implementing it as a second copy of
  the gate order would drift from the first.
- `src/personal_agent/grounding/source_registry.py` (cont.) — `RegisteredSource` retains the
  invocation text that produced it and carries an `invocation_check_required` flag, set from the
  tool's classification (`MODEL_AUTHORED_CODE_TOOLS` plus query-language tools such as `mcp_esql`)
  and **independent of `Entitlement`**. Entitlement assignment for a `MODEL_AUTHORED_CODE_TOOLS`
  result additionally consults the **agent-writable address list** (D3): a result whose invocation
  addresses one of those stores registers `AGENT_DERIVED` rather than `OBSERVED`. The list is
  configuration, not a literal in the checker, so adding a store does not require a code change; its
  members at authoring time are the sandbox scratch directory, agent-authored artifact rows, and the
  knowledge graph. `_register`'s reuse key widens from `(kind, origin, content)`
  to include the invocation for flagged sources, without which two `bash` calls returning identical
  bytes collapse to one source carrying the first call's invocation — the defect that would make
  AC-10 unsatisfiable. The executor already passes the arguments alongside the content
  (`_register_tool_source`, whose call site notes "the arguments travel with the content"), so this
  is retention, not new plumbing. `_tainted` keeps its existing write-then-read job and is **not**
  repurposed.
- `src/personal_agent/orchestrator/executor.py` — attachment registration;
  `grounding_verification_completed` gains D1's fields, including **both** span-outcome maps —
  `observed_span_outcomes` keyed on entitlement and `invocation_checked_span_outcomes` keyed on the
  `invocation_check_required` flag — each split by the outcome received rather than by a fixed list,
  since a `bash` span belongs to both; near-miss marker counting, split by whether D7 resolved.
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

> **PARTIALLY LAPSED 2026-09-02 (FRE-1357).** With D2, D3 and most of D7 withdrawn, the criteria
> written for them lapse with them. **Standing:** **AC-4** (a zero-compliance turn is diagnosable
> from its own document), **AC-6** (the alert fires), **AC-7** (near-miss detection is
> discriminating) and **AC-8** (image spans are checked in both directions). **AC-4 is amended** —
> see below. **Lapsed entirely:** AC-1, AC-2, AC-3, AC-5, AC-9, AC-10, AC-11, AC-12, **AC-13** and
> AC-14, retained below unedited as the record of what the withdrawn decisions would have had to
> prove. ADR-0140 carries the criteria for the replacement path.
>
> **AC-13 does not survive in part, and an earlier draft of this note wrongly said it did.** Its arm
> (a) reaches `UNRESOLVED` *through the resolver* — matching `bash` by origin, applying the
> eight-character digest floor, concluding ambiguity — and the resolver is withdrawn. The surviving
> D7 obligation is therefore restated, not salvaged:
>
> - **AC-15 — A citation-shaped near-miss is recorded as a naming failure, not as silence.** A span
>   whose **only** citation-shaped marker fails `CITATION_MARKER_PATTERN` scores **`UNRESOLVED`**, not
>   `UNCITED`; a span carrying a valid marker alongside a near-miss binds the valid one and may score
>   `PASSED`. No resolution against the registry is performed and none is required — a marker that
>   names nothing resolvable *is* the `UNRESOLVED` case. · **Check:** replay FRE-1327's recorded
>   `[S@bash-tempo-trace-dba5b2]`; a multiplicity probe for the second arm; and an ES query asserting
>   `UNRESOLVED` is non-zero over a window in which `near_miss_markers` is non-zero. · *Fails if* the
>   FRE-1327 span still scores `UNCITED`; **or** if it scores any outcome that implies a source was
>   selected (`NOT_CONTAINED`, `SOURCE_NOT_ENTITLED`) — that would mean a resolver was implemented
>   after all, and it is the false-detection channel D7's own review rejected; **or** if a stray
>   near-miss prevents an otherwise compliant span from passing; **or** if `UNRESOLVED` stays at 0
>   while near-misses are counted. **Reaching this at all requires the span to bind the near-miss**,
>   which `parse_citations` does not do today — `_verify_span` short-circuits to `UNCITED` at
>   `verification.py:309`. **The binding rule is not left to the implementer**, because an
>   under-specified binder is either special-cased to FRE-1327's fixture or over-broad: a near-miss
>   binds by **exactly the rule a valid marker binds by** — the contiguous text from the end of the
>   previous marker (valid or near-miss) up to its own opening bracket, whitespace trimmed
>   (`citations.py`'s stated binding rule) — and its own characters are **stripped from the span text
>   before `claim_unit` sees them**, on the same ground that marker characters are protocol rather than
>   content. Without that strip the marker mints its own required tokens: measured, `S` folds to the
>   unit synonym `second`. · *Additionally fails if* the near-miss changes the binding of any
>   **other** span in the same output, which is how an over-broad binder shows itself.

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
admission-dependent controls live in **AC-1, AC-2, AC-3, AC-5, AC-8, AC-9, AC-10, AC-11, AC-12 and
AC-14**, and adjudication requires all fourteen; no individual criterion is claimed to do work it
cannot do. AC-13 joins AC-4, AC-6 and AC-7 in the group that cannot carry a positive arm: D7's
lattice bars `PASSED` by construction, so "a near-miss span passes" is not an outcome any correct
implementation can produce.

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
  again). *Scope, per D6:* the probe set is a **frozen fixture list with preregistered expected required
  tokens and outcomes**, not a population selected at run time. An earlier draft defined
  "literal-form" as `check_containment(span, invocation).contained == True`, which is circular — it
  selects the test population using the predicate under test, so a containment regression could
  reclassify a formerly covered probe as out of scope instead of failing. Adjudication is therefore
  two-stage against an independent oracle: **(i)** `check_containment` reports each frozen fixture's
  preregistered required tokens as covered by its invocation; **(ii)** verification converts that
  result into `INVOCATION_COVERED`. A regression in either stage fails, and neither can shrink the
  fixture list. Encoded forms (hex escapes, `--pretty=format:%x50`, concatenation, ANSI-C quoting,
  variable expansion, unquoted heredocs) are **out of scope by decision**, not oversight, and are
  listed as such in the fixture file so the boundary is recorded rather than inferred. The same
  fixture file carries the two round-5 out-of-scope entries with their reasons: the **cross-call
  shared-state channel**, and the **one-content-token authored frame with a non-figure hole**
  (`echo "Capital: $(whoami)"`). An out-of-scope entry without a recorded reason is an undeclared
  gap, not a decision, and adjudication rejects the fixture file if one appears.

- **AC-2 — Real tool evidence becomes citable, and containment still bites.** On one probe family
  built from the same `bash` output: **(a)** an assertion whose value appears in the output scores
  `PASSED`; **(b)** an assertion about the same output whose value does **not** appear in it scores
  `NOT_CONTAINED`. · **Check:** paired probes, both arms required on every family. · *Fails if*
  arm (b) passes — the ticket's own falsification clause, an unmeasurable 100% replacing an honest
  0% — or if arm (a) fails.

- **AC-3 — Confabulation against present evidence is detected, and not only on the case we named.**
  On a held-out set of recorded turns, not enumerated here, whose responses assert figures absent
  from their own tool output **and carry a resolvable citation**, those figures score
  `NOT_CONTAINED`. Trace `dba5b2cba1e0bece6c8b9396465a265c` carries a **separate and weaker**
  obligation, because its citation resolves to nothing: its fabricated figures move from `UNCITED` to
  `UNRESOLVED`. · **Check:** offline replay
  against stored turn records; tool content comes from the record, never re-executed. · *Fails if*
  those spans still score `UNCITED`; **or** if the named trace passes while the held-out set does
  not — the signature of an implementation special-cased to the one trace this ADR cites.
  **Round 5 rewrote this criterion because its named trace cannot carry it.** The trace's response
  cites `[S@bash-tempo-trace-dba5b2]`; the parser rejects it, so `_verify_span` short-circuits to
  `UNCITED` before containment, and D7 does not repair it either — the string matches no minted
  identifier (`S{ordinal}@{16-hex}`) and resolves ambiguously against the **four** `bash` sources
  that trace holds. **`NOT_CONTAINED` is unreachable on trace
  `dba5b2cba1e0bece6c8b9396465a265c`, by construction and permanently.** The criterion is therefore
  adjudicated on an **equivalent** — held-out recorded turns asserting figures absent from their own
  tool output — and the named trace carries a **separate, weaker** obligation: **its spans move from
  `UNCITED` to `UNRESOLVED`**, which is all D7 claims for the ambiguous case. · *Additionally
  fails if* the equivalent set is drawn entirely from one route: it must contain turns whose citation
  is well-formed (the D2 route) **and** turns whose near-miss resolves unambiguously (the D7 route),
  since a single-route set cannot distinguish an implementation that shipped half the decision;
  **or** if the named trace's spans still score `UNCITED`; **or** if any adjudication reports
  `NOT_CONTAINED` on the named trace, which would mean the resolver picked one of four candidates
  arbitrarily — a false detection, not a pass.

- **AC-4 — A zero-compliance turn is diagnosable from its own document.** Over a post-deploy window
  containing **at least the preregistered minimum** of turns with `passed_spans == 0` (seeded if the
  natural rate is below it), `turn_evidence_class` and the `tool_results_*` counts on that same
  document determine whether a non-zero was reachable, and `tool_results_admitted` reconciles
  against the source list in that trace's `source_registry_snapshot`, **filtered to tool-kind sources
  by `origin`** — the snapshot's `source_count` is every registered source and always includes the
  user message registered at turn start (`orchestrator/executor.py:3624`), plus any memory sources, so
  an unfiltered `tool_results_admitted == source_count` is false even for a correct implementation
  (corrected 2026-09-02, FRE-1357). · **Check:** ES query over
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
  **Check:** both span-outcome aggregations from D1 — `observed_span_outcomes` and
  `invocation_checked_span_outcomes` — read separately and never summed. · *Fails if* `OBSERVED`
  spans exist but none pass (registration without usability); **or** if `invocation_covered` is zero
  across AC-1's probes while `passed` is high (registration without checking — the vacuous
  implementation); **or** if `passed` is high while AC-2 arm (b) regresses; **or** if either field is
  absent, empty, or populated from the other's key — one field standing in for two questions is the
  defect round 5 was called to fix. **`uncitable_turn_rate`'s fall is
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

- **AC-10 — A refused covering invocation is repairable by retry, on byte-identical evidence.** A
  turn whose first attempt asserts from a covering invocation (`rg 'fish high mercury' report.txt`)
  is refused `INVOCATION_COVERED`; the D4 retry issues `cat report.txt` over a file containing
  **only** that line, so the two results are **byte-identical**, and the assertion **passes**. ·
  **Check:** a two-attempt probe driven through the real D4 loop, not a unit call to the checker. ·
  *Fails if* the second attempt is refused for the first attempt's invocation — which is what
  happens if source identity still dedupes on `(kind, origin, content)`, since both calls have
  origin `bash` and identical content, so the retry silently reuses the covering source — **or** if
  the first attempt is not refused, which would mean the negative check never fired and the probe
  proved nothing. *The byte-identical construction is deliberate: a probe whose two results differ
  would pass without exercising the deduplication defect this criterion exists to catch.*

- **AC-11 — The query-language path is checked, in both directions.** A composing ES|QL call
  (`ROW claim = "Paris has 9 million residents"`) registers a source whose span is refused
  `INVOCATION_COVERED`; a genuine index-reading ES|QL query over telemetry **retains its citation
  and passes**. · **Check:** paired `mcp_esql` probes, both arms; then read each outcome back off
  the emitted telemetry rather than off the checker's return value. · *Fails if* the composing arm
  passes (FRE-1306 unresolved); **or** if the index-reading arm loses its citation (the outcome
  FRE-1306's Option A was rejected for); **or** if the negative check never runs on `mcp_esql` at
  all, which is what happens if the clause is keyed on `Entitlement`/`OBSERVED` rather than on
  `invocation_check_required`; **or** — the round-5 arm — **if the composing refusal does not appear
  in `invocation_checked_span_outcomes`** on that turn's `grounding_verification_completed` document.
  Re-keying the aggregation without demonstrating that this case lands in it does not satisfy this
  criterion: `mcp_esql` is `EXTERNAL`, so it appears in no `OBSERVED`-keyed surface, and the
  detection would again be invisible to the ADR's own instrumentation.

- **AC-12 — All three arms of the invocation-composition check fire, and each is the *only* arm that
  catches its fixture.** The frozen fixture file carries, **per arm**, at least one
  composition that the **other two arms both miss**: for arm 1, a fully-literal figure payload
  (`printf '2026'` asserting *"There are 2026."*); for arm 2, an entity hole
  (`echo "Paris has 9 million $(whoami)"`); for arm 3, a one-content-token frame with a computed
  figure (`echo "found: $(ls | wc -l)"`). It additionally carries compositions that trip more than
  one arm — `printf 'Paris has 9 million residents'` trips all three — so the file records overlap
  rather than implying exclusivity. Each such span
  is refused `INVOCATION_COVERED`. · **Check:** run the fixture set with each arm individually
  disabled and record which fixtures survive. · *Fails if* any fixture survives with **all** arms
  enabled — an arm is not implemented; **or** if disabling any single arm fails to free that arm's
  designated fixture, each of which is measured to be caught by that arm alone: **arm 1** →
  `printf '2026'` asserting *"There are 2026."*; **arm 2** → `echo "Paris has 9 million $(whoami)"`;
  **arm 3** → `echo "found: $(ls | wc -l)"`. An implementation that stubbed any one arm would
  otherwise pass. **All three arms carry such a fixture** — round 5's first draft claimed arm 1 was
  retained "for precision, not closure" and its review refuted that with `printf '2026'`, which arm 2
  misses (one-token literal run) and arm 3's non-empty guard disables. · *Also fails if* the
  false-rejection controls regress. **The controls are named because round 5's own review broke them:** `cat
  report.txt` over a file holding `2026` yields required `('#2026',)` and an **empty** non-figure
  set, which made arm 3 vacuously true and rejected it. So `cat` over numeric-only content, `date
  +%Y`, `ls` with numeric filenames, a scalar `psql -tAc`, and `grep 'mercury' file.txt` →
  *"this fish is high in mercury"* must **all** register and pass. · **No exclusivity is asserted
  and none is tested.** The arms overlap: `printf 'Paris has 9 million residents'` trips all three,
  and `echo "the fish is high in $(basename /x/mercury)"` trips arms 2 and 3 because `mercury`
  reaches the invocation through the path. **All three arms are load-bearing**, each on the
  fixture named above, and this criterion tests all three independently. *It exists because an
  implementation shipping arm 1 alone satisfies AC-1 in full — AC-1's probes are all
  fully-literal — and, symmetrically, because round 5's own first draft dismissed arm 1 as
  redundant when `printf '2026'` shows it is not.*

- **AC-13 — A malformed marker becomes a rejection with a reason, and never an admission.** All
  three D7 rows hold. **(a) No or ambiguous match → `UNRESOLVED`**, gates not run: replaying
  FRE-1327's recorded `[S@bash-tempo-trace-dba5b2]` against that trace's registry yields `UNRESOLVED`:
  segment `bash` matches four sources by origin, and segment `dba5b2` sits below the 8-character
  digest floor, so the match is ambiguous. **(b) Unique match, gates reject → that gate's
  own outcome**: a near-miss resolving to an `AGENT_DERIVED` source yields `SOURCE_NOT_ENTITLED`, not
  a containment outcome, because D7 defers to the standard sequence rather than restating it.
  **(c) Unique match, gates would pass → `MALFORMED_CITATION`**, never `PASSED`.
  `MALFORMED_CITATION` is absent from `_TRUE_NO_SOURCE` and `_MACHINE_UNDECIDED`; a turn whose spans
  are all row (c) reports `no_source_count == 0`, and one whose spans are all row (a) reports
  `no_source_count` equal to its span count. · **Check:** unit assertions per row; the FRE-1327
  replay; frozenset membership assertions; resolver tests that `[S@a]` (one hex character) does **not**
  resolve and that `[S@bashful]` does **not** match a `bash` source (segment equality, not substring);
  a **multiplicity** probe — a span carrying both a valid marker and a near-miss binds the valid one
  and may score `PASSED`; and a scan of the verification path asserting no code path yields `PASSED`
  for a span whose *selected binding* is a near-miss, including after the asynchronous entailment
  pass. · *Fails if* the FRE-1327 span
  still scores `UNCITED` (D7 not implemented); **or** if it scores anything other than `UNRESOLVED`
  — `NOT_CONTAINED` and `MALFORMED_CITATION` both mean the resolver picked one of four candidates
  arbitrarily, a false detection worse than the silence it replaces; **or** if any span whose selected
  binding is a near-miss scores `PASSED` (Option 9's admission channel); **or** if a near-miss
  adjacent to a valid marker prevents that span from passing — a stray near-miss must not degrade a
  compliant span; **or** if `MALFORMED_CITATION` joins either frozenset — round 3
  caught this accounting defect for `INVOCATION_COVERED`, round 5 reproduced it on `UNRESOLVED`, and
  this criterion exists so a third instance fails a test rather than a review; **or** if row (a)
  spans are booked outside `_TRUE_NO_SOURCE`, which would under-count genuine no-source turns; **or**
  if `UNRESOLVED` stays at 0 documents over a post-deploy window in which `near_miss_markers` is
  non-zero, meaning near-misses are still counted and never adjudicated.

- **AC-14 — The terminus rule binds, and it binds on the address.** On a probe family differing
  **only** in the address read: for **each** of D3's normative address classes — the sandbox scratch
  directory, agent-authored artifact rows, and the knowledge graph — a read addressed into it
  registers `AGENT_DERIVED` and its span is refused `SOURCE_NOT_ENTITLED`, while a read of
  byte-identical content addressed outside every class registers `OBSERVED` and its span **passes**.
  · **Check:** one paired probe **per address class**, contents byte-identical within each pair,
  asserting the registered entitlement and the per-span outcome. · *Fails if* any single class is
  untested or untriggered — a rule implemented as a prefix test against the one path family this
  criterion happened to probe satisfies a single-pair version while leaving artifact rows and the
  knowledge graph unguarded, which is the gap round 5's review found in this criterion's first
  draft; **or** if both arms of any pair register the same entitlement — either the rule is absent (both `OBSERVED`, and
  FRE-1338's shape is live on the filesystem) or it is a blanket denial of read-back (both
  `AGENT_DERIVED`, which is Option 10 and costs D2 its own worked example); **or** if the two arms
  differ for any reason other than the address, which the byte-identical construction exists to rule
  out. *The residual is stated rather than tested: this criterion cannot distinguish an
  agent-authored file outside the address list from an owner-authored one inside it. That is the gap
  the cross-turn write ledger closes, and it is ticketed.*


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
- [FRE-1327](https://linear.app/frenchforest/issue/FRE-1327) — the confabulation case study. Its
  span becomes **`UNRESOLVED`** rather than `UNCITED` (D7), its marker resolving ambiguously against
  four `bash` sources; `NOT_CONTAINED` and `MALFORMED_CITATION` are both unreachable on this trace
  and the ADR claims neither. The confabulation itself is **not** resolved here
- [FRE-1284](https://linear.app/frenchforest/issue/FRE-1284) — the compliance metric this
  de-confounds
- [FRE-1285](https://linear.app/frenchforest/issue/FRE-1285) — enforcement selection, which keys on
  that metric
- [FRE-1302](https://linear.app/frenchforest/issue/FRE-1302) · [FRE-1303](https://linear.app/frenchforest/issue/FRE-1303)
  — the memory entitlement axis, untouched here and named in D3's provisional ordering
- [FRE-1306](https://linear.app/frenchforest/issue/FRE-1306) — `mcp_esql` admits a model-authored
  literal; resolved by D2's negative check, which needs neither a parse nor a select/compose line
- [FRE-1325](https://linear.app/frenchforest/issue/FRE-1325) — nothing consumes the grounding signal
- [ADR-0098](ADR-0098-memory-substrate-and-lifecycle-architecture.md) **Amendment A**, §A6 — entitlement
  follows the provenance terminus; the narrowing this ADR's `OBSERVED` widening had to be composed
  with, and the source of D3's address-level terminus rule
- [FRE-1338](https://linear.app/frenchforest/issue/FRE-1338) — a model can cite pages it never read;
  the leak Amendment A closes in the knowledge graph and D3 addresses on the filesystem
- [FRE-1347](https://linear.app/frenchforest/issue/FRE-1347) — Amendment A T3, which carries the
  ADR-0138 D2 amendment record as its AC-4 and is blocked on this round
- [FRE-1349](https://linear.app/frenchforest/issue/FRE-1349) — round 5: the three verified defects
  above and the two-amendment coordination problem

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

### 2026-08-29 - Review round 3 (Codex, adversarial — on the round-2 deltas)

**Changed By:** `adr` session, on FRE-1328
**Reason:** Six blocking findings against round 2's fixes. Code claims re-verified before acting.

- **The FRE-1306 promise was unreachable as written.** The negative clause was keyed on `OBSERVED`
  sources; `mcp_esql` is a typed retrieval earning `EXTERNAL`, so the clause would never have run on
  the one case the ADR repeatedly claimed to resolve. The check is now keyed on a registry-assigned
  `invocation_check_required` flag, independent of entitlement — *can this call have composed its own
  result* is a different question from *how far do we trust it*, and conflating them is what broke
  the promise. AC-11 asserts both arms.
- **AC-10's "recoverable by construction" was false.** `_register` dedupes on
  `(kind, origin, content)`; `rg` and `cat` share origin `bash`, so byte-identical output collapses
  to one source **carrying the first, covering invocation** — the retry is refused again. The
  invocation now participates in source identity for flagged sources, and AC-10 is rewritten to use
  the byte-identical case so it actually exercises the defect.
- **Per-source scope reopened a channel turn-scoped had closed.** Two concurrently dispatched `bash`
  calls, one carrying a payload in its command line and one running `ps -eo args`, launder without
  touching disk. This is **conceded in D6**, not closed: restoring the turn-scoped check would
  reintroduce the D4 poisoning, trading a defect that fires on ordinary turns against one requiring
  a deliberately staged side channel. The residual is now stated in its general form — any channel
  carrying one call's invocation into another call's output — rather than as "two `bash` calls".
- **`INVOCATION_COVERED` was assigned to the wrong accounting family.** Round 2 placed it with
  `_TRUE_NO_SOURCE`, which feeds `no_source_count` and means *no admissible source exists* — it
  would have reported a caught laundering attempt as an absence of evidence. It is now specified as
  a rejection outcome barred from that set, and the note records that blocking, retry directives and
  serialization already work generically for any non-`PASSED` outcome, so no further wiring is owed.
- **AC-1 was circular.** Defining "literal-form" as `check_containment(...).contained == True`
  selects the test population with the predicate under test, so a containment regression could
  shrink the population instead of failing. Replaced with frozen fixtures carrying preregistered
  required tokens, adjudicated in two independent stages.
- Stale turn-scoped wording removed from the normative rule and the consequences; the duplicated
  false-rejection consequence merged; the generative tools enumerated in the D2 table as well.

**Review budget note.** The `/adr` contract caps Codex review at three rounds and this is round 3, so
**these fixes have not themselves been adversarially reviewed.** Every prior round found real
defects in the previous round's fixes, and round 3's changes are structural — a new registry flag, a
widened deduplication key, and a conceded channel. That is the residual risk on this document, and
it is recorded here rather than left for a reader to discover.

### 2026-09-01 - Review round 5 (`adr` session, on FRE-1349)

**Changed By:** `adr` session, on FRE-1349
**Reason:** Three blocking defects found by Fable's round-4 review and independently verified by
master, plus the coordination problem created when ADR-0098 Amendment A landed a second, opposite
amendment against ADR-0138 D2. Round 4's fixes had **not** been reviewed — the round-3 note recorded
that as the document's residual risk, and this round is that review. All code claims below were
re-verified in source before acting. Nothing from this ADR had reached `src/`, so every defect was
still a text defect.

- **`NOT_CONTAINED` was unreachable on the trace the ADR names, and the reason is worse than a dead
  branch.** `_verify_span` (`verification.py:295`) returns `UNCITED` the moment `_identifier_for`
  yields `None`. FRE-1327's model wrote `[S@bash-tempo-trace-dba5b2]`, which fails
  `CITATION_MARKER_PATTERN` (`citations.py:45`), so no `CitedSpan` exists and every gate after the
  first is unreachable. **Registering the `bash` output does not make an unparseable marker parse** —
  so D6's headline claim (*"converts the system's blindest failure into its most legible one"*) and
  AC-3 were both false for the one trace this document is built on. **D7** is new: a citation-shaped
  near-miss stops short-circuiting to `UNCITED`: it resolves on registry-minted attributes and, on a
  unique match, runs the standard gate sequence, with `CheckOutcome.MALFORMED_CITATION` reserved for
  the one row where the gates would otherwise have returned `PASSED`. `PASSED` is barred from the lattice by construction, so the model's own text can
  select among registered sources but never create admissibility; Option 9 records the permissive
  version and why it was rejected.

  **Round 5's first draft of D7 was itself wrong, in two ways its own review caught.** It resolved
  near-misses against **minted identifiers**, which are `S{ordinal}@{16-hex-digest}` — a string a
  near-miss by definition fails to reproduce — so it would have delivered nothing; resolution is now
  by **source attribute**. And it clamped a contained near-miss to `UNRESOLVED`, which is a member of
  `_TRUE_NO_SOURCE` (`verification.py:120`) and feeds `no_source_count`, so a botched citation
  against present evidence would have been booked as an **absence** of evidence — the identical
  accounting defect round 3 caught for `INVOCATION_COVERED`, reproduced on a different outcome value
  one round later. Hence a new member barred from that set.

  **The consequence is a claim withdrawn, not merely relocated.** FRE-1327's trace holds **four**
  `bash` refusals, so `bash-tempo-trace-dba5b2` resolves ambiguously and containment never runs on
  it. **`NOT_CONTAINED` is unreachable on trace `dba5b2cba1e0bece6c8b9396465a265c`, permanently.**
  What the ADR delivers there is the narrower and honest thing — the span stops reading as *the model
  did not try* and reads as **`UNRESOLVED`** — the model named a source that does not exist. AC-3 is
  adjudicated on an equivalent, and AC-13 fails an implementation that reports `NOT_CONTAINED` **or**
  `MALFORMED_CITATION` on the named trace, since either could only come from picking one of four
  candidates arbitrarily.
- **The monitoring was blind to the case the ADR headlines — round 3's fix, one layer too shallow.**
  Round 3 correctly re-keyed the D2 **check** from `OBSERVED` to `invocation_check_required` and
  wrote down why the two answer different questions. It left the **metric** keyed on `OBSERVED`.
  `mcp_esql` is `EXTERNAL` (`source_registry.py:346`, docstring at `:787`), so the FRE-1306 detection
  would have been refused correctly and then been invisible to the surface this ADR designates as its
  evidence. D1 now emits **two** fields — `observed_span_outcomes` (entitlement) and
  `invocation_checked_span_outcomes` (flag) — which do not nest, since an attachment is `OBSERVED`
  without an invocation and `mcp_esql` is invocation-checked without `OBSERVED`. AC-11 now requires
  the refusal to be **observed landing in** the second field, not merely for the aggregation to be
  re-keyed.
- **The negative check failed open on partial composition, which is a literal form, not an encoded
  one.** `echo "Paris has $(ls | wc -l) million residents"` produces a fabricated world-fact in one
  round trip: coverage over required tokens is all-or-nothing, so the single computed token `#3`
  leaves `missing` non-empty, `.contained` false, and the span passing. D6's claim to close
  laundering's *literal* forms was therefore an overclaim. D2's check now has **three arms** — span
  coverage, fragment contiguity at ≥ 2 content tokens, and non-figure coverage of the result — each
  closing a class the others miss, with AC-12 requiring each of arms 2 and 3 to be load-bearing on a
  fixture the others do not catch. **Two further defects in round 5's own first draft were caught by
  its review.** Arm 3 was stated as "the invocation supplies every non-figure content token of the
  result" — a universal quantification that is **vacuously true over an empty set**, so
  `cat report.txt` over a file holding `2026` (required `('#2026',)`, non-figure set empty) was
  rejected, along with `date +%Y`, numeric `ls` and scalar `psql`; arm 3 now carries a non-empty
  guard. And the arms were presented as each closing a distinct class, which measurement refutes:
  `printf 'Paris has 9 million residents'` trips all three and the entity-free fixture trips two, so
  the exclusivity claim is withdrawn. *(That draft then went too far the other way and called arm 1
  "retained for precision, not closure"; a later review round refuted it with `printf '2026'` and
  restored all three arms as load-bearing — see below.)*
  The D2 worked example's token arithmetic was also wrong and is now measured — `claim_unit("Paris
  has 3 million residents")` returns `('paris', '#3000000', 'residents')`, folding the magnitude word
  into the figure, not the `{paris, #3, million, residents}` the draft asserted. **The refinement that
  looked principled was rejected as a trap** (Option 7):
  keying the fragment arm on entity/figure membership rather than a token count readmits
  `echo "the fish is high in $(basename /x/mercury)"`, reinstating the entity-free vacuity round 1
  of this document already found once in the `.passed`/`.contained` choice. D6 now carries a table of
  which forms are closed and which remain open, replacing the sentence that was wrong.
- **D2 is amended once, with both directions in view (FRE-1349 AC-4).** ADR-0139 widens
  admissibility with `OBSERVED`; Amendment A A6 narrows it by making entitlement follow the
  provenance terminus. D3 now states how they compose rather than placing them adjacently: **the
  terminus rule follows the bytes, not the tool.** A live observation has no chain, so A6 has nothing
  to test and `OBSERVED` stands; a **read-back of persistent state is a retrieval wearing an
  observation's clothes** and inherits A6's terminus test whatever tool carries it. The seam is
  concrete, not abstract — `bash("cat /tmp/notes.md")` over a file the agent wrote in an **earlier**
  turn is FRE-1338's shape on the filesystem, and `_taint` cannot see it because it is turn-scoped and
  matches argument values exactly (`source_registry.py:1076`). The rule binds at the **address**,
  because that is decidable today; the cross-turn write ledger that would bind it to the author is
  filed rather than folded in. Option 10 records the default-deny alternative, rejected because it
  denies `cat report.txt` a citation and so removes D2's own worked example.
- **Two stale claims retired.** D3's *"provisional ordering"* paragraph deferred the
  memory-at-`EXTERNAL` inconsistency to FRE-1302/FRE-1303; A6 has since resolved it in master's
  direction, so the deferral is withdrawn rather than restated. D5's *"Memory entitlement is
  untouched"* became false on 2026-08-30 and now says what actually changed it.
- **AC-12, AC-13 and AC-14 are new**, one per round-5 decision; AC-1's fixture file gains the two
  round-5 out-of-scope entries with their reasons, and rejects any out-of-scope entry lacking one.

**Round 5's own adversarial review (Codex), and what it found.** FRE-1349 AC-5 required round 5's
fixes to be reviewed rather than shipped on the strength of having been written carefully. It
returned **seven blocking findings, all confirmed against source before acting**, and five of them
were defects in round-5 text rather than in what round 5 inherited: D7 resolving against identifiers
it could never match; the `UNRESOLVED` clamp corrupting `no_source_count`; arm 3 vacuous on a
figure-only result; the arms' exclusivity claim refuted by measurement; AC-14 satisfiable by a
one-prefix implementation leaving two of three address classes unguarded. It also found that
`echo "Capital: $(whoami)"` — accepted here as a residual — contradicted the absolute phrasing of the
ADR-0138 amendment note, which is now stated with its reach rather than as a totality. Every code
claim in this ADR that a reader could check was re-derived by running the normalizer rather than
reasoning about it, which is how the token-arithmetic error was found.

**A second review round, on the first round's fixes, found five more.** Every one was against text
round 5 had just written, and three of them changed the design again. **D7's single new outcome
collapsed two accounting families** — a near-miss matching nothing means no source was brought to
bear, while a near-miss matching one supporting source means a source existed and only the marker was
wrong; one enum member cannot sit both inside and outside `_TRUE_NO_SOURCE`, so the first row now
takes the **existing** `UNRESOLVED` and only the third takes the new member. **D7's closed lattice
contradicted gate ordering**: a near-miss resolving to an `AGENT_DERIVED` source is rejected
`SOURCE_NOT_ENTITLED` at `verification.py:330` long before containment, so D7 now defers to the
standard gate sequence instead of keeping a second, drifting copy of it. **Resolution by "identifying
attributes" was underspecified into a false-detection channel** — a one-hex-character digest prefix
would resolve "unambiguously" by coincidence, and `label` is agent-influenceable for memory sources
(`captains_log/turn_evidence.py:240`), so matching is now restricted to `origin` plus a ≥8-character
digest prefix. It also refuted round 5's claim that **arm 1 is retained "for precision, not
closure"**: `printf '2026'` is a fully-literal payload that only arm 1 catches, and the example
offered for the precision framing was itself wrong, since arm 2 refuses that whole source anyway.
Three stale assertions that FRE-1327 becomes `NOT_CONTAINED` survived in normative text — D1, the
Positive Consequences, and AC-3's own opening clause — and are now consistent with the withdrawal.

**A third review round found five more, and one invalidated a table this document had just presented
as measured.** Arm 2 was written as *"some maximal literal run of the invocation"* — which reads well
and defines nothing, since isolating a quoted argument from its command head needs a shell parse this
ADR refuses everywhere else. Under the shipped normalizer `normalize_tokens("printf '2026'")` is
`('printf', '#2026')` — **two** tokens — so the arm-1 fixture introduced one round earlier was wrong
as stated. Arm 2 is now defined without any parse: the **longest common contiguous run** of
`normalize_tokens(invocation)` and `normalize_tokens(result)`, threshold 2, with the per-fixture table
re-measured against the real normalizer rather than argued. The round also found three surviving
`MALFORMED_CITATION`-for-FRE-1327 assertions in normative text, where the correct outcome after the
three-outcome split is `UNRESOLVED`; AC-12 simultaneously requiring and denying arm 1's load-bearing
status; **no precedence rule for a span carrying several markers**, now settled as *a well-formed
marker always wins*, so a stray near-miss cannot degrade a compliant span; and origin matching left
ambiguous between substring and segment, now fixed as **exact segment equality**.

**The standing risk, restated rather than retired.** Round 5 found defects in round 4's fixes; round
5's first review found defects in round 5's fixes; its second found defects in those; its third found
defects in those. That is this document's whole history and it has not stopped. The changes remain structural — a new
`CheckOutcome` member, a check that went from one arm to three, an entitlement rule consulting an
address list, and a headline claim withdrawn. **A further round would be a reasonable thing to want**,
and the honest summary is that this document converges slowly because each fix is load-bearing enough
to have failure modes of its own.

### 2026-09-02 - Review round 6 (`adr` session, on FRE-1357) — partially withdrawn

**Changed By:** `adr` session (FRE-1357), on the owner's Route 1 decision
**Reason:** D2, D3 and D7 reviewed adversarially, with every code claim verified in source and every
token-arithmetic claim produced by running the shipped normalizer. The round was commissioned to
answer one question beyond finding defects: **is the churn defects, or is it altitude?**

**The convergence answer: 4 blocking findings, against 7 / 5 / 5 in rounds 1–3. The count fell; the
trend did not flatten — and the diagnosis is altitude.** Three of the four findings are the previous
round's answer reappearing one level down:

- Round 3 replaced arm 2's undefined "maximal literal run" with a token rule. Round 6 measured that
  the token rule false-rejects the shape D2 exists to protect: `grep 'passed_count' logs.json` (run
  of 2 — snake_case splits on `_`), `rg 'source_registry tool' src/` (3), `git log --grep='cost
  gate'` (2), `psql "SELECT name, city"` against its own header (2), ES|QL `KEEP trace_id,
  passed_count` (4). Arm 2 is source-scoped, so each rejects the **whole source** — directly
  contradicting **AC-11**, which requires an index-reading ES|QL query to keep its citation. AC-12's
  false-rejection controls contain none of these shapes, so AC-12 would have gone green.
- Round 3 set precedence between marker *kinds* (a valid marker wins over a near-miss). Round 6
  found precedence between resolution *attributes* undefined, and the default perverse: a candidate
  matches on `origin` **or** an ≥8-hex digest prefix, and the match must be unique, so
  `[S@bash-<the correct digest>]` on a four-`bash` turn resolves ambiguously. The rule is worse the
  more the model gets right.
- Round 5 replaced "who wrote it" with "where you read" because the former was undecidable. Round 6
  found the latter equally undecidable from a command line without the shell parse the ADR refuses
  everywhere else — and string-matching the invocation instead reproduces the failure
  `_reads_tainted`'s own docstring records.

The fourth finding is that D7 has no entry point at all: `_verify_span` short-circuits to `UNCITED`
at `verification.py:309` before any of it runs, and `strip_citation_markers` — built from the same
pattern that fails to match a near-miss — leaves the marker's characters in the span text.
Measured, `claim_unit` over the FRE-1327-shaped span yields
`('trace','four','bash','calls','succeeded','second','tempo','dba5b2')`; three of those eight tokens
are minted by the marker itself, `S` folding to the unit synonym `second`. D7's row three is
therefore unreachable by construction, and AC-3's D7 arm and AC-13(c) would have passed vacuously.

Two non-blocking: the Implementation Notes still specified the **withdrawn** "maximal-literal-run
extractor" that D2's body replaced in round 3 — the implementer-facing half disagreeing with the
normative half, on the ticket that builds arm 2; and two line anchors had drifted
(`verification.py:615` for a field at `:622`; `turn_evidence.py:240` for returns at `:241`/`:256`).
Both substantive claims behind those anchors verified true, including that `RegisteredSource.label`
derives from `memory_item_identity` (`source_registry.py:915`).

**What held.** All seven rows of D2's arm-2 fixture table reproduce exactly against the shipped
normalizer; `ARBITRARY_CODE_TOOLS`' ten members match D2's split table; the `(kind, origin, content)`
dedupe key; the `.contained`-versus-`.passed` argument; the entitlement gate at `verification.py:330`.

**Why the diagnosis is altitude and not a fourth repair.** D2 arms 2–3, D7's resolver and D3's
address rule are three attempts to make one discriminator — *model-composed versus world-observed* —
decidable by string arithmetic. This document's own Context already says that question is
undecidable for arbitrary shell, and each round has been rediscovering it under a new name. The
external record agrees from an independent direction: a 2026 survey of evidence tracing in LLM
agents finds no system that semantically validates tool-output content before citing it; CaMeL,
FIDES and NeuroTaint all label values where they are produced; and GuardFall (June 2026) named the
precise failure of inspecting raw shell command text — *"the filter and the shell end up looking at
two different things"*.

**Outcome.** The owner ruled **Route 1** on 2026-09-02 — follow the capability route, and follow
Anthropic's lead. The premise is corrected at program level in
[ADR-0140](ADR-0140-the-model-is-not-a-security-boundary.md); D6 is retired there; D2, D3 and most
of D7 are withdrawn here; D1 and D4 stand; the replacement path is **D8** above. On the ADR-0098
Amendment A precedent, this round stopped and narrowed rather than patching a fourth time — one
level higher than FRE-1357 anticipated, because the premise rather than the predicate was where the
grain was wrong.
