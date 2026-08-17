# Last session — 2026-08-16/17 (four acceptance criteria that measured the wrong thing)

## Doing / discussing

**FRE-1269 is the only open work and it is parked on an owner action.** The owner has taken the
diagnostic-overlay experiment screenshot and **shared it directly with build1**, bypassing master —
that is the fastest path and correct; do not re-insert yourself into that loop. Build1 holds the WebKit
research and is analysing it. Everything else across two days is Done and deployed. **Do not close
FRE-1269 on a green suite** — owner screenshot validation is its stated gate and the whole point of the
ticket.

## What was decided and why

**Four of master's acceptance criteria failed the same way, and the build seats implemented each one
faithfully.** FRE-1263's AC-4 specified a three-row textarea, wrong against the reference. FRE-1264
carried nine criteria and not one measured geometry. FRE-1266's AC-4 named the `footer`, already flush,
instead of the card inside it. FRE-1267's AC-1 named the right element but forced a synthetic inset in a
harness that reports zero. **Every one checked the source, or a synthetic condition, rather than the
rendered result on the owner's device.** Do not read the repeated PWA bounces as build-seat quality.

**Nothing in this repository can observe the mode the owner actually runs.** Headless Chromium reports
`env(safe-area-inset-bottom)` as 0 and cannot emulate standalone `display-mode`, so a headless screenshot
is flush by construction. The owner **declined** an iOS device harness when offered — correctly. Their
screenshots are the instrument. **Do not re-propose the harness.**

**Master's `--safe-top` hypothesis was raised and refuted — do not resurrect it.** Master proposed that
`874 − 62 (safe-top) = 812` meant the viewport was legitimately top-inset-shifted and already reaching
the physical bottom, making `100vh` an overshoot. Build1 refuted it with evidence rather than preference:
if the origin were genuinely shifted, our header's own `paddingTop: calc(env(safe-area-inset-top) +
0.75rem)` = 74px inside that shifted frame would double-reserve the inset and show a ~136px gap under the
Dynamic Island. No such top-side symptom appears in any of seven rounds of screenshots. Master verified
that against the images and accepted the refutation. `62 = --safe-top` is treated as probably
coincidental, explicitly not settled.

**Instructions in ticket comments do not get executed.** The seeded-negative requirement was written into
ticket comments twice and dropped both times; the identical instruction on a **PR/bounce comment** landed
immediately. lifecycle-rules designates ticket comments read-for-context — the contract was working and
master was using the wrong channel. Misread as seat indiscipline for a day and a half.

**The seeded-negative practice paid for itself on FRE-1266.** Master's diagnosis there was necessary but
not load-bearing; the required pre-change test kept failing after it, which surfaced the real cause —
`sr-only` spans resolving their containing block to the root div and inflating the document's own
`scrollHeight`. No source reading produces that. FRE-1255 remains the vehicle for making the rule
structural.

**During a GitHub outage, a merge that reports failure may have applied.** Two merges (#923, #925)
returned HTTP 503 and landed anyway. Check PR state rather than trusting the exit code; blind retrying
would have made a mess.

## Worktrees — anything special

**build1** holds the full FRE-1269 context — WebKit-301994 research, the codex review transcript, the
diagnostic tooling design — **and now the owner's experiment screenshot, sent directly.** Its own handoff
asks to keep that context. Re-deriving it cold would be wasteful. build2 and adr are idle and clean.

## Sequence position + drift

**Both days ran off the console's Observability directive, deliberately and at the owner's direction** —
the PWA arc began with "i want Seshat's UI to have the same layout and font size/design as Claude Code"
and never returned. Owner-directed deviation, not drift. No console write; the file sits at 41 of its
60-line bound with no commits from these two days.

## Answers for the fresh start

- **Why is FRE-1269 in Awaiting Deploy?** Deliberately. Merged and deployed; the owner's screenshot
  validation is the closing condition and cannot be done for them. Steps are in its ticket comments.
- **Why is the owner's app worse than yesterday morning?** Master advised re-adding the home-screen icon
  to activate standalone mode. It worked, and our layout handles standalone worse than the broken state
  it replaced. Master caused that. Safari-tab still renders correctly and is the fallback. Say so plainly.
- **Why four diagnostic PRs and no fix on FRE-1269?** Each was gated on specific reasoning — most
  importantly that a `100vh` probe proves the unit *resolves*, not that content painted there is
  *visible* (WebKit 301994's own distinction). **Master has told build1 the next PR here must be the fix
  or the finding that none exists, not more instrumentation.** Hold that line.
- **Round 7's screenshot proved nothing** — the overlay's measurement refresh didn't depend on the
  experiment toggle, so the numbers were stale regardless. Fixed in #925. Round 8 is the real experiment.
- **Open for the owner:** FRE-1255 (ADR-0137 mapping re-audit, and the seeded-negative vehicle) ·
  FRE-1268 (flaky ModelPicker contrast) · FRE-1270 (watcher redelivers a queued event without
  re-checking PR state — found during the outage) · whether screenshot-validation becomes a standing
  console directive for PWA visual work.
