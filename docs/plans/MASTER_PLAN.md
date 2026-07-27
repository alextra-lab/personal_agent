# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-27

## 0. ADR-0125 — the turn evidence contract (Accepted; the live build)

Names harness health and output quality as distinct dimensions, bars a dimension-1 producer from
user-facing context, and decides what every turn must durably record. The verification oracle is
**deferred** — only its feasibility bounds are decided.

**In flight:** FRE-1002 (evidence-path boundary + CI truncation guard) · FRE-1005 (usage edge, joins
recall records to the supersession chain).

**Awaiting approval, in the order they should go:** **FRE-1001** (non-nullable producer `source` —
pairs with FRE-1007, same fix on two fields) · **FRE-1003** (remove the reflection-recall path; this is
what actually retires ADR-0067, whose header is already superseded) · **FRE-1010** (blocked by FRE-1002)
· **FRE-1006** the seam, genuinely blocked until 1001/1002/1005 land.

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

**Next: FRE-993** — four things, led by **trim an over-long digest instead of discarding it** (today
rejection *regenerates*, so ~47% of sessions pay twice and store nothing). Then Amendment C's sizing, an
items×words cap, and the deterministic-call fail-safe. Sequencing: **once trimming exists the 250→400
flip stops being urgent** — trimming removes the destruction, the bound only sets how often it runs.

**Then FRE-987** — bound the *transient* retry path. The reason-based terminality split is correct
design; the defect is that transient has no bound at all.

**The two gates before the sweep returns:** bound calibrated (**done** — FRE-994) and retry path bounded
(FRE-987, open). Amendment C's 400 is **not** licence. When it returns, gate on the **empty-digest rate**,
not the parse rate — an empty digest marks a session clean and is never retried; first measurement is 2%.

**Also open:** FRE-989 (cost attribution — four confirmed read-path defects) · FRE-990 (reflection has no
enable flag; the cadence flag inverts) · FRE-988 (cost-tracker connection pooling) · FRE-1007 (producers
declare their reasoning configuration, fail-closed) · FRE-1008 (the two prompt hashes cannot differ).

## 2. Knowledge-graph identity — FRE-998

No Session or Turn node has ever carried `user_id`; the write path never populates it. Existing sessions
were backfilled 2026-07-26, so this ticket is the **write path**, without which the gap reopens. Also
covers 1,828 turn nodes attached to no session at all. Decide: property on the nodes, or a first-class
user node with an owns relationship.

## 3. Deploy + verification queue

Thirteen tickets in Awaiting Deploy, most predating 2026-07-26. **None to be closed on "deployed and
healthy"** — each needs its acceptance criterion proven. FRE-996/997 are deployed but unverifiable yet
(997 needs traffic days; 996 needs the sweep on).

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
- **FRE-999** is an umbrella sitting at `Needs Approval` — by our own rules umbrellas belong in `Backlog`.
  Master will move it on a word.
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
