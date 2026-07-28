# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-28

## 0. ADR-0126 — reading the living-knowledge substrate (Accepted; the next build)

Gives ADR-0098's Claim and Stance layers their first reader. Stance pushes on two surfaces (an
always-present behavioural profile, and topic-scoped enrichment riding an entity selection already made);
Claims are **pull-only**; current-only is pre-committed on every push surface; and **D7** is the
generalisable rule — *an ADR that ships a producer must carry at least one criterion that fails if nothing
reads its output.*

**Chain is dispatchable** — a hold set on 2026-07-28 was lifted the same day (it rested on a summary, not
the tickets; ADR-0126 never mentions `digest` or `capture`). Do not re-set it.

**Delivered:** FRE-1016 (claims pull). **In flight:** FRE-1018 (chain-on-pull, `context:keep`, dispatched
but held on an unactioned approval card). **Remaining:** FRE-1015 → FRE-1017, then **FRE-1019** the seam.
**The ADR closes on FRE-1019 only** — removing each of the four consumers must turn *named* assertions
red from a green baseline.

**FRE-1015 is deliberately unlabelled**: it rides the entity selection FRE-1021 says fades. Leave it
parked until FRE-1021 is approved and measured.

**Relay-gap check is COMPLETE** (2026-07-28): FRE-1016 and FRE-1018 are clear; the D2 precondition binds
AC-1's positive half, AC-5's *push* half and AC-6's populated control only. **FRE-1019 had the gap and it
is now a binding comment on that ticket** — without the precondition, a precondition failure is
indistinguishable from a successful mutation, which is a *false pass of the seam*.

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

Background LLM streams stay **disabled**; **no budget cap is to be raised.** The **summary sweep is LIVE
again** since 2026-07-28 12:26Z, bounded by FRE-987's retry pacing (ADR-0124 Amendment D).

**Watch it:** gate on the **empty-digest rate**, not the parse rate. A failing session should log once and
stay quiet through its backoff; an exhausted cap should produce **one stand-down**, not a denial per
session. **This bounds a *failing session* (~8 attempts/day, was 288); it does NOT bound aggregate spend.**

**FRE-987 fixes unbounded *retry*. Unbounded *input* is a separate, unowned problem.** ADR-0124 triggers
wholesale regeneration on an **idle clock**; the 2026-07-26 explore note (`docs/research/2026-07-26-session-analyzer-pillar.md`
§2) and the session-summarizer brainstorm brief (§4A) both conclude rebuild should fire **on accumulated
delta**, as a hybrid of incremental deltas plus periodic full rebuild. ADR-0127 **D9** assigns this fork to
**ADR-0124's trigger** — so it is written down at last, **and it still has no owner.** That is the open item.

Keep the correction that goes with it: the cost incident was **a bug, not an indictment of wholesale
regeneration**. Wholesale is right for sessions that end; it breaks under never-ending ones because
`f(all captures)` grows monotonically.

**Still open, explicitly not done:** the March `CONTEXT_INTELLIGENCE_SPEC.md` and its cited survey
`docs/research/context_management_research.md` cover *within-session* context construction and are cited by
**neither** ADR-0124, ADR-0125 nor ADR-0127. **Two layers to reconcile; do not start a third.**

**Do not re-propagate two false figures** (corrected by ADR-0127, measured): there is **no labelled corpus**
— 1,916 of 1,943 ratings are backfilled defaults, leaving **27** expressed judgments, so "1,933 rated turns
as DSPy signal" is false. And the capture corpus is **1,941 turns, not 8,880**.

**Also open:** FRE-989 (cost attribution) · FRE-990 (reflection has no enable flag; the cadence flag
inverts) · FRE-1007 (producers declare reasoning configuration, fail-closed) · FRE-1008 (the two prompt
hashes cannot differ) · FRE-1013 (entity class — premise stale, re-measure first).

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

**Seventeen sit in Awaiting Deploy. This column is a *verification* queue, not a deploy queue.** None
closes on "deployed and healthy" — each needs its acceptance criterion proven, and **UNVERIFIABLE is a
first-class verdict**.

**Read this before working the column.** It regrows on its own: Linear's GitHub integration links a PR
that names a ticket in its **branch, title _or body_**, then drives that ticket's state from that PR's
lifecycle. Master's own docs PRs dragged ~12 tickets backwards on 2026-07-28 alone (FRE-1001 went
`Done → In Progress` **45 seconds** after being closed). **Name tickets by subject, not identifier, in
docs PR bodies.** A ticket carrying a full evidence comment while sitting here is the tell. **FRE-1011**
is the mechanical guard; narrowing Linear's linking rule is the owner-side alternative.

- **Verifiable NOW — all four unblocked by the sweep returning:** **FRE-993** (trim) · **FRE-996** (JSON
  contract) · **FRE-992** (durable capture store) · **FRE-1003** (reflection-recall removal — the most
  mechanically checkable: the module is either gone from the running container or it is not).
- **On a 24h clock, cannot close before ~08:00Z 2026-07-29:** **FRE-987** (captains_log daily spend ÷
  session-summary-generated events = the cost-per-digest figure nobody has) · **FRE-988** (connect events
  vs priced-call volume; **baseline 90 connects/24h** pre-deploy, *not* the ticket's headline 527).
- **Merged but NOT deployed:** **FRE-1016** — landed at `61210170`, after the 12:26Z deploy at `3c9d8f08`.
- **Deployed 12:26Z, needs verification:** **FRE-998** (graph identity write path).
- **Nine never checked at all**, oldest from 1 July: FRE-717 · 739 · 986 · 936 · 970 · 972 · 943 · 971 · 969.

**Carried residual (FRE-1002, closed):** that shortened excerpts actually carry the truncation marker is
**UNVERIFIABLE at the traffic seen so far** — zero markers, but the limits are 400/800 chars against a p99
user message of 400. Re-check once the sweep has run a full day. Not a defect signal.

**Known board drift:** `reconcile_board.py` reports **FRE-432** (Backlog), **FRE-875** and **FRE-983**
(Approved) — each a merged PR against a non-Done state. Not closed blind; they need the same verification.

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
- **FRE-1013** premise is stale — 425 Personal entities now exist, so "class never emitted" is false;
  re-measure before building (noted on the ticket).
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
