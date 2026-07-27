# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-27 (FRE-1002/993/1010 merged; deploys held for a batch)

## 0. ADR-0125 — the turn evidence contract (Accepted; the live build)

Names harness health and output quality as distinct dimensions, bars a dimension-1 producer from
user-facing context, and decides what every turn must durably record. The verification oracle is
**deferred** — only its feasibility bounds are decided.

**Merged, awaiting the batched deploy:** FRE-1002 (evidence-path boundary + CI truncation guard) ·
FRE-1010 (per-item-kind render; the cap of 3 retired). The guard half of 1002 is live now — CI is not
the gateway — but both marking halves are runtime and need the rebuild. FRE-1010 emptied FRE-1002's
allowlist by **deleting** the truncation it exempted, which is that file's intended steady state.

**In flight:** **FRE-1001** on build1 (non-nullable producer `source`; pairs with FRE-1007, same fix on
two fields) · **FRE-1003** on build2 (remove the reflection-recall path — the behavioural half of
retiring ADR-0067) · **FRE-1012** on the adr seat.

**FRE-1012 is the unlock.** ADR-0098's Claim substrate is write-only, so FRE-1005's AC-4 fixture cannot
exist and **FRE-1006 inherits the same premise** — meaning ADR-0125 could not close. It is Approved and
dispatched as ADR work, and its output must be a *decision* about whether claims become recallable and
how, not an implementation. FRE-1005 stays blocked by it and un-blocks automatically at its merge.

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

**FRE-993 merged 2026-07-27, awaiting the batched deploy.** Trim-not-discard shipped: measured on
FRE-994's own 96 content-bearing records at zero model calls, the rejection rate goes **0.469 → 0.000** at
the old 250 bound and **0.073 → 0.000** at Amendment C's 400. The 0.469 reproduces C1's 47% from the
records rather than restating it. Sizing moved to target 120 / ceiling 400; call ceiling unchanged.
A trimmed digest now **declares** itself (`items_dropped` stored and rendered, marker tokens counted
against the ceiling) — self-review's find, and the trim fires on ~7.3% of digests, so it is a real rate.
Two boundaries held: reasoning configuration stayed with **FRE-1007**, and the words-per-item cap is
recorded in the ADR as a **study, not a refuted lever** — C4 and the prompt-signature guard refute only
the items-per-slot half, and trimming drops the urgency. Not filed as a ticket; recording beats filing.

**Building now on build2: FRE-988** (cost-tracker connection pooling), with FRE-1003 queued behind it.
Quick to build, **not** quick to close — its acceptance criterion is a post-deploy measurement of connect
events against priced-call counts over a window, so it will land in Awaiting Deploy and wait like the
others.

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

**Deploys are HELD for a batched deploy** (owner, 2026-07-27 14:00Z). Deployed image is `af29060d`;
main carries **four undeployed source commits** (FRE-1002, FRE-993 ×2, FRE-1010). Standing-approval classes
batch too unless urgent.

Fourteen tickets in Awaiting Deploy. **None to be closed on "deployed and healthy"** — each needs its
acceptance criterion proven. Verified 2026-07-27 that every one of the pre-hold merges is already an
ancestor of the running SHA, so that column was never a deploy queue: it is master's *verification*
backlog. From the hold onward it means what it says again.

**FRE-997 closed Done 2026-07-27** on live evidence — its fail-open signal fired seven times on real
traffic — and the first reading inverted the audit's prediction: the model emits *nothing* for entity
class, so every entity is filed under the `World` default. Filed as **FRE-1013**.

**FRE-996 is UNVERIFIABLE, not passing** — zero digest calls have run since deploy, so its hypothesis has
nothing to test against. **FRE-992**'s code claims verify in the deployed source, but its runtime is
untested for the same reason, and its 46 stranded sessions were **not** recovered (118 of 119 sessions
carry a generated-at stamp; 6 hold a digest). Recover them only *after* FRE-993 and FRE-987 land —
clearing the stamps sooner arms 46 sessions against a producer that still discards and still retries
unbounded.

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
