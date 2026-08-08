---
name: explore
description: Use in the explore session (cc-explore) to run a commissioned study end to end — measure against the live system, carry per-finding evidence under the admissibility rule, write the research document, file Backlog tickets, and open the document's own PR for master's disposition. Never merges, never deploys, never promotes its own tickets.
---

# Explore session — run the study, carry the evidence, propose within the cap

Read `.claude/skills/lifecycle-rules.md` first (§ Explore session, § Coordination stores). This is the
**working** skill: `prime-explore` rebuilds situational awareness, this one runs a commissioned study.
Implements ADR-0135 D3–D6.

**Argument: none → resolve NEXT via the external dispatch resolver**
(`python -m scripts.dispatch.next_resolver --stream explore --json`). A nonzero exit, invalid JSON, or a
printed error → STOP and surface stderr; never reconstruct the busy-guard/priority/blocked-by logic
inline (ADR-0113 §1). A `null` result (the stream is occupied, or no eligible `Approved` candidate — the
resolver conflates both) → STOP. An explicit `FRE-…` id skips the queue.

**At pickup, move your own ticket → `In Progress`** (`save_issue state="In Progress"`). This is
lifecycle-rules D4's existing named delegation, not a new grant — and it is what makes the busy guard
real: a stream whose ticket never leaves `Approved` can be re-dispatched under itself, resetting a live
context mid-study.

**You are an extension of the owner, not of master** (ADR-0135 D1). Master supplies project clarity and
exigence at the **exit**, aimed at one target: whether your *proposals* are possible. Nobody briefs you
in. The commission states the attack, never the answer — and a study that merely confirms its
commission's hypothesis has failed at its own terms.

---

## Scope — read-only on everything operational, with exactly two exceptions

You never merge, never deploy, never mutate the Linear **control** plane (states beyond your own pickup,
labels, relations, priorities), never write `docs/plans/OWNER_CONSOLE.md`, never label dispatch, never
rebuild the gateway, never touch `main`, never edit `src/`.

**The two bounded exceptions, and nothing beyond them:**

1. **`Backlog` tickets and comments** — see *Filing*, below.
2. **Your own research document, on your own branch, in its own PR** — see *Branch, path and PR scope*.

Proposing is not executing. If a conclusion needs **executing**, it reaches master through the owner,
never your own hand.

---

## Measure against the live system — never reason from code

**Contract, not habit.** Every claim about what the system *does* is a measurement against the running
system. Reading the code tells you what it *would* do if that path executed, at *some* revision — which
is a different claim, and the gap between them is where studies go wrong.

- A code path you have read is a **hypothesis**. It becomes a finding when a live query confirms it.
- A configuration value in the repo is a **default**. The live value is whatever the running process
  holds; read it there.
- "The board says this shipped" is not deployment evidence. Identify deployed code by container file
  hash (FRE-1131 found `main` already carrying FRE-1115's code while its ticket said otherwise).
- Where a claim genuinely cannot be measured, say so and mark it **UNVERIFIABLE**. Do not substitute a
  code reading and present it at the confidence of a measurement.

Code citations remain welcome and expected — as *provenance for an identifier* (below) and as
explanation for a measured result. What they may not do is stand in for the result.

---

## The admissibility rule for negative findings

A zero is the one result that looks identical whether you are right or your instrument is wrong. This
rule exists because that failure has occurred twice on this project's own record — once in a merged
research document (FRE-1131, erratum PR #804), once in an ADR review three days later, on the same
subject, by a different agent.

<!-- RULE:START -->

**A finding whose verdict is negative — zero, never, does not fire, cannot engage, does not compose,
however phrased — is INADMISSIBLE unless it carries, on that finding itself, every arm stated below.**

The arms are stated per finding, not once per document. A global assurance in a method appendix does not
discharge them: that is precisely what was asserted, and believed, while the finding it should have
caught went out wrong.

<!-- ARM-1:START -->

#### Arm 1 — target-identifier provenance, for the queried identifier in the queried store

Satisfied by either form:

- **1a — a raw instance.** A document, row or record quoted verbatim, exhibiting that identifier, drawn
  from **the same store the finding queries**, within the same window. This settles the question
  outright and is the preferred form.
- **1b — a live producer.** Where no instance exists — the honest case for a mechanism that truly never
  fires — cite the emit site **at the deployed revision**, not at any revision, **plus** evidence that
  its enclosing path executes: a sibling emit from that same code path returning non-zero over the same
  window. A citation alone is not enough; a dead branch emits nothing and looks identical to a wrong
  name.

**The store matters as much as the name.** An identifier can be entirely real in this repo and still be
absent from the store you queried — a real event-bus stream is not an `agent-logs` event. Provenance
against *some* producer is not provenance against the one feeding the store under query.

<!-- ARM-1:END -->

<!-- ARM-2:START -->

#### Arm 2 — path liveness, on the same query with only the identifier varied

A control returning non-zero using the **identical** index pattern, time range and filter predicates,
changing nothing but the target identifier. Quote its actual output.

This excludes an unreachable index and a malformed filter — a different failure from a wrong name, at
negligible cost to rule out. It does **not** establish that your identifier is real; a control can
succeed against a store that has never held the thing you are asking about.

<!-- ARM-2:END -->

<!-- ARM-3:START -->

#### Arm 3 — scope match: the verdict claims no more than the query covers

Every narrowing predicate the query carries — service, environment, time window, session or tenant
subset — is named in the finding, and the verdict is stated at that scope. **A global claim drawn from a
scoped query is inadmissible.**

This is the only check that catches a zero produced by a wrong *predicate* rather than a wrong *name*:
a query accidentally restricted to service A returns zero for a mechanism that writes under service B,
and every identifier in it is real.

<!-- ARM-3:END -->

<!-- RULE:END -->

**A finding whose verdict is positive** — "this value is 260" — needs no arm at all. A wrong identifier
cannot produce a non-zero, so the result is self-validating. State its query and its actual output and
move on; the rule above is a guard on one failure mode, not a tax on every finding.

**The rule is stated by semantics, not by keyword.** A negative verdict written as a positive sentence
("the mechanism is idle", "the path is quiet") is still a negative and still carries the arms. Ask what
the claim would be falsified by, not how it is phrased.

### Maintenance note — the arms must stay separately deletable

The arms sit in explicitly anchored blocks (`<!-- ARM-n:START -->` / `<!-- ARM-n:END -->`) for a reason
that is not stylistic. ADR-0135's seam ticket (FRE-1195) adjudicates this rule by **deleting arm 1 from
a copy of this text** and checking that the remainder then admits a finding the full rule rejects. If
the deletion is not a defined operation, that test cannot be run and the ADR's central claim — that arm
1 is the arm doing the work — cannot be adjudicated at all.

Three constraints follow. An editor tidying this section must preserve all three:

1. **Keep the anchors.** They make "delete arm 1" mechanical rather than editorial.
2. **No arm count inside the rule block.** The operative sentence reads *every arm stated below*, never
   a fixed number. A hardcoded count would make the arm-1-deleted variant self-contradictory instead of
   merely shorter.
3. **No cross-arm references inside an arm.** Each arm states its own test in full and never points at
   another by number, so removing any one strands nothing. Commentary comparing them belongs out here,
   outside the block — as this note is.

`tests/scripts/test_explore_skill_contract.py` enforces all three by performing the deletion.

### UNVERIFIABLE — the test that separates it from a negative

**UNVERIFIABLE is a first-class verdict, never silently equivalent to a negative.** A negative asserts
that the thing does not happen. UNVERIFIABLE asserts nothing about the thing at all — only that this
instrument cannot see it. Collapsing the two is how a measurement failure gets reported as a system
property.

Apply this test, on arm 1:

| What you found | Verdict |
|---|---|
| Arm 1 **can be produced, and you produced it** | **NEGATIVE** — admissible once the remaining arms are carried too |
| Arm 1 **cannot be produced** after real search — no raw instance in that store, and no producer for that identifier feeding it | **UNVERIFIABLE** — record what you searched and where |
| Arm 1 **not attempted** | **No verdict yet.** You have an unfinished measurement, not a result. Neither value is available to you |

The third row is the one that matters in practice: "I could not satisfy arm 1" and "I did not try"
produce the same empty evidence and must not produce the same verdict.

---

## Worked example — a liveness-only negative, and why the rule rejects it

From `docs/research/2026-08-03-fre-1131-context-memory-alignment-audit.md` §F1 — a real finding that
reached a merged document and required an erratum.

| Mechanism | Fire event | Count since 07-23 |
|---|---|---|
| B — within-session hard gate (ADR-0061) | `within_session_compressed` | **0** |
| D's per-turn evaluator (FRE-944) | `cache_reset_decision` | **94** |

Row B was reported as *"the within-session hard gate never fires."* Row D sat in the same table as the
control: same index, same window, same query shape, non-zero. Applying the rule as written:

- **Arm 2 — satisfied.** Row D varies only the identifier and returns 94. The index is reachable, the
  filter is well-formed.
- **Arm 3 — satisfied.** The index and the window (`since 07-23`) are named, and the verdict is stated
  at that scope.
- **Arm 1 — not satisfied.** No `agent-logs` document exhibiting `within_session_compressed` is quoted,
  and none exists. The identifier is real in this repo — it is the event-bus stream
  `stream:context.within_session_compressed` (`src/personal_agent/events/models.py:149`) — but the
  event bus is not the store the query hit.

**Verdict: inadmissible, for want of arm 1.** Note what a liveness-only rule would have done here: the
control passed, in the same table, and the finding was still wrong. That is the whole reason arm 1
exists.

**What arm 1 returns when you actually go looking.** The emits under this mechanism are
`within_session_compression_hard_trigger` (`src/personal_agent/orchestrator/executor.py:4139`) and
`within_session_compression_recorded`
(`src/personal_agent/telemetry/within_session_compression.py:137`) — 260 all-time, 2 since 2026-07-20.
The finding was never a negative at all: it was a **positive**, read off a wrong identifier. Searching
for provenance does not merely license the claim; here it reverses it.

**And if no arm-1 form had existed** — no instance in `agent-logs`, no producer feeding it — the verdict
would be **UNVERIFIABLE**, not "never fires."

---

## The deliverable shape

One document per study, at `docs/research/<date>-fre-XXXX-<slug>.md`. The shape is fixed — a reviewer
must be able to check any single finding without reconstructing the study.

**Per finding, every finding, MUST carry:**

| Field | Content |
|---|---|
| **Verdict** | POSITIVE · NEGATIVE · UNVERIFIABLE |
| **The query** | as actually run, verbatim — store, index pattern, predicates, window |
| **Its actual output** | the real numbers or rows returned, quoted. Not a summary of them, not a restatement of what they mean |
| **The arms** | when and only when the verdict is negative, stated on that finding |

**The document MUST also carry, each exactly once:**

- **A method appendix** — what you measured against, which stores and windows, what you rejected and
  why, and the identifier resolutions you performed.
- **A `## Proposals` section — the single place recommendations appear.** A recommendation stated
  anywhere else in the document is still a recommendation and master must still disposition it;
  scattering them is how a study exceeds its own cap without noticing. **At most ten.** Overflow is
  inadmissible: consolidate before the PR, do not append.
- **A `## Filed tickets` list** naming the id of every ticket this study filed. Every ticket on this
  project is created under one Linear account, so authorship cannot be recovered from the board, and a
  census keyed on tickets that *quote* a finding misses one that paraphrases it. **An unlisted ticket
  traceable to this study is itself a violation** — the list is the instrument, so its completeness is
  the obligation.

Ten is the cap on what master adjudicates, not a target to fill. Three well-founded proposals beat ten
padded ones, and the cap is calibrated on real work (FRE-1131 stated seven).

---

## The durable substrate map

These are the traps that cost previous studies real findings. They live here, not in a brief, so no
commission has to restate them.

- **`_count`, never `_cat`.** `_cat` output is human-formatted and silently truncates; use `_count`, or
  `_search` with `size=0`, and read the number you actually asked for.
- **Elasticsearch counts are provisional (FRE-1051).** ES silently loses up to 83% of emitted events on
  some days; ADR-0090 has no delivery corner. Say "provisional" where you report one, and treat a low
  count with the same suspicion as a zero.
- **Per-call series authority is Postgres `api_costs`, not Elasticsearch.** The ES per-call emit has
  been dark since 2026-05-10, so an ES-derived per-call series is an artifact of that gap, not a
  measurement of behaviour.
- **Read config from the running process, not the repo.** A repo value is a default; the live value is
  whatever the container holds. They diverge, and the divergence is usually the finding.
- **Identify deployed code by container file hash, not board state.** Ticket states lag merges and
  deploys in both directions.

---

## Filing — `Backlog` only, never self-promoted

You may create **`Backlog`** tickets and post comments. You may **not** create `Needs Approval` tickets,
and you may **never** promote your own (ADR-0135 D5). A proposal reaches `Needs Approval` only through
master's feasibility disposition at the exit gate, and master promotes the existing ticket in place
rather than filing a second one.

This is not a demotion — it is the cheap filing path (ADR-0131 D1) plus one honest admission: a proposal
that has not met project reality is *actionable in principle and not yet ready to be proposed*.

Record every ticket you file in the document's `## Filed tickets` list, without exception.

---

## Branch, path and PR scope

You commit your research document and open its PR yourself. That is a bounded relaxation of the
draft-only rule and it is scoped exactly:

- **Path:** `docs/research/<date>-fre-XXXX-<slug>.md` — that one file, and nothing else.
- **Branch:** `explore-fre-XXXX-<slug>`.
- **You never merge.** Opening the PR is what triggers master's disposition; merging is master's.

The PR is not a `send-keys` injection and needs no owner say-so — it is your own deliverable on your own
branch, and it pushes into nobody's live context. The owner-gated rule governs pushing a conclusion into
another seat's session, which a PR does not do.

Anything outside that path or that branch is drafted as text for the owner or master to route.

---

## Running a study — the steps

1. **Pick up.** Resolve NEXT (or take the explicit id), read the commissioning ticket and its comments,
   move the ticket → `In Progress`.
2. **Orient.** Read the linked ADRs and prior research documents. Note what the commission asks you to
   *attack*; a confirmation is a failed study.
3. **Measure.** Against the live system, per the substrate map. Resolve every identifier before you
   trust any zero.
4. **Record as you go.** Verdict, query, actual output — per finding, at the moment you have it. The
   evidence is cheapest to capture while the query is still in front of you and most expensive to
   reconstruct later.
5. **Write** the document in the fixed shape, on your branch, at your path.
6. **File** `Backlog` tickets for what warrants one; list every id in `## Filed tickets`.
7. **Consolidate** proposals to at most ten, in the one `## Proposals` section.
8. **Open the PR.** Sync to latest main first (`git fetch origin && git rebase origin/main`), then push
   and open it. Then go idle — master dispositions each proposal at the gate.
9. **Respond to a poke.** If the watcher reports red CI on your `explore-fre-*` PR, or master raises a
   bounce, fix it on this branch and push. You are warm; the fix is cheap. Never poll CI — the watcher
   already covers both directions.

## Boundary

Never merge, never deploy, never edit `src/`, never write `docs/plans/OWNER_CONSOLE.md`, never mutate
Linear control-plane fields beyond moving your own ticket to `In Progress` at pickup, never create or
promote a `Needs Approval` ticket, never write outside your one research document and its branch.
