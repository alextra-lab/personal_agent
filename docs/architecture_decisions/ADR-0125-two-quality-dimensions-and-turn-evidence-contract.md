# ADR-0125: The Two Quality Dimensions and the Turn Evidence Contract

**Status:** Proposed
**Date:** 2026-07-27
**Deciders:** Project owner; adr session (Opus)
**Tags:** architecture, observability, capture, verification, self-improvement, memory, boundary
**Supersedes (on acceptance):** ADR-0067 (Reflection Surfacing in Context Assembly) — see D2. While
this ADR is `Proposed`, ADR-0067 correctly still reads `Accepted`; the header change is an
implementation consequence, not a claim of present fact.
**Backing ticket:** FRE-999
**Evidence base:** `docs/research/2026-07-27-feedback-layer-and-evidence-contract-audit.md`

---

## Context

### The issue

This project has been pursuing two different quality questions without ever naming them as
different:

| | **Dimension 1 — harness health** | **Dimension 2 — output quality** |
|---|---|---|
| Question | optimised, efficient, cost-effective? | factually true, complete, useful? |
| Consumer | the owner as **operator** | the owner as **user / student** |
| Evidence | telemetry | conversation + tool records + recalled knowledge |
| Failure mode | slow, expensive, flaky | wrong, ungrounded, useless |
| Cadence | continuous / background | post-session |
| Mutability of the artifact | irrelevant — consumed and discarded | essential — later sessions supersede |

Because the distinction was never drawn, components built for one dimension were conscripted into
the other, and the confusion became structural rather than incidental.

### The evidence that the distinction is missing

The decisive finding is what a capture records about memory recall — in full
(`captains_log/capture.py:69-70`):

```
memory_context_used: bool = False
memory_conversations_found: int = 0
```

A boolean and a count. **No identities.** The only place per-turn entity identities are persisted is
`telemetry/compaction.py`, whose comment reads: *"Maps session_id → set of entity_ids dropped in the
most recent compaction."*

> **The system durably records which facts it discarded, and not which facts it relied on.**

That is not a missing field. It is the signature of a capture layer instrumented to observe whether
the machine ran — counts, booleans, durations, CPU, step totals, iteration caps — and never whether
the output was true. **Dimension 2 has no evidence base because nobody declared dimension 2
existed.**

### Three conflations that follow from it

- **Reflection recall (ADR-0067).** A dimension-1 producer's output injected into dimension-2
  surface. Its selection required a non-empty proposed change and a `seen_count >= 2`, treating
  recurrence as signal — but a rejected proposal recurs *because* it was rejected and its generating
  condition persists. It therefore preferentially surfaced ideas the owner had already declined, into
  the system prompt of unrelated conversations. The prod `.env` records the owner's decision of
  2026-07-26: *"…every session in a fortnight into conversation context, including rejected
  proposals… disable, then remove properly — ADR-0067 is Accepted and must be superseded, not
  deleted."*
- **The session digest (ADR-0124).** A dimension-2 artifact scoped by a dimension-1 constraint — a
  token budget — producing a 250-token bound that ADR-0124 D3 itself records as provisional and
  never empirically derived.
- **`status_contradiction`.** A dimension-2 *verification* function originally specified inside a
  dimension-2 *summarizer*. ADR-0124 Amendment B extracted it to "a future verification oracle."
  That extraction reads in hindsight as scope reduction; it was in fact the first time the
  verification layer was named as a distinct thing.

### What is not broken

The dimension-1 machine is **not** incoherent, contrary to first impressions from reading the file
tree. ADR-0105 diagnosed the historical producer split on 2026-07-02 and its convergence shipped:
seven of eight children are Done (FRE-714, 715, 716, 718, 719, 720, 721). The eighth — **FRE-717,
the assembled seam that closes the loop** — has been in `Awaiting Deploy` since 2026-07-08, and the
arc is code-complete: `brainstem/jobs/outcome_ingestion.py` writes a realized-value signal and
`promotion.py:458` reads it back to rank and suppress. **The loop exists in code and has never run
end to end once.**

Likewise the reflection producer is correctly scoped for what it is. An earlier reading in this
session treated its `reply_length: int` input and its five infrastructure focus areas as a defect;
the owner corrected it — reflection was only ever intended as system-side. That retraction is
recorded in the audit. **The gap is not a bad dimension-1 producer; it is that dimension 2 has no
producer at all.**

### Definition of terms, fixed by the owner (2026-07-27)

**Verification** means *the model producing factually true and complete output*, assessed
**post-session**. It does **not** mean verifying that the harness is optimised, cost-efficient, or
fit for purpose. That is dimension 1 and has its own machinery.

---

## Decision

### D1 — The two dimensions are an architectural axis, and every producer declares its dimension

Dimension 1 (harness health) and dimension 2 (output quality) are named, distinct, and
non-interchangeable. Every producer of a durable derived artifact — a proposal, a digest, a finding
— **declares which dimension it serves**, and that declaration is **structurally required, not
conventional**.

Concretely, ADR-0105's `source` discriminator becomes **non-nullable**: a write that omits it is
rejected, not defaulted. This closes the enforcement gap in ADR-0105 AC-1, where
`source: ProposalSource | None` left the invariant as a convention.

**Dimension is a property of the producer, not of the subject.** A dimension-1 producer may reason
about anything it likes; what is constrained is where its output may go (D2). This preserves
ADR-0106's governing principle — *nothing gates the model's reasoning path* — and its owner-stated
non-goal of "a rigid process that constrains the model's reasoning."

### D2 — Boundary invariant: dimension-1 output never enters user-facing context

**A dimension-1 producer's output may never be written into, or injected into, user-facing
conversation context.** Isolation is achieved at write time by where the output is stored, never at
read time by a filter that can be forgotten — consistent with ADR-0105's engine-level `sysgraph`
isolation and ADR-0115's write-time dispatch.

**This supersedes ADR-0067** (Reflection Surfacing in Context Assembly, Accepted 2026-05-09). Its
decision is void under this invariant: reflections classify as findings, findings live in the
isolated store, and the isolated store exists precisely so self-improvement data can never reach
the user knowledge graph or user context. ADR-0067 is superseded rather than deleted, per the
owner's instruction.

Consequently the recall path is **removed**, not merely defaulted off. Today
`request_gateway/context.py:309` reads `if settings.reflection_recall_enabled:` and then imports and
injects the reflections section — a live, configuration-gated injection point — while
`settings.py:2307` defaults that setting to `True` and only the prod `.env` turns it off. So any
fresh deploy, test stack, or rebuilt image lacking that line re-enables the behaviour silently.

**Flipping the default is insufficient and this ADR does not accept it as the remedy.** "May never"
is a configuration-independent claim: while the call site exists, the invariant holds only by
setting. The call site goes.

The general rule this encodes, for future surfaces:

> **A recall surface that guesses relevance and lands in the system prompt has asymmetric cost — a
> miss is invisible, a false positive taxes every turn it fires on.** Injected context is
> fail-closed. Hedging the label ("past observations, not current directives") is a wish, not a
> mechanism.

### D3 — The turn evidence contract

Every turn **durably records** the following, so that post-session verification of output quality is
*possible* later. This is the decision this ADR primarily exists to make, because it is the one that
is cheap now and expensive once a corpus must be migrated.

| # | Required record | Why dimension 2 needs it |
|---|---|---|
| 1 | user message, full and untruncated | the question completeness is judged against |
| 2 | assistant response, full text | the claims to be extracted and adjudicated |
| 3 | reasoning / thinking trace where the provider emits one | diagnoses *why* a claim was wrong |
| 4 | per tool call: name, arguments, status, **full result payload** (or an artifact-store pointer to it), and ordering | adjudicates "I did X" and "the data says Y" |
| 5 | **identities of the memory items recalled into the turn**, with their scores | adjudicates assertions about stored knowledge; detects facts that were available and unused |
| 6 | the assembled context actually sent to the model, or its component manifest | establishes what the model *could* have used |
| 7 | trace / session / turn identifiers | joinability |
| 8 | model and parameters | attributes a failure mode |

Items 1, 2, 7 and 8 exist today. Item 3 is partial. **Item 5 does not exist** — a boolean and a
count only. **Items 4 and 6 require confirmation before their chain tickets are sized**: for item 4,
ADR-0124 Amendment B stopped tool payload *delivery to the digest* while storage was retained, and
whether the full payload survives durably is unverified; for item 6, `prompt_manifest` (FRE-409) is
a *likely* but unconfirmed satisfier. Neither was established by the backing audit, and this ADR
does not assert them.

### D4 — Recall identities carry a usage edge

Item 5 is not merely a list. The record links a recalled item to the turn that used it, so the
system can answer *which turns relied on this claim* — and, joined to ADR-0098's supersession chain,
*which turns relied on it before it was replaced, and why it was replaced*.

This is the single change that pays into both dimensions: trust calibration for the memory layer
(dimension 1) and the evidence base for verification (dimension 2). On the dimension-1 side,
`corroboration_count` and `last_confirmed` on `KnowledgeWeight` are the natural consumers — the
backing audit **found no populator for them, but did not exhaustively prove one absent**, so the
claim here is only that *no consumption signal currently feeds them*, not that the fields are
entirely unwritten. The implementation ticket confirms this before relying on it.

### D5 — No silent truncation on any evidence path

Content on a path feeding a durable artifact or assembled context is stored **whole**, or shortened
with an **explicit marker** recording that it was shortened and by how much. Silent truncation is
prohibited.

This retires the 200-character idiom. The figures that condemn it are **recorded in the digest
producer's own docstring** (`second_brain/session_summary.py:14-19`): user messages at p50 58
characters — already below the cut — against assistant responses at p50 1,847, so the clip discarded
roughly **89% of the assistant text** where a session's outcome lives. *That docstring asserts the
result without carrying the query or dataset that produced it*, so it is a recorded project figure
rather than an independently reproducible measurement; the implementation ticket re-derives it before
sizing anything on it.

The idiom is known at **at least eleven sites, and the enumeration is not exhaustive** — codex review
of this ADR found two the backing audit had missed (`orchestrator/executor.py:3442`,
`captains_log/reflection_dspy.py:434`) on top of the nine it listed. The implementation therefore
owes a **guard**, not a fixed list of edits (see AC-5).

The most damaging instance is live in context assembly (`request_gateway/context.py:240`): when an
episode has no digest, assembled context receives the **user message clipped to 200 characters and
no assistant text at all** — strictly worse than the summarizer the owner already rejected, which at
least kept 200 characters of the answer. Against measured digest delivery of 6 across 61 qualifying
sessions, this is what the model sees for roughly 90% of recalled episodes.

### D6 — The verification oracle is deferred; only its bounds are decided

The oracle's design and implementation are **explicitly out of scope**. The general form — verify any
claim against the world — is not implementable, and this ADR stops implying otherwise. What is
decided now are the constraints any future oracle design must satisfy, so that D3 captures the right
things:

- **Relative to available evidence, never to the world.** The oracle verifies whether the model
  faithfully used what it had. This is the standard *faithfulness* / *attribution* framing —
  FActScore (Min et al. 2023), RAGAS faithfulness (Es et al. 2023), AIS (Rashkin et al. 2021).
- **Three-valued verdicts — CONFIRMED / REFUTED / UNVERIFIABLE** — with `UNVERIFIABLE` first-class
  and never silently treated as a pass. This mirrors the evidence contract already applied by hand
  at the master acceptance gate.
- **Completeness is judged only against evidence the system held**: the user's question, tool
  payloads (the strongest signal — *the tool returned ten rows, the answer discussed three and did
  not say so*), and recalled-but-unused facts. Completeness against the world is `UNVERIFIABLE`.
- **Sampled, not exhaustive.** One session in N is sufficient for a measurement instrument. Given
  the 2026-07-25 cost incident, this is a design constraint rather than a later optimisation.

### D7 — Taxonomy is derived from the consumer, not from an ontology

Where a facet or class is introduced in service of this contract, it is **derived from the question
its consumer must answer**, never from a taxonomy of what things are. A verification-oriented facet
("how would I check this claim?") is a different axis from a retrieval-oriented one ("what is this
about"), and forcing one taxonomy to serve both is how a taxonomy begins working against the
process.

Two operating rules follow:

- **Write-time dispatch only for isolation boundaries that should never be crossed**, where a
  forgettable read-time filter is genuinely weaker (D2, ADR-0105, ADR-0115).
- **Read-time facets for anything retrieval-related**, where the cut may be wrong and must stay
  revisable. Write-time routing is first-write-wins for *location*: ADR-0098 D2 deliberately killed
  first-write-wins for claim *content*, and a misdispatched item is in the wrong store permanently.

---

## Alternatives Considered

### Option 1: Build the verification oracle now

**Description:** Design and implement post-session verification in this ADR, deriving capture
requirements as an implementation detail.

**Pros:** One decision instead of two; the capture contract is validated by a real consumer rather
than a projection; no risk of capturing the wrong things.

**Cons:** The general oracle is not implementable and the bounded one is unproven here; it would be
shaped by whatever capture happens to exist, entrenching current gaps; and it commits significant
build effort to a component whose feasibility the owner explicitly doubts.

**Why Rejected:** The owner's concern — *"we're moving too fast towards the conceptual verification
oracle that may or may not be possible to implement"* — is correct for the general form. More
decisively, the audit shows the project has twice paid for producers whose consumers were never
measured (the digest, and reflection recall). Building a third before its inputs exist would repeat
that pattern at larger scale. Deferring costs nothing provided the evidence contract lands, which is
what D3 does.

### Option 2: Defer the capture contract as well — decide nothing until the oracle is designed

**Description:** Treat capture as an implementation detail of a future oracle ADR; change nothing
now.

**Pros:** Avoids capturing fields no consumer has yet justified; smallest immediate footprint;
maximum optionality.

**Cons:** Capture changes are cheap while the schema is being touched and expensive once a corpus
exists to migrate. Every session that passes without item 5 is a permanent hole in the historical
record — recall identities cannot be reconstructed after the fact, because the ranking that produced
them is not deterministic across index state changes.

**Why Rejected:** Asymmetric and irreversible cost. Additionally, D4's usage edge pays into
dimension 1 on its own — it is the missing populator for `corroboration_count` and `last_confirmed`
— so it is justified even if the oracle is never built.

### Option 3: Capture everything at full fidelity and decide later

**Description:** Drop the contract; retain complete raw traces for every turn indefinitely and let
future consumers query whatever they need.

**Pros:** No consumer needs to be anticipated; nothing is lost; simplest rule to state.

**Cons:** Unbounded storage and retention cost against an Elasticsearch surface with an open
over-sharding remediation (FRE-983) and no written retention policy. It also does not solve the
actual problem: the system already logs a great deal — durations, counts, CPU, step totals — and
still cannot answer which facts a turn relied on. **Volume was never the constraint; the absence of
a contract was.**

**Why Rejected:** "Log everything" is how the current asymmetry arose. A contract that names eight
required records is both cheaper and more useful than an instruction to retain more of what is
already the wrong shape.

### Option 4: Keep one dimension — treat output quality as a facet of harness health

**Description:** Reject the axis. Extend the existing dimension-1 producers with quality signals and
let one pipeline serve both questions.

**Pros:** No new concept; reuses the converged ADR-0105 pipeline, its dedup, its single promotion
entrypoint, and its funnel; nothing to isolate.

**Cons:** The two have different evidence, consumers, cadences, mutability requirements, and failure
modes. The empirical record is that conflating them produced the reflection-recall pollution, the
budget-scoped digest, and a capture layer blind to output quality.

**Why Rejected:** The conflation is the root cause this ADR exists to address. Choosing it would be
choosing the diagnosed disease.

---

## Consequences

### Positive Consequences

- Dimension 2 acquires an evidence base for the first time, making post-session verification
  *possible* without committing to any particular oracle design.
- The reflection-recall class of failure becomes structurally impossible rather than
  configuration-dependent, and stops depending on one line of one `.env` file.
- ADR-0105's AC-1 becomes a type rather than a convention.
- The memory layer gains a consumption signal its trust fields were designed to accept.
- Silent truncation stops destroying the assistant text where session outcomes live — enforced by a
  guard rather than a one-time sweep, so newly introduced clips cannot reappear.
- The dimension-1 machine is freed to be judged on its own terms — and its remaining defect is
  identified precisely: one undeployed seam, not architectural incoherence.

### Negative Consequences

- Capture volume grows. Item 4 (full tool payloads) and item 5 (recall identities with scores) are
  the two material additions, and item 4 may be large — mitigated by permitting an artifact-store
  pointer rather than inline bytes.
- Retention policy, currently accidental, becomes load-bearing and must be written. This ADR does
  not write it; it makes the omission consequential.
- A non-nullable `source` is a breaking change for any producer that currently omits it, and legacy
  rows will carry nulls that a backfill or an explicit "unknown" sentinel must address.
- Removing rather than disabling reflection recall deletes a code path that a future dimension-2
  producer might have reused in a different form.

### Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The contract captures the wrong things because no consumer has validated it | Medium | High — an expensive, wrong schema | AC-6 requires a dry run over real sessions that only *locates* evidence and never judges; it fails on capture gaps, so the contract is validated before an oracle exists |
| Capture growth becomes a cost or sharding problem | Medium | Medium | Artifact-store pointers for large payloads; retention policy named as owed; FRE-983 already open |
| Non-nullable `source` breaks a live producer | Low | Medium | Both producers already set it (`insights/engine.py` sets `STATISTICAL_DETECTOR`); change is enforcement, not new plumbing |
| Legacy nulls block the constraint | Medium | Low | Backfill or an explicit sentinel decided in the implementation ticket, not here |
| The deferred oracle never gets built and the capture cost is wasted | Medium | Low | D4's usage edge is independently justified by dimension-1 trust calibration |
| Taxonomy coverage assumptions are stale | Medium | Medium | The audit's `class=None` figures predate ADR-0115 (Implemented 2026-07-12); a fresh count is owed before any decision leans on taxonomy coverage |

---

## Implementation Notes

- **Two chains, filed separately.** A capture chain implementing D3/D4/D5, and a dimension-1 fixes
  chain carrying the surviving audit findings. Neither is blocked by the other.
- **FRE-717 sequencing.** Closing the ADR-0105 loop is dimension-1 work and is master's to deploy.
  It should follow the dimension-1 fixes rather than lead them, so realized value measures a
  producer whose known defects have been addressed.
- **Items 4 and 6 are verification tasks before they are build tasks.** Confirm whether full tool
  payloads survive durably, and whether `prompt_manifest` genuinely satisfies item 6. Both answers
  size the capture chain and neither is established today.
- **Doc drift for master, on acceptance.** Flip the ADR-0067 reflection-surfacing header to
  `Superseded by ADR-0125`. This is deliberately *not* an acceptance criterion — a header edit
  proves nothing about behaviour, which AC-2 covers — but it is drift master should reconcile at
  the gate.
- **Doc drift for master, pre-existing.** Two distinct ADRs are numbered 0067
  (`ADR-0067-reflection-surfacing-in-context-assembly.md` and
  `ADR-0067-skill-nudge-injection.md`). This ADR supersedes the former only. The collision predates
  this work and should be reconciled separately.
- **The audit's retraction is deliberate.** The reflection producer is correctly scoped for
  dimension 1; do not "fix" `reply_length`.

---

## Verification / Acceptance Criteria

- **AC-1 — A produced proposal cannot exist without a declared dimension.** · **Check:** attempt a
  proposal write with the source omitted and confirm it is rejected rather than defaulted; then
  query the proposal store for rows with a null source and require zero. · *Fails if* a null-source
  row can be created by any code path, or if any production row carries a null after the migration.

- **AC-2 — Dimension-1 output cannot reach assembled context under *any* configuration.** The
  invariant is configuration-independent, so the test is too. · **Check:** with a corpus containing
  reflections and findings, assemble context for a turn **with `reflection_recall_enabled` forced
  `True`** (and every other related toggle forced to its most permissive value), and assert no
  reflection or finding body appears in the assembled prompt — which can only hold once the call
  site at `request_gateway/context.py:309` is gone rather than gated. Repeat with stock defaults.
  · *Fails if* any finding-class text appears under any setting, or if absence holds only because a
  flag is off. **A flag flip alone fails this criterion.**

- **AC-3 — The capture records the memory items actually *admitted into the assembled context*, by
  identity — not the candidate set, and not a count.** · **Check:** run a turn where the ranked
  candidate set is deliberately larger than the admitted set (force budget trimming), then compare
  the capture's recorded identifiers against the identifiers present in the assembled prompt.
  Require exact equality with the **admitted** set; items dropped by compaction must appear as
  dropped, not as used. · *Fails if* the capture holds only a boolean or count, if it records the
  ranked set instead of the admitted set, or if a trimmed item is indistinguishable from a used one.

- **AC-4 — The usage join returns exactly the turns that used a claim, and nothing else.** ·
  **Check:** construct a claim used in a known set of turns, subsequently superseded, and also
  ranked-but-not-admitted in at least one further turn. Join recalled-item records to the
  supersession chain and require the returned turn set to **equal** the known using-turn set —
  excluding the ranked-but-not-admitted turn and excluding turns after the supersession. · *Fails
  if* the returned set is a superset, a subset, or includes a post-supersession or
  ranked-but-unused turn. **Returning "something" is not a pass.**

- **AC-5 — A guard fails CI on a newly introduced silent clip on any evidence path.** The decision
  is path-general, so the criterion is a guard rather than an enumeration — the site list is known
  to be incomplete. · **Check:** add the guard, then feed it a **known-bad input** — a newly
  introduced bare `[:N]` clip on an evidence path — and confirm CI fails; feed it a properly marked
  truncation and confirm CI passes. Separately, run a turn whose assistant response materially
  exceeds 200 characters and confirm stored byte length equals emitted byte length. · *Fails if* the
  guard passes the known-bad input, if it fires on a correctly marked truncation, or if stored
  length is less than emitted with no marker.

- **AC-6 — SEAM: the record is *sufficient to contradict a false claim*, proven deterministically
  and without the oracle.** · **Check:** two parts, neither requiring a model. **(i) Coverage:** over
  every session in a defined 7-day window, mechanically assert that all eight D3 records are present
  and mutually joinable — every tool call named in the assistant text resolves to a tool record,
  every recorded recall identifier resolves to a live claim, every turn joins to its session.
  **(ii) Negative control:** on a deliberately constructed turn whose assistant text asserts an
  action that was *not* performed, confirm the stored record alone is sufficient to contradict it —
  by a deterministic check comparing asserted action to tool records. · *Fails if* any session in
  the window has a missing or unjoinable required record, **or if the planted false claim cannot be
  contradicted from the record alone.** Part (ii) is the discriminating half: a contract that merely
  stores fields passes coverage and fails the negative control.

**Seam owner (assembled intent):** **AC-6** is the assembled seam, and specifically its **negative
control**. The evidence contract is not delivered because its child tickets merged, nor because
fields exist and are populated — it is delivered when a planted false claim is demonstrably
refutable from the record. No single child ticket proves that; master holds the ADR against AC-6.
AC-1 through AC-5 are asserted independently by their own children. **AC-2 additionally carries the
behavioural retirement of ADR-0067** — its status-header change is documentation drift for master to
reconcile at the gate, deliberately *not* an acceptance criterion, because a header edit proves
nothing about behaviour.

---

## References

- `docs/research/2026-07-27-feedback-layer-and-evidence-contract-audit.md` — the evidence base for every measurement cited here
- `docs/research/2026-07-26-session-summarizer-brainstorm-brief.md` — the brief this session opened from
- ADR-0067 — Reflection Surfacing in Context Assembly (Accepted 2026-05-09; **superseded by this ADR**)
- ADR-0061 — Within-Session Progressive Context Compression (Accepted, implemented 2026-05-01; soft trigger retired 2026-07-22)
- ADR-0092 — Context-Compaction Observability and Surfacing (Implemented 2026-06-23) — the four compaction mechanisms, verified against code
- ADR-0097 — Ingested-Knowledge Taxonomy (Proposed — hypothesis, held loosely; class vocabulary refined by ADR-0115)
- ADR-0098 — Memory Substrate and Lifecycle Architecture (Accepted 2026-06-27) — D2 living-claim supersession
- ADR-0105 — Convergent Self-Improvement Pipeline and System Graph (Accepted) — the `source` discriminator, `sysgraph` isolation, and the FRE-717 seam
- ADR-0109 — Entity Taxonomy Redesign (Accepted 2026-07-03; Amendment 1 2026-07-04, FRE-782)
- ADR-0115 — Knowledge Class Axis: Emission, Persistence, Dispatch (Implemented 2026-07-12; supersedes ADR-0106)
- ADR-0124 — Session-Summary Producer and Phased Consumption (Accepted 2026-07-23; Amendments A and B)
- FRE-999 — this ADR's umbrella ticket
- FRE-717 — ADR-0105 T4, the loop-closure seam, in Awaiting Deploy since 2026-07-08
- FRE-983 — Elasticsearch telemetry index-lifecycle remediation (retention and sharding)
- Min, S. et al. 2023, *FActScore: Fine-grained Atomic Evaluation of Factual Precision*, arXiv:2305.14251
- Es, S. et al. 2023, *RAGAS: Automated Evaluation of Retrieval Augmented Generation*, arXiv:2309.15217
- Rashkin, H. et al. 2021, *Measuring Attribution in Natural Language Generation*, arXiv:2112.12870
- Shirky, C. 2005, *Ontology is Overrated: Categories, Links, and Tags*
- Ranganathan, S. R., *Colon Classification* — facet analysis

---

## Status Updates

### 2026-07-27 - Proposed
**Changed By:** adr session (Opus), owner-directed
**Reason:** Authored from the 2026-07-26/27 exploration and audit. Scope confirmed with the owner:
decide the evidence contract now, defer the verification oracle. Awaiting owner acceptance.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
