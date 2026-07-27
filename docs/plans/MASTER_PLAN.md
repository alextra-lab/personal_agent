# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-27 (batch deployed at 18:21Z — `a24f4d0f`; ADR-0126 Proposed)

## 0. ADR-0125 — the turn evidence contract (Accepted; the live build)

Names harness health and output quality as distinct dimensions, bars a dimension-1 producer from
user-facing context, and decides what every turn must durably record. The verification oracle is
**deferred** — only its feasibility bounds are decided.

**Deployed 2026-07-27 18:21Z:** FRE-1002 · FRE-1010 · FRE-1001 (with FRE-993, §1). **FRE-1010 is Done** —
the only ticket closed on live evidence: three post-deploy turns admitted their whole candidate set,
where the old cap of 3 would have discarded items 4–5. Its entity criteria stay **seam-test-proven, not
observed**, for the reason in FRE-1021 below.

**In flight:** **FRE-1003** on build2 (remove the reflection-recall path — the behavioural half of
retiring ADR-0067). **build1 is idle with an empty queue.**

**FRE-1012 produced ADR-0126, merged at Proposed — and the ticket is deliberately still open.** Its
deliverable is a *decision*, and merging a proposal is not making one. Closing it would resolve
FRE-1005's block and let a worker build against an unratified design. **Owner acceptance is the gate**;
on acceptance master flips the status header, closes 1012, drops the block, and 0126's seven criteria
become ticketable with AC-8 held as the seam.

**ADR-0126 in one line:** Stance is ADR-0098's first consumer on two surfaces (always-present behavioural
profile · topic-scoped enrichment riding an entity selection already made); Claims are **pull-only**;
current-only pre-committed on every push surface; and D7 encodes the authoring rule — *an ADR that ships
a producer must carry at least one criterion that fails if nothing reads its output.*

**FRE-1021 — the finding that bears on 0126 D2, from live records.** The same question asked at 12:04 and
18:32 returned 3 entities then **zero**: the conversation's own recent turns became recall candidates and
displaced the entities out of a top-5 set ranked on raw similarity across mixed kinds. So **the KG's
contribution to a topic decays as the owner engages with it** — and D2's chosen surface fades exactly
where use is highest. Verified by candidate identity, not by timing; the deploy is exonerated. n=4, no
measured rate yet — that is what FRE-1021 exists to establish.

**New at the FRE-1010 gate: FRE-1014** — the admission resolver's docstring justifies its multiset match
on the renderer emitting an order-preserving prefix; FRE-1010 made it a *subsequence*. Low frequency, but
it is a false justification sitting in an instrument, which is the failure class this thread exists to
kill. Sequence it with the "admitted must mean non-empty" amendment if both are built together.

**FRE-1006 closes the ADR — not the last child to merge, and not "the fields are populated."** It closes
when a planted machine-readable false claim is refuted from the stored record by exact comparison.

Two things the contract still owes, both deliberately deferred and neither yet ticketed:
**evidence item 3** (reasoning trace — no capture field exists, and on the bound Anthropic models the raw
chain of thought is never returned, so this needs a *feasibility* ticket first), and an amendment so that
**`admitted` requires non-empty rendered content** (FRE-1010 proved an item can be `admitted: true` and
still convey nothing).

## 1. Cost, Process and Monitoring Audit

Background LLM streams stay **disabled** and the summary sweep stays **off** until both gates are met;
no budget cap is to be raised.

**FRE-993 merged and deployed 2026-07-27, but inert.** Trim-not-discard shipped: measured on
FRE-994's own 96 content-bearing records at zero model calls, the rejection rate goes **0.469 → 0.000** at
the old 250 bound and **0.073 → 0.000** at Amendment C's 400. The 0.469 reproduces C1's 47% from the
records rather than restating it. Sizing moved to target 120 / ceiling 400; call ceiling unchanged.
A trimmed digest now **declares** itself (`items_dropped` stored and rendered, marker tokens counted
against the ceiling) — self-review's find, and the trim fires on ~7.3% of digests, so it is a real rate.
Two boundaries held: reasoning configuration stayed with **FRE-1007**, and the words-per-item cap is
recorded in the ADR as a **study, not a refuted lever** — C4 and the prompt-signature guard refute only
the items-per-slot half, and trimming drops the urgency. Not filed as a ticket; recording beats filing.

**FRE-988 was BOUNCED at the gate** (PR 703) — a src-logic diff on the cost path reviewed as if trivial:
no codex plan-review, code review at `low` effort, security review skipped. Plus an unanswered question:
`connect()` returns early whenever the pool is non-null, so a pool that goes terminal is never rebuilt,
where the old per-call pool re-established by construction. build2 is fixing it, then takes FRE-1003.

**Then FRE-987** — bound the *transient* retry path. The reason-based terminality split is correct
design; the defect is that transient has no bound at all.

**The two gates before the sweep returns:** bound calibrated (**done** — FRE-994) and retry path bounded
(FRE-987, open). Amendment C's 400 is **not** licence. When it returns, gate on the **empty-digest rate**,
not the parse rate — an empty digest marks a session clean and is never retried; first measurement is 2%.

**Also open:** FRE-989 (cost attribution — four confirmed read-path defects) · FRE-990 (reflection has no
enable flag; the cadence flag inverts) · FRE-1007 (producers declare their reasoning configuration,
fail-closed) · FRE-1008 (the two prompt hashes cannot differ) · FRE-1013 (entity class never emitted).

## 2. Knowledge-graph identity — FRE-998

No Session or Turn node has ever carried `user_id`; the write path never populates it. Existing sessions
were backfilled 2026-07-26, so this ticket is the **write path**, without which the gap reopens. Also
covers 1,828 turn nodes attached to no session at all. Decide: property on the nodes, or a first-class
user node with an owns relationship.

## 3. Deploy + verification queue

**Batch deployed 2026-07-27 18:21Z**, owner-authorised. Running `a24f4d0f`; health green on all five
components; **zero undeployed source commits**. `cloud-sim-embeddings` revived on rebuild and was
re-stopped (standing rule). Rollback: rebuild at `af29060d`.

**Fourteen tickets in Awaiting Deploy, and all fourteen are now deployed.** So the column is once again
master's *verification* backlog, not a deploy queue. **None to be closed on "deployed and healthy"** —
each needs its acceptance criterion proven.

**Blocked on the sweep, not on deploy:** FRE-993, FRE-996 and FRE-992 all need the summary sweep running
to verify, and that is gated on **FRE-987**. Deploying changed nothing for them.

**FRE-992's framing was corrected** at the deploy: the capture store is a Docker *named volume* on the VPS
filesystem — durable, survives rebuilds (proven: a 12:04 capture outlived the 18:21 rebuild and supplied
the evidence that exonerated the deploy). The stale thing is the host bind path under `telemetry/`, which
holds ~1,600 files and nothing from today. Stale-bind-path vs live-volume, not ephemeral vs durable.

**Post-deploy watch, cost audit:** entity-extraction call rate (FRE-1002's fallback fix should raise it
slightly, bounded at 5 attempts) and assembled-context size (FRE-1010). Expected directions pre-recorded
on both tickets so they stay separable.

## 4. Reduce the backlog

40+ at Needs Approval and ~80 Approved, most unlabelled (parked), including **twelve P0s months old** —
FRE-940 (replayed approval cards), FRE-927 (broken seat escapes both reconcilers), FRE-867 (seats hang on
non-allowlisted prompts). The last two are the same class as the stalls that keep needing a human.
Method: verify per cluster, cancel the provable with a one-line reason, bring judgment calls to the owner.
Provable cull classes — already-fixed ghosts · superseded-ADR trees (FRE-729–732, FRE-810/811/814) ·
`[Thread]` placeholders that can never be Done (FRE-401/418/397) · work gated on events that never
happened (FRE-443). Run `scripts/reconcile_board.py` first.

## 5. Pipeline hardening — filed, held, and now overdue

**FRE-976** (Linear-reconciled dispatch) · **FRE-975** (gate master on a review-complete signal) ·
**FRE-977** (explore first-class dispatch) · **FRE-1011** (guard docs PRs against ticket tokens).
FRE-976 has now bitten ~6 times in two days — dispatch pokes landing in a seat's prompt buffer
unsubmitted, each needing a human to notice; one cost three hours. FRE-1011 addresses a board-corruption
trap that has fired 11 times across 7 sessions and is explicitly a *mechanical guard* problem, not a
discipline one.

## 6. Then, in order

Memory Recall · Telemetry residuals (FRE-983 ES lifecycle, parked mid-phase) · Configuration Management ·
Linear async feedback · Seshat Inference.

---

## Awaiting an owner decision

- **ADR-0120 cost governance** — Proposed, and it gates a **seven-ticket P0 chain** (FRE-898–905). All
  cost work stays ask-first until it is settled.
- **FRE-1013** — entity class is never emitted; 100% of entities filed under the `World` default. Test the
  cheap hypothesis first: the field may simply be absent from the extraction prompt.
- **Backlog cull scope + gate** (§4).
- **FRE-937** — design reversed: the turn-progress surface should *fade* after the response completes,
  not collapse to a persistent summary. Also a blank tools counter and a stuttering synthesis label.
- **FRE-991** — Analyzer-pillar investigation, Urgent. Master recommends High so the queue has one front.
- **FRE-885** · **FRE-805** · **FRE-621** — Needs Approval.

## To fix, unscheduled

- **Personal data already committed to the public repo** — cities, venues and a personal name in
  `scripts/study/eval_artifacts/frozen/`, `scripts/eval/fre435_memory_recall/semantic_probe.yaml`,
  `docs/research/EVALUATION_DATASET.md`, `docs/plans/completed/`. Repo-wide, spans prior tickets.
  **Owner sets scope** — redact-in-place or history rewrite; redaction alone leaves it in git history.
- **The cost gate reserves against an estimator that runs a third light** — cl100k undercounts billed
  Anthropic input by **1.535×**, and the tool definition adds **1,663 tokens/call** uncounted.
- **D3's loss question is unanswered.** FRE-994's loss endpoint failed its own validity gate (extractor
  recall 0.788 vs 0.80) and was barred rather than rescued. Any retry needs a different extraction design
  first — the reference set systematically dropped explicitly-left-open questions.
- **Frozen-reset action never fires on gateway turns** (ADR-0092 #7). FRE-954 sits behind it.
- **FRE-912** — narrowed by FRE-913, not eliminated; parked-Approved.
- **Worker seats strand on non-edit prompts** — FRE-911's `acceptEdits` covers file edits only.
- **Duplicate ADR-0067** — two ADRs share the number. ADR-0125 supersedes only reflection-surfacing, by
  title; renumber so "supersede ADR-0067" stops being ambiguous.
- **Research index unmaintained since March** — `docs/research/README.md` lists no July documents beyond
  the 2026-07-27 audit.
- **`master-914`** — stale worktree on the closed `fre-909-seat-rename`, the only reason that branch
  survives.
