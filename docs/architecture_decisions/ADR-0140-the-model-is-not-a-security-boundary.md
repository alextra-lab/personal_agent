# ADR-0140: The Model Is Not a Security Boundary — Seshat's Declared Threat Model, and the Layer Rule Every Other ADR Cites

**Status:** Proposed
**Date:** 2026-09-02
**Deciders:** Project owner (decision, 2026-09-02 — "Route 1, and we follow Anthropic's lead"), `adr` session (drafting)
**Tags:** security, threat-model, grounding, agent-architecture, prompt-injection

---

## Context

**Seshat has no declared threat model.** ADR-0138 — the grounding contract, the document that
decides what counts as evidence for an assertion — names no adversary anywhere in it. The only
declaration of a *model-intent* threat model in the entire corpus is **ADR-0139 D6**, and it sits
inside the ADR whose design depends on it. ADR-0089 declares one scoped to artifact execution
(users, sharing) — a different asset. ADR-0063, ADR-0003 and ADR-0070 use the phrase in passing
without declaring anything.

### The location is the defect, before the value is

D6 is decision six of seven in the document it justifies. A premise that determines whether an
entire approach is correct cannot be a subsection of that approach: it is discovered after the
design, it is argued against that design's alternatives, and it is invisible to every other
document that should honour it — ADR-0138's admissibility rule, ADR-0028's tool-integration tiers,
the sandbox and egress posture.

### What D6 declared, and what it cost

> "**The model is careless, not adversarial.** This is a decision, not an assumption, and it is the
> load-bearing premise of D2."

From that premise ADR-0139 derived result-level admissibility for arbitrary-code tools, and rejected
**Option 2** (typed wrappers only, `bash` stays inadmissible) with the words: *"It is the correct
answer under an **adversarial** threat model, and the owner settled that question the other way."*

The derivation has been reviewed five times. Rounds 1–3 produced 7, then 5, then 5 blocking
findings; round 5 produced three more and introduced D7; round 6 (FRE-1357) produced four more.
**Every one of them is in a model-layer predicate** — a rule that inspects the model's own output
after the fact and tries to decide whether the model composed it. Round 6's measurements are the
clearest statement of the pattern: D2's arm 2 rejects `grep 'passed_count' logs.json`,
`rg 'source_registry tool' src/`, `git log --grep='cost gate'`, a `psql` column header and an
ES|QL `KEEP` projection — and it rejects the **whole source**, not the offending span. Each round
fixed the predicate at one grain and exposed the next grain down.

### The axis is wrong, not only the value

"Careless or adversarial" conflates two independent questions:

| Axis | Question | What the evidence says |
|---|---|---|
| **Model intent** | Does *our* model try to launder its own words into evidence? | No laundering attempt, literal or encoded, appears in the 100 recorded refusals. FRE-1327 was **confabulation**, not laundering. |
| **Input integrity** | Is what reaches the model trustworthy? | Seshat reads the web, a shared knowledge graph, and MCP tool results. FRE-1338 is an in-house instance of one session's authorship crossing into the next through the shared graph. |

D6 derives a design that holds only if nothing can *supply* intent, from evidence showing only that
our model has none of its own. Flipping the value to "adversarial" does not repair that; it
over-corrects on the same wrong axis.

### What the ecosystem we build on actually does

Anthropic's containment philosophy treats the model's intent as **irrelevant** — the architecture is
designed to work whether misbehaviour is careless or adversarial:

> "Rather than supervising what the agent does, we supervise what it's *able* to do by enforcing
> access boundaries."
>
> "even with best-in-class defenses, protection in the model layer will never be 100% effective,
> which is why it can't stand alone."
>
> "tool output is an attack surface even when the tool is trusted."

OpenAI's public position is the same conclusion from the other end: prompt injection may never be
fully solved, because a model cannot reliably enforce privilege boundaries inside one token
sequence. OWASP's 2026 agentic guidance states it flatly — model protections are not your security
boundary. The research direction is uniform: CaMeL, FIDES, NeuroTaint and Agent-Sentry all label a
value **where it is produced** and propagate the label through execution; none inspects a result for
traces of its own arguments. A 2026 survey of evidence tracing in LLM agents finds **no** system that
semantically validates tool-output content before citing it, and treats the question as open.

And Anthropic has already shipped the answer to Seshat's specific problem. The Citations API does not
check whether the model fabricated a citation — it removes the ability to. Documents are chunked
before generation, the model emits an index, and the API extracts `cited_text` from the source, so
"citations are guaranteed to contain valid pointers to the provided documents." Capability, not
inspection.

---

## Decision

**Intent is not a design input. The model is not a security boundary. Controls are classified by the
layer they act at, and no invariant rests on a model-layer control alone.**

This ADR declares the premise once. Other ADRs **cite** it and must not restate it.

### T1 — Intent is not a design input

No design in this repository may rest on a claim about what the model *intends*. Whether a bad
outcome arises from carelessness, confabulation, a jailbreak, or content that supplied the intent is
irrelevant to whether the design must hold. This retires "careless, not adversarial" as a load-bearing
premise anywhere in the corpus.

The empirical observation that our model has shown no laundering behaviour remains true and remains
worth recording; it is **evidence about behaviour**, admissible for prioritisation and inadmissible
as a security premise.

### T2 — Two axes, declared separately

| Axis | Declared position |
|---|---|
| **Model intent** | Not a design input (T1). Behavioural observations inform priority, never boundaries. |
| **Input integrity** | **Untrusted.** Tool results, retrieved pages, MCP responses and knowledge-graph content are attack surface even when the tool itself is trusted. |

Untrusted inputs are a standing condition, not a threat that must be demonstrated before it is
designed for.

### T3 — The layer rule

Every control is one of two kinds, and each ADR states which for each control it introduces:

- **Capability-layer** — the harness decides what the agent is *able* to do or *able* to produce:
  tool provisioning, parameter schemas, sandboxes, egress control, what the registry will mint, what
  a format permits the model to emit. Deterministic; enforced outside the model.
- **Model-layer** — an inspection of what the model produced, after it produced it: content
  predicates, classifiers, prompt instructions, output checks.

**Invariants come in two kinds, and the rule applies to one of them.**

- A **boundary invariant** decides what the agent is *able* to do or produce, and what may enter a
  trusted set — *may this tool mint a source*, *may this text be delivered*, *may this call reach the
  network*. Its failure is a breach.
- A **judgement check** decides whether something already inside the boundary supports a claim —
  *does this source contain this assertion*, *does this image support this sentence*. Its failure is a
  wrong verdict.

> **A boundary invariant may never rest solely on a model-layer control.** A judgement check *is*
> model-layer by nature — ADR-0138's containment check and D3(d)'s entailment escalation both are —
> and is legitimate provided it is declared as a judgement, never as a boundary, and its failure mode
> is recorded rather than implied.
>
> **And because almost every judgement here sits upstream of a delivery decision, declaring the label
> is not sufficient. A model-layer control must declare, justify and instrument its
> absence behaviour** — a stated answer to *what happens when this control is absent, times out,
> errors, or returns nothing*, with that outcome distinguishable in telemetry from a verdict the
> control actually reached. **Fail-open is permitted where what the control protects is a quality
> property and its absence is observable; it is forbidden where the control gates a capability
> boundary.** The declaration and the instrumentation, not the label and not the direction, are what
> keep a judgement from silently becoming the boundary.

**This third clause exists because the first two are relabelable and the review that produced this
ADR proved it.** The span extractor is a model classifying assertions (`grounding/extractor.py`),
run over the final reply and feeding verification, and in enforcement mode its verdict decides
whether the reply is retried, replaced or delivered (`orchestrator/executor.py:1656-1669`,
`:6762-6789`). Containment is the same shape: a judgement whose `PASSED`/`NOT_CONTAINED` outcome
drives `enforcement.decide()` (`grounding/enforcement.py:89`). Argued one way each is a judgement;
argued the other each gates delivery, which T3's own list calls a boundary.

**An earlier draft of this clause asserted that both fail closed. That was false, and the truth is
what makes the clause useful rather than decorative.** Two behaviours, and only one of them fails
closed: a *malformed or incomplete* extractor payload does — uncovered text becomes non-exempt and
sets `degraded` (`grounding/span_policy.py:134`, `:160`, `:239`) — while an extractor **exception**,
a verification exception, or a denied budget reservation produces an `unavailable` verification
(`orchestrator/executor.py:1661`, `:1671`) which `decide()` **delivers**
(`grounding/enforcement.py:89`). That fail-open is deliberate and argued in `decide()`'s own
docstring: a broken extractor is a fact about Seshat's accounting, not evidence about the model's
claim, and refusing the user's turn for our bookkeeping punishes them for it. **Under this clause
that choice is legitimate** — grounding compliance is a quality property, not a containment
boundary; nothing escapes, a possibly-ungrounded sentence is delivered — **and it is legitimate
precisely because the turn is recorded as unverified and never as verified-and-passing, so a wave of
them reads as the malfunction it is.** Had the same control gated egress or tool authorisation,
fail-open would be forbidden and no docstring could rescue it.

**What T3 forbids, then, is not fail-open. It is fail-open that is undeclared, unjustified, or
indistinguishable in the record from a verdict.**

**This distinction is the whole diagnosis of ADR-0139's five review rounds.** Its D2 arms were a
*boundary* control — they decided whether a source could be minted at all — implemented as a
judgement predicate over the result's own text. Containment is the same *kind* of predicate and is
fine, because what it judges has already crossed a boundary decided elsewhere. Getting the two
confused is what produced a rule that had to be repaired at a finer grain every round.

The practical consequence is a different argument shape. For a judgement check, "this has a hole"
stops being a blocking objection (all of them do) and "this false-rejects the legitimate majority"
becomes decisive. For a boundary control, a hole is still fatal.

### T4 — Applied to evidence: admissibility is a capability property

**A source is admissible because of how the harness obtained it, never because a checker inspected
its content.** Admissibility is decided at registration from facts the harness holds — which tool ran,
what the parameter schema permitted it to carry, what the executor recorded — and is not re-derived
from the result text at verification time.

Three consequences follow immediately, and ADR-0139 is revised to carry them:

1. **Arbitrary-code tools stay inadmissible.** `bash` and its family take a model-authored command
   line; no capability-layer fact distinguishes observation from composition, so no source is minted.
   This is ADR-0139 Option 2, which becomes correct under T1 rather than under an adversarial model.
2. **Typed retrieval carries evidence.** A tool whose parameters *select or address* rather than
   *compose* is admissible by construction — ADR-0138 D2's original parameter-schema boundary,
   restored on its invocation axis after ADR-0139's widening lapses, **and narrowed once**: see
   below. It is not a pure restoration and this ADR does not claim one.

**The one narrowing, stated rather than smuggled in.** ADR-0138 D2 offers a worked example — *"for a
database query, the returned rows are a source and the SQL is not"*. Under T4's classification test
that example no longer holds as written: SQL is a query language, `SELECT 'Paris has 9 million
residents'` composes, and a tool taking model-authored SQL is compose-capable whatever it returns.
**T4 therefore narrows ADR-0138 D2's invocation axis: a query-language parameter composes, and the
database-rows example is superseded.** The cost is real and is the same cost as `bash` — a
model-authored query earns no citation — and the remedy is the same: a typed query tool whose
parameters name an index, filters and fields, with the query built by the harness. That is a D8
wrapper. Recording this narrowing here matters because ADR-0138 is `Accepted` and a rule narrowed by
another document without its own text changing is the drift this project keeps paying for; the
amendment note in ADR-0138 carries it.
3. **Attachments are admissible** because the harness received them; the model did not author the
   bytes. The tier that records this is a capability fact, not a content judgement.

**The classification test, stated because the boundary is only as good as it.** A tool's parameters
*compose* when any of them can carry a **program, a command line, or a query language** — an
expression the tool will evaluate. They *select or address* when they name a thing to fetch and the
tool's own logic decides the bytes. A schema of typed scalars is not sufficient evidence of the
second: **one string parameter holding a language is a composing parameter.**

Applying that test finds a misclassification. **`mcp_esql` is in `TYPED_RETRIEVAL_TOOLS`
(`source_registry.py:346`) and must not be.** ES|QL is a query language, and the module's own audit
records the channel — *"ES|QL's `ROW a = \"…\"` emits a model-authored literal with no index
involved"* (`source_registry.py:787`). ADR-0139 D2 reached it through the `invocation_check_required`
flag; with D2 withdrawn that flag does not exist, so on the classification table alone `ROW claim =
"Paris has 9 million residents"` would reach the typed-retrieval branch and mint a source holding the
model's own sentence. Reclassifying it is FRE-1306's Option A, whose cost — telemetry queries lose
their citation — is the same cost Route 1 already accepts for `bash`, and whose remedy is the same: a
typed telemetry-query wrapper, D8's first named wrapper rather than a new kind of work.

**Two precisions, and the first of them is a finding in its own right.** *(i)* An earlier draft said
the channel is "not reachable today" because `mcp_esql` carries `allowed_in_modes: []`
(`config/governance/tools.yaml:584-586`). **That mitigation does not exist.** `allowed_in_modes` is
declared on the policy model (`governance/models.py:104`), populated for **44 tools**, and described
in `request_gateway/governance.py`'s own docstring as "the gate" — but **nothing enforces it**: the
permission check reads `tool_def.allowed_modes` and `tool_policy.forbidden_in_modes` only
(`tools/executor.py:181-188`), and MCP tool definitions are built with `allowed_modes` hardcoded to
`["NORMAL", "DEGRADED"]` (`mcp/types.py:81-88`) before the gateway registers them
(`mcp/gateway.py:182`, `:197`). A tool believed disabled in every mode is therefore permitted in two.
**That is a live governance gap affecting far more than this ADR** — it is filed separately and at
higher priority than the reclassification it was invoked to excuse. *(What is not established here is
exposure:* whether any of those 44 tools is currently discovered and registered depends on which MCP
servers run, which this review did not test. The enforcement gap is verified; the reachability is
not.) *(ii)* **This ADR specifies the change and does not make it.** The `adr`
session may not edit `src/`, so the reclassification is filed as its own ticket; until that ticket
lands the table is wrong even though nothing can reach it. Saying "this ADR does not withdraw D2
without closing what D2 was covering" — as an earlier draft did — was false: what it does is *name*
what D2 was covering and carry it as a filed obligation. Its parameter schema is not verifiable in
this repository either, since MCP schemas arrive at runtime from `list_tools()` (`mcp/types.py`), so
the classification rests on ES|QL being a query language, which is not in doubt.

**Content predicates may subtract, never add.** A capability-layer decision admits a source; a
content predicate may then *withhold* part or all of what it carries — `strip_argument_echo`
(`source_registry.py:452`, reached at `:984`) removing argument-echoed fields is exactly this, and is
retained. What no content predicate may do is *confer* admissibility on a source the capability layer
would not have admitted, or restore one it refused. Admission is monotone downward from the
capability decision, which is why the existing field-level defence is compatible with T4 and why D2's
arms were not.

*Precision on that example, because an earlier draft of this paragraph got it wrong and the error is
the kind this ADR is about.* Stripping **every** field does not yield an empty string and therefore
does not reach the `NO_CONTENT` refusal: the function returns `json.dumps(kept)`, which is `"{}"`
(`source_registry.py:480-483`), and registration tests only `admissible.strip()` (`:1012-1013`), so a
source is minted holding `"{}"`. Nothing citable follows from it — containment cannot match a claim against
`"{}"` — so this is a cosmetic defect rather than a laundering channel, and it is **filed rather than
fixed here**. It does not change the rule; it changes the worked example, and it is recorded so the
next reader does not repeat the inference.

### T5 — How this ADR binds

An ADR introducing a control states its layer. An ADR relying on a threat-model premise **cites this
document and does not restate it**, on the pattern `.claude/skills/lifecycle-rules.md` already uses
for process invariants. Where an existing ADR declares a premise that contradicts T1 or T2, this ADR
governs and that ADR is amended.

---

## Alternatives Considered

### Option 1: Flip D6's value — declare the model adversarial

**Description:** Keep the careless/adversarial axis and change the answer, making ADR-0139 Option 2
correct by that document's own text.

**Pros:**
- Smallest possible edit; reaches the same conclusion for grounding.
- Consistent with the conservative reading.

**Cons:**
- Preserves the wrong axis. Anthropic's stated position is that intent is *irrelevant*, not that it
  is hostile, and the difference decides future arguments: an intent axis invites "but the model
  would never do that" as a design move, whichever value is set.
- Licenses unbounded pre-emptive hardening against a threat nothing has attempted, which is the
  failure mode `feedback_dont_prematurely_clamp_new_capabilities` records.
- Says nothing about input integrity, which is the axis the evidence (FRE-1338) actually implicates.

**Why Rejected:** It repairs the value of a premise whose *shape* is the defect, and it would have to
be re-litigated the first time someone argues our model is well-behaved.

### Option 2: Amend D6 in place and leave it in ADR-0139

**Description:** Correct the value or the axis, but keep the declaration where it is.

**Pros:**
- No new document; nothing else to keep in sync.

**Cons:**
- The premise stays invisible to ADR-0138, whose admissibility rule it governs, and to ADR-0028's
  tool tiers.
- It remains argued against one ADR's alternatives, which is how it came to be settled by the design
  it was meant to constrain.

**Why Rejected:** The location is the defect. A program-level premise living inside one consumer
produced exactly the outcome observed — two ADRs on the same subject, one declaring a threat model
the other never saw.

### Option 3: Declare no threat model; decide per ADR

**Description:** The status quo made explicit — each ADR reasons about adversaries in its own scope.

**Pros:**
- Zero cost; matches how a research project has operated to date.
- Avoids a document that could become stale boilerplate.

**Cons:**
- It is the condition that produced this ADR. ADR-0138 declared nothing and ADR-0139 declared
  something ADR-0138 could not see.
- Every future ADR re-derives the premise, and they will not agree.

**Why Rejected:** The corpus has already demonstrated the failure. A premise re-derived per document
is a premise that drifts per document.

### Option 4: Adopt CaMeL-style information-flow control wholesale

**Description:** Implement the research answer directly — a custom interpreter over the agent's plan
that tracks per-value provenance and capabilities, blocking flows by policy.

**Pros:**
- The strongest available guarantee, and the direction the field is converging on.
- Would close the cross-call and read-back channels structurally rather than by concession.

**Cons:**
- Requires extracting control and data flow from every turn into an interpretable plan — a
  re-architecture of the orchestrator, not a decision.
- Research-grade and unproven at this scale; disproportionate to a single-owner research harness
  (`feedback_use_platform_functionality_dont_build_it`).

**Why Rejected:** T3 captures the operative principle — provenance is a property the harness assigns,
never one inferred from content — without committing to the machinery. The machinery stays available
as a later upgrade, and the cross-turn write ledger (FRE-1356) is its first affordable increment.

### Option 5: Keep result-level admissibility and repair the predicates once more

**Description:** ADR-0139's current path, with round 6's four findings fixed — arm 2 rethresholded,
D7's resolver given attribute precedence, D3's address rule replaced.

**Pros:**
- Preserves the citability of everything the agent learns by doing, which is the measured defect
  (2 of 222 spans passing).
- Every individual finding is fixable.

**Cons:**
- It is the fourth repair at a grain that has produced defects in each of five rounds, and round 6's
  findings are each the previous round's answer reappearing one level down.
- It makes a model-layer control the boundary for the invariant, which T3 forbids and which both
  Anthropic and OpenAI state cannot hold.

**Why Rejected:** The owner ruled Route 1 on 2026-09-02. The technical ground is that the churn is
altitude rather than defects: the discriminator "was this result composed by the model?" is not
decidable from content, and five rounds of evidence say so.

---

## Consequences

### Positive Consequences

- **One premise, cited rather than re-derived.** ADR-0138 gains a threat model it never had.
- **Arguments about model-layer controls get a decision procedure.** Holes stop being fatal and
  false-rejection cost becomes decisive, which is the argument round 6 could not make cleanly.
- **The grounding design stops churning.** The five-round review cycle in ADR-0139 D2/D3/D7 ends by
  removing the class of rule that produced it.
- **Alignment with the platform we build on.** Seshat's containment reasoning matches the reasoning
  of the model provider it runs on, so their guidance is directly applicable rather than translated.
- **ADR-0138 D2's parameter-schema boundary is restored on its invocation axis** after ADR-0139's
  widening lapses — the rule the codebase's own docstrings already argue for — **and narrowed once**,
  by T4's classification test: a query-language parameter composes, so D2's database-rows example is
  superseded. Restored-and-narrowed, never "restored, not amended".

### Negative Consequences

- **Everything the agent learns by running a command stays uncitable until a typed tool exists for
  it.** This is a real, ongoing cost, and it is the cost ADR-0139 was written to remove. It is
  accepted here in exchange for a boundary that holds, and it is made *visible and ordered* by
  ADR-0139 D1's instrument rather than left implicit.
- **A provisioning obligation.** Each new evidence source needs a typed tool before it can ground
  anything. Demand-driven (see ADR-0139's revision) but non-zero.
- **A classification obligation on every future ADR** — each control must name its layer. Small, but
  it is new process, and process that no one enforces decays.
- **Work is withdrawn.** ADR-0139 D2/D3/D7 and the tickets implementing them are cancelled or
  re-scoped; roughly five rounds of design effort is retained as record rather than as decision.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| The typed-tool treadmill never runs, and uncitable turns become permanent | **High** | AC-3 fails in both directions: a wrapper shipped with no uncitable-turn evidence, *and* a persistent uncitable population with no wrapper filed. ADR-0139 D1 is the instrument and ships first |
| The cost is hidden by exempting uncitable turns from the metric | **High** | AC-5 asserts the `uncitable` class stays classified, counted and published beside the compliance rate — not averaged into it, which would confound the number D1 exists to de-confound. This is ADR-0139 Option 3, rejected there and re-forbidden here |
| "Capability-layer" becomes a label attached to model-layer checks to exempt them | Medium | AC-2 tests the behaviour, not the label: admission must be decidable without reading the result content |
| The layer rule becomes boilerplate nobody applies, or "judgement" becomes the label that exempts anything | Medium | T3's third clause makes the test **fail-closed behaviour**, not the label: a model-layer control gating delivery must declare what happens when it is absent, times out or returns nothing. AC-1 tests the boundary behaviourally in both directions, including the unclassified-tool default-deny that covers tools not yet invented |
| Declaring inputs untrusted is read as licence for broad pre-emptive hardening | Medium | T1's second paragraph keeps behavioural evidence admissible for prioritisation; work still competes on measured need |

---

## Implementation Notes

**This ADR is mostly documentary, with one code change it may not defer.** It declares a premise and
revises what other documents may rest on; its implementation is the amendment of those documents and
the re-scoping of the tickets that flowed from the retired premise. The exception is the `mcp_esql`
reclassification, which cannot wait: withdrawing D2 removes the only thing that was covering it.

- `docs/architecture_decisions/ADR-0139-*.md` — revised in the same PR: **D1 and D4 stand; D2, D3
  and D7 are withdrawn** with their reasoning retained as record and a pointer to this ADR. The
  replacement path — typed retrieval, ordered by D1's instrument — is stated there because it is the
  application, not the premise.
- `docs/architecture_decisions/ADR-0138-*.md` — gains a one-line citation of this ADR where its
  independence rule is stated. Its D2 parameter-schema boundary is **unchanged**; ADR-0139's
  amendment of it lapses with D2's withdrawal.
- `src/personal_agent/grounding/source_registry.py` — **one change: `mcp_esql` leaves
  `TYPED_RETRIEVAL_TOOLS` (`:346`) and joins the compose-capable set**, per T4's classification test.
  This closes FRE-1306 by capability rather than by the withdrawn invocation check, and is filed as
  its own ticket because it changes a verdict in production. The categorical branch for
  `ARBITRARY_CODE_TOOLS` (`:973-983`) is otherwise **unchanged in behaviour**, and is now the decided
  rule rather than a status quo awaiting replacement; its docstring is the record of why and already
  says so. `strip_argument_echo` (`:452`) is **retained** — under T4 it subtracts and never adds, so it
  is a compliant supplementary control; the emptiness check it feeds is at `:1012-1013`.
- **The knowledge-graph recall path is a live T2 violation and is filed, not fixed here.** Recalled
  memory is joined into `_volatile_block` and inlined into the current user message
  (`orchestrator/executor.py:5752-5763`), so graph content — agent-writable, and the FRE-1338
  channel — reaches the model outside any tool-result channel. T2 declares that content untrusted;
  closing it is a message-assembly change with its own blast radius and belongs in its own ticket
  rather than inside this ADR's diff.
- Ticket dispositions are carried on FRE-1357 and are not restated here.

**What this ADR deliberately does not decide:** which typed tools get provisioned, in what order, or
against what threshold. That is demand-driven work ordered by ADR-0139 D1's measurements and is
filed separately — deciding it here would repeat D6's error of settling a downstream question inside
an upstream document.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

> **Adjudication:** on this ADR's umbrella ticket, after **two** consecutive measurement windows
> have completed with ADR-0139 D1 deployed — two because AC-3 and AC-5 are both statements about
> persistence, and one window cannot carry them. A criterion whose qualifying population never
> materialised is recorded **not yet adjudicable**, never *met*: a criterion that passes because
> nothing happened is the vacuity this ADR exists to stop tolerating.

- **AC-1 — No tool whose parameters can carry a program mints a source.** For every tool the registry
  can classify, admissibility is decided against **T4's classification test applied to the tool's own
  schema** — and each tool whose schema admits a program, command line or query language registers
  **no** source. Named members at authoring time: `bash`, `run_python`, `mcp_browser_evaluate`,
  `mcp_browser_run_code`, **and `mcp_esql`**. **Second arm, because a named list cannot cover tools
  that do not exist yet:** a tool absent from every classification table registers **no** source —
  the `UNCLASSIFIED_TOOL` default-deny (`source_registry.py:989-998`) — and that arm is what covers
  an MCP server introducing a compose-capable tool at runtime (`mcp/gateway.py`), which the named
  list structurally cannot. · **Check:** probe each named tool through `register_tool_result` with a
  composing payload (`printf 'Paris has 9 million residents'`;
  `ROW claim = "Paris has 9 million residents"`) and assert `source is None`; then probe a synthetic
  tool name present in no table and assert the same. · *Fails if* any named tool mints a source — `mcp_esql` is the one that does today, and this criterion exists because it
  survived the previous design; **or** if the probe set is derived **from the classification tables
  themselves** rather than from tool schemas, which is circular and passes whenever the table is
  wrong. That circularity is exactly how `mcp_esql` reached this point, so a probe set that inherits
  it does not satisfy this criterion.

- **AC-2 — The boundary did not collapse into blanket refusal, and admission is content-independent
  upward.** **(a)** A typed retrieval — `read`, `fetch_url` — registers a source, and a span asserting
  a value genuinely present in that result scores `PASSED`. **(b)** The *same* tool, given content crafted to look model-composed, is **never refused with an
  invocation-flavoured admissibility** — `MODEL_AUTHORED_INVOCATION` or any successor naming
  composition. It may be refused `NO_CONTENT`, because T4 permits a content predicate to subtract; what
  it may not do is have its *classification* changed by its content. · **Check:** paired probes; both
  arms required; read the `admissibility` value on refusal and the per-span outcome on admission. ·
  *Fails if* arm (a) fails — a boundary that admits nothing is not a boundary, it is Option 3 with
  better vocabulary, and it would satisfy AC-1 trivially; **or** if arm (b) yields a
  composition-flavoured refusal, which would mean a content predicate is deciding classification,
  contrary to T4. *An earlier draft required byte-identical verdicts across the two contents, which
  contradicted T4's own subtraction rule: a legitimate downward subtraction would have failed it.*

- **AC-3 — The treadmill is demand-driven, and it actually runs.** The qualifying threshold is
  **preregistered in the roadmap ticket before the first window opens** and is not re-set after a
  window is read. Then, over two consecutive windows: every typed wrapper provisioned traces to an
  `uncitable` population above that threshold, **and** no such population persists across both windows
  without a wrapper ticket filed against it. · **Check:** ES query over `grounding_verification_completed`
  for the `uncitable` class, keyed on a **`refused_tool_origins` field the roadmap ticket must add to
  that event**. · *Fails if* that field does not exist — and this is a real obligation, not a
  formality: an `uncitable` turn by definition admitted **nothing**, so `source_registry_snapshot`
  holds only admitted sources (`orchestrator/executor.py:2244-2258`) and cannot name what was refused,
  while the only record carrying `tool_name` on a refusal is the DEBUG event
  (`orchestrator/executor.py:1450-1456`) whose join D1 exists to abolish. Without the new field this
  criterion is not checkable and the treadmill has no ordering signal. *An earlier draft keyed this
  check on the snapshot's origins and was wrong for exactly that reason.* · *Also fails if* a
  wrapper ships with no qualifying population behind it (speculative provisioning — Option 2's original
  objection, live); **or** if a qualifying population persists across both windows with nothing filed
  (the treadmill has stalled and the cost has quietly become permanent); **or** if the threshold is set
  or revised after any window is read. *If no population reaches the threshold in either window, this
  criterion is **not yet adjudicable** — it is not met.* **That escape has a deadline:** after four
  windows with no qualifying population, the threshold is treated as **wrong** and must be re-derived
  from the observed distribution and re-preregistered, with the re-derivation recorded on the umbrella.
  A criterion that can sit unadjudicated indefinitely is not a forcing function, which is what an
  earlier draft of this line permitted.

- **AC-4 — Untrusted input reaches the model only through tool-result channels.** **One probe per
  declared source class** — knowledge-graph recall, tool results, fetched web content, and MCP server
  responses — each carrying a unique marker string, asserting the marker appears in **no** system block
  and **no** user text block of the emitted request. · **Check:** capture the assembled request per
  seeded turn; assert marker placement per class. · *Fails if* the marker appears outside a tool-result
  channel for any class; **or** if fewer than all four classes are probed — T2 names four and a
  single-class probe can pass while another assembly path violates it, which is what an earlier draft
  of this criterion permitted. **This criterion is red at authoring time and is meant to be:** recalled memory is joined
  into `_volatile_block` and inlined into the current user message
  (`orchestrator/executor.py:5752-5763`), so graph content — agent-writable, and FRE-1338's own
  channel — reaches the model as user text today. T2 is a declaration until this closes; the closing
  change is filed as its own ticket, and this criterion is how we know it landed rather than being
  described.

- **AC-5 — The cost stayed visible.** Compliance continues to be reported over `citable` turns with
  the **`uncitable` class published beside it**, per ADR-0139 D1 — classified and counted, never
  silently dropped. · **Check:** the same ES query, plus the published `uncitable_turn_rate`. ·
  *Fails if* the `uncitable` class is empty over a window in which arbitrary-code refusals are non-zero
  — the classification stopped and the cost became invisible; **or** if `uncitable_turn_rate` ceases to
  be published. *Note the change from an earlier draft, which asserted these turns stay in the
  **compliance** denominator: that contradicts D1, which deliberately reports compliance over `citable`
  turns only. The invariant is that the class stays measured and visible, not that it is averaged into
  a number it would only confound.*

---

## References

- [ADR-0138](ADR-0138-the-model-may-generate-but-may-not-assert.md) — the grounding contract; its D2 parameter-schema boundary is restored by T4
- [ADR-0139](ADR-0139-what-the-agent-learns-by-doing.md) — D6 is retired by this ADR; D2/D3/D7 withdrawn, D1/D4 stand
- [ADR-0098](ADR-0098-memory-substrate-and-lifecycle-architecture.md) Amendment A §A6 — the provenance-terminus rule for retrieval
- [ADR-0089](ADR-0089-artifact-execution-security-model.md) — the existing scoped threat model for artifact execution; unchanged, and now a consumer of T3
- [ADR-0028](ADR-0028-external-tool-cli-migration.md) — tool-integration tiers; the provisioning path T4 names
- [ADR-0134](ADR-0134-activity-alerting-absence-as-a-first-class-signal.md) D1 — denominators, and why an unmeasured class is an invisible failure class
- FRE-1357 — round 6, whose four findings and convergence verdict produced this ADR; carries the per-ticket dispositions
- FRE-1349 — round 5 of ADR-0139, and its residual-risk statement
- FRE-1347 — implements ADR-0098 Amendment A §A6 and writes D2's **authorship**-axis narrowing into
  ADR-0138's own text. `Approved` and **out of scope here**: this ADR restores only D2's *invocation*
  axis, and nothing in it changes FRE-1347's deliverable
- FRE-1306 — `mcp_esql` emits a model-authored literal in one round trip; closed by T4's
  classification test rather than by the withdrawn invocation check
- FRE-1338 — one session's authorship crossing into the next through the shared knowledge graph (the input-integrity instance)
- FRE-1327 — the confabulation case study; evidence for the model-intent axis, not for the boundary
- [How we contain Claude — Anthropic Engineering](https://www.anthropic.com/engineering/how-we-contain-claude) — "supervise what it's *able* to do"; the model layer "can't stand alone"; "tool output is an attack surface even when the tool is trusted"
- [Citations — Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/citations) — capability-based citation: the provider extracts `cited_text`, the model emits an index
- [Mitigate jailbreaks and prompt injections — Claude Platform Docs](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/mitigate-jailbreaks) — untrusted content belongs in tool-result blocks
- [OpenAI on prompt injection in browser agents](https://cyberscoop.com/openai-chatgpt-atlas-prompt-injection-browser-agent-security-update-head-of-preparedness/) — may never be fully solved
- [OWASP Top 10 for Agentic Applications 2026](https://goteleport.com/blog/owasp-top-10-agentic-applications/) — model protections are not your security boundary
- [Defeating Prompt Injections by Design (CaMeL)](https://arxiv.org/pdf/2503.18813) — capability and provenance tracking in an interpreter (Option 4)
- [From Agent Traces to Trust: Evidence Tracing and Execution Provenance in LLM Agents](https://arxiv.org/html/2606.04990v1) — no surveyed system semantically validates tool-output content before citing it
- [GuardFall: shell-injection gap in AI coding agents](https://thehackernews.com/2026/06/guardfall-exposes-open-source-ai-coding.html) — why inspecting raw command text fails: "the filter and the shell end up looking at two different things"

---

## Status Updates

### 2026-09-02 - Proposed
**Changed By:** `adr` session (FRE-1357)
**Reason:** Authored on the owner's Route 1 decision, after round 6 of ADR-0139's review found four
blocking defects — all in model-layer predicates, three of them the previous round's answer
reappearing one level down. The convergence verdict was **altitude, not defects**; this ADR is the
narrowing, taken one level above where FRE-1357 anticipated it.
