# Last session — 2026-07-28 (the sweep came back, bounded; and master kept reasoning from proxies)

## READ THIS FIRST — environment is UP, fully deployed, sweep is LIVE

- **Deployed 12:26Z at `3c9d8f08`.** Health green on all five components. Joinability probe green
  (`6ceed4df`). `cloud-sim-embeddings` re-stopped after the rebuild — standing rule, it revives every time.
- **The summary sweep is RUNNING again** (`AGENT_SESSION_SUMMARY_ENABLED=true`, flipped 12:26Z, owner-authorised).
  Both gates were met. `.env` backed up to `.env.bak-20260728-sweep-reenable`.
- **A `make restart` does NOT pick up an `.env` change** — compose reads env at container *creation*.
  Use `make up SERVICE=…`. Master nearly reported the sweep re-enabled while it sat off.
- Background LLM streams (other than the sweep) stay **disabled**. **No budget cap is to be raised.**

## Doing / discussing (≤5 sentences)

The morning restored the summary sweep behind FRE-987's retry bound and then spent the day on what that
exposed: a digest producer asking a model to transcribe text it could have quoted from a locator, and a
sweep whose four early-return paths were silent at INFO. Both were ticketed, built, merged, deployed and
**verified live within hours** — session `73417fbd`, which had failed twice that morning, produced a digest
on its first attempt with its correction intact. ADR-0127 landed from the adrs seat and **falsified its own
ticket's premise** (there is no labelled corpus: 1,916 of 1,943 ratings are backfilled defaults, leaving 27).
The through-line, again, was **master reasoning from summaries instead of artifacts** — four times, each
caught by the artifact disagreeing. The session ended on the owner's frustration at the Awaiting-Deploy
column, which turned out to be **largely master's own doing**.

## Commits — the story behind the last 12

- **#723 → FRE-1025** — the sweep's four silent guards now speak at INFO with a queryable `reason`. Codex
  caught that *master's ticket contradicted itself* (AC demanded one record per tick; a later paragraph
  invited transition-only logging). The criterion won. A **fifth** silent path master missed — the
  completion log gated behind `if considered:` — was found and fixed here.
- **#724 → ADR-0127** (Proposed) — three codex rounds, two BLOCK on real AC defects. Round three verified
  every cited ADR's status header against source.
- **#725** — master's hold on the ADR-0126 chain **lifted, because it was wrong**. Held on "four of five
  consume assembled context", a characterisation taken from ADR-0126's *summary*. ADR-0126 contains **no
  mention of `digest` or `capture` anywhere**.
- **#726 → FRE-1024** — the model now only points; the code quotes the span from the locator. Codex found a
  HIGH master's ticket had *also* missed: an all-corrections digest, all ungroundable, becomes empty →
  GENERATED → written → freshness advanced → **session permanently retired**. The new `ungrounded_digest`
  reason is deliberately **transient**, its comment citing this exact lesson.
- **#727 → FRE-1016** — Claims pull path. Scoping anchors on `(:Person {user_id})-[:HAS_FACT]->` rather than
  the entity visibility filter — which matters, because that filter's first clause (`visibility IS NULL`)
  is **fail-open** and every Claim has NULL visibility.

## Worktrees — anything special

- **build** — on merged `fre-1024`, clean, idle. Nothing preserved.
- **build2** — on merged `fre-1016`, clean, idle. **FRE-1018 is dispatched but has never started**: the
  orchestrator logs `kind=hold reason=card-already-surfaced` every 5 min since ~13:46. That is the FRE-940
  replayed-approval-card class. It self-escalates past a threshold; nothing is lost.
- **adrs** — on merged `adr-0127`, clean, idle, ~330k tokens of stale context. Wants `/clear` before reuse.
- **explore** — untouched for 2 days.

## Plan position + drift

MASTER_PLAN is current. Two deliberate deviations, both right: the ADR-0126 hold was **set and then lifted
the same day** (recorded with its reason in §0), and FRE-1015 remains **unlabelled on purpose** — it rides
the entity selection FRE-1021 says fades, so FRE-1016 was dispatched as the safer head instead.

**The verification backlog is master's standing debt and was NOT worked.** Six remain in the audit project;
four are verifiable immediately.

## Answers for the fresh start

- **The Awaiting-Deploy column was regrowing from master's own docs PRs.** Linear's GitHub integration links
  on branch, title **and body**, then drives the ticket's state from that PR's lifecycle. Seven docs PRs
  today carried FRE tokens in the body: FRE-988 ×4, FRE-987 ×3, FRE-991 ×3, plus eight others. FRE-1001 went
  `Done 20:05:17 → In Progress 20:06:02` — **the PR announcing it was closed re-opened it.** Restored
  FRE-1001, FRE-1002, FRE-991. **Memory already carried this rule in full and master violated it anyway** —
  ~18 occurrences across 8 sessions. Treat as needing the mechanical guard (**FRE-1011**) or a narrowed
  Linear linking setting (owner-only); *not* more discipline. Until then: name tickets **by subject, not
  identifier**, in docs PR bodies.
- **NEXT, and it is master's own debt:** four verifications, all unblocked by the sweep returning —
  **FRE-993** (trim), **FRE-996** (JSON contract), **FRE-992** (durable capture store), **FRE-1003**
  (reflection-recall removal; most mechanically checkable). **FRE-987 and FRE-988 cannot close before
  ~08:00Z** — both need a 24h window. FRE-987's baseline: 90 connects in the 24h before deploy; FRE-988's
  cost baseline is on its ticket.
- **Owner decisions still pending:** ADR-0127's seven tickets (FRE-1026–1032) at Needs Approval — two heads
  become pickable, pin **FRE-1027**. The **private-by-default scope brainstorm** (does it cover entities and
  World knowledge, or only turns and Personal claims?) which unparks **FRE-674** — its spec is otherwise
  complete on the ticket. And **FRE-1021**, still unapproved, which gates FRE-1015.
- **Master's failure mode, unchanged from yesterday and worth watching for.** Four times today master
  reasoned from a summary rather than the artifact: the ADR-0126 hold; "no attribution" for a non-owner's
  turn (it had a `PARTICIPATED_IN` edge all along); a guessed Neo4j property name returning a confident
  NULL; a reconstructed sweep predicate. Each was caught by opening the artifact. **The habit that works:
  enumerate keys, read the real query, run the guard — never infer the shape.**
- **ast-grep is now nudged in `.claude/CLAUDE.md`** (auto-loaded). It went unused for months because skill
  discovery matches the *technique name* an agent never reaches for — "grep for a call site" is never framed
  as "structural search". Worked patterns and the wrong-node-kind trap are documented there.
