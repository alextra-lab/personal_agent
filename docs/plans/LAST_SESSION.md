# Last session — 2026-07-18 (dispatch automation repaired; seats visible again)

## Doing / discussing  (≤5 sentences)

Spent the day fixing the dispatch automation, ending with all seats visible and the dispatcher restarted
on merged code. Two threads remain open and both belong to the owner, not the next session: **PR 573 is
red** on a genuine design conflict (below), and the **owner-led ADR debate has not started** — `cc-adrs`
is synced and ready, and the owner said the ADR-0118/0119 chain (FRE-880 and successors) stays unapproved
until they correct the ADR themselves. Do not start either unilaterally.

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
- **PR 573 — MASTER_PLAN forward-only.** Written, pushed, **still red.** See drift.

## Worktrees — anything special

- **`cc-adrs`** — synced to `8b0491ad`, clean, on `docs/adr-0120-cost-governance` with **zero commits on
  it**; the branch name is aspirational, no ADR-0120 work exists there. It had been 169 files stale and
  was therefore reading an **old `/adr` skill** — plausibly the pre-fix version that wrote-asked-published
  without debating, the exact FRE-809 failure. The sync removed that trap.
- All four worktrees are at `8b0491ad` with identical skills. `master-914` is detached at origin/main.

## Plan position + drift

**MASTER_PLAN on `main` is still the old 151-line version** — the forward-only rewrite and the
`MASTER_PLAN_HISTORY.md` deletion live entirely in unmerged PR 573. Do not assume the plan reflects the
owner's instruction yet.

**Why 573 is red, and why it is not mine to fix:** `scripts/reconcile_board.py` machine-parses
MASTER_PLAN for ticket and "Implemented/live" claims, and `test_real_master_plan_extracts_nonzero_claim_set`
asserts the current file yields a non-zero set. A forward-only plan yields zero *by construction*. The
reconciler assumes MASTER_PLAN is a status document — precisely what the owner said it must stop being.
Either the reconciler points at Linear (where status is authoritative) or that extraction path retires.
**Owner design call.** My recommendation is Linear; unrequested and non-binding.

**Skill drift, fixed but unmerged:** `prepare-reset` Step 3 on `main` still instructs appending to
`MASTER_PLAN_HISTORY.md` — the deleted file. Correction is in PR 573. **A `/prepare-reset` run before 573
merges will be told to recreate it; ignore that instruction.**

## Answers for the fresh start

- **Safe to restart the daemons?** Owner's standing rule: restart if nothing is queued or the kill switch
  is engaged. Verify with the *daemon's own* `skip / no-candidate` logs, not the resolver alone.
- **Is `--stream adrs` valid?** No. Stream key is `adr`; `adrs` is the worktree/seat spelling. It now
  fails loudly instead of printing `none` (PR 574).
- **Did dispatch ever silently die from this?** No. The systemd unit passes no `--streams`, so it used
  `DEFAULT_STREAMS`, all three real. The hole was latent.
- **Why is `cc-master` on a different CC version?** Long-running process predating the updates. Not a defect.
- **Four tickets sit in Awaiting Deploy** (884, 739, 866, 717), all from prior sessions, none from this
  one. Not reviewed here — worth a reconcile pass.
- **`cc-sessions` self-protection** was fixed on disk (machine-local, not in the repo): `SELF` now gates on
  `$TMUX`, because outside tmux it returned the most-recently-active session and would protect the wrong seat.

## The thing worth carrying forward

Three times today I was confidently wrong in the same shape: a wrong causal theory, a fix at the wrong
layer, and a test that would have passed with the fix deleted. Each was caught from **outside** — the
owner, then an independent reviewer. What did not catch them was my own certainty, which felt identical
in all three cases. On anything consequential, get the outside check.
