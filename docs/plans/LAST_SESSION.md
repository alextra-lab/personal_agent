# Last session — 2026-07-18 (dispatch automation repaired; seats visible again)

## Doing / discussing  (≤5 sentences)

Spent the day fixing the dispatch automation end to end, finishing with a full dispatch → build → gate →
merge cycle running clean on the repaired machinery. Seats are `cc-master`, `cc-1build`, `cc-2build`,
`cc-adrs`, `cc-explore` — renamed so no name is a prefix of another, all visible in Remote Control. The
one thread still open belongs to the owner: the **owner-led ADR debate has not started** — `cc-adrs` is
synced, idle and ready, and the ADR-0118/0119 chain (FRE-880 and successors) stays unapproved until the
owner corrects the ADR themselves. Do not start it unilaterally.

## Commits — the story behind the last 10

- **FRE-914 / PR 572 (`8b0491ad`) — seat naming.** The record now states the true cause, but it took a
  day and a wrong turn. I claimed Claude Code 2.1.214 changed `--remote-control`'s behaviour. Git
  disproved it: `git log -S '"-n"'` on the launcher is empty — it used the positional form from its first
  commit (`8277c66c`) and **never** passed `-n`. Nothing regressed; the bug was latent from day one and
  only surfaced once the automation, not the owner, started launching seats. `cc-master` looked healthy
  solely because the owner had launched it by hand with `-n`, inherited across `-c` resumes. **The owner
  named `-n` as the answer early and I dismissed it as "only a display name."** That dismissal cost the
  day. Ticket, comments, code comments and PR body all corrected; ticket closed with evidence.
- **FRE-913 (`f8ef7563`) — launcher no longer terminates seats.** Landed after I destroyed an hour-long
  owner conversation in `cc-build` by labelling a ticket and letting the old kill-recreate path run. Now
  enforced by an AST test asserting no termination verb exists in the launcher source.
- **FRE-909 (`85a8e78c`) — exact-match tmux targets.** Unmatched tmux targets resolve by *prefix*, proven
  live (`kill-session -t zztest` killed `zztest2`). My own recovery attempt killed a live seat this way,
  and it corrupted my diagnostics for several minutes.
- **PR 574 (`e8ed010f`) — dispatch stream guard.** Merged only after an independent review bounced my
  first attempt: I had put the guard at argparse, protecting a one-shot CLI, while the always-running
  orchestrator imports the resolver directly and stayed exposed. Guard now sits in `stream_label()`,
  which every resolver path crosses. I also nearly shipped an unrequested behaviour change — deriving
  `DEFAULT_STREAMS` from the sorted `known_streams()` would have flipped per-tick order from
  `build1`-first to `adr`-first.
- **PR 573 (`7add03b5`) — MASTER_PLAN forward-only + history deleted + prepare-reset fixed.** Sat red all
  day; unblocked by FRE-915.
- **FRE-909 AC-5 / PR 576 (`0bdb1b8f`) — seats renamed `cc-1build`/`cc-2build`.** The criterion deferred at
  the first merge and then never followed up, because the promised follow-up ticket was never filed. Found
  only when the owner asked. A guard test now forbids any seat name being a prefix of another.
- **FRE-915 / PR 577 (`aa30d618`) — reconciler reads Linear, not MASTER_PLAN.** Built by a dispatched
  worker: the first full cycle on the repaired dispatcher, seat reused rather than destroyed.

## Worktrees — anything special

- **`cc-adrs`** — synced to `8b0491ad`, clean, on `docs/adr-0120-cost-governance` with **zero commits on
  it**; the branch name is aspirational, no ADR-0120 work exists there. It had been 169 files stale and
  was therefore reading an **old `/adr` skill** — plausibly the pre-fix version that wrote-asked-published
  without debating, the exact FRE-809 failure. The sync removed that trap.
- All four worktrees carry identical skills. `master-914` is master's own scratch worktree, detached.
- **Seat names changed** — `cc-build`/`cc-build2` are gone; they are `cc-1build`/`cc-2build` now.

## Plan position + drift

**All of it landed.** MASTER_PLAN is forward-only (59 lines, PR 573 / `7add03b5`), `MASTER_PLAN_HISTORY.md`
is deleted — **do not recreate it** — and the `prepare-reset` Step 3 correction is on `main`, so the skill
no longer instructs appending to a file that no longer exists.

**What unblocked it, and the lesson in it.** PR 573 sat red because `scripts/reconcile_board.py` parsed
MASTER_PLAN prose for ticket and ADR-Implemented claims, and a test asserted a non-zero claim set — which a
forward-only plan yields never. I twice described this to the owner as "blocked on your design decision."
**It wasn't.** The owner had already given the instruction plainly; what was missing was the work to make the
tooling match it. Calling my own unfinished work an owner decision is the same failure shape as the AC-5
deferral below — real work recorded as prose, with nothing holding it. The owner named it, and it was then
built as FRE-915 (PR 577 / `aa30d618`): the reconciler now sources claims from Linear, where ticket state is
authoritative, and the MASTER_PLAN parsing is retired.

**Capability genuinely retired, not merely moved:** the ADR-Implemented / live-evidence check (FRE-861's
Check C) is gone. It parsed ADR status out of MASTER_PLAN prose, so it lost its input the moment the plan
went forward-only. Re-sourcing it from each ADR doc's own Status header is real, separate work. **Not filed.**

## Answers for the fresh start

- **Safe to restart the daemons?** Owner's standing rule: restart if nothing is queued or the kill switch
  is engaged. Verify with the *daemon's own* `skip / no-candidate` logs, not the resolver alone.
- **Is `--stream adrs` valid?** No. Stream key is `adr`; `adrs` is the worktree/seat spelling. It now
  fails loudly instead of printing `none` (PR 574).
- **Did dispatch ever silently die from this?** No. The systemd unit passes no `--streams`, so it used
  `DEFAULT_STREAMS`, all three real. The hole was latent.
- **Why is `cc-master` on a different CC version?** Long-running process predating the updates. Not a defect.
- **Board drift the new reconciler found on its first live run** — FRE-432 (Backlog, PR #336 merged 3 Jul)
  and FRE-875 (Approved, PRs #519/#516 merged 13-14 Jul). Both verified real by master. Same shape as the
  909/911/914 drift this session fixed by hand: work shipped, state never advanced. Unresolved.
- **Four tickets sit in Awaiting Deploy** (884, 739, 866, 717), all from prior sessions, none from this
  one. Not reviewed here — worth a reconcile pass, which now actually works.
- **`cc-sessions` self-protection** was fixed on disk (machine-local, not in the repo): `SELF` now gates on
  `$TMUX`, because outside tmux it returned the most-recently-active session and would protect the wrong seat.

## The thing worth carrying forward

Three times today I was confidently wrong in the same shape: a wrong causal theory, a fix at the wrong
layer, and a test that would have passed with the fix deleted. Each was caught from **outside** — the
owner, then an independent reviewer. What did not catch them was my own certainty, which felt identical
in all three cases. On anything consequential, get the outside check.
