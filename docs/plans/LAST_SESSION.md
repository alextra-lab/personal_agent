# Last session — 2026-07-27 (evening: batch deploy, ADR-0126 accepted, and the instrument turned on its author)

## READ THIS FIRST — environment is UP and fully deployed

- **Batch deployed 18:21Z at `a24f4d0f`**, owner-authorised after a hold from 14:00Z. Health green on all
  five components. **Zero undeployed source commits** — main is docs-ahead only.
- **`cloud-sim-embeddings` was re-stopped after the rebuild** (it revives every time — standing rule).
- **The six background LLM streams and the summary sweep remain OFF.** Nothing this session re-enabled
  anything. The sweep's second gate, **FRE-987**, is still open and still unlabelled.
- Captures live in a **Docker named volume** at `/var/lib/docker/volumes/seshat_seshat_captains_log_cloud/_data`
  — on the VPS filesystem, durable across rebuilds. Read them there, not via `docker exec`: it works when
  the container is down. The host bind path under `telemetry/` is **stale** (~1,600 files, nothing today).

## Doing / discussing (≤5 sentences)

ADR-0126 was written, reviewed over three codex rounds, amended once on new evidence, and **accepted by the
owner** — it gives ADR-0098's write-only Claim/Stance substrate its first consumer, and encodes the rule
that an ADR shipping a producer must carry a criterion that fails if nothing reads its output. Four merges
and a batch deploy landed; three owner turns on the PWA then verified FRE-1010 live and produced a new
finding (FRE-1021) about entities being displaced from recall. The through-line was **instruments that lie**
— vacuous acceptance criteria, a docstring justifying itself on a property the code no longer has, a
"currently yellow" cluster status frozen as a permanent claim. Master was corrected five times, twice by
the adrs seat and twice by the owner. The immediate open thread is the **nine-ticket verification backlog**
and **FRE-987**, which gates three of them.

## Commits — the story behind the last 10

- **PR #697 → FRE-1002** — evidence-path guard. Its `is_fallback` fold-in was *required*, not optional: once
  the summary carries a truncation marker the old comparison against a raw 200-char clip can never match.
  Side effect recorded before deploy: long-message extractions that were wrongly counted successful now
  correctly enter the retry path, so entity-extraction calls rise slightly (bounded at 5).
- **PR #699 → FRE-993** — digest trim. Rejection 0.469 → 0.000 at the old bound, measured on FRE-994's own
  96 records at **zero model calls**. The 0.469 *reproduces* Amendment C's 47% rather than restating it.
  Self-review's own find is the best part: a trimmed digest reached storage indistinguishable from a whole
  one, now fixed with `items_dropped` whose marker tokens count against the ceiling. **Inert until the
  sweep returns.**
- **PR #701 → FRE-1010** — per-item-kind render, cap of 3 retired. Master set a bounding condition; the
  seat discharged it *better*, bounding entity descriptions too (the larger exposure — never truncated
  before). Worst case unbounded → ~20,000 chars.
- **PR #704 → FRE-1001** — the day's strongest handoff. **Codex earned its keep**: it caught, before any
  code, that the 49 null-source files are stale April host data, not the deployed store. Without it a
  migration would have run against orphaned data and been reported as AC proof.
- **PRs #705/#707/#709 → ADR-0126.** Written, three codex rounds (blocking findings in 1 and 2), then a
  *self-initiated* amendment when master's FRE-1021 evidence arrived. Accepted by the owner at 19:16Z.

## Worktrees — anything special

- **build1** — building **FRE-1020** (write-side supersession guard unreachable), Opus, started 19:16:11Z.
- **build2** — on the **FRE-988 bounce**, PR #703 still open. Bounced for review tier: a src-logic diff on
  the *cost* path with no codex plan-review, code review at `low` effort, security review skipped. Plus an
  unanswered availability question (`connect()` never rebuilds a pool that goes terminal). FRE-1003 queued.
- **adrs** — free; FRE-1012 closed Done. It corrected master twice today and was right both times.
- **`master-914`** — still a stale worktree on the closed `fre-909-seat-rename`. Removal offered, not taken.

## Plan position + drift

MASTER_PLAN was rewritten three times today and is current. **Deliberate deviation, and it was right:** the
owner held deploys for a batch at 14:00Z, so four merges accumulated before going out together — which is
also why the 12:04 pre-deploy capture survived to exonerate the deploy later.

**Known drift, now recorded rather than left verbal:** `reconcile_board.py` reports **3 FAIL** — FRE-432,
FRE-875, FRE-983 — each a merged PR against a non-Done state, dating from 3 / 14 / 25 July. None was
introduced this session. They are *not* closed blind because that would violate the evidence contract;
they need acceptance verification like the rest of the backlog.

## Answers for the fresh start

- **Do NOT re-enable the summary sweep and do NOT raise any budget cap.** Both standing.
- **The Awaiting-Deploy column is not a deploy queue.** All 14 entries are deployed; what they await is
  *master's acceptance verification*. Nine have never been checked at all (FRE-717, 739, 986, 936, 970,
  972, 943, 971, 969). Three more (FRE-993, 996, 992) cannot be verified until the sweep runs — gated on
  FRE-987. This backlog is the single biggest thing master owes.
- **FRE-1021 is the live finding and it is n=4, not a rate.** Entities and episode-derived candidates
  compete in one ranked, capped, fused pool, so accumulating conversation on a subject pushes that
  subject's entities beneath the cap. Master first described this as "the conversation's own turns" — that
  was **wrong**; all four turns are in *different sessions*, so it is ordinary cross-session recall. The
  measurement must record **session identity**, not just kind and score.
- **ADR-0126's five implementation tickets are Approved and parked**, sequence written as relations
  (1015 → 1017, 1016 → 1018, seam 1019 blocked by all four). Unlabelled deliberately: both streams were
  occupied, and unlabelled-Approved = parked is the safe default. Label the head when a stream frees.
- **A relay gap to check.** The D2 precondition reached the ADR and AC-3 on FRE-1017 but **not** AC-1 on
  FRE-1015; master added it as a binding comment there. **1016, 1018 and 1019 were not checked** for the
  same gap.
- **ADR-0100 is owed a correction** — it demoted its recency gate on the stated premise that ADR-0098 owns
  correctness-over-time, and on live data it does not (FRE-1020). Small docs change, master's to make.
- **Master's failure mode this session, stated plainly so the next one watches for it.** Five times master
  asserted a state before checking it: a seat "stalled" when it was mid-turn; a ticket "in flight" before
  verifying the launch; "the conversation's own turns" before checking session IDs; "before the tickets
  exist" when six had existed for 70 minutes; and a sequencing flip written into Linear **inverted**,
  against a module dependency stated plainly in the tickets' own text. The pattern is reasoning from the
  proxy instead of opening the artifact — the exact failure this whole ADR thread exists to remove. The
  adrs seat caught two; the owner caught two.
- **Owner-set boundary, now in memory:** master does **not** send plan/design feedback to a build seat
  unless asked. Surface the recommendation and stop — don't relay, don't offer to. Dispatch actuation
  (labels, relations, `/build` pokes) stays master's.
