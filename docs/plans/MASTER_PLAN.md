# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-27

## 0. Cost, Process and Monitoring Audit — the live thread

The only active workstream. Background LLM streams stay **disabled** and the summary sweep stays **off**
until the two gates below are met; no budget cap is to be raised.

**Digest chain.** FRE-994 **is done** — the curve ran ($1.74, study lane). Its answer was not a
different constant: elasticity is **0.16**, so the prompt cannot deliver any bound tested, and at the
deployed 250 **47% of content-bearing digests are rejected for length**. ADR-0124 **Amendment C** splits
the three numbers D3 conflated — prompt target 180→**120**, enforced rendered ceiling 250→**400**, call
ceiling 2,048 unchanged and never binding.

**Next: FRE-993**, rewritten 2026-07-27 — its original premise (truncation at the 2,048 ceiling) was
falsified by FRE-994. Scope is now four things: **trim an over-long digest instead of discarding it**
(the big one — today rejection *regenerates*, so ~47% of sessions pay twice and store nothing);
Amendment C's sizing; an items×words cap (the untested cell — tokens aren't countable by the model);
and the deterministic-call fail-safe. Note the sequencing: **once trimming exists, the 250→400 flip
stops being urgent** — trimming removes the destruction, the bound only sets how often it runs. The
flip is still an owner decision at FRE-993's gate; FRE-994 changed no runtime behaviour.

**Every backend producer must declare its reasoning configuration** (owner direction 2026-07-27).
Verified against litellm 1.89.2: with no effort hint, litellm sends no `thinking` and no
`output_config`, so the *provider* default applies — on `claude-sonnet-5` that is adaptive thinking at
**`high`** effort. `session_summary` and `insights` sit there **by omission**; only `captains_log`
chose (`medium`). The parameter vocabulary is **provider-specific** — effort/thinking are Anthropic
concepts and two roles are bound to OpenAI — so study each provider's surface and verify what litellm
forwards. Never disable thinking on a tool-using model: on Opus 5 it can emit a tool call as visible
text, silently.

**Then the retry policy.** FRE-987 — bound the *transient* failure path. Owner's constraint: the
reason-based terminality split is correct design; the defect is that transient has no bound at all.

**Two gates before the sweep is re-enabled.** The bound calibrated — **FRE-994 discharges this** — and
the retry path bounded (FRE-987, still open). Amendment C's 400 is **not** licence to re-enable. When it
does go back on, gate on the **empty-digest rate**, not the parse rate — an empty digest marks a session
clean and is never retried. FRE-994 gives the first measurement of it: **2%**, alongside 2% contract
drift.

**Also open in the project:** FRE-989 (cost attribution — now carries four confirmed read-path defects,
including that role-attributed spend is unanswerable from Elasticsearch), FRE-990 (reflection has no
enable flag; the cadence flag inverts), FRE-988 (cost-tracker connection pooling).

## 1. ADR-0125 — two quality dimensions + the turn evidence contract (awaiting acceptance)

The summarizer brainstorm was held 2026-07-26/27 and opened into something broader. **ADR-0125 is on main
at `Proposed`** (PR 686, merge `cbb7f321`); acceptance is the owner's and has not happened. It names
harness health and output quality as distinct dimensions, bars a dimension-1 producer from user-facing
context (superseding ADR-0067 reflection-surfacing **on acceptance**), and decides the turn evidence
contract. The verification oracle is **deferred** — only its feasibility bounds are recorded.

Nothing is dispatchable until the owner accepts. On acceptance, master labels and wires the chain in one
action: **FRE-1000** (blocking measurement gate — sizes the rest) → **FRE-1004** → **FRE-1005**;
**FRE-1001** and **FRE-1002** run in parallel; **FRE-1006** is the seam and closes ADR-0125 — *not* its
last merged child, and *not* populated fields. Dimension-1 chain **FRE-1003** (remove the reflection-recall
path) is independent and parallel. Recommendation carried from the ADR seat: **FRE-717 follows FRE-1003**,
so realized value measures a producer whose known defects are already fixed.

Gates FRE-993 (the summarizer decision now sits inside this frame).

## 2. Knowledge-graph identity — FRE-998

No Session or Turn node has ever carried `user_id`; the write path never populates it. Existing sessions
were backfilled 2026-07-26, so this ticket is the **write path**, without which the gap reopens. Also
covers 1,828 turn nodes attached to no session at all. Decide deliberately: property on the nodes, or a
first-class user node with an owns relationship.

## 3. Deploy + verification queue

Twelve tickets in Awaiting Deploy. **FRE-996 and FRE-997 are deployed** (18:10Z) but not verifiable yet —
997 needs days of traffic, 996 needs the sweep enabled. The other ten predate 2026-07-26. None to be
closed on "deployed and healthy"; each needs its acceptance criterion proven.

## 4. Reduce the backlog

~80 Approved, most unlabelled (parked). Method: verify per cluster, cancel the provable with a one-line
reason, bring judgment calls to the owner. Provable cull classes — already-fixed ghosts · superseded-ADR
trees (FRE-729–732, FRE-810/811/814) · `[Thread]` placeholders that can never be Done (FRE-401/418/397) ·
work gated on events that never happened (FRE-443). Run `scripts/reconcile_board.py` before culling.
Owner to settle scope (Approved only vs all open states) and gate (cancel directly vs list-first).

## 5. Pipeline hardening — filed, held

**FRE-976** (Linear-reconciled dispatch) · **FRE-975** (gate master on a review-complete signal) ·
**FRE-977** (explore first-class dispatch). Held for the explore pipeline-architecture study. FRE-976 bit
four times on 2026-07-26 — stale dispatch claims and modal dialogs silently swallowing dispatches, each
needing a human to notice.

## 6. Then, in order

Memory Recall · Telemetry residuals (FRE-983 ES lifecycle, parked mid-phase) · Configuration Management ·
Linear async feedback · Seshat Inference.

---

## Awaiting an owner decision

- **ADR-0125** — accept or reject (§1). Acceptance unlocks **FRE-999** + its seven children
  (FRE-1000–1006), all `Needs Approval` and unlabelled. Approve them individually; master labels.
- **ADR-0120 cost governance** — Proposed. All cost work ask-first.
- **Backlog cull scope + gate** (§4).
- **FRE-937** — the owner has reversed its recorded design: the turn-progress surface should *fade* after
  the response completes rather than collapse to a persistent summary. Also carries a blank tools counter
  and a synthesis label that reads as a stutter. Parked.
- **FRE-991** — Analyzer-pillar investigation, Urgent. Master recommends dropping to High so the queue has
  one unambiguous front.
- **FRE-885** · **FRE-805** · **FRE-621** — Needs Approval.

## To fix, unscheduled

- **Personal data already committed to the public repo** (surfaced by FRE-994's security pass, *not*
  introduced by it — FRE-994 redacted its own files). Cities, venues and a personal name sit in
  `scripts/study/eval_artifacts/frozen/`, `scripts/eval/fre435_memory_recall/semantic_probe.yaml`,
  `docs/research/EVALUATION_DATASET.md` and `docs/plans/completed/`. Repo-wide, spans several prior
  tickets. **Owner sets the scope** — redact-in-place, or history rewrite. Note that redaction alone
  leaves it in the git history.
- **The cost gate reserves against an estimator that runs a third light.** cl100k undercounts billed
  Anthropic input by **1.535×**, and the tool definition adds **1,663 tokens/call** uncounted. Measured
  in FRE-994 against FRE-996's committed records. Belongs with the cost audit (FRE-989 neighbourhood).
- **D3's loss question is still unanswered.** FRE-994's loss endpoint failed its own precommitted
  validity gate (extractor recall 0.788 vs 0.80) and was barred rather than rescued. The cause is
  reusable: the automated reference set systematically dropped explicitly-left-open questions — exactly
  what `unresolved` exists for. Any retry needs a different extraction design first.

- **Frozen-reset action never fires on gateway turns** (ADR-0092 #7). FRE-954 sits behind it.
- **FRE-912** — narrowed by FRE-913, not eliminated; parked-Approved.
- **Worker seats strand on non-edit prompts** — FRE-911's `acceptEdits` covers file edits only.
- **Duplicate ADR-0067** — two Accepted ADRs share the number (skill-nudge-injection, and
  reflection-surfacing-in-context-assembly). Renumber; it makes "supersede ADR-0067" ambiguous.
  ADR-0125 supersedes only the reflection-surfacing one, by title.
- **Research index unmaintained since March** — `docs/research/README.md` lists no July documents. The
  2026-07-27 audit was added; the rest were not backfilled.
