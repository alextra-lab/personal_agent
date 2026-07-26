# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-26

## 0. Cost, Process and Monitoring Audit — the live thread

The only active workstream. Background LLM streams stay **disabled** and the summary sweep stays **off**
until the two gates below are met; no budget cap is to be raised.

**Digest chain, in order.** FRE-994 (compression curve — *in flight*) → FRE-993 (producer generation +
sizing + fail-safe). FRE-994 sets the token bound empirically; ADR-0124 D3 flagged the incumbent 250 as
provisional and its curve was never run. FRE-993 then makes the call ceiling and the digest bound
consistent and adds the fail-safe: never re-issue an identical deterministic call.

**Then the retry policy.** FRE-987 — bound the *transient* failure path. Owner's constraint: the
reason-based terminality split is correct design; the defect is that transient has no bound at all.

**Two gates before the sweep is re-enabled.** The bound calibrated (FRE-994), and the retry path bounded
(FRE-987). When it does go back on, gate on the **empty-digest rate**, not the parse rate — an empty
digest marks a session clean and is never retried.

**Also open in the project:** FRE-989 (cost attribution — now carries four confirmed read-path defects,
including that role-attributed spend is unanswerable from Elasticsearch), FRE-990 (reflection has no
enable flag; the cadence flag inverts), FRE-988 (cost-tracker connection pooling).

## 1. Summarizer redesign — brainstorm, not yet held

Owner's call: the session summarizer needs redoing, and continuously re-reading every turn to maintain a
summary is unrealistic. Two candidate directions, both open — **chunk summaries** with periodic rebuild,
or **no summaries at all**, using the knowledge graph. Brief with the evidence, ADR chain and measurement
traps is on main (`docs/research/2026-07-26-session-summarizer-brainstorm-brief.md`). Master to drive it
in `cc-adrs`. Gates FRE-993.

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

- **ADR-0120 cost governance** — Proposed. All cost work ask-first.
- **Backlog cull scope + gate** (§4).
- **FRE-937** — the owner has reversed its recorded design: the turn-progress surface should *fade* after
  the response completes rather than collapse to a persistent summary. Also carries a blank tools counter
  and a synthesis label that reads as a stutter. Parked.
- **FRE-991** — Analyzer-pillar investigation, Urgent. Master recommends dropping to High so the queue has
  one unambiguous front.
- **FRE-885** · **FRE-805** · **FRE-621** — Needs Approval.

## To fix, unscheduled

- **Frozen-reset action never fires on gateway turns** (ADR-0092 #7). FRE-954 sits behind it.
- **FRE-912** — narrowed by FRE-913, not eliminated; parked-Approved.
- **Worker seats strand on non-edit prompts** — FRE-911's `acceptEdits` covers file edits only.
- **Duplicate ADR-0067** — two Accepted ADRs share the number (skill-nudge-injection, and
  reflection-surfacing-in-context-assembly). Renumber; it makes "supersede ADR-0067" ambiguous.
