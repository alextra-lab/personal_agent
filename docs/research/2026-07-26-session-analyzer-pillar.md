# The idea: one analysis pillar, built around a reasoning Analyzer

*Owner's idea, captured 2026-07-26 by cc-explore. Companion to
`2026-07-26-harness-self-analysis-deep-dive-queue.md`.
Everything measured here was checked against live cloud-sim prod or source, not recalled.
Nothing here is an ADR. Decided items are marked; everything else is open.*

---

## Objective

**The harness runs efficiently and improves.** The pedagogical layer is served indirectly —
building this *is* the harness learning thread.

---

## The idea in one paragraph

Replace four overlapping subsystems — Insights, Captain's Log reflection, Context Quality,
`quality_monitor` — with **one pillar** whose centre is a **Session Analyzer**: a reasoning agent
that may freely investigate a session the way the master CC session gates a PR. It reads telemetry,
logs **and the codebase**, iterates (hypothesis → check → revise) under a hard bound, consults its own
previous analyses for cross-session patterns, uses the System Graph for correlation, and hands
findings to Linear for the owner to judge. The owner remains the actuator.

---

## Why replace rather than repair — the evidence

| Finding | Measure |
|---|---|
| Almost nothing reaches the owner | **`linear_issue_id` on 6 of 1,872** reflections — a 0.3% delivery rate, caused by the ADR-0040 200-open-issue throttle (`promotion.py:279–300`, capped 5/run) |
| Tickets don't survive contact with the code | The reflector proposes source changes **having never read the source**. `failure_path_fix_location` asks for `file.py::SYMBOL` from a model with no repository access |
| Dedup is broken and self-inflicting | **832 distinct fingerprints of 942** — 88% textually distinct, topically concentrated. The taxonomy forces miscategorisation, which *manufactures* the duplication |
| It never sees cost | `cost` is a valid proposal category; tokens and dollars are not among the inputs |
| It never asks whether it worked | Every focus area is execution mechanics. `final_state: COMPLETED` means the orchestrator finished, not that the answer was right |
| Two of four subsystems are inert | `optimizer.py` dormant by its own docstring; Context Quality Phase 2 built, wired, switched off since April |

---

## The pattern being copied, stated precisely

The master gate works because of four parts:

1. **Deterministic collectors emit raw facts.** `pr_gate.py` surfaces each required check's raw state,
   one-to-one with its source field — it never synthesises "CI passed." `reconcile_board.py` is its
   board-side twin.
2. **A signal trust boundary.** Master trusts CI, codex review, security review *at their stated
   altitude* and does not re-derive them. Re-deriving is named as the failure mode.
3. **Master's own thin check** — the layer no signal covers: does the delivered thing meet the backing
   ADR's objective; doc drift; seam ownership.
4. **An evidence contract.** Fixed fields; missing evidence bounces without reconstruction.

**Where the analogy breaks:** master gates against a **spec** — an ADR with acceptance criteria.
*A session has no spec.* Strip it away and the gate becomes an opinion generator. Supplying the
missing normative reference is the central design problem.

Candidate ground truths, ascending strength: `outcome` (too coarse) → **1,933 user turn ratings**
(`user-turn-ratings-*`, keyed by `trace_id`, integer rating, joinable to captures — already exists)
→ stated objectives (strongest, slowest).

---

## The non-negotiable constraint: independent ground

From `docs/research/2026-07-22-fact-verifier-guardian.md`:

> A system observing its own construction learns **coherence**, not **correctness** — the same biases
> that produce a bad construction produce a bad self-assessment of it. A verifier that checks a claim
> against its own reasoning becomes a second confabulator.

Four kinds of independent ground: **source · re-execution · independent frame · outcome.**

For code proposals the independent ground is **the repository**. That is exactly what today's
reflector cannot reach, and exactly why its tickets don't hold up. Giving the Analyzer the codebase is
not a convenience — it is the difference between coherence and correctness.

---

## The verification oracle — where it fits

The oracle is already a live thread, not a new idea. ADR-0124 Amendment B (2026-07-24) *moved*
verification out of the summariser and parked it there: the summariser became conversation-only, and
`status_contradiction` adjudication left for "the downstream verification oracle." The design belongs
to its own ADR after its own research.

**Analyzer and Oracle are the same move applied to different objects.** Both check a claim against
independent ground. The Analyzer checks *a proposal* against the codebase. The Oracle checks *a fact*
against the evidence it was extracted from. Same constraint, same failure mode if violated.

**Three ways it plausibly serves this pillar:**

**1. As the Analyzer's gate — completing the master pattern.** Open fork 4 asks what replaces the
owner's real-time steering when the Analyzer runs unattended. The Oracle is a candidate answer: the
Analyzer proposes, the Oracle independently verifies against source before anything is promoted, the
owner decides. That is builder → gate → human, which is the delivery loop being copied in the first
place. The fact-verifier doc already anticipated this class — it lists *"master-style delivery claims
('done / verified live') → durable evidence (merged SHA, health, ACs)"* and calls it **a guardian for
the guardian.**

**2. As the teacher signal.** The doc's sharpest point is that self-observation yields coherence, not
correctness, and that "learning from how you learn needs an **external teacher** to label which
constructions were good." If the Analyzer is ever to improve — DSPy-optimised or otherwise — something
has to label its output as right or wrong. Owner verdicts and turn ratings are one source; a verifier
checking proposals against source is a second, and it scales in a way owner attention does not.

**3. As the reason to get the substrate decision right once.** The owner's recorded direction
(2026-07-23) is a **VO-dump**: heavy tool payloads and artifacts written to a dedicated location with
its own lifecycle, read on demand — no external process, no new core infrastructure. Three
observations carried forward in ADR-0124:

- *The store substantially exists.* `TaskCapture.tool_results` already persists full payloads to disk
  and to `agent-captains-captures-*`. The work is **"split the record"** — a light capture on the
  memory path, heavy evidence with its own retention. ADR-0069's R2 artifact store is the natural home
  for bytes, which is what keeps "no new infrastructure" true.
- *Splitting by storage beats splitting by formatter.* Today the separation is a prompt-builder
  convention a refactor can silently undo. Structural separation cannot regress by accident.
- *Retention sets the oracle's reach.* Purge aggressively and facts whose evidence has aged out become
  permanently unverifiable. Probably the right trade — verification is most valuable near extraction —
  but it must be a stated consequence, not a discovery.

**The hard constraint already recorded:** tool payloads continue to be captured and stored. Only their
delivery to the summariser stopped. *"Payloads are not memory" must not slide into "payloads are not
needed."*

**Sequencing.** The Oracle is explicitly *"its own build, not a bolt-on"* — so it is **not** a
dependency for a first Analyzer. But the substrate question it raises is the same one queue entry 4
raises, and it should be answered once: what is the durable evidence record, how is it split, and how
long is it kept. Deciding that for the Analyzer while ignoring the Oracle would mean deciding it twice.

---

## Design decisions

### Settled in discussion

- **Trigger on events, not a cadence.** Master doesn't review on a timer; it reviews when a PR opens.
  The dispatch stack already made this migration — ADR-0110 (poll) → **ADR-0116 (event-driven,
  Accepted)**. An event-driven analyzer with nothing to do costs nothing; a sweep with nothing to do
  still runs. *This is the single choice that structurally prevents the failure that stopped the harness.*
- **Bounds must be terminal, not ceilings.** The sweep had a ceiling of 2 and reached 311 because the
  exclusion predicate required a terminal reason and `budget_denied` was classed transient. **No
  forward progress must itself be terminal.** A stuck analysis emits *"undetermined within budget."*
- **Don't inherit the dedup.** Free-form reasoning output has no template, so it will vary *more* than
  the current corpus that fingerprinting already fails on.
- **Volume is not the constraint.** July: 40 sessions, 165 captures. The loop that stopped the harness
  ran 358 attempts in 24 hours. You can afford deep analysis; you cannot afford cheap analysis on a timer.

### Open forks

**1. Where does the reasoner run?** CC as an external agent · an in-harness sub-agent
(`orchestrator/sub_agent.py`, already mapped for this in the 2026-06-26 brief) · swappable.
Recommendation: **decide the contract first, not the host** — the evidence package in, the structured
verdict out. Then the reasoner is replaceable, which it will need to be. Note: a CC reasoner puts its
cost outside the harness ledger — either a clean separate budget or a violation of the 100%-visibility
objective. Decide deliberately.

**2. Unit of analysis, given sessions never end.** If a session endlessly evolves, `f(all captures)`
has unbounded, monotonically growing input — turn 500 costs 500 turns of tokens every time.
So analysis must be **incremental**. But incremental **compounds error**: a wrong reading at turn 40
is inherited forever, with no repair point, whereas wholesale re-derives from ground truth each time
and self-heals. The standard resolution is a **hybrid** — cheap deltas plus periodic full rebuild
(keyframes, event-sourcing snapshots, log compaction). Rebuild fires on accumulated delta, not a clock.

*Correction of record: the cost incident was a bug (unreachable ceiling), not an indictment of
wholesale regeneration. Wholesale is fine for sessions that end. It breaks under never-ending
sessions for a different reason — unbounded input.*

**3. Batching vs iterative depth.** Batched consolidated calls and free iterative reasoning pull
opposite ways: batching = fewer, larger calls across many sessions; iteration = many calls deepening
on one. Which dominates decides the cost model, the trigger design, and whether cross-session analysis
is a separate pass or a property of the batch.

**4. Unattended steering.** Interactive analysis works partly because the owner redirects it in
real time. Unattended, that has to be replaced by something — a rubric, an adversarial critic pass,
bounded scope, or a requirement to return *evidence and uncertainty* rather than conclusions.

**5. Promotion to Linear.** TBD. Note the 200-open-issue throttle is the current bottleneck and is
invisible; ADR-0105 D6 makes it a queryable funnel state.

---

## What the Analyzer should evaluate

Today's signature asks one question — *how did the machine run?* Three are needed, each with
different evidence:

| Question | Evidence | Status today |
|---|---|---|
| **Did it work?** | user message, assistant response, rating, sentiment | **never asked** |
| **How did it run?** | steps, tools, errors, timing | the only thing asked |
| **What did it cost, for what return?** | tokens, dollars, model, cache hits — joined to the other two | **blind** |

The third is the one that can answer *"is this worth running,"* which is the question the whole audit
is about.

Do not discard the existing detectors. The 7 insight analyzers, the KG gauges, error patterns and
context-quality incidents are a **library of known-good questions** — they become the Analyzer's
starting repertoire, not deletions.

---

## DSPy: you have half of it

**What DSPy is for.** You declare a signature (inputs → outputs with descriptions) *and a metric and
examples*, and the optimizer compiles the instructions until the model performs. Declaring the
objective and iteratively improving the instructions until capable — the thing described as wanted —
**is DSPy's actual thesis.**

**What is in use.** A hand-written `GenerateReflection` signature run through `ChainOfThought`.
No metric. No training set. No compiled prompt. This is DSPy-as-structured-output, not
DSPy-as-optimizer. The improvement loop is available and unused.

**The missing ingredient isn't missing.** 1,933 rated turns joinable to captures by `trace_id`.
Objective + metric + examples is the whole requirement; two of three already exist.

**Four ways the current signature has aged badly**

1. **The docstring is the objective, and it's April-era.** Five focus areas — performance, errors,
   tool usage, mode/governance, caching/parallelisation. All mechanics, no outcome.
2. **`ChangeScope` cannot name half the system.** Missing: `memory` · `request_gateway` · `cost_gate` ·
   `sysgraph` · `observability` · `events` · `mcp` · `storage` · `gateway` · `transport` · `delegation`.
   Memory — the core subsystem — has no scope value. Since category+scope *is* the dedup fingerprint
   namespace, the model is forced to pick a wrong value and that wrong value corrupts dedup.
   **Confusing, not merely stale** — it manufactures the duplication that was later measured.
3. **Instructions are hidden where the optimizer can't reach them.** `prompt_manifest`'s *input field
   description* is a twenty-line sub-prompt: what the manifest is, three patterns to watch for, which
   output fields to set, which scope value to use. DSPy optimises instructions; content buried in
   per-field `desc` strings is invisible to that mechanism. Hand-written mega-descriptions are exactly
   what optimisation is meant to replace.
4. **One call, three unrelated jobs, mostly empty.** ADR-0056 (failure path), FRE-409 (prompt
   composition) and FRE-328 (missing skills) were each grafted on separately. Nearly every output
   field says *"empty string if…"*. A typical invocation pays full inference to return blanks.

**What did age well — keep it.** FRE-409's prompt-composition analysis is genuinely the
"which inputs to add or remove" capability: it receives the ordered component IDs, the cacheable
static-prefix hash and the trailing-7-day mean rating for the callsite, and must name a specific
`component_id`. `failure_path_fix_location` demands file + symbol. `missing_skill_names` enforces
`{domain}-{noun}` so the same gap gets the same name and becomes clusterable.

**The consequence for the Analyzer.** DSPy optimises a *fixed signature*. An iterative agent is not a
signature — so DSPy is probably the wrong frame for the Analyzer's outer loop, and the **right** frame
for the bounded checks inside it. That maps cleanly onto skills-as-runbooks: the agent decides *what to
investigate*; each investigation is a small, optimisable, individually-measurable signature. Which also
cures the accretion — new questions become new skills, not more fields on one overloaded call.

---

## Open questions carried forward

1. What is the normative reference a session is judged against?
2. Session-end doesn't exist (`SESSION_CLOSED` defined, never emitted; no turn count, no closed flag).
   Build it, or define the unit as something that does exist — idle window, trace, thread?
3. Is "session" even the right unit, or is it **thread** — inferred, not declared? The four learning
   threads are the natural topical unit, and clicking *new* currently forces a filing decision before
   the conversation exists.
4. What is the cost of one analysis, and therefore what trigger rate is affordable?
5. Does the Analyzer read its own prior output as *context* or as *input to re-reason over*?
   The second risks amplifying its own conclusions.
6. Retention policy for the capture substrate — currently accidental, and it determines whether any of
   this has a substrate in two years.
