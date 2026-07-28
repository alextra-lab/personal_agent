# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-27

## 0. ADR-0126 — reading the living-knowledge substrate (Accepted; the next build)

Gives ADR-0098's Claim and Stance layers their first reader. Stance pushes on two surfaces (an
always-present behavioural profile, and topic-scoped enrichment riding an entity selection already made);
Claims are **pull-only**; current-only is pre-committed on every push surface; and **D7** is the
generalisable rule — *an ADR that ships a producer must carry at least one criterion that fails if nothing
reads its output.*

**Five tickets Approved and parked, sequence written as relations:** FRE-1015 → FRE-1017 ·
FRE-1016 → FRE-1018 · **FRE-1019** the seam, blocked by all four. Unlabelled deliberately — label the head
when a stream frees. **The ADR closes on FRE-1019 only**: removing each of the four consumers must turn
*named* assertions red from a green baseline. Not on the last child merging.

**Before dispatching, check one thing:** the D2 precondition (target entity must be in the recall set;
failure is **INCONCLUSIVE**) reached FRE-1017's AC-3 and was added by master to FRE-1015's AC-1. **FRE-1016,
FRE-1018 and FRE-1019 were never checked** for the same relay gap.

## 1. ADR-0125 — the turn evidence contract (Accepted; residual)

**FRE-1003 merged** (PR #715) — the reflection-recall path is *deleted*, not flag-disabled: module, call
site and all four settings gone, so no configuration can revive it. **FRE-1005** is unblocked and
parked-Approved; **both streams are now free with empty eligible sets.**

**Still owed from FRE-1003's codex review, deliberately not built there:** an AST/import-boundary guard
forbidding any context-assembly dependency on a dimension-1 producer module. That generalises to **D1's
producer→dimension enforcement** — fold it into whoever scopes D1, don't re-file it.

**FRE-1006 closes the ADR** — when a planted machine-readable false claim is refuted from the stored record
by exact comparison. Not when the fields are populated.

**FRE-1014** — the admission resolver's docstring justifies its multiset match on a property the renderer no
longer has. Sequence with the *`admitted` must require non-empty content* amendment if built together.

Still owed, neither ticketed: **evidence item 3** (reasoning trace — no capture field, and the bound
Anthropic models never return raw chain-of-thought, so this needs a *feasibility* ticket first), and that
amendment.

## 2. Cost, Process and Monitoring Audit

Background LLM streams stay **disabled** and the summary sweep stays **off** until both gates are met;
**no budget cap is to be raised.**

**FRE-987 is the only gate left** before the sweep returns — bound the *transient* retry path. Three
tickets in §4 cannot be verified until it lands. When the sweep returns, gate on the **empty-digest rate**,
not the parse rate.

**FRE-987 fixes unbounded *retry*. Unbounded *input* is a separate, unowned problem** — and the
resolution has now been reached **independently twice** without ever landing in an ADR, so it is named
here to stop it being rediscovered a third time.

ADR-0124 triggers wholesale regeneration on an **idle clock**. Both the 2026-07-26 explore note
(`docs/research/2026-07-26-session-analyzer-pillar.md` §2) and the session-summarizer brainstorm brief
(§4A, which credits the explore note) conclude the opposite: rebuild should fire **on accumulated delta,
not a clock**, as a **hybrid** — cheap incremental deltas plus periodic full rebuild (keyframes,
event-sourcing snapshots, log compaction).

The explore note's *correction of record* is the part to keep: the cost incident was **a bug, not an
indictment of wholesale regeneration**. Wholesale is correct for sessions that end. It breaks under
never-ending sessions for a *different* reason — `f(all captures)` grows monotonically, so turn 500
costs 500 turns of tokens on every rebuild. Neither FRE-987 nor ADR-0124 addresses that, and neither
should: ADR-0124 was scoped to sessions that end, and that assumption is what is failing.

**Vehicle: FRE-991** (Urgent, Needs Approval since 2026-07-26, unlabelled) — the explore session's own
output. Nothing moves here until it is approved. Related but distinct prior art: the March
`CONTEXT_INTELLIGENCE_SPEC.md` and its cited survey `docs/research/context_management_research.md`
cover *within-session* context construction and are cited by **neither** ADR-0124 nor ADR-0125; whoever
takes FRE-991 should reconcile the two layers rather than start a third.

FRE-988 (the other gate) is merged and deployed; its bounce paid off twice over — codex, once its dead
broker was repaired, returned a **block** naming a second real defect (unlocked check-then-act in
`connect()`, so racing coroutines could each build a pool and a loser could null a winner's). Fixed with
double-checked locking, and the tests were **mutation-verified**: the lock reverted, the tests confirmed
failing, then restored.

**Also open:** FRE-989 (cost attribution) · FRE-990 (reflection has no enable flag; the cadence flag
inverts) · FRE-1007 (producers declare reasoning configuration, fail-closed) · FRE-1008 (the two prompt
hashes cannot differ) · FRE-1013 (entity class never emitted).

## 3. Memory recall — FRE-1021 and FRE-1020

**FRE-1020 is Done** — deployed and verified live: 6 new Claims in two distinct `(asserted_by, confidence)`
pairs, where the whole graph had exactly one before. The **ADR-0100 note is written**, so that debt is
discharged. D6's corroboration gate stays open as **FRE-1022**; ADR-0098 does **not** close here.

**Two things the verification found, which set the next work here:**
- **FRE-1023** (filed, Needs Approval) — the borderline-attribution signal logs its overlap scores but **no
  claim key**, and a turn yields several claims, so a decision can't be joined to the claim it decided. The
  retune evidence FRE-1020 leaned on is unusable as built. One-field fix.
- **The accepted residual fired inside the first six claims**, not as a tail event: a first-person symptom
  report landed `agent` on an exact 0.571/0.571 tie. Safe direction (a tie must never mint `user`), but
  **do not read an attribution split as ground truth** until FRE-1023 lands. Related design property:
  authorship comes from the *turn a claim was extracted from*, not the fact's origin — a fact you stated
  days ago, re-extracted from the assistant's restatement, is `agent`-derived by construction.

**FRE-1021** — entities and episode-derived candidates compete in one ranked, capped, fused pool, so a
subject's accumulating conversation pushes its own entities beneath the cap. **Measure the rate first**;
n=4 is a mechanism, not a number. Record **session identity** alongside kind and score. Bears on ADR-0126
D2, whose topic-scoped surface rides the selection that fades.

## 4. Verification backlog — master's own debt

**Fourteen tickets sit in Awaiting Deploy; thirteen are deployed.** FRE-1003 is the exception — merged
after the 20:49Z deploy, and behaviourally inert in prod (the path has been flag-disabled since
2026-07-26), so it batches with the next rebuild rather than needing one. The column is master's
*verification* backlog, not a deploy queue. **None closes on "deployed and healthy"** — each needs its
acceptance criterion proven, and **UNVERIFIABLE is a first-class verdict**.

- **Verifiable tomorrow, on a clock already running:** FRE-988 — needs a 24h window comparing connect
  events against priced-call volume. Deployed 20:49Z; **baseline is 90 connects in the preceding 24h**
  (not the ticket's headline 527 — the harness has been far quieter under the cost halt). If connects
  still track calls, this is **Verify Failed**, not Done.
- **Nine never checked at all:** FRE-717 · 739 · 986 · 936 · 970 · 972 · 943 · 971 · 969.
- **Three blocked on the sweep** (so on FRE-987): FRE-993 · FRE-996 · FRE-992.

**Verification residual carried forward from FRE-1002** (closed Done): that shortened excerpts actually
carry the truncation marker is **UNVERIFIABLE at current traffic** — zero markers across 6,047 log docs
and 121 captures, but the limits are 400/800 chars against a p99 user message of 400, only three short
turns have run since deploy, and the reflection excerpt paths are off under the cost halt. Re-check when
the background streams return; it is not a defect signal.

**Known board drift, not yet cleared:** `reconcile_board.py` reports 3 FAIL — **FRE-432** (Backlog),
**FRE-875** and **FRE-983** (Approved) — each with a merged PR against a non-Done state, from 3 / 14 / 25
July. Not closed blind: they need the same acceptance verification as the rest.

## 5. Reduce the backlog

40+ at Needs Approval and ~80 Approved, most unlabelled (parked), including **twelve P0s months old** —
FRE-940 (replayed approval cards), FRE-927 (broken seat escapes both reconcilers), FRE-867 (seats hang on
non-allowlisted prompts). The last two are the same class as the stalls that keep needing a human.
Method: verify per cluster, cancel the provable with a one-line reason, bring judgment calls to the owner.
Provable cull classes — already-fixed ghosts · superseded-ADR trees (FRE-729–732, FRE-810/811/814) ·
`[Thread]` placeholders that can never be Done (FRE-401/418/397) · work gated on events that never
happened (FRE-443). Run `scripts/reconcile_board.py` first.

## 6. Pipeline hardening — filed, held, and now overdue

**FRE-976** (Linear-reconciled dispatch) · **FRE-975** (gate master on a review-complete signal) ·
**FRE-977** (explore first-class dispatch) · **FRE-1011** (guard docs PRs against ticket tokens).
FRE-976 keeps biting — dispatch pokes landing unsubmitted in a seat's buffer, each needing a human to
notice. FRE-1011 addresses a board-corruption trap that has fired 11 times across 7 sessions and is a
*mechanical guard* problem, not a discipline one.

## 7. Then, in order

Telemetry residuals (FRE-983 ES lifecycle, parked mid-phase) · Configuration Management ·
Linear async feedback · Seshat Inference.

---

## Awaiting an owner decision

- **ADR-0120 cost governance** — Proposed, and it gates a **seven-ticket P0 chain** (FRE-898–905). All
  cost work stays ask-first until it is settled.
- **FRE-1013** — entity class never emitted; every entity filed under the `World` default. Test the cheap
  hypothesis first: the field may simply be absent from the extraction prompt.
- **FRE-1014** · **FRE-1021** — at Needs Approval.
- **Backlog cull scope + gate** (§5).
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
- **49 orphaned capture files** under `telemetry/captains_log` — pre-containerisation April dev data, not
  the deployed store (which is the Docker volume). Surfaced by FRE-1001; owner's call to remove or ignore.
  They are what nearly caused a migration to be run against the wrong substrate and reported as AC proof.
